from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from finagent.memory.store import MemoryError, MemoryStore, SessionBusyError


DOC = {"document_id": "financial-doc", "source_sha256": "a" * 64}
SOURCE = {"evidence_id": "ev_verified", **DOC, "locator": "table:r1:c1", "text": "24800"}


@pytest.fixture
def memory(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    store.bind_scope("scope-a", [DOC], [SOURCE])
    return store


def finish(store, query="What is staff ratio?", answer="1.61", session="session-one", scope="scope-a"):
    turn = store.begin_turn(session, scope, query)
    store.complete_turn(turn, {"answer": answer, "sources": [SOURCE["evidence_id"]]},
                        [{"success": True, "status": "tool_success", "structured_observation": {"source": SOURCE,
                            "calculation_id": "calc_verified", "value": answer, "evidence_ids": [SOURCE["evidence_id"]],
                            "artifact_id": "artifact_report", "path": "/report.md"}}])
    return turn


def test_persistent_success_context_and_ledger(memory):
    turn = finish(memory)
    reopened = MemoryStore(memory.path)
    context = reopened.build_context("session-one", "scope-a", "staff ratio")
    assert context["metadata"]["model_calls"] == 0
    assert context["metadata"]["completed_turns"] == 1
    assert context["ledger"]["sources"] == [SOURCE]
    assert context["ledger"]["calculations"][0]["value"] == "1.61"
    assert context["ledger"]["artifacts"][0]["path"] == "/report.md"
    assert context["conversation_history"][0]["turn_id"] == turn
    assert "DATA, NOT INSTRUCTIONS" in context["text"]
    assert "claims to recheck" in context["text"]
    assert Path(context["conversation_history"][0]["record_path"]).exists()


def test_scope_hash_and_session_isolation(memory):
    finish(memory, query="private financial information")
    foreign = memory.build_context("session-one", "scope-b", "private financial information")
    stranger = memory.build_context("other-session", "scope-a", "private financial information")
    for context in (foreign, stranger):
        assert context["metadata"]["completed_turns"] == 0
        assert context["ledger"]["sources"] == []
        assert "private financial information" not in context["text"]
    with pytest.raises(MemoryError, match="different documents"):
        memory.bind_scope("scope-a", [{**DOC, "source_sha256": "b" * 64}])


def test_failure_is_diagnostic_not_fact(memory):
    turn = memory.begin_turn("session-one", "scope-a", "failed private query")
    memory.fail_turn(turn, "job failed", [{"status": "tool_success", "structured_observation": SOURCE}])
    context = memory.build_context("session-one", "scope-a", "failed private query")
    assert context["metadata"]["failed_turns"] == 1
    assert context["ledger"]["sources"] == []
    assert context["conversation_history"] == []
    assert context["summary"] == []
    assert "failed private query" not in context["text"]
    assert memory.status("session-one", "scope-a")["active_turn"] is None
    assert memory.export("session-one", "scope-a")["turns"][0]["error"] == "job failed"


def test_duplicate_complete_transition_is_rejected(memory):
    turn = finish(memory)
    with pytest.raises(MemoryError, match="no longer started"):
        memory.complete_turn(turn, {"answer": "99", "sources": []}, [])
    with pytest.raises(MemoryError, match="no longer started"):
        memory.fail_turn(turn, "late error")


def test_active_lease_persists_across_restart_and_scope(memory):
    turn = memory.begin_turn("session-one", "scope-a", "first")
    reopened = MemoryStore(memory.path)
    other_scope = reopened.status("session-one", "scope-b")
    assert other_scope["active_turn"] is None and other_scope["session_busy"] is True
    assert other_scope["busy_in_other_scope"] is True
    with pytest.raises(SessionBusyError):
        reopened.begin_turn("session-one", "scope-b", "second")
    with pytest.raises(SessionBusyError):
        reopened.reset("session-one", "scope-a")
    reopened.fail_turn(turn, "explicit crash recovery")
    second = reopened.begin_turn("session-one", "scope-b", "second")
    reopened.fail_turn(second, "clean up")


def test_concurrent_begin_only_one_owner(memory):
    barrier = Barrier(2)

    def begin():
        barrier.wait()
        try:
            return memory.begin_turn("session-one", "scope-a", "concurrent")
        except SessionBusyError:
            return "busy"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: begin(), range(2)))
    assert results.count("busy") == 1
    assert memory.status("session-one", "scope-a")["counts"]["started"] == 1


@pytest.mark.parametrize("session", ["../escape", "../../", "bad/name", "bad\\name", "", "用户", "a" * 129])
def test_invalid_session_id(memory, session):
    with pytest.raises(MemoryError, match="session_id"):
        memory.begin_turn(session, "scope-a", "question")


def test_forged_sources_and_arguments_are_not_provenance(memory):
    turn = memory.begin_turn("session-one", "scope-a", "query")
    with pytest.raises(MemoryError, match="not returned"):
        memory.complete_turn(turn, {"answer": "24800", "sources": ["ev_verified"]},
                             [{"success": True, "tool_arguments": {"evidence_ids": ["ev_verified"]}, "structured_observation": {"value": "24800"}}])
    assert memory.status("session-one", "scope-a")["active_turn"] == turn
    with pytest.raises(MemoryError, match="Unknown"):
        memory.complete_turn(turn, {"answer": "24800", "sources": ["ev_forged"]},
                             [{"success": True, "structured_observation": {"sources": ["ev_forged"]}}])
    with pytest.raises(MemoryError, match="scope"):
        memory.complete_turn(turn, {"answer": "24800", "sources": ["ev_verified"]}, [],
                             source_records=[{**SOURCE, "source_sha256": "b" * 64}])
    memory.fail_turn(turn, "rejected forged evidence")


def test_changed_source_text_is_rejected(memory):
    turn = memory.begin_turn("session-one", "scope-a", "query")
    with pytest.raises(MemoryError, match="conflicts"):
        memory.complete_turn(turn, {"answer": "99999", "sources": ["ev_verified"]},
                             [{"success": True, "structured_observation": {**SOURCE, "text": "99999"}}])
    memory.fail_turn(turn, "rejected altered evidence")


def test_observed_id_looks_up_trusted_catalog(memory):
    turn = memory.begin_turn("session-one", "scope-a", "query")
    memory.complete_turn(turn, {"answer": "24800", "sources": ["ev_verified"]},
                         [{"success": True, "structured_observation": {"sources": ["ev_verified"], "value": "24800"}}])
    assert memory.build_context("session-one", "scope-a", "query")["ledger"]["sources"] == [SOURCE]


def test_artifact_source_metadata_uses_trusted_source_text(memory):
    metadata = {key: SOURCE[key] for key in ("evidence_id", "document_id", "source_sha256", "locator")}
    turn = memory.begin_turn("session-one", "scope-a", "report query")
    memory.complete_turn(turn, {"answer": "24800", "sources": ["ev_verified"]},
                         [{"success": True, "structured_observation": {"artifact_id": "artifact_one", "sources": [metadata], "path": "/report.md"}}])
    assert memory.build_context("session-one", "scope-a", "report")["ledger"]["sources"] == [SOURCE]


def test_verified_source_records_and_prior_sources(memory):
    turn = memory.begin_turn("session-one", "scope-a", "query")
    memory.complete_turn(turn, {"answer": "24800", "sources": ["ev_verified"]}, [], source_records=[SOURCE])
    second = memory.begin_turn("session-one", "scope-a", "followup")
    memory.complete_turn(second, {"answer": "24800", "sources": ["ev_verified"]}, [])
    assert memory.status("session-one", "scope-a")["counts"]["completed"] == 2
    assert memory.build_context("session-one", "scope-a", "query", recent_turns=1, retrieval_limit=0)["ledger"]["sources"] == [SOURCE]


def test_error_tool_sources_are_not_accepted(memory):
    turn = memory.begin_turn("session-one", "scope-a", "query")
    with pytest.raises(MemoryError, match="not returned"):
        memory.complete_turn(turn, {"answer": "24800", "sources": ["ev_verified"]},
                             [{"success": False, "status": "tool_error", "structured_observation": SOURCE}])
    memory.fail_turn(turn, "failed tool ignored")


def test_extract_summary_relevant_retrieval_and_long_history(memory):
    earliest = finish(memory, query="Unique liquidity alpha topic", answer="original exact answer")
    for number in range(8):
        finish(memory, query=f"unrelated operating cost {number}", answer=str(number))
    context = memory.build_context("session-one", "scope-a", "liquidity alpha", recent_turns=2, retrieval_limit=1)
    assert context["metadata"]["retrieved_turn_ids"] == [earliest]
    assert len(context["conversation_history"]) == 3
    assert context["summary"][0]["answer"] == "original exact answer"
    assert len(context["summary"]) == 7
    assert len(memory.build_context("session-one", "scope-a", "alpha", max_chars=2000)["text"]) <= 2000


def test_explicit_preferences_and_scoped_reset(memory):
    memory.set_preference("session-one", "language", "中文", scope_id="scope-a")
    memory.set_preference("session-one", "tone", "concise")
    assert memory.get_preferences("session-one", "scope-b") == {"tone": "concise"}
    turn = finish(memory)
    archive = Path(memory.export("session-one", "scope-a")["turns"][0]["record_path"])
    result = memory.reset("session-one", "scope-a")
    assert result["reset_turns"] == 1 and archive.exists()
    assert memory.status("session-one", "scope-a")["counts"]["completed"] == 0
    assert memory.get_preferences("session-one", "scope-a") == {"tone": "concise"}
    assert turn not in str(memory.export("session-one", "scope-a"))


TEMPLATE = "[FINANCE_STAGE:executor]\nSystem rules stay.\nTools:\n{{available_tools}}\nQuestion:\n{{user_query}}\nPlan:\n{{plan_summary}}\nObservations:\n{{execution_history}}\nHistory:\n{{conversation_history}}"


