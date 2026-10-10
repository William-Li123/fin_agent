"""Sequential evaluation: one finished case per step, frozen test catalog, complete denominator."""
from __future__ import annotations
import math
from pathlib import Path
from uuid import uuid4
from finagent.evaluation.dataset import load_manifest, host_gold
from finagent.io import digest, read_json, save_json, runtime_fingerprint

BENCHMARK_NOTE = """\nEvaluation output convention: return a numeric raw arithmetic result (not a formatted percent string).
For percentage or growth ratios, use the fraction resulting from division without a presentation-only multiplication by one hundred.
Amounts retain the source scale unless the question asks a unit conversion. The explanation may additionally state formatted percentages.
This convention is benchmark-specific; do not apply it to unrelated user reports."""


def answer_matches(answer, expected) -> bool:
    if expected in ("yes", "no"):
        return isinstance(answer, str) and answer.strip().lower() == expected
    if isinstance(answer, bool) or answer is None:
        return False
    try:
        value = float(answer)
        target = float(expected)
    except (ValueError, TypeError, OverflowError):
        return False
    return math.isfinite(value) and math.isfinite(target) and round(value, 5) == round(target, 5)


def evaluate_case(agent, manifest: dict, case: dict, output_dir: Path, *, candidate: dict | None = None) -> dict:
    # Validate the host-only labels before spending money; never include them in the run inputs.
    expected = host_gold(manifest, case["split"])[case["case_id"]]["answer"]
    path = Path(manifest["_root"]) / case["input_path"]
    # Recheck every input against the frozen manifest before executing it.
    import hashlib
    if hashlib.sha256(path.read_bytes()).hexdigest() != case["input_sha256"]:
        raise ValueError("Input changed after freeze")
    question = read_json(path)["question"]
    result = agent.run(question + BENCHMARK_NOTE, [path], session_id="eval_" + uuid4().hex,
        run_dir=output_dir, split=case["split"], case_id=case["case_id"], source_group=case["source_group"],
        candidate_skill=candidate, validation_mode=candidate is not None)
    final = result.get("final") or {}
    passed = result["status"] == "completed" and final.get("status") == "answered" and answer_matches(final.get("answer"), expected)
    score = {"case_id": case["case_id"], "source_group": case["source_group"], "status": result["status"],
        "passed": passed, "predicted_answer": final.get("answer"), "expected_answer": expected,
        "error": result.get("error"), "steps": result["steps"], "run_dir": result["run_dir"],
        "skill_fingerprint": result["skill_fingerprint"],
        "runtime_fingerprint": result["runtime_fingerprint"],
        "evaluation_context": digest({"model": result["model"], "config": agent.config.__dict__, "dataset": manifest["selection"],
                                       "skills": result["skill_fingerprint"], "runtime": result["runtime_fingerprint"]})}
    # Labels live outside the agent run scope and are never added to conversational memory.
    return score


def run_evaluation(agent, manifest_path: Path, output_dir: Path, *, split: str = "test", on_case=None,
                   learn_from_failures: bool = False) -> dict:
    if learn_from_failures and (split != "dev" or not getattr(agent, "learning", None)):
        raise ValueError("Automatic evaluation learning requires a development split and a learning service")
    manifest = load_manifest(manifest_path)
    cases = [c for c in manifest["cases"] if c["split"] == split]
    if not cases:
        raise ValueError("No selected evaluation cases")
    fingerprint = agent.registry.fingerprint()
    code_hash = runtime_fingerprint()
    output_dir = Path(output_dir)
    freeze = {"dataset_sha256": digest({k: v for k, v in manifest.items() if k != "_root"}),
              "skill_fingerprint": fingerprint, "case_ids": [c["case_id"] for c in cases], "split": split,
              "config": agent.config.__dict__, "model": getattr(agent.model, "model", None), "runtime_fingerprint": code_hash}
    if (output_dir / "freeze.json").exists():
        raise ValueError("Evaluation output directory already frozen; use a fresh directory")
    save_json(output_dir / "freeze.json", freeze)
    rows = []
    for index, case in enumerate(cases):
        if agent.registry.fingerprint() != fingerprint:
            raise RuntimeError("Skills changed after evaluation freeze")
        if runtime_fingerprint() != code_hash:
            raise RuntimeError("Implementation changed after evaluation freeze")
        score = evaluate_case(agent, manifest, case, output_dir / "runs" / f"case_{index:03d}")
        if learn_from_failures and not score["passed"]:
            # Pass only run diagnostics, never the expected value or reference program.
            try:
                directory = Path(score["run_dir"])
                request = read_json(directory / "request.json")
                diagnostic = score.get("error") or "Development scorer rejected the numeric conclusion; inspect the source selection, period, units and computation. Reference answers are not supplied."
                experience = agent.learning.record_failure(case["case_id"], request["question"],
                    read_json(directory / "trace.json"), diagnostic, split="dev", source_group=case["source_group"])
                candidate = agent.learning.propose(experience)
                score["learning"] = {"experience_id": experience, "candidate_id": candidate["id"], "status": "quarantined"}
            except Exception as error:
                score["learning"] = {"status": "failed", "error": str(error)}
        rows.append(score)
        save_json(output_dir / "scores.json", rows)
        if on_case:
            on_case(score)
    report = {"dataset": "FinQA", "revision": manifest["selection"]["revision"], "split": split,
        "total": len(cases), "correct": sum(r["passed"] for r in rows), "custom_answer_accuracy": sum(r["passed"] for r in rows) / len(cases),
        "completed": sum(r["status"] == "completed" for r in rows), "results": rows,
        "metric": "Custom scalar/yes-no equality rounded to five decimals in raw exe_ans scale; not official program accuracy",
        "denominator": "Every frozen case, including failures", "freeze": freeze}
    save_json(output_dir / "report.json", report)
    return report
