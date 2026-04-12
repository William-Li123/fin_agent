from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.agent import AgentEvent, build_agent_service
from backend.memory import build_memory_service
from backend.rag import build_rag_service
from backend.sandbox import build_sandbox_manager
from backend.session import SessionRecord, SessionStore


def _configure_console_encoding() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    _configure_console_encoding()
    print("正在初始化 agent，请稍等...")
    sandbox_manager = build_sandbox_manager()
    sandbox_manager.prepare()
    rag_service = build_rag_service()
    memory_service = _build_optional_memory_service()
    store = SessionStore()
    print("初始化完成。")
    print("说明：当前最终答案使用生成完成后的字符流式打印，不是模型 token 级实时流。")

    while True:
        session = _choose_session(store, sandbox_manager)
        if session is None:
            print("已退出。")
            return

        _display_session_history(session)
        session_workspace = sandbox_manager.ensure_session_workspace(session.session_id)
        print(f"当前会话 filesystem 范围：{session_workspace}")
        agent = build_agent_service(
            rag_service=rag_service,
            memory_service=memory_service,
            sandbox_manager=sandbox_manager,
            filesystem_allowed_dirs=[session_workspace],
            filesystem_cwd=session_workspace,
        )
        should_exit = _chat_loop(agent, store, session.session_id, memory_service=memory_service)
        if should_exit:
            print("已退出。")
            return


def _choose_session(store: SessionStore, sandbox_manager):
    while True:
        sessions = store.list_sessions()
        print("\n=== 会话列表 ===")
        print("[N] 创建新会话")
        print("[Q] 退出")
        if sessions:
            for index, summary in enumerate(sessions, start=1):
                print(f"[{index}] {summary.name} | 轮次={summary.turn_count} | 最近更新={summary.updated_at}")
        else:
            print("(暂无历史会话)")

        choice = input("请选择会话：").strip()
        if not choice:
            continue

        normalized = choice.lower()
        if normalized == "q":
            return None
        if normalized == "n":
            name = input("请输入会话名称：").strip()
            if not name:
                print("会话名称不能为空。")
                continue
            session = store.create_session(name)
            return _ensure_session_binding(store, session, sandbox_manager)

        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(sessions):
                session = store.load_session(sessions[index - 1].session_id)
                return _ensure_session_binding(store, session, sandbox_manager)

        print("无效选择，请重新输入。")


def _ensure_session_binding(store: SessionStore, session: SessionRecord, sandbox_manager) -> SessionRecord:
    binding = sandbox_manager.describe_session_binding(session.session_id)
    current_binding = session.metadata.get("sandbox")
    if current_binding == binding:
        return session
    return store.update_metadata(session.session_id, {"sandbox": binding})


def _display_session_history(session: SessionRecord) -> None:
    print(f"\n=== 当前会话：{session.name} ===")
    sandbox_binding = session.metadata.get("sandbox")
    if isinstance(sandbox_binding, dict):
        print(f"Sandbox 工作区：{sandbox_binding.get('workspace_host_path', '')}")
    if not session.turns:
        print("(这是一个新会话，还没有历史消息)")
        return

    for turn in session.turns:
        print(f"\nUser: {turn.user_message}")
        print(f"Assistant: {turn.assistant_message}")


def _chat_loop(agent, store: SessionStore, session_id: str, *, memory_service=None) -> bool:
    print("\n输入消息开始对话。输入 /back 返回会话列表，输入 /exit 退出程序。")
    while True:
        user_message = input("\nYou> ").strip()
        if not user_message:
            continue
        if user_message == "/back":
            return False
        if user_message == "/exit":
            return True

        session = store.load_session(session_id)
        conversation_history = store.build_conversation_history(session)
        result = agent.run(
            user_message,
            session_id=session_id,
            conversation_history=conversation_history,
            event_handler=_print_agent_event,
        )
        store.append_turn(
            session_id,
            user_message=user_message,
            assistant_message=result.final_answer,
            plan=result.plan.raw,
            iteration_count=result.iteration_count,
            execution_history=result.execution_history,
        )
        if memory_service is not None:
            try:
                memory_service.append_conversation_turn(
                    session_id,
                    user_message=user_message,
                    assistant_message=result.final_answer,
                )
            except Exception as exc:
                print(f"[memory] failed to persist conversation turn: {exc}")

        print("\nAssistant> ", end="", flush=True)
        _stream_text(result.final_answer)
        print()


def _build_optional_memory_service():
    try:
        service = build_memory_service()
    except Exception as exc:
        print(f"[memory] disabled: {exc}")
        return None

    try:
        status = service.get_runtime_status()
    except Exception as exc:
        print(f"[memory] status unavailable: {exc}")
        return service

    if not status.oracle_configured:
        print("[memory] Oracle credentials are not configured, automatic conversation persistence is disabled.")
        return None
    if not status.oracle_reachable:
        print("[memory] Oracle is configured but not reachable, automatic conversation persistence is disabled.")
        return None
    print("[memory] Oracle-backed conversation persistence is enabled.")
    return service


def _print_agent_event(event: AgentEvent) -> None:
    event_type = event.event_type
    if event_type == "run_started":
        print("\n[状态] 开始处理请求")
    elif event_type == "planning_started":
        print("[状态] 正在规划")
    elif event_type == "planning_completed":
        steps = event.data.get("steps", [])
        step_count = len(steps) if isinstance(steps, list) else 0
        print(f"[状态] 规划完成，步骤数={step_count}")
    elif event_type == "execution_started":
        current_step = event.data.get("current_step", "")
        print(f"[状态] 正在执行：{current_step}")
    elif event_type == "execution_completed":
        status = event.data.get("status", "")
        print(f"[状态] 当前步骤决策完成，状态={status}")
    elif event_type == "tool_started":
        print(f"[状态] 正在调用工具：{event.data.get('tool_name', '')}")
    elif event_type == "tool_completed":
        tool_name = event.data.get("tool_name", "")
        success = event.data.get("success", False)
        status_text = "成功" if success else "失败"
        print(f"[状态] 工具调用完成：{tool_name}（{status_text}）")
    elif event_type == "finalizer_started":
        print("[状态] 正在整理最终答案")
    elif event_type == "finalizer_completed":
        print("[状态] 最终答案已生成")
    elif event_type == "run_completed":
        print("[状态] 本轮处理完成")


def _stream_text(text: str, *, delay_seconds: float = 0.008) -> None:
    for char in text:
        print(char, end="", flush=True)
        time.sleep(delay_seconds)


if __name__ == "__main__":
    main()
