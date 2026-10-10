"""Check an independently installed wheel, including its packaged Docker worker. No model requests."""
import argparse
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--package-root", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
target = args.package_root.resolve()
sys.path.insert(0, str(target))
import finagent
from finagent.io import save_json
from finagent.skills import SkillRegistry
from finagent.tools import ResearchWorkspace

assert Path(finagent.__file__).is_relative_to(target), "Check must import from the installed wheel"
assert len(SkillRegistry(state_dir=args.output).catalog()) == 6
source = save_json(args.output / "input.json", {"id": "wheel-smoke", "question": "Revenue change?",
    "pre_text": ["Synthetic smoke test, values in millions"], "post_text": [],
    "table": [["Metric", "Current", "Previous"], ["Revenue", "120", "100"]]})
with ResearchWorkspace([source], args.output / "run") as workspace:
    result = workspace.execute("finance_terminal", {"action": "python", "code": '''
t = finance.read_table()
refs = [c["evidence_id"] for row in t["rows"] for c in row["cells"] if c.get("row_label") == "Revenue"]
emit(finance.calculate(expression="120-100", evidence_ids=refs))
'''})
    assert result["success"], result.get("error")
    assert any(c["value"] == "20" for c in workspace.observed_calculations.values())
summary = {"installed_wheel_import": True, "packaged_skills": 6, "packaged_docker_sdk": True}
save_json(args.output / "verification.json", summary)
print(summary)
