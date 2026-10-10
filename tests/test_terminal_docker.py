"""Opt-in live container acceptance; no model API or external data request."""
import json
import os
from pathlib import Path

import pytest

from finagent.tools import ResearchWorkspace


@pytest.mark.skipif(os.environ.get("FINAGENT_DOCKER_TESTS") != "1",
                    reason="Set FINAGENT_DOCKER_TESTS=1 with the local terminal image built")
def test_live_docker_analysis_sdk_artifacts_and_file_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-secret-that-must-not-enter-container")
    source = tmp_path / "financial.json"
    source.write_text(json.dumps({"id": "docker-fixture", "question": "Revenue growth?",
        "pre_text": ["Amounts in millions of USD."], "post_text": [],
        "table": [["Revenue", "2024", "2025"], ["Revenue", "100", "125"]]}), encoding="utf-8")
    with ResearchWorkspace([source], tmp_path / "run") as workspace:
        first = workspace.execute("finance_terminal", {"action": "python", "timeout_seconds": 45, "code": '''
import os
from pathlib import Path
import pandas as pd
import pypdf, openpyxl, matplotlib
assert "DEEPSEEK_API_KEY" not in os.environ
assert Path("/inputs").is_dir()
assert len(list(Path("/inputs").glob("*.json"))) == 1
table = finance.read_table(row_start=1)
cells = table["rows"][0]["cells"]
refs = [cells[1]["evidence_id"], cells[2]["evidence_id"]]
result = finance.compare_periods(current=cells[2]["text"], previous=cells[1]["text"], evidence_ids=refs)
assert result["change_percent"] == "25"
pd.DataFrame({"year": [2024, 2025], "revenue": [100, 125]}).to_csv("revenue.csv", index=False)
emit(result)
emit(finance.render_chart(title="Revenue", labels=["2024", "2025"], values=["100", "125"], evidence_ids=refs))
emit(finance.write_report(title="Revenue research", body="Revenue increased by 25 percent.", evidence_ids=refs))
'''})
        assert first["success"], first
        assert first["structured_content"]["backend"] == "docker"
        assert len(workspace.observed_calculations) == 1
        assert len(workspace.observed_artifacts) == 2
        assert len(workspace.observed_evidence_ids) == 3
        assert all(Path(item["path"]).is_file() for item in workspace.observed_artifacts.values())
        second = workspace.execute("finance_terminal", {"action": "shell", "command": "cat revenue.csv"})
        assert second["success"], second
        assert "2025,125" in second["structured_content"]["stdout"]
