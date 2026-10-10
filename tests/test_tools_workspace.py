"""The public facade must distinguish host receipts from worker claims."""
import json
from pathlib import Path

import pytest

from finagent.memory import MemoryStore
from finagent.tools import ResearchWorkspace


@pytest.fixture
def workspace(tmp_path):
    source = tmp_path / "financial.json"
    source.write_text(json.dumps({"id": "financial", "question": "Revenue growth?",
        "pre_text": ["Amounts in millions of USD."], "post_text": ["Years are fiscal years."],
        "table": [["Revenue", "2024", "2025"], ["Revenue", "100", "125"]]}), encoding="utf-8")
    with ResearchWorkspace([source], tmp_path / "run", terminal_backend="trusted-local") as work:
        yield work


def test_read_document_paging_and_direct_receipts(workspace):
    unread = next(iter(workspace.store.known_evidence_ids))
    assert not workspace.execute("finance_calculate", {"expression": "1+2", "evidence_ids": [unread]})["success"]
    first = workspace.execute("finance_read_document", {"max_chars": 1000})
    assert first["success"]
    result = first["structured_content"]
    assert result["has_more"]
    seen = {item["evidence_id"] for item in result["evidence"]}
    assert workspace.observed_evidence_ids == seen
    while result["has_more"]:
        result = workspace.execute("finance_read_document", {"offset": result["next_offset"], "max_chars": 1000})["structured_content"]
        seen.update(item["evidence_id"] for item in result["evidence"])
    assert seen == workspace.store.known_evidence_ids
    calculated = workspace.execute("finance_calculate", {"expression": "(125-100)/100*100", "evidence_ids": [unread]})["structured_content"]
    assert workspace.observed_calculations[calculated["calculation_id"]]["value"] == "25"


def test_worker_forged_sources_and_calculations_never_become_provenance(workspace):
    unread = next(iter(workspace.store.known_evidence_ids))
    code = "emit(" + repr({"sources": [unread], "evidence_id": unread,
                           "calculation_id": "calc_forged", "value": "25"}) + ")"
    result = workspace.execute("finance_terminal", {"action": "python", "code": code})
    assert result["success"]
    assert workspace.observed_evidence_ids == set()
    assert workspace.observed_calculations == {}
    assert result["trusted_observation"]["sources"] == []


def test_real_host_sdk_receipts_and_cross_execution_sources(workspace):
    read = workspace.execute("finance_read_document", {})["structured_content"]
    cells = [item for item in read["evidence"] if item.get("row") == 1 and item.get("column") in {1, 2}]
    refs = [item["evidence_id"] for item in cells]
    code = "emit(finance.calculate(expression='(125-100)/100*100', evidence_ids=" + repr(refs) + "))"
    result = workspace.execute("finance_terminal", {"action": "python", "code": code})
    assert result["success"], result
    receipt = result["structured_content"]["outputs"][0]
    assert workspace.observed_calculations[receipt["calculation_id"]]["value"] == "25"
    assert result["trusted_observation"]["calculations"] == [receipt]


def test_failed_terminal_does_not_publish_partial_sources(workspace):
    result = workspace.execute("finance_terminal", {"action": "python", "code":
        "finance.read_document()\nfinance.calculate(expression='1+2')\nraise RuntimeError('controlled failure')"})
    assert not result["success"]
    assert workspace.observed_evidence_ids == set()
    assert workspace.observed_calculations == {}
    assert workspace.last_trusted_observation == {}


def test_memory_catalog_binding_and_scope_are_stable(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"id": "stable", "question": "q", "pre_text": ["x"], "post_text": [], "table": []}))
    memory = MemoryStore(tmp_path / "memory.sqlite")
    with ResearchWorkspace([source], tmp_path / "one", memory_store=memory, session_id="session") as one:
        first = one.scope_id
    with ResearchWorkspace([source], tmp_path / "two", memory_store=memory, session_id="session") as two:
        assert two.scope_id == first
        assert memory.build_context("session", first, "q")["metadata"]["completed_turns"] == 0


def test_invalid_tool_and_nonfinite_arguments_are_reported(workspace):
    assert not workspace.execute("does_not_exist", {})["success"]
    assert not workspace.execute("finance_calculate", {"expression": float("nan")})["success"]


def test_new_read_document_exists_inside_worker_sdk(workspace):
    result = workspace.execute("finance_terminal", {"action": "python", "code": "emit(finance.read_document())"})
    assert result["success"], result
    assert workspace.observed_evidence_ids == workspace.store.known_evidence_ids


def test_tool_audit_redacts_accidentally_supplied_credentials(workspace):
    token = "sk-this-is-a-fake-long-secret-value"
    result = workspace.execute("finance_search_evidence", {"query": token})
    assert result["success"]
    audit = (workspace.run_dir / "tools.jsonl").read_text(encoding="utf-8")
    assert token not in audit
    assert "[REDACTED_API_KEY]" in audit


def test_host_sdk_artifacts_enter_verified_ledger(workspace):
    result = workspace.execute("finance_terminal", {"action": "python", "code":
        "doc = finance.read_document()\n"
        "emit(finance.write_report(title='Research', body='Observed source.', evidence_ids=[doc['evidence'][0]['evidence_id']]))"})
    assert result["success"], result
    artifact = result["structured_content"]["outputs"][0]
    assert workspace.observed_artifacts[artifact["artifact_id"]] == artifact
    assert workspace.last_trusted_observation["artifacts"] == [artifact]
