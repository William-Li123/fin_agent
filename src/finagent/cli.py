"""Single supported command-line entry point. No API requests at import/doctor time."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from finagent.core.agent import AgentConfig, ResearchAgent
from finagent.evaluation.dataset import load_manifest, prepare
from finagent.evaluation.runner import evaluate_case, run_evaluation
from finagent.ingestion.documents import ingest
from finagent.io import read_json, redact, save_json
from finagent.learning import LearningService
from finagent.memory.store import MemoryStore
from finagent.models import BudgetLedger, ModelClient
from finagent.skills import SkillRegistry


def _print(value):
    print(redact(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)))


def _services(args, *, paid=False):
    state = args.state.resolve()
    registry = SkillRegistry(state_dir=state)
    model = None
    if paid:
        ledger = BudgetLedger.open(state / "budget.sqlite3")
        model = ModelClient(ledger, base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
    def complete(messages, stage):
        if model is None:
            raise ValueError("This command does not initialize a model client")
        return model.complete(messages, stage=stage)
    learning = LearningService(state, registry, complete)
    return registry, model, learning


def parser():
    root = argparse.ArgumentParser(prog="finagent", description="Financial research with scoped evidence and validated skill learning")
    root.add_argument("--state", type=Path, default=Path(".finagent"), help="Persistent state, memory, skills and one shared cost ledger")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check installation and Docker; no model requests")
    budget = sub.add_parser("budget")
    budget_sub = budget.add_subparsers(dest="operation", required=True)
    init = budget_sub.add_parser("init")
    init.add_argument("--ceiling", required=True)
    init.add_argument("--carried", default="0", help="Commitments already spent/reserved outside this ledger")
    budget_sub.add_parser("status")
    ingestion = sub.add_parser("ingest")
    ingestion.add_argument("input", type=Path)
    ingestion.add_argument("--output", type=Path, required=True)
    ingestion.add_argument("--question", default="")
    ask = sub.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--input", type=Path, action="append", required=True)
    ask.add_argument("--session", default="default")
    ask.add_argument("--output", type=Path)
    ask.add_argument("--max-steps", type=int, default=14)
    ask.add_argument("--learn-on-failure", action="store_true")
    ask.add_argument("--mcp-config", type=Path)
    ask.add_argument("--terminal-backend", choices=["docker", "trusted-local"], default="docker")
    skills = sub.add_parser("skills")
    skills.add_argument("operation", choices=["list", "show", "disable", "enable", "rollback", "versions"])
    skills.add_argument("name", nargs="?")
    skills.add_argument("--version")
    mem = sub.add_parser("memory")
    mem.add_argument("operation", choices=["status", "export", "preference", "fail-turn"])
    mem.add_argument("--session", default="default")
    mem.add_argument("--scope")
    mem.add_argument("--key")
    mem.add_argument("--value")
    evaluation = sub.add_parser("eval")
    es = evaluation.add_subparsers(dest="operation", required=True)
    prep = es.add_parser("prepare")
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--dev-count", type=int, default=12)
    prep.add_argument("--test-count", type=int, default=12)
    prep.add_argument("--seed", type=int, default=20261010)
    prep.add_argument("--raw-cache", type=Path)
    run = es.add_parser("run")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--split", choices=["dev", "test"], default="test")
    run.add_argument("--learn-on-failure", action="store_true", help="Propose quarantined skills for scored development failures only")
    learn = sub.add_parser("learn")
    ls = learn.add_subparsers(dest="operation", required=True)
    ls.add_parser("list")
    record = ls.add_parser("record")
    record.add_argument("--run", type=Path, required=True)
    record.add_argument("--error", required=True, help="Development failure diagnostic; do not pass gold answers")
    propose = ls.add_parser("propose")
    propose.add_argument("experience")
    validation = ls.add_parser("validate")
    validation.add_argument("candidate")
    validation.add_argument("--manifest", type=Path, required=True)
    validation.add_argument("--output", type=Path, required=True)
    validation.add_argument("--count", type=int, default=3)
    promote = ls.add_parser("promote")
    promote.add_argument("candidate")
    promote.add_argument("--report")
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            docker = shutil.which("docker")
            info = {"python": os.sys.version.split()[0], "docker_executable": docker,
                    "api_key_configured": bool(os.environ.get("DEEPSEEK_API_KEY")), "model_requests": 0}
            if docker:
                for label, command in (("engine", [docker, "info", "--format", "{{.ServerVersion}}"]),
                                       ("image", [docker, "image", "inspect", "finance-agent-terminal:local", "--format", "{{.Id}}"] )):
                    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
                    info[label] = result.stdout.strip() if result.returncode == 0 else "unavailable"
            info["skills"] = len(SkillRegistry(state_dir=args.state).catalog())
            _print(info)
        elif args.command == "budget":
            ledger = BudgetLedger(args.state / "budget.sqlite3", ceiling_cny=args.ceiling, carried_cny=args.carried) if args.operation == "init" else BudgetLedger.open(args.state / "budget.sqlite3")
            _print(ledger.status())
        elif args.command == "ingest":
            _print({"inputs": [str(p) for p in ingest(args.input, args.output, question=args.question)]})
        elif args.command == "skills":
            registry, _, _ = _services(args)
            if args.operation == "list":
                _print(registry.catalog())
            elif not args.name:
                raise ValueError("This operation requires a skill name")
            elif args.operation == "show":
                _print(registry.load(args.name))
            elif args.operation == "rollback":
                _print(registry.rollback(args.name, args.version))
            else:
                _print(getattr(registry, args.operation)(args.name))
        elif args.command == "memory":
            memory = MemoryStore(args.state / "memory.sqlite3")
            if args.operation == "preference":
                if not args.key or args.value is None:
                    raise ValueError("preference requires --key and --value")
                memory.set_preference(args.session, args.key, args.value, scope_id=args.scope)
                _print({"saved": True})
            elif args.operation == "fail-turn":
                if not args.key or not args.value:
                    raise ValueError("Use --key TURN_ID --value REASON to release an abandoned lease")
                memory.fail_turn(args.key, args.value)
                _print({"released": args.key})
            else:
                if not args.scope:
                    raise ValueError("memory status/export requires --scope from the run result")
                _print(getattr(memory, args.operation)(args.session, args.scope))
        elif args.command == "ask":
            registry, model, learning = _services(args, paid=True)
            config = AgentConfig(max_steps=args.max_steps, terminal_backend=args.terminal_backend, learn_on_failure=args.learn_on_failure)
            agent = ResearchAgent(model, registry, args.state, config=config, learning=learning, mcp_config=args.mcp_config)
            result = agent.run(args.question, args.input, session_id=args.session, run_dir=args.output)
            _print(result)
            return 0 if result["status"] == "completed" else 1
        elif args.command == "eval":
            if args.operation == "prepare":
                _print({"manifest": str(prepare(args.output, dev_count=args.dev_count, test_count=args.test_count, seed=args.seed, raw_cache=args.raw_cache))})
            else:
                registry, model, learning = _services(args, paid=True)
                agent = ResearchAgent(model, registry, args.state, learning=learning)
                report = run_evaluation(agent, args.manifest, args.output, split=args.split,
                    learn_from_failures=args.learn_on_failure,
                    on_case=lambda row: _print({k: row[k] for k in ("case_id", "status", "passed", "steps")}))
                _print({k: v for k, v in report.items() if k != "results"})
        elif args.command == "learn":
            registry, model, learning = _services(args, paid=args.operation in {"propose", "validate"})
            if args.operation == "list":
                _print({"experiences": learning.experiences(), "candidates": learning.candidates(), "reports": learning.reports()})
            elif args.operation == "record":
                result = read_json(args.run / "result.json")
                request = read_json(args.run / "request.json")
                identity = learning.record_failure(result["case_id"] or result["run_id"], request["question"], read_json(args.run / "trace.json"),
                    args.error, split="dev" if result["split"] == "interactive" else result["split"], source_group=result["source_group"])
                _print({"experience_id": identity})
            elif args.operation == "propose":
                _print(learning.propose(args.experience))
            elif args.operation == "promote":
                _print(learning.promote(args.candidate, args.report))
            else:
                manifest = load_manifest(args.manifest)
                candidate = learning.candidate(args.candidate)
                experience = next(e for e in learning.experiences() if e["id"] == candidate["source_experience_id"])
                cases = [c for c in manifest["cases"] if c["split"] == "dev" and c["case_id"] != experience["case_id"]
                         and c["source_group"] != experience["source_group"]][:args.count]
                agent = ResearchAgent(model, registry, args.state)
                def evaluate(case, skill):
                    return evaluate_case(agent, manifest, case, args.output / ("candidate" if skill else "baseline") / ("run_" + uuid4().hex), candidate=skill)
                report = learning.validate(args.candidate, cases, evaluate)
                save_json(args.output / "validation.json", report)
                _print(report)
        return 0
    except (ValueError, KeyError, RuntimeError, OSError) as error:
        _print({"error": f"{type(error).__name__}: {error}"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
