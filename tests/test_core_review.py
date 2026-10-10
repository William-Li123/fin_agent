"""Regression cases found while reviewing the orchestration boundaries."""
import json

import pytest

from finagent.core.agent import AgentConfig, ResearchAgent, SYSTEM
from finagent.core.context import ContextWindow
from finagent.core.validation import validate_final


def final(answer, **extra):
    return {"status": "answered", "answer": answer, "explanation": "A source-backed financial conclusion.",
            "sources": ["ev_observed"], **extra}


def test_advertised_context_recall_action_actually_executes(tmp_path):
    context = ContextWindow(tmp_path / "context")
    context.append({"action": "tool", "tool": "sample"}, {"success": True, "evidence": "archived source text"})
    example = next(json.loads(line) for line in SYSTEM.splitlines()
                   if line.startswith("{") and '"tool":"context_read"' in line)
    result = context.recall(**example["arguments"])
    assert "archived source text" in result["text"]


def test_nonscalar_calculation_receipt_cannot_authorize_arbitrary_number():
    comparisons = {"calc_comparison": {"calculation_id": "calc_comparison", "delta": "10",
                   "change_percent": "20", "evidence_ids": ["ev_observed"]}}
    with pytest.raises(ValueError):
        validate_final(final(999, calculation_id="calc_comparison"), {"ev_observed"}, comparisons)


@pytest.mark.parametrize("answer", ["12%", "$1,200", "NaN", "Infinity", "-inf"])
def test_numeric_formatted_or_nonfinite_strings_cannot_bypass_calculation(answer):
    with pytest.raises(ValueError):
        validate_final(final(answer), {"ev_observed"}, {})


def test_insufficient_evidence_requires_null_answer():
    with pytest.raises(ValueError):
        validate_final(final("a definite claim", status="insufficient_evidence"), {"ev_observed"}, {})


@pytest.mark.parametrize("answer", [" ", "\n\t\r", "\u3000"])
def test_whitespace_only_text_cannot_be_an_answered_conclusion(answer):
    with pytest.raises(ValueError):
        validate_final(final(answer), {"ev_observed"}, {})


@pytest.mark.parametrize("answer", [123, 123.0, "123", "1.23e2"])
def test_finite_answers_matching_verified_receipt_remain_accepted(answer):
    calculations = {"calc_verified": {"calculation_id": "calc_verified", "value": "123",
                                      "evidence_ids": ["ev_observed"]}}
    result = validate_final(final(answer, calculation_id="calc_verified"), {"ev_observed"}, calculations)
    assert result["status"] == "answered"
    assert result["answer"] == answer


def test_nonempty_research_text_remains_accepted_without_numeric_receipt():
    result = validate_final(final("Revenue increased across the cited periods."), {"ev_observed"}, {})
    assert result["status"] == "answered"


class EmptyRegistry:
    def fingerprint(self):
        return "fixed-review-catalog"

    def catalog(self):
        return []


class FailedClaimModel:
    model = "review-fake"

    def complete(self, messages, *, stage):
        return json.dumps({"action": "finish", "final": final("unverified-failed-claim-marker", sources=["ev_invented"])})


def test_failed_run_is_diagnostic_only_and_never_retrieved_as_facts(tmp_path):
    document = tmp_path / "input.json"
    document.write_text(json.dumps({"id": "review-document", "question": "What is revenue?", "pre_text": ["Revenue was reported."],
                                    "post_text": [], "table": [["year", "revenue"], ["current", "100"]]}), encoding="utf-8")
    agent = ResearchAgent(FailedClaimModel(), EmptyRegistry(), tmp_path / "state",
                          config=AgentConfig(max_steps=1, terminal_backend="trusted-local"))
    result = agent.run("What is revenue?", [document], session_id="review_session")
    assert result["status"] == "failed"
    context = agent.memory.build_context("review_session", result["scope_id"], "revenue")
    assert context["metadata"]["completed_turns"] == 0
    assert context["metadata"]["failed_turns"] == 1
    assert context["ledger"]["sources"] == []
    assert "unverified-failed-claim-marker" not in context["text"]
