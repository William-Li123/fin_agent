"""Explicit paid acceptance driver; credentials come only from environment or a stdin pipe."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import sys
from finagent.core.agent import ResearchAgent
from finagent.evaluation.dataset import load_manifest
from finagent.evaluation.runner import evaluate_case, run_evaluation
from finagent.io import read_json, save_json
from finagent.learning import LearningService
from finagent.models import BudgetLedger, ModelClient
from finagent.skills import SkillRegistry


def emit(value):
    import json
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["case", "dev", "test", "workflow", "followup", "propose", "validate"])
    p.add_argument("--key-stdin", action="store_true")
    p.add_argument("--state", type=Path, default=Path(".finagent"))
    p.add_argument("--manifest", type=Path, default=Path("data/acceptance/host/manifest.json"))
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--index", type=int, default=0)
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--source-run", type=Path)
    p.add_argument("--candidate")
    p.add_argument("--session", default="acceptance_workflow")
    args = p.parse_args()
    if args.key_stdin:
        os.environ["DEEPSEEK_API_KEY"] = sys.stdin.readline().strip()
    ledger = BudgetLedger.open(args.state / "budget.sqlite3")
    model = ModelClient(ledger)
    registry = SkillRegistry(state_dir=args.state)
    learning = LearningService(args.state, registry, lambda messages, stage: model.complete(messages, stage=stage))
    agent = ResearchAgent(model, registry, args.state, learning=learning)
    manifest = load_manifest(args.manifest)
    dev = [c for c in manifest["cases"] if c["split"] == "dev"]
    if args.mode == "case":
        result = evaluate_case(agent, manifest, dev[args.index], args.output / "run")
        save_json(args.output / "score.json", result)
        emit(result)
    elif args.mode in {"dev", "test"}:
        result = run_evaluation(agent, args.manifest, args.output, split=args.mode, on_case=emit)
        emit({k: v for k, v in result.items() if k != "results"})
    elif args.mode in {"workflow", "followup"}:
        path = Path(manifest["_root"]) / dev[args.index]["input_path"]
        doc = read_json(path)
        if args.mode == "workflow":
            query = (doc["question"] + "\nUse the terminal-analysis skill. Use finance_terminal Python to batch source retrieval and evidence-backed "
                "finance calculations through its finance SDK. Also generate a source-cited Markdown report and an SVG chart with meaningful observed values. "
                "Read the resulting report to check it exists. Return the calculated answer with a calculation_id. This is a real document workflow acceptance check.")
        else:
            query = "根据本会话上一轮的计算结果，用中文解释你当时使用了哪些数值、它们分别来自哪里，以及计算公式。保留来源引用；不要重新生成报告。"
        result = agent.run(query, [path], session_id=args.session, run_dir=args.output, split="dev",
                           case_id=dev[args.index]["case_id"], source_group=dev[args.index]["source_group"])
        emit(result)
    elif args.mode == "propose":
        result = read_json(args.source_run / "result.json")
        request = read_json(args.source_run / "request.json")
        diagnostic = result.get("error") or result.get("tool_errors") or "The independent development scorer rejected the numeric conclusion. Inspect source selection, period, unit and arithmetic; no reference answer is supplied."
        experience = learning.record_failure(result["case_id"], request["question"], read_json(args.source_run / "trace.json"), diagnostic,
                                             split=result["split"], source_group=result["source_group"])
        candidate = learning.propose(experience)
        save_json(args.output / "candidate.json", candidate)
        emit({"experience_id": experience, "candidate_id": candidate["id"], "name": candidate["name"], "status": "quarantined"})
    else:
        candidate = learning.candidate(args.candidate)
        experience = next(e for e in learning.experiences() if e["id"] == candidate["source_experience_id"])
        cases = [c for c in dev if c["source_group"] != experience["source_group"]][:args.count]
        def evaluate(case, skill):
            index = cases.index(case)
            row = evaluate_case(agent, manifest, case, args.output / ("candidate" if skill else "baseline") / f"case_{index:03d}", candidate=skill)
            emit({"arm": "candidate" if skill else "baseline", **row})
            return row
        report = learning.validate(args.candidate, cases, evaluate)
        save_json(args.output / "validation.json", report)
        if report["approved"]:
            promoted = learning.promote(args.candidate, report["id"])
            save_json(args.output / "promotion.json", promoted)
        emit({k: v for k, v in report.items() if k != "outcomes"})
    emit({"budget": ledger.status()})


if __name__ == "__main__":
    main()
