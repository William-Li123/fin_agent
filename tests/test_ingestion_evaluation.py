"""Real file parsing and evaluator isolation, without fetching a dataset or API."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from finagent.core.agent import AgentConfig
from finagent.evaluation.dataset import REVISION, host_gold, load_manifest
from finagent.evaluation.runner import answer_matches, evaluate_case, run_evaluation
from finagent.ingestion.documents import FIELDS, ingest
from finagent.io import read_json, save_json, runtime_fingerprint
from finagent.tools import ResearchWorkspace


def document(identity="company/report/case", question="What is growth?"):
    return {"id": identity, "question": question, "pre_text": ["Values in millions."], "post_text": [],
            "table": [["metric", "prior", "current"], ["revenue", "100", "125"]]}


@pytest.mark.parametrize("field", ["qa", "gold", "answer", "program", "exe_ans", "expected_answer"])
def test_generic_json_ingestion_rejects_gold_or_annotation_fields(tmp_path, field):
    source = save_json(tmp_path / "annotated.json", {**document(), field: "HOST_ONLY_LABEL"})
    output = tmp_path / "clean"
    with pytest.raises(ValueError, match="exactly"):
        ingest(source, output)
    assert not list(output.glob("*.json"))


def test_json_ingestion_preserves_sources_and_explicit_question_override(tmp_path):
    original = document()
    source = save_json(tmp_path / "source.json", original)
    paths = ingest(source, tmp_path / "clean", question="Compare the two periods.")
    parsed = read_json(paths[0])
    assert set(parsed) == FIELDS
    assert parsed["table"] == original["table"]
    assert parsed["question"] == "Compare the two periods."
    assert read_json(source) == original
    manifest = read_json(tmp_path / "clean" / "manifest.json")
    assert manifest["original_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    with ResearchWorkspace(paths, tmp_path / "run", terminal_backend="trusted-local") as work:
        assert work.execute("finance_read_table", {})["success"]


def test_csv_quoted_values_and_unicode_reach_evidence_without_numeric_reformatting(tmp_path):
    source = tmp_path / "财务.csv"
    source.write_text('metric,prior,current\n营收,"1,200","1,500"\nmissing,,N/A\n', encoding="utf-8-sig")
    paths = ingest(source, tmp_path / "clean", question="How did revenue change?")
    parsed = read_json(paths[0])
    assert parsed["table"][1] == ["营收", "1,200", "1,500"]
    assert parsed["table"][2] == ["missing", "", "N/A"]
    with ResearchWorkspace(paths, tmp_path / "run", terminal_backend="trusted-local") as work:
        read = work.execute("finance_read_document", {})["structured_content"]
        assert any(record["text"] == "1,200" for record in read["evidence"])
        assert any(record["text"] == "N/A" for record in read["evidence"])


def test_xlsx_sheets_are_distinct_and_uncached_formulas_remain_missing(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    workbook.active.title = "Statements"
    workbook.active.append(["metric", "value"])
    workbook.active.append(["revenue", 125])
    workbook.active.append(["uncached_formula", "=B2*2"])
    additional = workbook.create_sheet("Assumptions")
    additional.append(["discount_rate", 0.1])
    source = tmp_path / "financials.xlsx"
    workbook.save(source)
    workbook.close()
    paths = ingest(source, tmp_path / "clean")
    assert len(paths) == 2
    docs = [read_json(path) for path in paths]
    assert len({item["id"] for item in docs}) == 2
    assert docs[0]["table"][1] == ["revenue", "125"]
    assert docs[0]["table"][2] == ["uncached_formula", ""]
    assert docs[1]["table"][0] == ["discount_rate", "0.1"]
    assert "cached values" in read_json(tmp_path / "clean" / "manifest.json")["limitations"]


def pdf_file(path, *, text=None, encrypted=False):
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    if text:
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = content
    if encrypted:
        writer.encrypt("local-test-password")
    writer.write(path)
    writer.close()
    return path


def test_pdf_text_ingestion_preserves_page_locator_and_rejects_ocr_only_input(tmp_path):
    source = pdf_file(tmp_path / "annual.pdf", text="Revenue was USD 125 million.")
    paths = ingest(source, tmp_path / "clean")
    parsed = read_json(paths[0])
    assert "[PDF page 1]" in parsed["pre_text"][0]
    assert "Revenue was USD 125 million." in parsed["pre_text"][0]
    assert parsed["table"] == []
    with pytest.raises(ValueError, match="no extractable text"):
        ingest(pdf_file(tmp_path / "image-only.pdf"), tmp_path / "empty")
    with pytest.raises(ValueError, match="Encrypted"):
        ingest(pdf_file(tmp_path / "encrypted.pdf", text="Locked", encrypted=True), tmp_path / "locked")


@pytest.mark.parametrize("answer,expected,match", [
    (0.25, 0.25, True), ("0.25000001", 0.25, True), (25, 0.25, False), ("25%", 0.25, False),
    ("YES", "yes", True), ("no", "yes", False), (True, 1, False), (None, 0, False),
    (float("nan"), 0, False), ("Infinity", 1, False), ("$1,200", 1200, False),
])
def test_raw_scalar_evaluation_never_rescales_or_decodes_answers(answer, expected, match):
    assert answer_matches(answer, expected) is match


def frozen_fixture(root: Path, *, split="test"):
    cases = []
    gold = {}
    for index, label in enumerate(("correct", "wrong-scale", "failed")):
        identity = f"company-{index}/year/{label}"
        path = save_json(root / "inputs" / split / label / "input.json", document(identity, f"Growth case {label}?"))
        cases.append({"case_id": identity, "source_group": f"company-{index}/year", "split": split,
            "input_path": path.relative_to(root).as_posix(), "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        gold[identity] = {"answer": 0.25, "program": "HOST_ONLY_ORACLE_SENTINEL"}
    gold_path = save_json(root / "host" / "gold" / f"{split}.json", gold)
    return save_json(root / "host" / "manifest.json", {"dataset": "FinQA", "selection": {"revision": REVISION, "seed": 7},
        "sources": {}, "cases": cases, "gold_sha256": {split: hashlib.sha256(gold_path.read_bytes()).hexdigest()}})


class EvaluatorProbeAgent:
    """Observe exactly what the evaluator gives the agent, without accessing gold."""
    config = AgentConfig(terminal_backend="trusted-local")
    model = SimpleNamespace(model="evaluation-probe")

    def __init__(self):
        self.fingerprint = "frozen-procedures"
        self.registry = SimpleNamespace(fingerprint=lambda: self.fingerprint)
        self.received = []

    def run(self, question, input_paths, **kwargs):
        docs = [read_json(path) for path in input_paths]
        assert all(set(doc) == FIELDS for doc in docs)
        forwarded = json.dumps({"question": question, "documents": docs, "options": kwargs}, default=str)
        assert "HOST_ONLY_ORACLE_SENTINEL" not in forwarded
        assert "expected_answer" not in forwarded
        self.received.append(forwarded)
        label = kwargs["case_id"].rsplit("/", 1)[-1]
        status = "failed" if label == "failed" else "completed"
        return {"status": status, "final": None if status == "failed" else {"status": "answered", "answer": 25 if label == "wrong-scale" else 0.25},
            "error": "controlled runtime failure" if status == "failed" else None, "steps": 1,
            "run_dir": str(kwargs["run_dir"]), "skill_fingerprint": self.fingerprint, "model": self.model.model,
            "runtime_fingerprint": runtime_fingerprint()}


def test_evaluation_keeps_failed_cases_in_denominator_and_labels_out_of_agent_scope(tmp_path):
    manifest_path = frozen_fixture(tmp_path / "dataset")
    agent = EvaluatorProbeAgent()
    report = run_evaluation(agent, manifest_path, tmp_path / "evaluation")
    assert report["total"] == 3
    assert report["completed"] == 2
    assert report["correct"] == 1
    assert report["custom_answer_accuracy"] == pytest.approx(1 / 3)
    assert [item["passed"] for item in report["results"]] == [True, False, False]
    assert len(agent.received) == 3
    assert len({json.loads(item)["options"]["session_id"] for item in agent.received}) == 3
    assert "not official program accuracy" in report["metric"]
    assert read_json(tmp_path / "evaluation" / "freeze.json")["case_ids"] == [row["case_id"] for row in report["results"]]


def test_frozen_input_or_gold_mutation_is_detected(tmp_path):
    manifest_path = frozen_fixture(tmp_path / "dataset")
    manifest = load_manifest(manifest_path)
    gold_path = tmp_path / "dataset" / "host" / "gold" / "test.json"
    gold_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Gold integrity"):
        host_gold(manifest, "test")
    source = tmp_path / "dataset" / manifest["cases"][0]["input_path"]
    source.write_text(json.dumps(document(question="Tampered input")), encoding="utf-8")
    agent = EvaluatorProbeAgent()
    with pytest.raises(ValueError, match="integrity"):
        run_evaluation(agent, manifest_path, tmp_path / "evaluation")
    assert agent.received == []


def test_corrupt_host_gold_is_rejected_before_evaluation_can_spend_a_model_call(tmp_path):
    manifest_path = frozen_fixture(tmp_path / "dataset")
    manifest = load_manifest(manifest_path)
    (tmp_path / "dataset" / "host" / "gold" / "test.json").write_text("{}", encoding="utf-8")
    agent = EvaluatorProbeAgent()
    with pytest.raises(ValueError, match="Gold integrity"):
        evaluate_case(agent, manifest, manifest["cases"][0], tmp_path / "evaluation")
    assert agent.received == []


def test_catalog_cannot_change_between_frozen_evaluation_cases(tmp_path):
    manifest_path = frozen_fixture(tmp_path / "dataset")
    agent = EvaluatorProbeAgent()

    def mutate_catalog(score):
        agent.fingerprint = "changed-procedures"

    with pytest.raises(RuntimeError, match="Skills changed"):
        run_evaluation(agent, manifest_path, tmp_path / "evaluation", on_case=mutate_catalog)
    assert len(agent.received) == 1
    assert len(read_json(tmp_path / "evaluation" / "scores.json")) == 1


class LearningRecorder:
    def __init__(self):
        self.failures = []
        self.proposals = []

    def record_failure(self, case_id, question, trace, error, *, split, source_group):
        self.failures.append({"case_id": case_id, "question": question, "trace": trace, "error": error,
                              "split": split, "source_group": source_group})
        return f"experience-{len(self.failures)}"

    def propose(self, experience_id):
        self.proposals.append(experience_id)
        return {"id": f"candidate-{len(self.proposals)}"}

    def promote(self, *args, **kwargs):
        raise AssertionError("Evaluation must quarantine proposals; promotion requires independent validation")


@pytest.mark.parametrize("split", ["test", "holdout"])
def test_evaluation_learning_rejects_test_and_holdout_before_any_run(tmp_path, split):
    agent = EvaluatorProbeAgent()
    agent.learning = LearningRecorder()
    # Rejection must precede even manifest access, ensuring no model or learning
    # call occurs when an operator accidentally requests holdout learning.
    with pytest.raises(ValueError, match="development split"):
        run_evaluation(agent, tmp_path / "not-read.json", tmp_path / "evaluation",
                       split=split, learn_from_failures=True)
    assert agent.received == []
    assert agent.learning.failures == []
    assert agent.learning.proposals == []
    assert not (tmp_path / "evaluation" / "freeze.json").exists()


def test_development_wrong_answer_learns_from_diagnostics_without_oracle_and_keeps_catalog_frozen(tmp_path):
    class SavedTraceProbe(EvaluatorProbeAgent):
        def run(self, question, input_paths, **kwargs):
            result = super().run(question, input_paths, **kwargs)
            directory = Path(kwargs["run_dir"])
            save_json(directory / "request.json", {"question": question})
            save_json(directory / "trace.json", [{"step": 1, "action": {"action": "finish", "final": result["final"]},
                                                    "success": result["status"] == "completed"}])
            return result

    manifest_path = frozen_fixture(tmp_path / "dataset", split="dev")
    agent = SavedTraceProbe()
    agent.learning = LearningRecorder()
    before = agent.registry.fingerprint()
    report = run_evaluation(agent, manifest_path, tmp_path / "evaluation", split="dev", learn_from_failures=True)
    assert report["total"] == 3 and report["correct"] == 1
    assert len(agent.learning.failures) == 2
    assert len(agent.learning.proposals) == 2
    wrong = next(item for item in agent.learning.failures if item["case_id"].endswith("wrong-scale"))
    assert wrong["split"] == "dev"
    assert wrong["source_group"] == "company-1/year"
    assert wrong["trace"][0]["action"]["final"]["answer"] == 25
    assert "scorer rejected" in wrong["error"]
    forwarded = json.dumps(agent.learning.failures)
    assert "HOST_ONLY_ORACLE_SENTINEL" not in forwarded
    assert "expected_answer" not in forwarded
    assert "\"program\"" not in forwarded
    assert "0.25" not in forwarded
    assert agent.registry.fingerprint() == before
    assert all(item["skill_fingerprint"] == before for item in report["results"])
    assert "learning" not in report["results"][0]
    assert [item["learning"]["status"] for item in report["results"][1:]] == ["quarantined", "quarantined"]
