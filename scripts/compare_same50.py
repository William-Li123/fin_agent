"""Re-evaluate an exact legacy FinQA cohort, without resampling or teaching its labels.

The general dataset loader deliberately rejects repeated company/year groups. This
adapter instead preserves every original case, records repeated groups explicitly,
and checks bytes, IDs, scope, labels and the historical first-attempt baseline.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import sys

from finagent.core.agent import AgentConfig, ResearchAgent
from finagent.evaluation.dataset import REVISION, source_group
from finagent.evaluation.runner import BENCHMARK_NOTE, answer_matches, evaluate_case
from finagent.io import read_json, runtime_fingerprint, save_json
from finagent.models import BudgetLedger, ModelClient
from finagent.skills import SkillRegistry


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def prepare(legacy_manifest: Path, legacy_results: Path, destination: Path,
            prior_manifests: list[Path] = ()) -> Path:
    legacy_manifest, legacy_results, destination = (
        Path(p).resolve() for p in (legacy_manifest, legacy_results, destination))
    target = destination / "host" / "manifest.json"
    if destination.exists():
        raise ValueError("Use a fresh dataset directory; never overwrite a frozen cohort")
    manifest, results = read_json(legacy_manifest), read_json(legacy_results)
    if manifest.get("dataset") != "FinQA" or manifest["selection"]["revision"] != REVISION:
        raise ValueError("Unsupported legacy dataset revision")
    cases = manifest["cases"]
    ids = [case["case_id"] for case in cases]
    if len(ids) != 50 or len(set(ids)) != 50 or any(c["split"] != "test" for c in cases):
        raise ValueError("Expected exactly the original fifty distinct test questions")
    metrics = results["metrics"]["test"]
    baseline_rows = metrics["rows"]
    if (metrics["denominator"] != 50 or metrics.get("duplicate_case_attempts")
            or len(baseline_rows) != 50 or {r["case_id"] for r in baseline_rows} != set(ids)):
        raise ValueError("Historical baseline must contain one first attempt per selected case")
    gold_path = (legacy_manifest.parent / manifest["gold_paths"]["test"]).resolve()
    if not gold_path.is_relative_to(legacy_manifest.parent / "gold") or sha(gold_path) != manifest["gold_sha256"]["test"]:
        raise ValueError("Legacy gold integrity or scope mismatch")
    raw_gold = read_json(gold_path)
    gold = {row["case_id"]: {"answer": row["exe_ans"], "program": row["program"]} for row in raw_gold["cases"]}
    if len(raw_gold["cases"]) != 50 or set(gold) != set(ids):
        raise ValueError("Legacy gold ID mismatch")
    if not all(answer_matches(row["answer"], row["answer"]) for row in gold.values()):
        raise ValueError("Unusable reference value")
    validated = []
    for case in cases:
        path = (legacy_manifest.parent / case["input_path"]).resolve()
        if not path.is_relative_to(legacy_manifest.parent.parent / "inputs") or sha(path) != case["input_sha256"]:
            raise ValueError("Legacy input scope or hash mismatch")
        doc = read_json(path)
        if set(doc) != {"id", "question", "pre_text", "post_text", "table"} or doc["id"] != case["case_id"]:
            raise ValueError("Forbidden model input fields or ID mismatch")
        validated.append((case, path))
    exposures = []
    for prior in prior_manifests:
        prior = Path(prior).resolve()
        previous = read_json(prior)
        previous_ids = {c["case_id"] for c in previous["cases"]}
        previous_groups = {source_group(i) for i in previous_ids}
        exposures.append({"manifest": str(prior), "sha256": sha(prior),
                          "same_case_ids": [i for i in ids if i in previous_ids],
                          "same_source_group_case_ids": [i for i in ids if source_group(i) in previous_groups]})
    converted = []
    for case, path in validated:
        relative = Path("inputs/test") / hashlib.sha256(case["case_id"].encode()).hexdigest()[:20] / "input.json"
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, output)  # Preserve the exact historical model input bytes.
        converted.append({"case_id": case["case_id"], "source_group": source_group(case["case_id"]),
                          "split": "test", "input_path": relative.as_posix(), "input_sha256": sha(output)})
    new_gold = save_json(destination / "host/gold/test.json", gold)
    baseline = {"source_path": str(legacy_results), "source_sha256": sha(legacy_results),
                "model": results["model"], "settings": results["frozen_settings"],
                "cost_cny": results["cost_cny"], "model_calls": results["model_calls_this_campaign"],
                "metrics": {k: v for k, v in metrics.items() if k != "rows"}, "rows": baseline_rows}
    baseline_path = save_json(destination / "host/baseline.json", baseline)
    group_counts = Counter(c["source_group"] for c in converted)
    prepared = {"schema": "legacy_same50_v1", "dataset": "FinQA", "selection": manifest["selection"],
                "cases": converted, "gold_sha256": {"test": sha(new_gold)},
                "baseline_sha256": sha(baseline_path), "legacy_manifest": str(legacy_manifest),
                "legacy_manifest_sha256": sha(legacy_manifest),
                "repeated_source_groups": {g: n for g, n in group_counts.items() if n > 1},
                "prior_new_agent_exposure": exposures,
                "protocol": "Exact historical test cohort, reused comparison; first attempt only; no learning or tuning"}
    save_json(target, prepared)
    load_cohort(target)
    return target


def load_cohort(path: Path) -> dict:
    path = Path(path).resolve()
    manifest = read_json(path)
    root = path.parent.parent
    if (manifest.get("schema") != "legacy_same50_v1" or manifest.get("dataset") != "FinQA"
            or manifest["selection"]["revision"] != REVISION):
        raise ValueError("Unsupported paired comparison manifest")
    cases = manifest["cases"]
    ids = [c["case_id"] for c in cases]
    if len(ids) != 50 or len(set(ids)) != 50:
        raise ValueError("All fifty distinct original questions are required")
    group_counts = Counter(source_group(i) for i in ids)
    if manifest["repeated_source_groups"] != {g: n for g, n in group_counts.items() if n > 1}:
        raise ValueError("Repeated source group declaration mismatch")
    for case in cases:
        if case["split"] != "test" or case["source_group"] != source_group(case["case_id"]):
            raise ValueError("Invalid split or fabricated source group")
        candidate = (root / case["input_path"]).resolve()
        if not candidate.is_relative_to(root / "inputs") or sha(candidate) != case["input_sha256"]:
            raise ValueError("Input scope or hash mismatch")
        doc = read_json(candidate)
        if set(doc) != {"id", "question", "pre_text", "post_text", "table"} or doc["id"] != case["case_id"]:
            raise ValueError("Input contains forbidden fields or mismatched ID")
    baseline_path, gold_path = root / "host/baseline.json", root / "host/gold/test.json"
    if sha(baseline_path) != manifest["baseline_sha256"] or sha(gold_path) != manifest["gold_sha256"]["test"]:
        raise ValueError("Baseline or reference hash mismatch")
    gold, baseline = read_json(gold_path), read_json(baseline_path)
    if set(gold) != set(ids) or not all(answer_matches(g["answer"], g["answer"]) for g in gold.values()):
        raise ValueError("Invalid reference answers or IDs")
    rows = baseline["rows"]
    if len(rows) != 50 or {r["case_id"] for r in rows} != set(ids):
        raise ValueError("Baseline IDs mismatch")
    for metric in ("answer", "execution", "program"):
        if sum(r[metric + "_correct"] for r in rows) != baseline["metrics"][metric + "_correct"]:
            raise ValueError("Historical baseline aggregate disagrees with per-case results")
    manifest["_root"] = str(root)
    return manifest


def build_report(manifest, rows, freeze, ledger):
    baseline_path = Path(manifest["_root"]) / "host/baseline.json"
    if sha(baseline_path) != manifest["baseline_sha256"]:
        raise ValueError("Historical baseline changed after freeze")
    baseline = read_json(baseline_path)
    old = {r["case_id"]: r for r in baseline["rows"]}
    pairs = [{"case_id": r["case_id"], "old_correct": old[r["case_id"]]["answer_correct"],
              "new_correct": r["passed"], "old_status": old[r["case_id"]]["status"],
              "new_status": r["status"], "predicted_answer": r["predicted_answer"],
              "expected_answer": r["expected_answer"], "run_dir": r["run_dir"]} for r in rows]
    counts = Counter((p["old_correct"], p["new_correct"]) for p in pairs)
    prior_calls = set(freeze["prior_call_ids"])
    calls = [c for c in ledger.calls() if c["call_id"] not in prior_calls]
    usage = Counter()
    for call in calls:
        usage.update(call.get("usage") or {})
    cost = sum((Decimal(c["known_cny"] or "0") for c in calls), Decimal(0))
    return {"created_at": datetime.now(UTC).isoformat(), "dataset": "FinQA", "total": 50,
            "attempted": len(rows), "all_finished": len(rows) == 50, "correct": sum(r["passed"] for r in rows),
            "custom_answer_accuracy": sum(r["passed"] for r in rows) / 50,
            "completed": sum(r["status"] == "completed" for r in rows),
            "engineering_failures": sum(r["status"] != "completed" for r in rows),
            "insufficient_evidence": sum(r.get("final_status") == "insufficient_evidence" for r in rows),
            "baseline": {k: v for k, v in baseline.items() if k != "rows"},
            "paired_counts": {"both_correct": counts[True, True], "improved": counts[False, True],
                              "regressed": counts[True, False], "both_wrong": counts[False, False]},
            "delta_percentage_points": (sum(r["passed"] for r in rows) - baseline["metrics"]["answer_correct"]) * 2 if len(rows) == 50 else None,
            "new_program_accuracy": None,
            "metric": "Numeric/yes-no final answer equality rounded to five decimals, raw FinQA exe_ans scale; not official DSL program accuracy",
            "limitations": ["Reused historical cohort, not a new unseen holdout",
                            "No test-time learning, tuning or best-of retries",
                            "Whole-system comparison; old/new iteration caps and prompt/tool architecture differ",
                            "Repeated company/year groups are retained; cases are not independent documents",
                            "Known cost uses configured peak rates, not the supplier invoice"],
            "model_calls": len(calls), "usage": dict(usage), "known_cost_cny": str(cost),
            "call_ids": [c["call_id"] for c in calls], "budget_after": ledger.status(),
            "freeze": freeze, "pairs": pairs, "results": rows}


def run(manifest_path: Path, output: Path, state: Path, api_key=None):
    manifest_path, output, state = (Path(p).resolve() for p in (manifest_path, output, state))
    if output.exists():
        raise ValueError("Use a fresh output directory; no replacement or best-of attempts")
    manifest = load_cohort(manifest_path)  # Check every input and reference before any paid call.
    ledger = BudgetLedger.open(state / "budget.sqlite3")
    if ledger.status()["halted"]:
        raise ValueError("Ledger halted by uncertain accounting")
    model = ModelClient(ledger, api_key=api_key)
    registry = SkillRegistry(state_dir=state)
    agent = ResearchAgent(model, registry, state, config=AgentConfig(learn_on_failure=False))
    freeze = {"created_at": datetime.now(UTC).isoformat(), "manifest_sha256": sha(manifest_path),
              "runtime_fingerprint": runtime_fingerprint(), "harness_sha256": sha(Path(__file__)),
              "skill_fingerprint": registry.fingerprint(), "case_ids": [c["case_id"] for c in manifest["cases"]],
              "config": agent.config.__dict__, "model": {"name": model.model, "base_url": model.base_url,
              "max_output_tokens": model.max_output_tokens, "timeout": model.timeout, "temperature": 0, "thinking": "disabled"},
              "benchmark_note": BENCHMARK_NOTE, "budget_before": ledger.status(),
              "prior_call_ids": [c["call_id"] for c in ledger.calls()], "protocol": manifest["protocol"],
              "repeated_source_groups": manifest["repeated_source_groups"],
              "prior_new_agent_exposure": manifest["prior_new_agent_exposure"]}
    save_json(output / "freeze.json", freeze)
    rows = []
    for index, case in enumerate(manifest["cases"]):
        if (runtime_fingerprint() != freeze["runtime_fingerprint"] or registry.fingerprint() != freeze["skill_fingerprint"]
                or sha(manifest_path) != freeze["manifest_sha256"] or sha(Path(__file__)) != freeze["harness_sha256"]):
            raise RuntimeError("Code, skills, harness or cohort changed after freeze")
        if ledger.status()["halted"]:
            break
        load_cohort(manifest_path)  # Recheck inputs, baseline and labels before spending on the next case.
        score = evaluate_case(agent, manifest, case, output / "runs" / f"case_{index:03d}")
        result = read_json(Path(score["run_dir"]) / "result.json")
        score["final_status"] = (result.get("final") or {}).get("status")
        score["elapsed_seconds"] = result["elapsed_seconds"]
        score["tool_error_count"] = len(result["tool_errors"])
        rows.append(score)
        save_json(output / "scores.json", rows)
        report = build_report(manifest, rows, freeze, ledger)
        save_json(output / "report.json", report)
        emit({"finished": len(rows), "case_id": score["case_id"], "passed": score["passed"],
              "status": score["status"], "correct_so_far": report["correct"],
              "known_cost_cny": report["known_cost_cny"], "model_calls": report["model_calls"]})
    emit({"all_finished": len(rows) == 50, "budget": ledger.status()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--legacy-manifest", type=Path, required=True)
    p.add_argument("--legacy-results", type=Path, required=True)
    p.add_argument("--destination", type=Path, required=True)
    p.add_argument("--prior-manifest", type=Path, action="append", default=[])
    p = sub.add_parser("run")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--state", type=Path, default=Path(".finagent"))
    p.add_argument("--key-stdin", action="store_true")
    args = parser.parse_args()
    if args.mode == "prepare":
        path = prepare(args.legacy_manifest, args.legacy_results, args.destination, args.prior_manifest)
        emit({"manifest": str(path), "sha256": sha(path)})
    else:
        key = sys.stdin.readline().strip() if args.key_stdin else None
        if args.key_stdin and not key:
            raise ValueError("Empty credential pipe")
        run(args.manifest, args.output, args.state, api_key=key)


if __name__ == "__main__":
    main()
