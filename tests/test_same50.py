"""Offline checks for the historical paired-cohort adapter; no model/ledger IO."""
from collections import Counter
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def harness():
    path = Path(__file__).resolve().parents[1] / "scripts" / "compare_same50.py"
    spec = importlib.util.spec_from_file_location("same50_test_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def legacy(tmp_path, harness):
    root = tmp_path / "legacy"
    cases, references, rows = [], [], []
    # Two different questions from each real-shaped company/year source group.
    # A general distinct-document dataset loader would incorrectly reject this.
    for index in range(50):
        identity = f"CORP{index // 2:02d}/2020/page_{index:03d}.pdf-1"
        document = {"id": identity, "question": f"What is the ratio in case {index}?",
                    "pre_text": ["Amounts are in millions."], "post_text": [],
                    "table": [["Year", "2019", "2020"], ["Revenue", "10", "20"]]}
        path = harness.save_json(root / "inputs" / f"case_{index:03d}.json", document)
        cases.append({"case_id": identity, "split": "test", "input_path": "../" + path.relative_to(root).as_posix(),
                      "input_sha256": harness.sha(path)})
        references.append({"case_id": identity, "exe_ans": 700000 + index,
                           "program": f"GOLD_LABEL_SENTINEL_{index}"})
        rows.append({"case_id": identity, "answer_correct": index < 35,
                     "execution_correct": index < 35, "program_correct": index < 31,
                     "status": "completed" if index < 48 else "error"})
    gold = harness.save_json(root / "host/gold/test.json", {"cases": references})
    manifest = harness.save_json(root / "host/manifest.json", {
        "dataset": "FinQA", "selection": {"revision": harness.REVISION, "seed": 20261010},
        "cases": cases, "gold_paths": {"test": "gold/test.json"}, "gold_sha256": {"test": harness.sha(gold)}})
    results = harness.save_json(root / "old_results.json", {
        "model": "fixture-model", "frozen_settings": {"max_iterations": 14},
        "cost_cny": "3.75", "model_calls_this_campaign": 329,
        "metrics": {"test": {"denominator": 50, "duplicate_case_attempts": 0,
                               "answer_correct": 35, "execution_correct": 35, "program_correct": 31,
                               "rows": rows}}})
    return {"root": root, "manifest": manifest, "results": results, "cases": cases,
            "destination": tmp_path / "paired"}


@pytest.fixture
def cohort(harness, legacy):
    return harness.prepare(legacy["manifest"], legacy["results"], legacy["destination"])


def install_fakes(monkeypatch, harness, *, failed_indices=()):
    observations = {"model_constructions": 0, "calls": []}

    class FakeLedger:
        @classmethod
        def open(cls, path):
            return cls()

        def status(self):
            return {"halted": False, "committed_cny": "22.84835420"}

        def calls(self):
            return []

    class FakeModel:
        def __init__(self, ledger, api_key=None):
            observations["model_constructions"] += 1
            self.model, self.base_url = "fixture-model", "https://invalid.example"
            self.max_output_tokens, self.timeout = 2048, 10

    class FakeRegistry:
        def __init__(self, state_dir):
            pass

        def fingerprint(self):
            return "frozen-skills"

    class FakeAgent:
        def __init__(self, model, registry, state, *, config):
            self.model, self.registry, self.config = model, registry, config
            assert not config.learn_on_failure

        def run(self, question, input_paths, **kwargs):
            index = len(observations["calls"])
            assert len(input_paths) == 1
            document = harness.read_json(input_paths[0])
            assert set(document) == {"id", "question", "pre_text", "post_text", "table"}
            assert "inputs" in input_paths[0].parts and "gold" not in input_paths[0].parts
            assert kwargs["split"] == "test"
            assert kwargs["candidate_skill"] is None and kwargs["validation_mode"] is False
            # Labels and programs are distinct sentinels absent from every
            # agent argument, input document and metadata field.
            exposed = json.dumps({"question": question, "document": document,
                                  "kwargs": {key: str(value) for key, value in kwargs.items()}})
            assert "GOLD_LABEL_SENTINEL" not in exposed
            assert not any(str(700000 + n) in exposed for n in range(50))
            observations["calls"].append({"case_id": kwargs["case_id"], "session": kwargs["session_id"],
                                          "question": question, "input": str(input_paths[0])})
            result = {"status": "error" if index in failed_indices else "completed",
                      "final": None if index in failed_indices else {"status": "answered", "answer": 0},
                      "error": "fixture failure" if index in failed_indices else None,
                      "steps": 1, "run_dir": str(kwargs["run_dir"]), "skill_fingerprint": "frozen-skills",
                      "runtime_fingerprint": "frozen-code", "model": "fixture-model",
                      "elapsed_seconds": 0.001, "tool_errors": []}
            harness.save_json(kwargs["run_dir"] / "result.json", result)
            return result

    monkeypatch.setattr(harness, "BudgetLedger", FakeLedger)
    monkeypatch.setattr(harness, "ModelClient", FakeModel)
    monkeypatch.setattr(harness, "SkillRegistry", FakeRegistry)
    monkeypatch.setattr(harness, "ResearchAgent", FakeAgent)
    monkeypatch.setattr(harness, "runtime_fingerprint", lambda: "frozen-code")
    monkeypatch.setattr(harness, "emit", lambda value: None)
    return observations


def test_prepare_keeps_exact_fifty_bytes_order_and_repeated_source_groups(harness, legacy, cohort):
    manifest = harness.load_cohort(cohort)
    assert [case["case_id"] for case in manifest["cases"]] == [case["case_id"] for case in legacy["cases"]]
    assert len(manifest["cases"]) == 50
    assert len(manifest["repeated_source_groups"]) == 25
    assert set(manifest["repeated_source_groups"].values()) == {2}
    assert Counter(case["source_group"] for case in manifest["cases"]) == manifest["repeated_source_groups"]
    for old, new in zip(legacy["cases"], manifest["cases"]):
        old_path = legacy["manifest"].parent / old["input_path"]
        new_path = Path(manifest["_root"]) / new["input_path"]
        assert old_path.read_bytes() == new_path.read_bytes()
        assert old["input_sha256"] == new["input_sha256"]


@pytest.mark.parametrize("target", ["input", "gold", "baseline"])
def test_tampering_is_rejected_before_model_or_ledger_use(harness, cohort, tmp_path, monkeypatch, target):
    manifest = harness.load_cohort(cohort)
    root = Path(manifest["_root"])
    paths = {"input": root / manifest["cases"][0]["input_path"],
             "gold": root / "host/gold/test.json", "baseline": root / "host/baseline.json"}
    path = paths[target]
    path.write_bytes(path.read_bytes() + b"\n")
    def forbidden(*args, **kwargs):
        pytest.fail("Corrupt cohort reached model initialization or a budget ledger")
    monkeypatch.setattr(harness.BudgetLedger, "open", forbidden)
    monkeypatch.setattr(harness, "ModelClient", forbidden)
    with pytest.raises(ValueError, match="hash|integrity"):
        harness.run(cohort, tmp_path / "result", tmp_path / "state")


@pytest.mark.parametrize("change", ["missing_case", "duplicate_case", "forbidden_field", "wrong_input_id", "input_escape", "baseline_aggregate", "repeated_group_metadata"])
def test_structural_corruption_rejected_even_with_updated_member_hash(harness, cohort, tmp_path, change):
    manifest = harness.read_json(cohort)
    root = cohort.parent.parent
    if change == "missing_case":
        manifest["cases"].pop()
    elif change == "duplicate_case":
        manifest["cases"][-1] = deepcopy(manifest["cases"][0])
    elif change in {"forbidden_field", "wrong_input_id", "input_escape"}:
        case = manifest["cases"][0]
        path = root / case["input_path"]
        doc = harness.read_json(path)
        if change == "forbidden_field":
            doc["answer"] = "GOLD_LABEL_SENTINEL"
        elif change == "wrong_input_id":
            doc["id"] = "wrong/2020/page.pdf-1"
        else:
            path = tmp_path / "outside.json"
            case["input_path"] = "../outside.json"
        harness.save_json(path, doc)
        case["input_sha256"] = harness.sha(path)
    elif change == "repeated_group_metadata":
        manifest["repeated_source_groups"] = {}
    else:
        path = root / "host/baseline.json"
        baseline = harness.read_json(path)
        baseline["metrics"]["answer_correct"] += 1
        harness.save_json(path, baseline)
        manifest["baseline_sha256"] = harness.sha(path)
    harness.save_json(cohort, manifest)
    with pytest.raises(ValueError):
        harness.load_cohort(cohort)


def test_sequential_fifty_runs_keep_failed_cases_no_retries_and_no_labels(harness, cohort, tmp_path, monkeypatch):
    observed = install_fakes(monkeypatch, harness, failed_indices={1, 48})
    output = tmp_path / "comparison"
    harness.run(cohort, output, tmp_path / "fake-state")
    report = harness.read_json(output / "report.json")
    expected = [case["case_id"] for case in harness.read_json(cohort)["cases"]]
    assert [call["case_id"] for call in observed["calls"]] == expected
    assert len({call["session"] for call in observed["calls"]}) == 50
    assert observed["model_constructions"] == 1
    assert report["total"] == report["attempted"] == len(report["pairs"]) == 50
    assert report["all_finished"] is True
    assert report["engineering_failures"] == 2 and report["completed"] == 48
    assert report["correct"] == 0 and report["custom_answer_accuracy"] == 0
    assert report["paired_counts"] == {"both_correct": 0, "improved": 0, "regressed": 35, "both_wrong": 15}
    assert report["delta_percentage_points"] == -70
    assert all(row["expected_answer"] == 700000 + index for index, row in enumerate(report["results"]))
    # An accidental rerun into the same frozen output must not buy new attempts.
    with pytest.raises(ValueError, match="fresh output"):
        harness.run(cohort, output, tmp_path / "fake-state")
    assert len(observed["calls"]) == 50


def test_prepare_rejects_incomplete_or_repeated_baseline_before_creating_destination(harness, legacy):
    results = harness.read_json(legacy["results"])
    results["metrics"]["test"]["rows"][-1] = deepcopy(results["metrics"]["test"]["rows"][0])
    harness.save_json(legacy["results"], results)
    with pytest.raises(ValueError, match="first attempt"):
        harness.prepare(legacy["manifest"], legacy["results"], legacy["destination"])
    assert not legacy["destination"].exists()


@pytest.mark.parametrize("target", ["future_input", "gold", "baseline"])
def test_mid_run_tampering_stops_before_any_further_agent_call(harness, cohort, tmp_path, monkeypatch, target):
    observed = install_fakes(monkeypatch, harness)
    manifest = harness.load_cohort(cohort)
    root = Path(manifest["_root"])
    paths = {"future_input": root / manifest["cases"][25]["input_path"],
             "gold": root / "host/gold/test.json", "baseline": root / "host/baseline.json"}
    original_evaluate = harness.evaluate_case

    def evaluate_then_corrupt(*args, **kwargs):
        score = original_evaluate(*args, **kwargs)
        path = paths[target]
        path.write_bytes(path.read_bytes() + b"\n")
        return score

    monkeypatch.setattr(harness, "evaluate_case", evaluate_then_corrupt)
    with pytest.raises(ValueError, match="hash|integrity|changed"):
        harness.run(cohort, tmp_path / "comparison", tmp_path / "fake-state")
    # A modification of question 26 must be caught before question 2 is paid
    # for, rather than waiting until the changed input becomes current.
    assert len(observed["calls"]) == 1
