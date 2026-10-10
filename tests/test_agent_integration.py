"""Exercise the complete local agent loop using actual tool observations.

Scripted models choose actions; they do not create evidence IDs, receipts, or
memory records. Those values must be produced by the production runtime.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from finagent.core.agent import AgentConfig, ResearchAgent
from finagent.core.context import ContextWindow
from finagent.io import read_json
from finagent.learning import LearningService
from finagent.skills import SkillRegistry


def write_document(path: Path, *, identity="company/report", question="What is revenue growth?") -> Path:
    path.write_text(json.dumps({"id": identity, "question": question, "pre_text": ["Amounts in millions of USD."],
        "post_text": [], "table": [["Revenue", "previous", "current"], ["Revenue", "100", "125"]]}), encoding="utf-8")
    return path


def observations(messages):
    return [json.loads(message["content"].split("\n", 1)[1]) for message in messages
            if message["content"].startswith("TOOL OBSERVATION ")
            and "structured_content" in json.loads(message["content"].split("\n", 1)[1])]


def finish(receipt):
    return {"action": "finish", "final": {"status": "answered", "answer": receipt["value"], "unit": "ratio",
        "explanation": "Revenue change divided by the prior period revenue, using the cited observed values.",
        "sources": receipt["evidence_ids"], "calculation_id": receipt["calculation_id"]}}


class FinancialSequenceModel:
    model = "scripted-financial-actions"

    def __init__(self):
        self.messages = []

    def complete(self, messages, *, stage):
        self.messages.append(messages)
        if not any(message["content"].startswith("LOADED PROCEDURAL SKILLS") for message in messages):
            return json.dumps({"action": "skill", "name": "financial-ratios"})
        results = observations(messages)
        if not results:
            return json.dumps({"action": "tool", "tool": "finance_read_document", "arguments": {}})
        latest = results[-1]
        assert latest["success"], latest
        record = latest["structured_content"]
        if "evidence" in record:
            values = {item["column"]: item for item in record["evidence"] if item.get("row") == 1 and item.get("column") in {1, 2}}
            assert set(values) == {1, 2}
            return json.dumps({"action": "tool", "tool": "finance_calculate", "arguments": {
                "expression": "(current-previous)/previous", "variables": {"previous": values[1]["text"], "current": values[2]["text"]},
                "evidence_ids": [values[1]["evidence_id"], values[2]["evidence_id"]]}})
        return json.dumps(finish(record))


class MemoryFollowupModel:
    model = "scripted-memory-followup"

    def __init__(self):
        self.memory = None

    def complete(self, messages, *, stage):
        if not any(message["content"].startswith("LOADED PROCEDURAL SKILLS") for message in messages):
            return json.dumps({"action": "skill", "name": "financial-ratios"})
        results = observations(messages)
        if results:
            assert results[-1]["success"], results[-1]
            return json.dumps(finish(results[-1]["structured_content"]))
        outer = next(json.loads(message["content"].split("\n", 1)[1]) for message in messages
                     if message["content"].startswith("SCOPED PREVIOUS MEMORY "))
        self.memory = json.loads(outer["text"][outer["text"].index("{"):])
        receipt = self.memory["observed_source_ledger"]["calculations"][0]
        return json.dumps({"action": "tool", "tool": "finance_calculate", "arguments": {
            "expression": "prior_result*2", "variables": {"prior_result": receipt["value"]},
            "evidence_ids": receipt["evidence_ids"]}})


def build_agent(tmp_path, model, *, learning=None, learn=False, max_steps=5):
    state = tmp_path / "state"
    registry = SkillRegistry(state_dir=state)
    return ResearchAgent(model, registry, state,
        config=AgentConfig(max_steps=max_steps, terminal_backend="trusted-local", learn_on_failure=learn), learning=learning)


def test_numeric_agent_loop_uses_host_evidence_and_calculation_receipts(tmp_path):
    source = write_document(tmp_path / "input.json")
    model = FinancialSequenceModel()
    agent = build_agent(tmp_path, model)
    result = agent.run("What is revenue growth?", [source], session_id="financial")
    assert result["status"] == "completed", result
    assert result["steps"] == 4
    assert result["final"]["answer"] == "0.25"
    assert len(result["final"]["sources"]) == 2
    trace = read_json(Path(result["run_dir"]) / "trace.json")
    assert [event["action"]["action"] for event in trace] == ["skill", "tool", "tool", "finish"]
    calculation = trace[2]["observation"]["structured_content"]
    assert result["final"]["calculation_id"] == calculation["calculation_id"]
    persisted = agent.memory.export("financial", result["scope_id"])["turns"][0]
    assert persisted["state"] == "completed"
    assert persisted["ledger"]["calculations"][0] == calculation
    assert agent.memory.status("financial", result["scope_id"])["active_turn"] is None


def test_restarted_agent_uses_scoped_memory_for_new_calculation_without_rereading(tmp_path):
    source = write_document(tmp_path / "input.json")
    first = build_agent(tmp_path, FinancialSequenceModel()).run("Revenue growth?", [source], session_id="shared_session")
    assert first["status"] == "completed", first
    followup_model = MemoryFollowupModel()
    restarted = build_agent(tmp_path, followup_model)
    second = restarted.run("Double the ratio we just calculated.", [source], session_id="shared_session")
    assert second["status"] == "completed", second
    assert second["memory"]["completed_turns"] == 1
    assert second["scope_id"] == first["scope_id"]
    assert second["final"]["answer"] == "0.5"
    assert second["final"]["sources"] == first["final"]["sources"]
    tools = [json.loads(line)["name"] for line in (Path(second["run_dir"]) / "tools.jsonl").read_text().splitlines()]
    assert tools == ["finance_calculate"]
    assert restarted.memory.status("shared_session", second["scope_id"])["counts"]["completed"] == 2


class FailingModel:
    model = "scripted-failure"

    def complete(self, messages, *, stage):
        if not any(message["content"].startswith("LOADED PROCEDURAL SKILLS") for message in messages):
            return json.dumps({"action": "skill", "name": "financial-ratios"})
        return json.dumps({"action": "tool", "tool": "finance_calculate", "arguments": {"expression": "missing_input/previous"}})


def test_failed_run_automatically_proposes_real_quarantined_skill(tmp_path):
    source = write_document(tmp_path / "input.json")
    agent = build_agent(tmp_path, FailingModel(), learn=True, max_steps=2)
    reflection_calls = []

    def reflect(messages, stage):
        reflection_calls.append((messages, stage))
        return json.dumps({"name": "learned-check-input-bindings", "description": "Check observed financial inputs before running arithmetic.",
            "triggers": ["missing variable", "failed calculation"],
            "body": "- Read the relevant source records before calculation.\n- Bind every variable to an observed value and preserve its period and unit.\n- If an input is absent, retrieve it or report insufficient evidence.\n- Verify the receipt and cite the original evidence."})

    service = LearningService(tmp_path / "state", agent.registry, reflect)
    agent.learning = service
    initial_fingerprint = agent.registry.fingerprint()
    result = agent.run("What is revenue growth?", [source], split="dev", case_id="failure-development", source_group="failure-source")
    assert result["status"] == "failed"
    assert result["learning"]["status"] == "quarantined", result["learning"]
    candidate = service.candidate(result["learning"]["candidate_id"])
    assert candidate["source_experience_id"] == result["learning"]["experience_id"]
    assert candidate["name"] == "learned-check-input-bindings"
    assert reflection_calls[0][1].startswith("learning.propose")
    assert len(reflection_calls) == 1
    assert agent.registry.fingerprint() == initial_fingerprint
    assert all(skill["name"] != candidate["name"] for skill in agent.registry.catalog())
    assert "missing_input" in json.dumps(reflection_calls[0][0])
    actual_calls = [json.loads(line) for line in (Path(result["run_dir"]) / "tools.jsonl").read_text().splitlines()]
    assert len(actual_calls) == 1
    assert actual_calls[0]["name"] == "finance_calculate"
    assert actual_calls[0]["arguments"]["expression"] == "missing_input/previous"
    assert actual_calls[0]["result"]["success"] is False
    assert actual_calls[0]["result"]["error"]
    assert "skill" not in actual_calls[0]["result"]["error"].lower()


@pytest.mark.parametrize("tool,arguments", [
    ("finance_calculate", {"expression": "1+2"}),
    ("finance_terminal", {"action": "python", "code": "emit(1+2)"}),
])
def test_calculation_and_code_are_blocked_before_skill_load(tmp_path, tool, arguments):
    class PrematureCalculationModel:
        model = "scripted-pre-skill-calculation"

        def complete(self, messages, *, stage):
            return json.dumps({"action": "tool", "tool": tool, "arguments": arguments})

    source = write_document(tmp_path / "input.json")
    agent = build_agent(tmp_path, PrematureCalculationModel(), max_steps=1)
    result = agent.run("What is revenue growth?", [source])
    assert result["status"] == "failed"
    assert result["loaded_skills"] == []
    assert "skill before executing" in result["tool_errors"][0]["error"]
    # The guard must reject before dispatch, so neither calculation nor terminal
    # execution appears in the production tool audit.
    assert not (Path(result["run_dir"]) / "tools.jsonl").exists()


def test_frozen_test_run_rejects_automatic_learning_before_any_model_call(tmp_path):
    source = write_document(tmp_path / "input.json")
    model = FinancialSequenceModel()
    agent = build_agent(tmp_path, model, learn=True)
    with pytest.raises(ValueError, match="disabled"):
        agent.run("Revenue growth?", [source], split="test")
    assert model.messages == []


def test_compacted_context_is_bounded_and_full_observation_can_be_recalled(tmp_path):
    context = ContextWindow(tmp_path / "context", max_chars=16000, observation_chars=3000)
    marker = "important-source-at-the-end"
    original = {"success": True, "text": "source text " * 3500 + marker, "evidence_id": "ev_archived"}
    context.append({"action": "tool", "tool": "finance_read_document"}, original)
    projected = context.messages("Respect evidence provenance.", "Retrieve the source.")
    observation_text = projected[-1]["content"].split("\n", 1)[1]
    projection = json.loads(observation_text)
    assert projection["incomplete"] is True
    assert marker not in projection["excerpt"]
    for index in range(15):
        context.append({"action": "tool", "tool": "finance_read_document", "arguments": {"offset": index}},
                       {"success": True, "text": "recent source " * 250})
    compacted = context.messages("Respect evidence provenance.", "Retrieve the source.", memory={"summary": "prior turn"})
    assert sum(len(message["content"]) for message in compacted) <= 16000
    assert context.compactions > 0
    assert any("event_0000.json" in message["content"] for message in compacted)
    chunks, offset = [], 0
    while True:
        part = context.recall("event_0000.json", offset=offset, limit=4000)
        chunks.append(part["text"])
        if part["next_offset"] is None:
            break
        offset = part["next_offset"]
    restored = json.loads("".join(chunks))
    assert restored["observation"] == original
    with pytest.raises(ValueError):
        context.recall("../result.json")


def test_loaded_skill_body_appears_only_after_explicit_skill_action(tmp_path):
    source = write_document(tmp_path / "input.json")
    registry = SkillRegistry(state_dir=tmp_path / "state")
    skill = registry.load(registry.catalog()[0]["name"])
    delegate = FinancialSequenceModel()

    class SkillAwareModel:
        model = "scripted-skill"
        calls = 0

        def complete(self, messages, *, stage):
            self.calls += 1
            if self.calls == 1:
                assert skill["body"] not in messages[0]["content"]
                return json.dumps({"action": "skill", "name": skill["name"]})
            loaded = [message for message in messages if message["content"].startswith("LOADED PROCEDURAL SKILLS")]
            assert loaded
            assert json.loads(loaded[0]["content"].split("\n", 1)[1])[0]["body"] == skill["body"]
            filtered = [message for message in messages if not message["content"].startswith("TOOL OBSERVATION ")
                        or "structured_content" in json.loads(message["content"].split("\n", 1)[1])]
            return delegate.complete(filtered, stage=stage)

    agent = build_agent(tmp_path, SkillAwareModel())
    result = agent.run("What is revenue growth?", [source])
    assert result["status"] == "completed", result
    assert result["loaded_skills"] == [{"name": skill["name"], "sha256": skill["sha256"]}]
