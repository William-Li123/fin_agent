"""Loopback HTTP boundary for the existing evidence-grounded research agent.

The browser receives catalogue IDs, never host paths or API credentials. Starting
a live run is the only route that can construct a paid model client; it reopens
the existing immutable budget authorization and never initializes one.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from finagent.core.agent import AgentConfig, ResearchAgent
from finagent.ingestion.documents import ingest
from finagent.io import read_json, redact, save_json
from finagent.learning import LearningService
from finagent.memory import MemoryStore
from finagent.models import BudgetLedger, ModelClient
from finagent.skills import SkillRegistry
from finagent.tools.store import FinanceStore

from .demo import DEMO_DOCUMENT, DEMO_QUESTIONS, ENGLISH_QUESTION, DemoModel
from .store import WebStore, now

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
ACTIVE = {"queued", "running"}
FORMATS = [".json", ".csv", ".txt", ".md", ".pdf", ".xlsx"]


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=120)


class SessionPatch(SessionInput):
    document_ids: list[str] | None = Field(default=None, max_length=20)


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=16000)
    mode: str = "live"
    document_ids: list[str] = Field(default_factory=list, max_length=20)
    learn_on_failure: bool = False


class RunCancelled(RuntimeError):
    pass


def _process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        # os.kill(pid, 0) terminates processes on Windows; query a handle instead.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is conservative.
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class _ObservedModel:
    def __init__(self, service, identity, delegate, cancel):
        self.service, self.identity, self.delegate, self.cancel = service, identity, delegate, cancel
        self.model = delegate.model
        self.cost = Decimal(0)

    @property
    def last_call_id(self):
        return getattr(self.delegate, "last_call_id", None)

    @property
    def last_usage(self):
        return getattr(self.delegate, "last_usage", None)

    def complete(self, messages, *, stage):
        if self.cancel.is_set():
            raise RunCancelled("已停止；不会发起新的模型请求")
        self.service.harvest(self.identity)
        is_learning = stage.startswith("learning.")
        self.service.event(self.identity, "model", "提炼失败经验" if is_learning else "模型选择下一步", {
            "stage": stage, "model": self.model,
            "scripted_demo": self.model == "scripted-demo-no-api"})
        # Never interrupt an in-flight billed request: ModelClient settles its
        # usage synchronously before cancellation can be raised here.
        response = self.delegate.complete(messages, stage=stage)
        usage = self.last_usage
        if isinstance(usage, dict):
            amount = usage.get("known_cny", usage.get("cost_cny"))
            if amount is not None:
                self.cost += Decimal(str(amount))
        if self.cancel.is_set():
            raise RunCancelled("当前模型请求已完成计费，已停止后续步骤")
        return response


class WebService:
    def __init__(self, state_dir: Path, *, model_factory=None, demo_delay=0.12, enable_demo=False):
        self.state = Path(state_dir).resolve()
        self.root = self.state / "web"
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = WebStore(self.root / "catalogue.sqlite3")
        self.registry = SkillRegistry(state_dir=self.state)
        self.model_factory = model_factory
        self.demo_delay = demo_delay
        self.enable_demo = enable_demo
        self.mutex = threading.RLock()
        self.cancel_flags: dict[str, threading.Event] = {}
        self.workers: dict[str, threading.Thread] = {}
        self._docker_cache = (0.0, {"available": False, "image_ready": False})
        for run in self.store.all("run"):
            if run["status"] in ACTIVE:
                if _process_alive(run.get("_owner_pid")):
                    raise RuntimeError("同一状态目录已有服务器任务运行，请等待完成后再启动")
                request_path = self.root / "runs" / run["id"] / "request.json"
                if request_path.is_file() and (self.state / "memory.sqlite3").is_file():
                    request = read_json(request_path)
                    memory = MemoryStore(self.state / "memory.sqlite3")
                    scope = request.get("scope_id")
                    if scope and request.get("session_id") == run["session_id"]:
                        lease = memory.status(run["session_id"], scope).get("active_turn")
                        if lease:
                            memory.fail_turn(lease, "Web server interrupted before completion")
                run.update(status="failed", error="服务重启打断了任务；历史记录已保留，付费账本未重置")
                self.store.put("run", run)
        if self.enable_demo:
            fixture = self.root / "documents" / "demo-company" / "source.json"
            if not fixture.exists():
                save_json(fixture, DEMO_DOCUMENT)
            if not self.store.get("document", "demo-company"):
                self.store.put("document", {"id": "demo-company", "name": "演示公司 · 两期财务数据.json",
                    "kind": "json", "bytes": fixture.stat().st_size, "created_at": now(), "synthetic": True,
                    "document_count": 1, "_paths": [str(fixture)]})
        else:
            # Migrate the selectable catalogue and current scope only. Previous
            # runs keep their snapshots, evidence receipts and downloadable files.
            removed = {"demo-company"} | {doc["id"] for doc in self.store.all("document") if doc.get("synthetic")}
            for session in self.store.all("session"):
                selected = session.get("document_ids", [])
                retained = [identity for identity in selected if identity not in removed]
                if retained != selected:
                    session.update(document_ids=retained, updated_at=now())
                    self.store.put("session", session)
            for identity in removed:
                self.store.delete("document", identity)

    def clean(self, value):
        if isinstance(value, dict):
            return {key: self.clean(item) for key, item in value.items()
                    if not key.startswith("_") and key.lower() not in {"api_key", "authorization", "password", "secret", "path", "run_dir", "input_path"}}
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            text = redact(value)
            configured = os.environ.get("DEEPSEEK_API_KEY")
            if configured:
                text = text.replace(configured, "[REDACTED_KEY]")
            text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", text)
            text = text.replace(str(self.state), "[local state]")
            text = re.sub(r"(?<![A-Za-z0-9+.-])[A-Za-z]:[\\/][^\s\"'<>]+", "[local path]", text)
            return text
        return value

    def require(self, kind, identity):
        value = self.store.get(kind, identity)
        if value is None:
            raise HTTPException(404, "记录不存在")
        return value

    def budget(self):
        path = self.state / "budget.sqlite3"
        if not path.is_file():
            return {"initialized": False, "ceiling_cny": None, "spent_cny": None, "remaining_cny": None, "frozen": False}
        try:
            data = BudgetLedger.open(path).status()
            return {"initialized": True, "ceiling_cny": data["ceiling_cny"], "spent_cny": data["all_committed_cny"],
                    "remaining_cny": data["remaining_cny"], "frozen": data["halted"], "estimated": True}
        except (ValueError, OSError, RuntimeError):
            return {"initialized": True, "ceiling_cny": None, "spent_cny": None, "remaining_cny": None, "frozen": True}

    def docker(self):
        if time.monotonic() - self._docker_cache[0] < 15:
            return self._docker_cache[1]
        status = {"available": False, "image_ready": False}
        executable = shutil.which("docker")
        if executable:
            try:
                for key, args in (("available", ["info", "--format", "{{.ServerVersion}}"]),
                                  ("image_ready", ["image", "inspect", "finance-agent-terminal:local", "--format", "{{.Id}}"] )):
                    status[key] = subprocess.run([executable, *args], capture_output=True, timeout=4, check=False).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._docker_cache = (time.monotonic(), status)
        return status

    def session_detail(self, identity):
        session = self.require("session", identity)
        session["runs"] = [self.run_detail(run["id"]) for run in self.store.all("run") if run["session_id"] == identity]
        return self.clean(session)

    def run_detail(self, identity):
        run = self.require("run", identity)
        if run["status"] in ACTIVE:
            self.harvest(identity)
            run = self.require("run", identity)
            run["elapsed_seconds"] = round(max(0, time.time() - run["_started_epoch"]), 2)
        return self.clean(run)

    def event(self, identity, kind, title, detail=None, *, success=None, event_id=None):
        event = {"id": event_id or "event_" + uuid4().hex, "type": kind, "title": title, "timestamp": now()}
        if detail is not None:
            text = detail if isinstance(detail, str) else json.dumps(self.clean(detail), ensure_ascii=False, indent=2)
            event["detail"] = self.clean(text)[:12000]
        if success is not None:
            event["success"] = success
        def add(run):
            if not any(item["id"] == event["id"] for item in run["events"]):
                run["events"].append(event)
        self.store.update("run", identity, add)

    def harvest(self, identity):
        directory = self.root / "runs" / identity
        for path in sorted((directory / "context").glob("event_*.json")):
            try:
                record = read_json(path)
            except (OSError, ValueError):
                continue
            action, observation = record["action"], record["observation"]
            kind = "skill" if action.get("action") == "skill" else "tool"
            success = observation.get("success", False)
            if not success:
                kind = "error"
            title = action.get("name", "加载技能") if action.get("action") == "skill" else action.get("tool", "校验返回内容")
            self.event(identity, kind, title, {"arguments": action.get("arguments", {}), "observation": observation},
                       success=success, event_id="observed_" + path.stem)
        steps = len(list((directory / "model").glob("step_*.json")))
        if steps:
            self.store.update("run", identity, lambda run: run.update(steps=steps))

    def document_paths(self, identities):
        if not identities or len(set(identities)) != len(identities):
            raise HTTPException(422, "请选择至少一份且不重复的资料")
        paths = []
        for identity in identities:
            document = self.require("document", identity)
            for source in document["_paths"]:
                path = Path(source).resolve()
                if not path.is_relative_to(self.root / "documents") or not path.is_file():
                    raise HTTPException(409, "资料文件已失效，请重新导入")
                paths.append(path)
        return paths

    def submit(self, session_id, payload):
        question = payload.question.strip()
        if not question:
            raise HTTPException(422, "请输入分析任务")
        if payload.mode not in {"demo", "live"}:
            raise HTTPException(422, "模式只能为 demo 或 live")
        if payload.mode == "demo" and not self.enable_demo:
            raise HTTPException(422, "当前服务仅支持真实 API 分析")
        with self.mutex:
            session = self.require("session", session_id)
            active = [run for run in self.store.all("run") if run["status"] in ACTIVE]
            if any(run["session_id"] == session_id for run in active):
                raise HTTPException(409, "当前会话已有正在运行的任务")
            if payload.mode == "live" and any(run["mode"] == "live" for run in active):
                raise HTTPException(409, "已有真实 API 任务运行，请等待完成")
            identities = payload.document_ids or session["document_ids"]
            paths = self.document_paths(identities)
            if payload.mode == "demo":
                if identities != ["demo-company"]:
                    raise HTTPException(422, "演示模式只分析内置合成资料；上传文件请切换真实 API 模式")
                if question not in [*DEMO_QUESTIONS, ENGLISH_QUESTION]:
                    raise HTTPException(422, "演示模式仅支持预设营收分析任务；自定义问题请使用真实 API 模式")
                if payload.learn_on_failure:
                    raise HTTPException(422, "演示模式不生成模型提议的学习候选")
            else:
                if not os.environ.get("DEEPSEEK_API_KEY"):
                    raise HTTPException(409, "服务器尚未配置 DEEPSEEK_API_KEY")
                budget = self.budget()
                if not budget["initialized"]:
                    raise HTTPException(409, "尚无持久化预算，请先通过 finagent budget init 初始化授权")
                if budget["frozen"]:
                    raise HTTPException(409, "预算账本已冻结，请通过 CLI 检查账务")
            identity = "web_run_" + uuid4().hex
            run = {"id": identity, "session_id": session_id, "question": question, "mode": payload.mode,
                "status": "queued", "created_at": now(), "events": [], "final": None, "artifacts": [],
                "steps": 0, "elapsed_seconds": 0, "cost_cny": "0" if payload.mode == "demo" else None,
                "cancel_requested": False, "_started_epoch": time.time(), "_owner_pid": os.getpid()}
            try:
                self.store.insert_run(run)
            except ValueError as error:
                raise HTTPException(409, str(error)) from None
            session.update(document_ids=identities, updated_at=now())
            if session["title"] == "新分析":
                session["title"] = question[:38]
            self.store.put("session", session)
            cancel = threading.Event()
            self.cancel_flags[identity] = cancel
            worker = threading.Thread(target=self.execute, args=(identity, paths, payload.learn_on_failure, cancel), daemon=True)
            self.workers[identity] = worker
            worker.start()
            return self.clean(run)

    def execute(self, identity, paths, learn, cancel):
        directory = self.root / "runs" / identity
        record = self.require("run", identity)
        watcher_stop = threading.Event()
        def watch():
            while not watcher_stop.wait(0.15):
                self.harvest(identity)
        watcher = threading.Thread(target=watch, daemon=True)
        observed = None
        try:
            self.store.update("run", identity, lambda run: run.update(status="running"))
            self.event(identity, "status", "演示任务开始" if record["mode"] == "demo" else "真实分析开始",
                       "使用合成数据与脚本决策；工具、来源核验和记忆均真实执行。" if record["mode"] == "demo" else "使用服务器环境变量中的凭据与既有预算账本。")
            if record["mode"] == "demo":
                delegate = DemoModel(self.demo_delay)
            else:
                ledger = BudgetLedger.open(self.state / "budget.sqlite3")
                delegate = self.model_factory(ledger) if self.model_factory else ModelClient(ledger,
                    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                    model=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
            observed = _ObservedModel(self, identity, delegate, cancel)
            learning = LearningService(self.state, self.registry, lambda messages, stage: observed.complete(messages, stage=stage)) if learn else None
            agent = ResearchAgent(observed, self.registry, self.state,
                config=AgentConfig(terminal_backend="docker", learn_on_failure=learn), learning=learning)
            watcher.start()
            result = agent.run(record["question"], paths, session_id=record["session_id"], run_dir=directory,
                               source_group="web-interactive-" + record["session_id"])
            self.harvest(identity)
            status = "completed" if result["status"] == "completed" else "cancelled" if cancel.is_set() else "failed"
            artifact_rows = []
            artifact_paths = {}
            for artifact in result.get("artifacts", []):
                path = Path(artifact["path"]).resolve()
                if not path.is_relative_to(directory.resolve()) or not path.is_file():
                    continue
                artifact_id = artifact["artifact_id"]
                artifact_paths[artifact_id] = {"path": str(path), "sha256": artifact["sha256"]}
                artifact_rows.append({"id": artifact_id, "name": artifact["filename"], "kind": artifact["kind"],
                    "url": f"/api/runs/{identity}/artifacts/{artifact_id}"})
            cost = "0"
            if record["mode"] == "live":
                # Includes proposal calls; known vs retained unknown amounts
                # remain separately visible in the shared ledger status.
                ids = {item["id"] for item in self.require("run", identity)["events"] if item["type"] == "model"}
                calls = delegate.ledger.calls()
                model_stages = {json.loads(item["detail"])["stage"] for item in self.require("run", identity)["events"]
                                if item["id"] in ids}
                cost = str(sum((Decimal(call["known_cny"] or call["reserved_cny"]) for call in calls if call["stage"] in model_stages), Decimal(0)))
            source_evidence = []
            if (result.get("final") or {}).get("sources"):
                store = FinanceStore(directory / "input_documents", directory)
                source_evidence = store.get_evidence(result["final"]["sources"])
            self.event(identity, "status" if status in {"completed", "cancelled"} else "error",
                       {"completed": "分析完成", "cancelled": "任务已停止", "failed": "分析未完成"}[status],
                       result.get("error"), success=status == "completed")
            self.store.update("run", identity, lambda run: run.update(status=status, final=self.clean(result.get("final")),
                error=self.clean(result.get("error")) if result.get("error") else None,
                steps=result.get("steps", 0), elapsed_seconds=result.get("elapsed_seconds", 0),
                artifacts=artifact_rows, _artifact_paths=artifact_paths, cost_cny=cost,
                source_evidence=self.clean(source_evidence), memory=self.clean(result.get("memory", {})),
                learning=self.clean(result.get("learning"))))
        except Exception as error:  # noqa: BLE001 - Persist sanitized failures at the worker boundary.
            diagnostic = self.clean(str(error))
            self.store.update("run", identity, lambda run: run.update(status="cancelled" if cancel.is_set() else "failed",
                              error=diagnostic, elapsed_seconds=round(time.time() - run["_started_epoch"], 3)))
            self.event(identity, "error", "任务未完成", diagnostic, success=False)
        finally:
            watcher_stop.set()
            if watcher.ident is not None:
                watcher.join(timeout=2)
            self.cancel_flags.pop(identity, None)
            with self.mutex:
                self.workers.pop(identity, None)

    def shutdown(self):
        for flag in list(self.cancel_flags.values()):
            flag.set()
        # A request being billed remains visible and accounted until it returns;
        # a hard process death is handled conservatively by BudgetLedger.
        for worker in list(self.workers.values()):
            worker.join(timeout=0.5)


def create_app(state_dir: Path, frontend_dir: Path | None = None, *, allowed_ports=None,
               model_factory=None, demo_delay=0.12, enable_demo=False):
    """Build a real-analysis application; ``enable_demo`` is an offline test hook."""
    service = WebService(state_dir, model_factory=model_factory, demo_delay=demo_delay, enable_demo=enable_demo)
    ports = set(allowed_ports or {8000, 5173})
    if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
        raise ValueError("Allowed ports must be valid local TCP ports")
    origins = [f"http://{host}:{port}" for host in ("127.0.0.1", "localhost", "[::1]") for port in ports]
    @asynccontextmanager
    async def lifespan(app):
        yield
        service.shutdown()
    app = FastAPI(title="fin_agent local workspace", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PATCH"],
                       allow_headers=["Content-Type", "X-Finagent-Client"], allow_credentials=False)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        try:
            host = urlsplit("http://" + request.headers.get("host", ""))
            host_ok = host.hostname in LOOPBACK and host.port in ports and not host.username and not host.password
        except ValueError:
            host_ok = False
        if not host_ok:
            return JSONResponse({"detail": "仅允许本机地址访问"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            return JSONResponse({"detail": "来源不受信任"}, status_code=403)
        if request.method in {"POST", "PATCH", "PUT", "DELETE"} and request.headers.get("X-Finagent-Client") != "web":
            return JSONResponse({"detail": "写入请求缺少本机客户端标识"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(Exception)
    async def safe_error(request, error):
        return JSONResponse({"detail": service.clean(str(error))[:1000] or "本机服务出错"}, status_code=500)

    @app.get("/api/status")
    def status():
        value = {"model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
                "api_ready": bool(os.environ.get("DEEPSEEK_API_KEY")), "budget": service.budget(),
                "docker": service.docker(), "default_mode": "live", "document_formats": FORMATS}
        if service.enable_demo:
            value["demo_supported_questions"] = DEMO_QUESTIONS
        return value

    @app.get("/api/sessions")
    def sessions():
        return service.clean(service.store.all("session"))

    @app.post("/api/sessions", status_code=201)
    def create_session(payload: SessionInput):
        value = {"id": "web_session_" + uuid4().hex, "title": payload.title.strip() if payload.title and payload.title.strip() else "新分析",
                 "created_at": now(), "updated_at": now(), "document_ids": []}
        service.store.put("session", value)
        return service.clean(value)

    @app.get("/api/sessions/{identity}")
    def session(identity: str):
        return service.session_detail(identity)

    @app.patch("/api/sessions/{identity}")
    def patch_session(identity: str, payload: SessionPatch):
        with service.mutex:
            value = service.require("session", identity)
            if any(run["session_id"] == identity and run["status"] in ACTIVE for run in service.store.all("run")):
                raise HTTPException(409, "任务执行中，请完成后再更改会话或资料范围")
            if payload.document_ids is not None:
                if payload.document_ids:
                    service.document_paths(payload.document_ids)
                value["document_ids"] = payload.document_ids
            if payload.title is not None:
                if not payload.title.strip():
                    raise HTTPException(422, "会话名称不能为空")
                value["title"] = payload.title.strip()
            value["updated_at"] = now()
            service.store.put("session", value)
            return service.clean(value)

    @app.get("/api/documents")
    def documents():
        return service.clean([doc for doc in service.store.all("document")
                              if service.enable_demo or not doc.get("synthetic")])

    @app.post("/api/documents", status_code=201)
    async def upload_document(file: Annotated[UploadFile, File()]):
        name = re.split(r"[\\/]", file.filename or "")[-1]
        if not name or len(name) > 180 or any(ord(char) < 32 for char in name):
            raise HTTPException(422, "文件名无效")
        suffix = Path(name).suffix.lower()
        if suffix not in FORMATS:
            raise HTTPException(422, "不支持此格式，请导入 JSON、CSV、TXT、MD、PDF 或 XLSX")
        identity = "doc_" + uuid4().hex
        folder = service.root / "documents" / identity
        folder.mkdir(parents=True)
        raw = folder / ("upload" + suffix)
        size = 0
        try:
            with raw.open("xb") as stream:
                while data := await file.read(1024 * 1024):
                    size += len(data)
                    if size > 25_000_000:
                        raise HTTPException(413, "文件超过 25 MB")
                    stream.write(data)
            paths = ingest(raw, folder / "inputs")
            # Validate strict document types now rather than fail in a paid run.
            FinanceStore(folder / "inputs", folder / "validation")
            if any(path.stat().st_size > 10_000_000 for path in paths):
                raise ValueError("解析后的单份资料超过 10 MB，请拆分后导入")
            value = {"id": identity, "name": name, "kind": suffix[1:], "bytes": size, "created_at": now(),
                     "synthetic": False, "document_count": len(paths), "_paths": [str(path) for path in paths]}
            service.store.put("document", value)
            return service.clean(value)
        except HTTPException:
            shutil.rmtree(folder)
            raise
        except Exception as error:  # noqa: BLE001 - Parsers may raise dependency-specific errors; remove partial uploads.
            shutil.rmtree(folder)
            diagnostic = str(error)
            if suffix == ".json" and diagnostic == "JSON must contain exactly id,question,pre_text,post_text,table; use eval prepare for FinQA":
                diagnostic = (f"文件『{name}』的 JSON 格式不符合财务文档要求。"
                    "请上传财务 CSV、XLSX 或 PDF；来源说明和校验记录无需作为分析资料上传。"
                    "已上传的文件仍可使用。")
            else:
                diagnostic = f"文件『{name}』导入失败：{diagnostic}"
            raise HTTPException(422, service.clean(diagnostic)[:1000]) from None
        finally:
            await file.close()

    @app.get("/api/documents/{identity}")
    def document(identity: str):
        value = service.require("document", identity)
        path = service.document_paths([identity])[0]
        doc = read_json(path)
        def paragraphs(key):
            items = doc[key] if isinstance(doc[key], list) else [doc[key]]
            return [item[:1500] for item in items[:8]]
        value["preview"] = {"pre_text": paragraphs("pre_text"), "post_text": paragraphs("post_text"),
                            "table": [[cell[:300] for cell in row[:12]] for row in doc["table"][:30]]}
        return service.clean(value)

    @app.post("/api/sessions/{identity}/runs", status_code=202)
    def create_run(identity: str, payload: RunInput):
        return service.submit(identity, payload)

    @app.get("/api/runs/{identity}")
    def run(identity: str):
        return service.run_detail(identity)

    @app.post("/api/runs/{identity}/cancel")
    def cancel_run(identity: str):
        run = service.require("run", identity)
        if run["status"] not in ACTIVE:
            return service.clean(run)
        flag = service.cancel_flags.get(identity)
        if flag:
            flag.set()
            service.store.update("run", identity, lambda row: row.update(cancel_requested=True))
            service.event(identity, "status", "正在停止", "等待当前工具或模型请求完成并结算；随后停止后续步骤。")
        return service.run_detail(identity)

    @app.get("/api/runs/{identity}/artifacts/{artifact_id}")
    def artifact(identity: str, artifact_id: str):
        run = service.require("run", identity)
        receipt = run.get("_artifact_paths", {}).get(artifact_id)
        if receipt is None:
            raise HTTPException(404, "输出文件不存在")
        path = Path(receipt["path"]).resolve()
        directory = (service.root / "runs" / identity).resolve()
        if not path.is_relative_to(directory) or path.suffix not in {".md", ".svg"} or not path.is_file():
            raise HTTPException(404, "输出文件不可访问")
        if hashlib.sha256(path.read_bytes()).hexdigest() != receipt["sha256"]:
            raise HTTPException(409, "输出文件与已验证记录不一致")
        return FileResponse(path, filename=path.name, media_type="image/svg+xml" if path.suffix == ".svg" else "text/markdown",
                            headers={"Content-Security-Policy": "sandbox; default-src 'none'", "Cache-Control": "no-store"})

    @app.get("/api/skills")
    def skills():
        return service.clean(service.registry.catalog())

    @app.get("/api/skills/{name}")
    def skill(name: str):
        try:
            return service.clean(service.registry.load(name))
        except (ValueError, KeyError):
            raise HTTPException(404, "技能不存在或未启用") from None

    @app.get("/api/learning")
    def learning():
        def no_calls(*args, **kwargs):
            raise RuntimeError("Learning inspection is read-only")
        learning = LearningService(service.state, service.registry, no_calls)
        return service.clean({"experiences": learning.experiences(), "candidates": learning.candidates(), "reports": learning.reports()})

    if frontend_dir is not None:
        frontend = Path(frontend_dir).resolve()
        if not (frontend / "index.html").is_file():
            raise ValueError("Frontend directory must contain a built index.html")
        @app.get("/{resource:path}")
        def static(resource: str):
            if resource == "api" or resource.startswith("api/"):
                raise HTTPException(404, "接口不存在")
            path = (frontend / resource).resolve()
            if not path.is_relative_to(frontend):
                raise HTTPException(404, "文件不存在")
            if not path.is_file():
                if Path(resource).suffix:
                    raise HTTPException(404, "文件不存在")
                path = frontend / "index.html"
            return FileResponse(path)
    return app
