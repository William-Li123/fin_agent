"""Offline HTTP tests exercise real tools, persistence and paid-call boundaries."""
from __future__ import annotations

import json
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")
from fastapi.testclient import TestClient

from finagent.io import read_json, save_json
from finagent.memory import MemoryStore
from finagent.models import BudgetLedger
from finagent.web import create_app
from finagent.web.demo import DEMO_QUESTIONS, DemoModel

HEADERS = {"X-Finagent-Client": "web"}


def client_for(tmp_path, *, enable_demo=True, **kwargs):
    app = create_app(tmp_path / "state", demo_delay=0, enable_demo=enable_demo, **kwargs)
    app.state.service._docker_cache = (time.monotonic(), {"available": False, "image_ready": False})
    return TestClient(app, base_url="http://127.0.0.1:8000", headers=HEADERS)


def session(client):
    response = client.post("/api/sessions", json={})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def submit_demo(client, identity):
    response = client.post(f"/api/sessions/{identity}/runs", json={
        "question": DEMO_QUESTIONS[0], "mode": "demo", "document_ids": ["demo-company"]})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def finished(client, identity):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        row = client.get(f"/api/runs/{identity}").json()
        if row["status"] not in {"queued", "running"}:
            return row
        time.sleep(0.01)
    pytest.fail("Offline run did not finish")


def test_demo_real_tools_artifacts_memory_and_restart_persist(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with client_for(tmp_path) as client:
        assert client.get("/api/status").json()["budget"]["initialized"] is False
        docs = client.get("/api/documents").json()
        assert docs[0]["synthetic"] is True
        assert client.get("/api/documents/demo-company").json()["preview"]["table"][1] == ["Revenue", "1200", "1440"]
        identity = session(client)
        first = finished(client, submit_demo(client, identity))
        assert first["status"] == "completed", first
        assert first["final"]["answer"] == "20"
        assert first["final"]["unit"] == "percent"
        assert first["final"]["calculation_id"].startswith("calc_")
        assert len(first["final"]["sources"]) == 2
        assert [item["text"] for item in first["source_evidence"]] == ["1200", "1440"]
        assert first["steps"] == 6
        assert first["cost_cny"] == "0"
        assert {item["kind"] for item in first["artifacts"]} == {"chart", "report"}
        for artifact in first["artifacts"]:
            response = client.get(artifact["url"])
            assert response.status_code == 200
            assert "attachment" in response.headers["content-disposition"]
            assert "sandbox" in response.headers["content-security-policy"]
            assert "svg" in response.text if artifact["kind"] == "chart" else "合成演示" in response.text
        assert {event["type"] for event in first["events"]} >= {"model", "skill", "tool", "status"}
        assert client.get("/api/learning").json() == {"experiences": [], "candidates": [], "reports": []}
        assert len(client.get("/api/skills").json()) == 6
        assert client.get("/api/skills/financial-ratios").json()["body"]
        run_path = tmp_path / "state" / "web" / "runs" / first["id"]
        persisted = read_json(run_path / "result.json")
        assert persisted["status"] == "completed"
        trace = read_json(run_path / "trace.json")
        assert [item["action"].get("tool") for item in trace if item["action"]["action"] == "tool"] == [
            "finance_read_document", "finance_calculate", "finance_render_chart", "finance_write_report"]
    assert not (tmp_path / "state" / "budget.sqlite3").exists()
    with client_for(tmp_path) as restarted:
        history = restarted.get(f"/api/sessions/{identity}").json()
        assert history["runs"][0]["id"] == first["id"]
        second = finished(restarted, submit_demo(restarted, identity))
        assert second["status"] == "completed", second
        assert second["memory"]["completed_turns"] == 1


def test_write_csrf_origin_and_host_boundaries(tmp_path):
    with client_for(tmp_path) as client:
        for overrides in ({"Origin": "https://evil.example"}, {"Host": "evil.example:8000"},
                          {"Origin": "null"}, {"Host": "127.0.0.1:9999"}, {"X-Finagent-Client": ""}):
            response = client.post("/api/sessions", json={}, headers=overrides)
            assert response.status_code == 403
        assert client.get("/api/status", headers={"Host": "evil.example:8000"}).status_code == 403
        assert client.post("/api/sessions", json={}, headers={"Origin": "http://localhost:5173"}).status_code == 201
        assert client.options("/api/sessions", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"}).status_code == 200


def test_upload_annotation_rejection_scope_and_honest_demo(tmp_path):
    with client_for(tmp_path) as client:
        invalid = {"id": "company", "question": "growth", "pre_text": [], "post_text": [], "table": [], "qa": {"gold": 20}}
        assert client.post("/api/documents", files={"file": ("annotation.json", json.dumps(invalid), "application/json")}).status_code == 422
        assert client.post("/api/documents", files={"file": ("run.exe", b"not executable", "application/octet-stream")}).status_code == 422
        response = client.post("/api/documents", files={"file": ("../../source.csv", b"Metric,2024,2025\nRevenue,100,200", "text/csv")})
        assert response.status_code == 201, response.text
        uploaded = response.json()
        assert uploaded["name"] == "source.csv" and uploaded["synthetic"] is False
        assert "_paths" not in uploaded
        assert client.get(f"/api/documents/{uploaded['id']}").json()["preview"]["table"][1] == ["Revenue", "100", "200"]
        identity = session(client)
        assert client.post(f"/api/sessions/{identity}/runs", json={"question": DEMO_QUESTIONS[0], "mode": "demo", "document_ids": [uploaded["id"]]}).status_code == 422
        assert client.post(f"/api/sessions/{identity}/runs", json={"question": "What is Apple's stock price?", "mode": "demo", "document_ids": ["demo-company"]}).status_code == 422
        assert client.patch(f"/api/sessions/{identity}", json={"document_ids": ["missing"]}).status_code == 404
        assert client.patch(f"/api/sessions/{identity}", json={"document_ids": ["demo-company", "demo-company"]}).status_code == 422
        assert not (tmp_path / "source.csv").exists()


def test_missing_key_or_budget_rejects_without_initializing_ledger(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with client_for(tmp_path) as client:
        identity = session(client)
        request = {"question": "Compute revenue growth", "mode": "live", "document_ids": ["demo-company"]}
        assert client.post(f"/api/sessions/{identity}/runs", json=request).status_code == 409
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-offline-example-credential-only")
        assert client.post(f"/api/sessions/{identity}/runs", json=request).status_code == 409
        status = client.get("/api/status")
        assert status.json()["api_ready"] is True
        assert "sk-offline" not in status.text
    assert not (tmp_path / "state" / "budget.sqlite3").exists()


def test_cooperative_cancel_waits_for_model_and_freezes_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-offline-example-credential-only")
    state = tmp_path / "state"
    ledger = BudgetLedger(state / "budget.sqlite3", ceiling_cny=10, carried_cny=0)
    entered, release = threading.Event(), threading.Event()
    class BlockingModel(DemoModel):
        model = "offline-paid-boundary-test"
        def __init__(self, ledger):
            super().__init__(delay=0)
            self.ledger, self.calls = ledger, 0
        def complete(self, messages, *, stage):
            self.calls += 1
            reservation = self.ledger.reserve(100, 100, stage=stage, model=self.model)
            entered.set()
            assert release.wait(5)
            self.last_usage = self.ledger.settle(reservation.call_id, {"prompt_tokens": 20, "completion_tokens": 20})
            return super().complete(messages, stage=stage)
    delegates = []
    def factory(ledger):
        model = BlockingModel(ledger)
        delegates.append(model)
        return model
    with client_for(tmp_path, model_factory=factory) as client:
        identity = session(client)
        response = client.post(f"/api/sessions/{identity}/runs", json={"question": "Revenue growth?", "mode": "live", "document_ids": ["demo-company"]})
        assert response.status_code == 202
        run_id = response.json()["id"]
        assert entered.wait(3)
        assert client.post(f"/api/sessions/{identity}/runs", json={"question": DEMO_QUESTIONS[0], "mode": "demo", "document_ids": ["demo-company"]}).status_code == 409
        assert client.patch(f"/api/sessions/{identity}", json={"title": "changed"}).status_code == 409
        other = session(client)
        assert client.post(f"/api/sessions/{other}/runs", json={"question": "second", "mode": "live", "document_ids": ["demo-company"]}).status_code == 409
        cancelled = client.post(f"/api/runs/{run_id}/cancel").json()
        assert cancelled["status"] == "running" and cancelled["cancel_requested"] is True
        assert ledger.status()["call_states"] == {"pending": 1}
        release.set()
        result = finished(client, run_id)
        assert result["status"] == "cancelled", result
        assert "已停止" in result["error"]
        assert result["cost_cny"] == "0.0002"
        assert delegates[0].calls == 1
        assert ledger.status()["call_states"] == {"settled": 1}
        assert ledger.status()["halted"] is False
        assert "sk-offline" not in json.dumps(result)
        assert str(state) not in json.dumps(result)


def test_artifact_tamper_unknown_path_and_static_fallback(tmp_path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html>financial workspace</html>", encoding="utf-8")
    (frontend / "asset.js").write_text("console.log('asset')", encoding="utf-8")
    with client_for(tmp_path, frontend_dir=frontend) as client:
        assert client.get("/").status_code == 200
        assert client.get("/session/some-id").status_code == 200
        assert client.get("/missing.js").status_code == 404
        assert client.get("/api/missing").status_code == 404
        assert client.get("/asset.js").status_code == 200
        identity = session(client)
        result = finished(client, submit_demo(client, identity))
        assert client.get(f"/api/runs/{result['id']}/artifacts/unknown").status_code == 404
        artifact = result["artifacts"][0]
        path = tmp_path / "state" / "web" / "runs" / result["id"] / artifact["name"]
        path.write_text("tampered", encoding="utf-8")
        assert client.get(artifact["url"]).status_code == 409


def test_upload_limit_rejects_and_cleans_partial_file(tmp_path):
    with client_for(tmp_path) as client:
        response = client.post("/api/documents", files={"file": ("large.txt", b"x" * 25_000_001, "text/plain")})
        assert response.status_code == 413
        documents = client.get("/api/documents").json()
        assert [item["id"] for item in documents] == ["demo-company"]
        assert len(list((tmp_path / "state" / "web" / "documents").iterdir())) == 1


def test_abandoned_web_turn_recovery_preserves_pending_accounting(tmp_path):
    with client_for(tmp_path) as client:
        identity = session(client)
        row = finished(client, submit_demo(client, identity))
        directory = tmp_path / "state" / "web" / "runs" / row["id"]
        request = read_json(directory / "request.json")
        memory = MemoryStore(tmp_path / "state" / "memory.sqlite3")
        turn = memory.begin_turn(identity, request["scope_id"], "abandoned task")
        interrupted = {**row, "id": "web_run_interrupted", "status": "running", "_owner_pid": None}
        client.app.state.service.store.put("run", interrupted)
        save_json(tmp_path / "state" / "web" / "runs" / interrupted["id"] / "request.json", request)
        ledger = BudgetLedger(tmp_path / "state" / "budget.sqlite3", ceiling_cny=10, carried_cny=0)
        reservation = ledger.reserve(100, 100, stage="web_run_interrupted/step-1", model="offline-test")
    before = ledger.status()
    with client_for(tmp_path) as restarted:
        interrupted = restarted.get("/api/runs/web_run_interrupted").json()
        assert interrupted["status"] == "failed" and "重启" in interrupted["error"]
        assert memory.status(identity, request["scope_id"])["active_turn"] is None
        assert memory.export(identity, request["scope_id"])["turns"][-1]["turn_id"] == turn
        assert memory.export(identity, request["scope_id"])["turns"][-1]["state"] == "failed"
        assert ledger.status() == before
        assert ledger.calls()[0]["call_id"] == reservation.call_id
        assert ledger.calls()[0]["status"] == "pending"


def test_startup_does_not_reclassify_another_live_server_task(tmp_path):
    import os
    with client_for(tmp_path) as client:
        client.app.state.service.store.put("run", {"id": "web_run_other", "status": "running",
            "_owner_pid": os.getpid(), "session_id": "web_session_other", "created_at": "now"})
        with pytest.raises(RuntimeError, match="已有服务器任务运行"):
            create_app(tmp_path / "state")
        assert client.app.state.service.store.get("run", "web_run_other")["status"] == "running"


def test_production_default_has_no_fixture_and_requires_own_documents(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app = create_app(tmp_path / "state")
    app.state.service._docker_cache = (time.monotonic(), {"available": False, "image_ready": False})
    with TestClient(app, base_url="http://127.0.0.1:8000", headers=HEADERS) as client:
        status = client.get("/api/status").json()
        assert status["default_mode"] == "live"
        assert "demo_supported_questions" not in status
        assert client.get("/api/documents").json() == []
        identity = session(client)
        assert client.get(f"/api/sessions/{identity}").json()["document_ids"] == []
        assert client.post(f"/api/sessions/{identity}/runs", json={"question": "分析营收"}).status_code == 422
        assert client.post(f"/api/sessions/{identity}/runs", json={
            "question": DEMO_QUESTIONS[0], "mode": "demo", "document_ids": ["demo-company"]}).status_code == 422
        response = client.post("/api/documents", files={"file": ("my-report.csv", b"Metric,2024,2025\nRevenue,1000,1500", "text/csv")})
        assert response.status_code == 201
        uploaded = response.json()
        assert client.patch(f"/api/sessions/{identity}", json={"document_ids": [uploaded["id"]]}).status_code == 200
        # With an uploaded scope and omitted mode the request is live, therefore
        # it checks real API configuration instead of returning a canned answer.
        response = client.post(f"/api/sessions/{identity}/runs", json={"question": "分析我的 CSV 营收增长"})
        assert response.status_code == 409 and "DEEPSEEK_API_KEY" in response.text
        assert client.patch(f"/api/sessions/{identity}", json={"document_ids": []}).status_code == 200
    assert not (tmp_path / "state" / "web" / "documents" / "demo-company").exists()
    assert not (tmp_path / "state" / "budget.sqlite3").exists()


def test_production_migrates_selected_demo_scope_but_preserves_history(tmp_path):
    with client_for(tmp_path, enable_demo=True) as client:
        identity = session(client)
        historical = finished(client, submit_demo(client, identity))
        response = client.post("/api/documents", files={"file": ("annual.csv", b"Metric,2024,2025\nRevenue,300,450", "text/csv")})
        uploaded = response.json()
        assert client.patch(f"/api/sessions/{identity}", json={"document_ids": ["demo-company", uploaded["id"]]}).status_code == 200
        directory = tmp_path / "state" / "web" / "runs" / historical["id"]
        snapshot = {path.name: path.read_bytes() for path in (directory / "input_documents").glob("*.json")}
    with client_for(tmp_path, enable_demo=False) as production:
        docs = production.get("/api/documents").json()
        assert [doc["id"] for doc in docs] == [uploaded["id"]]
        assert all(not doc["synthetic"] for doc in docs)
        assert production.app.state.service.store.get("document", "demo-company") is None
        assert production.get("/api/documents/demo-company").status_code == 404
        detail = production.get(f"/api/sessions/{identity}").json()
        assert detail["document_ids"] == [uploaded["id"]]
        assert detail["runs"][0]["final"] == historical["final"]
        assert detail["runs"][0]["source_evidence"] == historical["source_evidence"]
        for artifact in historical["artifacts"]:
            assert production.get(artifact["url"]).status_code == 200
        assert {path.name: path.read_bytes() for path in (directory / "input_documents").glob("*.json")} == snapshot
        new_id = session(production)
        assert production.get(f"/api/sessions/{new_id}").json()["document_ids"] == []
        assert production.post(f"/api/sessions/{new_id}/runs", json={
            "question": DEMO_QUESTIONS[0], "mode": "demo", "document_ids": ["demo-company"]}).status_code == 422


def test_production_uploaded_csv_runs_shared_agent_with_offline_injected_model(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-offline-example-credential-only")
    BudgetLedger(tmp_path / "state" / "budget.sqlite3", ceiling_cny=10, carried_cny=0)
    class CSVModel:
        model = "offline-csv-tools-test"
        def __init__(self, ledger):
            self.ledger = ledger
        def complete(self, messages, *, stage):
            if not any(item["content"].startswith("LOADED PROCEDURAL SKILLS") for item in messages):
                return json.dumps({"action": "skill", "name": "financial-ratios"})
            observations = [json.loads(item["content"].split("\n", 1)[1]) for item in messages
                            if item["content"].startswith("TOOL OBSERVATION ")]
            records = [item["structured_content"] for item in observations if "structured_content" in item]
            if not records:
                action = {"action": "tool", "tool": "finance_read_document", "arguments": {}}
            elif "evidence" in records[-1]:
                values = {item["column"]: item for item in records[-1]["evidence"] if item.get("row") == 1 and item.get("column") in {1, 2}}
                action = {"action": "tool", "tool": "finance_calculate", "arguments": {
                    "expression": "(current-previous)/previous*100", "variables": {
                        "previous": values[1]["text"], "current": values[2]["text"]},
                    "evidence_ids": [values[1]["evidence_id"], values[2]["evidence_id"]]}}
            else:
                calculation = records[-1]
                action = {"action": "finish", "final": {"status": "answered", "answer": calculation["value"],
                    "unit": "percent", "explanation": "Growth calculated from the user's uploaded CSV.",
                    "sources": calculation["evidence_ids"], "calculation_id": calculation["calculation_id"]}}
            return json.dumps(action)
    with client_for(tmp_path, enable_demo=False, model_factory=CSVModel) as client:
        uploaded = client.post("/api/documents", files={"file": (
            "my-revenue.csv", b"Metric,2024,2025\nRevenue,1000,1500", "text/csv")}).json()
        identity = session(client)
        response = client.post(f"/api/sessions/{identity}/runs", json={
            "question": "Compute my CSV revenue growth", "document_ids": [uploaded["id"]]})
        assert response.status_code == 202
        result = finished(client, response.json()["id"])
        assert result["mode"] == "live" and result["status"] == "completed", result
        assert result["final"]["answer"] == "50"
        assert {item["text"] for item in result["source_evidence"]} == {"1000", "1500"}
        assert client.get("/api/documents").json()[0]["synthetic"] is False
        assert BudgetLedger.open(tmp_path / "state" / "budget.sqlite3").calls() == []


def test_csv_source_urls_survive_preview_and_evidence_redaction(tmp_path):
    source_url = "https://www.apple.com/newsroom/2025/01/apple-reports-first-quarter-results/"
    http_url = "http://example.com/annual-report.csv"
    windows_path = "C:\\host\\private\\annual.csv"
    csv = f"Metric,Value\nSourceURL,{source_url}\nArchiveURL,{http_url}\nLocalFile,{windows_path}\nPosixStyleDrive,C:/host/private/annual.csv"
    with client_for(tmp_path, enable_demo=False) as client:
        response = client.post("/api/documents", files={"file": ("source-financials.csv", csv.encode(), "text/csv")})
        assert response.status_code == 201
        identity = response.json()["id"]
        preview = client.get(f"/api/documents/{identity}").json()["preview"]["table"]
        assert preview[1] == ["SourceURL", source_url]
        assert preview[2] == ["ArchiveURL", http_url]
        assert preview[3] == ["LocalFile", "[local path]"]
        assert preview[4] == ["PosixStyleDrive", "[local path]"]
        service = client.app.state.service
        paths = service.document_paths([identity])
        from finagent.tools.store import FinanceStore
        store = FinanceStore(paths[0].parent, tmp_path / "evidence-output")
        observed = service.clean(store.evidence_for_document())
        assert any(record["text"] == source_url for record in observed)
        assert any(record["text"] == http_url for record in observed)
        assert windows_path not in json.dumps(observed)
        assert "C:/host/private/annual.csv" not in json.dumps(observed)


def test_rejected_validation_json_names_failed_file_and_preserves_uploaded_csv(tmp_path):
    with client_for(tmp_path, enable_demo=False) as client:
        response = client.post("/api/documents", files={"file": (
            "apple_fy2024_fy2025.csv", b"Metric,FY2024,FY2025\nRevenue,391035,416161", "text/csv")})
        assert response.status_code == 201
        uploaded = response.json()
        response = client.post("/api/documents", files={"file": (
            "apple_sources_and_validation.json", json.dumps({"company": "Apple Inc.", "sources": [], "validation": {}}), "application/json")})
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "apple_sources_and_validation.json" in detail
        assert "JSON 格式不符合财务文档要求" in detail
        assert "来源说明和校验记录无需作为分析资料上传" in detail
        assert "已上传的文件仍可使用" in detail
        assert "FinQA" not in detail
        assert client.get("/api/documents").json() == [uploaded]
        assert client.get(f"/api/documents/{uploaded['id']}").json()["preview"]["table"][1] == ["Revenue", "391035", "416161"]
        directory = tmp_path / "state" / "web" / "documents"
        assert len(list(directory.iterdir())) == 1
        assert (directory / uploaded["id"] / "upload.csv").is_file()
        assert not (tmp_path / "state" / "budget.sqlite3").exists()
