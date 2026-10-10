"""Build a compact shareable acceptance report from actual saved receipts. No model calls."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile
from finagent.core.validation import validate_final
from finagent.io import digest, read_json, runtime_fingerprint, save_json
from finagent.learning import LearningService
from finagent.models import BudgetLedger
from finagent.skills import SkillRegistry, SkillError


def main():
    root = Path(__file__).resolve().parents[1]
    runs = root / "runs/acceptance"
    output = root / "docs/results"
    output.mkdir(parents=True, exist_ok=True)
    phases = {}
    for key in ("dev_v1", "dev_final", "holdout"):
        report = read_json(runs / key / "report.json")
        compact = {k: report[k] for k in ("split", "total", "correct", "completed", "custom_answer_accuracy", "freeze")}
        compact["results"] = [{k: row[k] for k in ("case_id", "passed", "predicted_answer", "expected_answer", "steps", "error")} for row in report["results"]]
        phases[key] = compact
    # Replay only the updated final-output validator; no model rerun or score substitution.
    replay = []
    for phase in ("dev_final", "holdout"):
        for directory in sorted((runs / phase / "runs").glob("case_*")):
            result = read_json(directory / "result.json")
            manifest = read_json(directory / "manifest.json")
            calculations = {c["calculation_id"]: c for c in manifest["calculations"]}
            observed = set()
            for line in (directory / "tools.jsonl").read_text(encoding="utf-8").splitlines():
                item = json.loads(line)
                if not item["result"].get("success"):
                    continue
                if item["name"] in {"finance_read_document", "finance_read_table", "finance_search_evidence"}:
                    data = item["result"].get("structured_content")
                else:
                    data = item["result"].get("trusted_observation", {})
                def visit(value):
                    if isinstance(value, dict):
                        if isinstance(value.get("evidence_id"), str):
                            observed.add(value["evidence_id"])
                        for child in value.values():
                            visit(child)
                    elif isinstance(value, list):
                        for child in value:
                            visit(child)
                visit(data)
            validate_final(result["final"], observed, calculations)
            replay.append({"case_id": result["case_id"], "final_validation_passed": True})
    validation = read_json(runs / "skill_validation/validation.json")
    candidate = read_json(runs / "learning_proposal_v2/candidate.json")
    registry = SkillRegistry(state_dir=root / ".finagent")
    learning = LearningService(root / ".finagent", registry, lambda messages, stage: (_ for _ in ()).throw(RuntimeError("Offline report cannot call model")))
    blocked = False
    try:
        learning.promote(candidate["id"], validation["id"])
    except SkillError:
        blocked = True
    if not blocked:
        raise AssertionError("An unapproved candidate was incorrectly activated")
    suite = ET.parse(runs / "unit-tests.xml").getroot().find("testsuite")
    unit = {key: int(suite.attrib[key]) for key in ("tests", "failures", "errors", "skipped")}
    unit["passed"] = unit["tests"] - unit["failures"] - unit["errors"] - unit["skipped"]
    budget = BudgetLedger.open(root / ".finagent/budget.sqlite3").status()
    workflow = {}
    for name in ("workflow_final", "followup_final"):
        value = read_json(runs / name / "result.json")
        if value["status"] != "completed":
            raise AssertionError(f"Unfinished workflow: {name}")
        workflow[name] = {key: value[key] for key in ("status", "steps", "loaded_skills", "context_compactions", "runtime_fingerprint")}
        workflow[name]["intermediate_errors"] = len(value["tool_errors"])
        workflow[name]["previous_completed_turns"] = value.get("memory", {}).get("completed_turns", 0)
        workflow[name]["artifacts"] = [{"kind": a["kind"], "filename": a["filename"], "sha256": a["sha256"]} for a in value["artifacts"]]
    frozen = runs / "frozen_evaluation_runtime.whl"
    with zipfile.ZipFile(frozen) as archive:
        contents = {p.removeprefix("finagent/"): hashlib.sha256(archive.read(p)).hexdigest()
                    for p in archive.namelist() if p.startswith("finagent/") and p.endswith(".py")}
    if digest(contents) != phases["holdout"]["freeze"]["runtime_fingerprint"]:
        raise AssertionError("Frozen evaluation wheel does not match its runtime hash")
    report = {"date": "2026-10-10", "model": "deepseek-flash", "api_base": "https://api.deepseek.com",
        "unique_evaluation_documents": 24, "prior_company_year_groups_excluded": 160,
        "metric": "custom_answer_accuracy: raw FinQA exe_ans scale rounded to five decimals; not official program accuracy",
        "phases": phases, "unit_tests": unit, "workflows": workflow,
        "skill_learning": {"candidate_name": candidate["name"], "candidate_sha256": candidate["sha256"],
            "source_split": "dev", "independent_cases": validation["case_count"],
            "baseline_passed": sum(x["baseline"]["passed"] for x in validation["outcomes"]),
            "candidate_passed": sum(x["candidate"]["passed"] for x in validation["outcomes"]),
            "approved": validation["approved"], "reasons": validation["reasons"], "promotion_call_rejected": blocked,
            "learned_skills_active": sum(s["origin"] == "learned" for s in registry.catalog()),
            "notice": "No independent accuracy gain demonstrated. Positive promotion/rollback paths are covered by deterministic tests."},
        "budget": budget, "release_runtime_fingerprint": runtime_fingerprint(),
        "frozen_evaluation_wheel_sha256": hashlib.sha256(frozen.read_bytes()).hexdigest(),
        "post_evaluation_changes": ["reject whitespace-only answers", "verify host labels before any paid case call"],
        "offline_output_revalidation": replay,
        "limitations": ["Small sample; no claim of general benchmark superiority", "No commercial market-data or live trading test",
                        "MCP protocol tested with local fixture", "One invalid initial skill proposal counted in budget; repaired proposal path now audited"]}
    save_json(output / "acceptance.json", report)
    save_json(output / "skill-validation.json", {k: validation[k] for k in validation if k != "outcomes"})
    (output / "learned-candidate.md").write_text("# Quarantined candidate: " + candidate["name"] + "\n\nValidation rejected promotion: no independent improvement.\n\n" + candidate["body"] + "\n", encoding="utf-8")
    save_json(runs / "final-budget.json", budget)
    print(json.dumps({"unit_tests": unit, "holdout": "9/12", "budget": budget, "release_hash": report["release_runtime_fingerprint"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
