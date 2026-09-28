from __future__ import annotations

import inspect
import json
import os
import re
import time
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from agents.runtime.contracts import AgentRuntimeContext
from core.logging import format_json_for_log, log_to_logger


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]
DEFAULT_RECURSION_LIMIT = 5000


async def ainvoke_stage_agent(
    agent: Any,
    *,
    message: str,
    context: AgentRuntimeContext,
    thread_id: str,
    logger: Any | None = None,
    label: str = "",
    log_cb: LogCallback | None = None,
    stream: bool | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """调用 stage agent 并返回 payload。

    `should_stop` 是给长循环 agent（比如 TDD）留的**外部终止谓词**：流循环每收到一个事件
    就问一次，为真就中断本次 session。存在的理由是很具体的失败模式 —— 工具层拒绝继续
    （`run_tests` 预算耗尽）**拦不住模型**，它会把那当成一次普通失败接着改、接着试。
    实测那次它收到 9 次拒绝仍空转了 18.6 分钟、写下 27 个再也无法验证的文件，最后是我手杀的
    （issue #13）。工具说「不」不够，得有人把 loop 停掉。

    注意：只有流式路径（默认）会响应这个谓词。`stream=False` 走 `agent.ainvoke`，
    那是一次不可中断的调用，传进来的 `should_stop` 会被忽略。
    """
    started_at = time.perf_counter()
    run_label = label or getattr(agent, "name", "") or context.phase or "stage-agent"
    log_to_logger(logger, "AGENT_CALL_START", label=run_label, thread_id=thread_id, body=message)
    await _emit_log(
        log_cb,
        run_label,
        f"agent call start: thread_id={thread_id}",
        node_id=context.node_id,
    )

    if _should_stream(stream):
        stream_payload = await _try_astream_stage_agent(
            agent,
            message=message,
            context=context,
            thread_id=thread_id,
            run_label=run_label,
            log_cb=log_cb,
            logger=logger,
            should_stop=should_stop,
        )
        if stream_payload is not None:
            duration_ms = (time.perf_counter() - started_at) * 1000.0
            log_to_logger(
                logger,
                "AGENT_CALL_END",
                label=run_label,
                thread_id=thread_id,
                body=f"duration_ms={duration_ms:.1f}\n{format_json_for_log(stream_payload)}",
            )
            await _emit_log(
                log_cb,
                run_label,
                f"agent call end: duration_ms={duration_ms:.1f}, payload={format_json_for_log(stream_payload)}",
                node_id=context.node_id,
            )
            return stream_payload

    # 这次回退调用原先**没有任何保护**：流式路径刚因为一次解析异常失败（见上面 except），
    # 回退再失败时异常就直接穿到 core/workflow.py 的节点级处理，节点被判死 ——
    # 实测 `IMPLEMENT raised for node REQ-1-2-2: JSONDecodeError: ...`（issue #51）。
    #
    # 降级成「返回一个说明性的 payload」而不是抛：session 会正常结束、且没有调用过
    # run_tests，于是 core/phases.py 的空 session 重试（issue #46）会接手，换一次全新尝试。
    # 一次偶发的 SDK 解析失败因此从「节点必红」变成「重试一次」。
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": message}]},
            context=context,
            config=build_agent_config(thread_id),
        )
    except Exception as exc:
        await _emit_log(
            log_cb,
            run_label,
            f"agent invocation failed: {type(exc).__name__}: {exc}. "
            "Returning a degraded payload so the stage can retry instead of dying.",
            status="warning",
            node_id=context.node_id,
        )
        log_to_logger(
            logger, "AGENT_CALL_FAILED", label=run_label, thread_id=thread_id, body=f"{type(exc).__name__}: {exc}"
        )
        return {
            "summary": (
                f"The agent invocation failed before producing a result: {type(exc).__name__}: {exc}. "
                "This is an infrastructure failure, not a judgement on the implementation."
            ),
            "_agent_call_failed": f"{type(exc).__name__}: {exc}",
        }
    await _log_completed_tool_batches(log_cb, result, label=run_label, node_id=context.node_id)
    await _log_agent_trace(log_cb, result, label=run_label, thread_id=thread_id, node_id=context.node_id)
    payload = extract_payload(result)
    duration_ms = (time.perf_counter() - started_at) * 1000.0
    log_to_logger(
        logger,
        "AGENT_CALL_END",
        label=run_label,
        thread_id=thread_id,
        body=f"duration_ms={duration_ms:.1f}\n{format_json_for_log(payload)}",
    )
    await _emit_log(
        log_cb,
        run_label,
        f"agent call end: duration_ms={duration_ms:.1f}, payload={format_json_for_log(payload)}",
        node_id=context.node_id,
    )
    return payload


def extract_payload(result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structured_response") if isinstance(result, dict) else None
    normalized = _normalize_payload_value(structured)
    if normalized is not None:
        return normalized

    final_text = _extract_final_message_text(result)
    parsed = parse_json_payload(final_text)
    if parsed is not None:
        return parsed
    return {"summary": final_text, "_raw_final_message": _stringify_final_message(result)}


def parse_json_payload(text: str) -> dict[str, Any] | None:
    current = (text or "").strip()
    for _ in range(3):
        if not current:
            return None
        next_string: str | None = None
        for candidate in _json_candidates(current):
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            normalized = _normalize_payload_value(payload)
            if normalized is not None:
                return normalized
            if isinstance(payload, str) and payload.strip():
                next_string = payload.strip()
        if next_string is None:
            return None
        current = next_string
    return None


def _normalize_payload_value(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump()
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {"items": dumped}
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"items": value}
    return None


def _extract_final_message_text(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return ""
    messages = result.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return ""
    for message in reversed(messages):
        role = _message_role(message)
        if role in {"human", "tool"}:
            continue
        text = _message_content_text(message)
        if text:
            return text
    return ""


def _stringify_final_message(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return ""
    messages = result.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return ""
    for message in reversed(messages):
        if _message_role(message) in {"human", "tool"}:
            continue
        return _truncate_text(_stringify_tool_args(_message_to_debug_payload(message)), max_chars=4000)
    return ""


def _message_to_debug_payload(message: Any) -> Any:
    if hasattr(message, "model_dump"):
        return message.model_dump()
    if isinstance(message, dict):
        return message
    return {
        "type": _message_role(message),
        "content": getattr(message, "content", None),
        "tool_calls": getattr(message, "tool_calls", None),
    }


async def _try_astream_stage_agent(
    agent: Any,
    *,
    message: str,
    context: AgentRuntimeContext,
    thread_id: str,
    run_label: str,
    log_cb: LogCallback | None,
    logger: Any | None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any] | None:
    if _should_use_sync_stream_v3():
        sync_payload = _try_stream_stage_agent_sync(
            agent,
            message=message,
            context=context,
            thread_id=thread_id,
            run_label=run_label,
            log_cb=log_cb,
            logger=logger,
            should_stop=should_stop,
        )
        if sync_payload is not None:
            return sync_payload

    if not hasattr(agent, "astream_events"):
        await _emit_log(log_cb, run_label, "agent streaming is unavailable; falling back to ainvoke.", node_id=context.node_id)
        return None

    await _emit_log(log_cb, run_label, "agent stream start.", node_id=context.node_id)
    final_state: dict[str, Any] | None = None
    stopped = False
    try:
        event_stream = agent.astream_events(
            {"messages": [{"role": "user", "content": message}]},
            context=context,
            config=build_agent_config(thread_id),
            version=os.environ.get("ARC_AGENT_STREAM_VERSION", "v2"),
        )
        if inspect.isawaitable(event_stream):
            event_stream = await event_stream
        async for event in event_stream:
            # 谓词在**处理事件之前**问：翻转它的那个事件（工具回执）本身还要照常记录，
            # 所以下一个事件才真正中断。跳出 async for 会给生成器发 aclose()，
            # LangGraph 随之取消这次 run。
            if should_stop is not None and should_stop():
                stopped = True
                break
            maybe_state = await _log_stream_event(
                log_cb,
                event,
                label=run_label,
                node_id=context.node_id,
            )
            if isinstance(maybe_state, dict):
                final_state = maybe_state
    except Exception as exc:
        await _emit_log(
            log_cb,
            run_label,
            f"agent stream failed; falling back to ainvoke. error={exc}",
            status="warning",
            node_id=context.node_id,
        )
        log_to_logger(logger, "AGENT_STREAM_FALLBACK", label=run_label, thread_id=thread_id, body=str(exc))
        return None

    if stopped:
        await _emit_log(
            log_cb,
            run_label,
            "agent stream stopped early: the stage asked for a hard stop, so this session ends here. "
            "The thread is abandoned rather than resumed, so no half-finished tool batch is carried over.",
            status="warning",
            node_id=context.node_id,
        )

    if final_state is None:
        if stopped:
            # 刻意**不**回落到 ainvoke：那会把整个 agent 重跑一遍，
            # 而我们刚刚决定的就是别再跑了。
            return {"summary": "Agent session stopped early at the caller's request."}
        await _emit_log(
            log_cb,
            run_label,
            "agent stream ended without final state; falling back to ainvoke.",
            status="warning",
            node_id=context.node_id,
        )
        return None

    await _log_agent_trace(log_cb, final_state, label=run_label, thread_id=thread_id, node_id=context.node_id)
    return extract_payload(final_state)


def _try_stream_stage_agent_sync(
    agent: Any,
    *,
    message: str,
    context: AgentRuntimeContext,
    thread_id: str,
    run_label: str,
    log_cb: LogCallback | None,
    logger: Any | None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any] | None:
    if not hasattr(agent, "stream_events"):
        return None
    try:
        stream = agent.stream_events(
            {"messages": [{"role": "user", "content": message}]},
            context=context,
            config=build_agent_config(thread_id),
            version=os.environ.get("ARC_AGENT_STREAM_VERSION", "v3"),
        )
        extensions = getattr(stream, "extensions", {}) or {}
        stream_names = [name for name in ("messages", "values", "subagents") if name in extensions]
        if not stream_names or not hasattr(stream, "interleave"):
            return None

        _emit_log_sync(log_cb, run_label, "agent stream start.", node_id=context.node_id)
        final_state: dict[str, Any] | None = None
        seen_message_count = 0
        for stream_name, item in stream.interleave(*stream_names):
            # 与 _try_astream_stage_agent 同一套终止语义。这条路目前不可达
            # （_should_use_sync_stream_v3 恒为 False），但参数既然接了就別默默吞掉。
            if should_stop is not None and should_stop():
                _emit_log_sync(
                    log_cb,
                    run_label,
                    "agent stream stopped early: the stage asked for a hard stop.",
                    status="warning",
                    node_id=context.node_id,
                )
                break
            if stream_name == "messages":
                text = _typed_stream_item_text(item)
                if text:
                    _emit_log_sync(
                        log_cb,
                        run_label,
                        f"model> {_truncate_text(text, max_chars=1200)}",
                        node_id=context.node_id,
                    )
                continue

            if stream_name == "subagents":
                _log_subagent_stream_item_sync(log_cb, item, label=run_label, node_id=context.node_id)
                continue

            if stream_name == "values" and isinstance(item, dict):
                messages = item.get("messages")
                if isinstance(messages, list) and len(messages) > seen_message_count:
                    new_messages = messages[seen_message_count:]
                    seen_message_count = len(messages)
                    formatted = _format_message_trace(new_messages)
                    if formatted:
                        _emit_log_sync(
                            log_cb,
                            run_label,
                            f"stream messages:\n{formatted}",
                            node_id=context.node_id,
                        )
                if _is_agent_state_with_payload(item):
                    final_state = item

        latest = getattr(stream, "output", None)
        if isinstance(latest, dict) and _is_agent_state_with_payload(latest):
            final_state = latest
        if final_state is None:
            _emit_log_sync(
                log_cb,
                run_label,
                "agent stream ended without final state; falling back to ainvoke.",
                status="warning",
                node_id=context.node_id,
            )
            return None
        _log_agent_trace_sync(log_cb, final_state, label=run_label, thread_id=thread_id, node_id=context.node_id)
        return extract_payload(final_state)
    except Exception as exc:
        _emit_log_sync(
            log_cb,
            run_label,
            f"agent stream failed; falling back to ainvoke. error={exc}",
            status="warning",
            node_id=context.node_id,
        )
        log_to_logger(logger, "AGENT_STREAM_FALLBACK", label=run_label, thread_id=thread_id, body=str(exc))
        return None


async def _log_stream_event(
    log_cb: LogCallback | None,
    event: Any,
    *,
    label: str,
    node_id: str,
) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        text = _typed_stream_item_text(event)
        if text:
            await _emit_log(log_cb, label, f"stream: {text}", node_id=node_id)
        return None

    event_name = str(event.get("event", "") or "")
    name = str(event.get("name", "") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}

    if event_name in {"on_chat_model_stream", "on_llm_stream"}:
        # The model is configured for non-streaming output. Ignore any provider
        # chunks that still arrive and render the complete response on *_end.
        return None

    if event_name in {"on_chat_model_end", "on_llm_end"}:
        # 每次模型调用的用量必须落盘 —— 它是解释「为什么一次提交烧 5 亿 token」
        # 的唯一入口。此前只把 usage 累计给 UI（app/agent/stream.py），ARC 这条路
        # 连字段都没碰过，于是本地只能靠数模型调用次数估算、平台只给一个总数。
        #
        # `cached` 尤其关键：网关每次响应都返回缓存命中数，而它正是
        # 「平台 143 秒/次 vs 本地 42 秒/次」的钥匙（issue #4 的领域）。
        # 逐次记录而不是只记总数：总数可以求和，反过来不行。issue #58。
        usage = _usage_line(data.get("output"))
        if usage:
            await _emit_log(log_cb, label, usage, node_id=node_id)
        text = _message_content_text(data.get("output"))
        if text:
            await _emit_log(log_cb, label, f"model-final> {_truncate_text(text, max_chars=2400)}", node_id=node_id)
        return None

    if event_name == "on_tool_start":
        tool_input = data.get("input")
        await _emit_log(
            log_cb,
            label,
            f"tool-call> {name or 'unknown'} args={_truncate_text(_stringify_tool_args(tool_input), max_chars=1000)}",
            node_id=node_id,
        )
        skill_name = _skill_name_from_read_call(name, tool_input)
        if skill_name:
            await _emit_log(log_cb, label, f"skill-loaded: {skill_name}", node_id=node_id)
        return None

    if event_name == "on_tool_end":
        await _emit_log(
            log_cb,
            label,
            f"tool-result> {name or 'unknown'} result={_truncate_text(_stringify_tool_args(data.get('output')), max_chars=2000)}",
            node_id=node_id,
        )
        return None

    if event_name in {"on_chain_end", "on_graph_end"}:
        output = data.get("output")
        if isinstance(output, dict) and _is_agent_state_with_payload(output):
            return output
    return None


def _skill_name_from_read_call(tool_name: str, tool_input: Any) -> str:
    """Return the selected skill name when the model reads its instruction file."""

    if tool_name != "read_file" or not isinstance(tool_input, dict):
        return ""
    file_path = str(tool_input.get("file_path") or tool_input.get("path") or "").strip().replace("\\", "/")
    match = re.fullmatch(r"/skills/([^/]+)/SKILL\.md", file_path)
    return match.group(1) if match else ""


def _is_agent_state_with_payload(state: dict[str, Any]) -> bool:
    structured = state.get("structured_response")
    if structured is not None:
        return True
    messages = state.get("messages")
    return isinstance(messages, list) and any(_message_role(message) not in {"human", "tool"} for message in messages)


async def _log_agent_trace(
    log_cb: LogCallback | None,
    result: dict[str, Any],
    *,
    label: str,
    thread_id: str,
    node_id: str,
) -> None:
    if not _should_log_full_agent_trace():
        return
    formatted = _format_message_trace(result.get("messages", []) if isinstance(result, dict) else [])
    if not formatted:
        await _emit_log(log_cb, label, f"agent trace is empty: thread_id={thread_id}", node_id=node_id)
        return
    await _emit_log(log_cb, label, f"agent trace: thread_id={thread_id}\n{formatted}", node_id=node_id)


async def _log_completed_tool_batches(
    log_cb: LogCallback | None,
    result: dict[str, Any],
    *,
    label: str,
    node_id: str,
) -> None:
    """Emit each completed tool-call batch from a non-streaming agent result."""

    messages = result.get("messages", []) if isinstance(result, dict) else []
    pending: list[dict[str, str]] = []
    for message in messages if isinstance(messages, list) else []:
        calls = _extract_tool_calls(message)
        if calls:
            await _emit_tool_batch(log_cb, label, pending, node_id=node_id)
            pending = [
                {
                    "id": str(call.get("id", "") or call.get("tool_call_id", "")),
                    "name": str(call.get("name", "") or call.get("tool_name", "") or "unknown"),
                    "args": _truncate_text(_stringify_tool_args(call.get("args")), max_chars=1200),
                    "result": "",
                }
                for call in calls
            ]
            continue

        if _message_role(message) != "tool" or not pending:
            continue
        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_call_id is None and isinstance(message, dict):
            tool_call_id = message.get("tool_call_id")
        matched = next((item for item in pending if item["id"] and item["id"] == str(tool_call_id or "")), None)
        target = matched or next((item for item in pending if not item["result"]), None)
        if target is not None:
            target["result"] = _truncate_text(_message_content_text(message), max_chars=2000)
        if all(item["result"] for item in pending):
            await _emit_tool_batch(log_cb, label, pending, node_id=node_id)
            pending = []

    await _emit_tool_batch(log_cb, label, pending, node_id=node_id)


async def _emit_tool_batch(
    log_cb: LogCallback | None,
    label: str,
    tools: list[dict[str, str]],
    *,
    node_id: str,
) -> None:
    if not tools:
        return
    payload = {
        "tools": [
            {"name": item["name"], "args": item["args"], "result": item["result"] or "[no result returned]"}
            for item in tools
        ]
    }
    await _emit_log(log_cb, label, f"tool-batch> {json.dumps(payload, ensure_ascii=False)}", node_id=node_id)


def _log_agent_trace_sync(
    log_cb: LogCallback | None,
    result: dict[str, Any],
    *,
    label: str,
    thread_id: str,
    node_id: str,
) -> None:
    if not _should_log_full_agent_trace():
        return
    formatted = _format_message_trace(result.get("messages", []) if isinstance(result, dict) else [])
    if not formatted:
        return
    _emit_log_sync(log_cb, label, f"agent trace: thread_id={thread_id}\n{formatted}", node_id=node_id)


def _should_log_full_agent_trace() -> bool:
    return str(os.environ.get("ARC_DEBUG_AGENT_TRACE", "")).strip().lower() in {"1", "true", "yes", "on"}


def _format_message_trace(messages: list[Any]) -> str:
    if not isinstance(messages, list) or not messages:
        return ""
    blocks: list[str] = []
    for index, message in enumerate(messages, start=1):
        role = _message_role(message)
        if role == "human":
            continue
        lines = [f"[{index}] {role.upper()}"]
        tool_calls = _extract_tool_calls(message)
        if tool_calls:
            lines.append("tool_calls:")
            for tool_index, call in enumerate(tool_calls, start=1):
                tool_name = str(call.get("name", "") or call.get("tool_name", "") or "unknown")
                tool_id = str(call.get("id", "") or call.get("tool_call_id", "") or "-")
                tool_args = _truncate_text(_stringify_tool_args(call.get("args")), max_chars=1000)
                lines.append(f"  {tool_index}. {tool_name} id={tool_id}")
                if tool_args:
                    lines.append(f"     args: {tool_args}")
        tool_result_meta = _extract_tool_result_meta(message)
        if tool_result_meta:
            lines.append(tool_result_meta)
        content = _message_content_text(message)
        if content:
            lines.append("content:")
            lines.append(_indent_block(_truncate_text(content, max_chars=2400)))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _message_role(message: Any) -> str:
    role = getattr(message, "type", None) or getattr(message, "role", None)
    if role is None and isinstance(message, dict):
        role = message.get("type") or message.get("role")
    if isinstance(role, str) and role:
        return role
    cls_name = message.__class__.__name__.lower()
    if "tool" in cls_name:
        return "tool"
    if "human" in cls_name:
        return "human"
    if "ai" in cls_name or "assistant" in cls_name:
        return "assistant"
    return cls_name or "message"


def _extract_tool_calls(message: Any) -> list[dict[str, Any]]:
    calls = getattr(message, "tool_calls", None)
    if calls is None and isinstance(message, dict):
        calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    normalized: list[dict[str, Any]] = []
    for call in calls:
        if isinstance(call, dict):
            normalized.append(call)
            continue
        normalized.append(
            {
                "name": getattr(call, "name", ""),
                "id": getattr(call, "id", ""),
                "args": getattr(call, "args", {}),
            }
        )
    return normalized


def _extract_tool_result_meta(message: Any) -> str:
    if _message_role(message) != "tool":
        return ""
    tool_name = getattr(message, "name", None)
    if tool_name is None and isinstance(message, dict):
        tool_name = message.get("name")
    tool_call_id = getattr(message, "tool_call_id", None)
    if tool_call_id is None and isinstance(message, dict):
        tool_call_id = message.get("tool_call_id")
    parts = ["tool_result:"]
    if tool_name:
        parts.append(f"name={tool_name}")
    if tool_call_id:
        parts.append(f"id={tool_call_id}")
    return " ".join(parts)


def _usage_line(message: Any) -> str:
    """把一次模型调用的用量格式化成一行日志；没有用量信息时返回空串。

    两种字段约定都读：langchain 归一化后的 `input_token_details: {cache_read,
    cache_creation}`，以及 OpenAI 原生的 `prompt_tokens_details: {cached_tokens}`。
    只认一种的话，换个 provider 就会静默变成 0 —— 而那会被读成「缓存完全没命中」，
    是个很贵的误判。

    `cache_hit` 是刻意算出来的：一次提交烧 5 亿 token（issue #57），
    而命中率是 0 还是 90% 决定完全不同的修法。
    """

    meta = getattr(message, "usage_metadata", None) or {}
    if not isinstance(meta, dict) or not meta:
        return ""

    prompt = int(meta.get("input_tokens") or 0)
    completion = int(meta.get("output_tokens") or 0)

    details = meta.get("input_token_details")
    details = details if isinstance(details, dict) else {}
    cached = details.get("cache_read")
    created = details.get("cache_creation")

    if cached is None:
        raw = getattr(message, "response_metadata", None)
        raw = raw if isinstance(raw, dict) else {}
        token_usage = raw.get("token_usage")
        token_usage = token_usage if isinstance(token_usage, dict) else {}
        prompt_details = token_usage.get("prompt_tokens_details")
        prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
        cached = prompt_details.get("cached_tokens")

    cached = int(cached or 0)
    created = int(created or 0)
    hit = f"{cached * 100 // prompt}%" if prompt else "-"
    return (
        f"model tokens: in={prompt:,} out={completion:,} "
        f"cached={cached:,} created={created:,} cache_hit={hit}"
    )


def _message_content_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return _content_text(content)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [_content_text(item) for item in content]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(content, dict):
        for key in ("text", "content", "output_text"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        parsed = content.get("parsed")
        if isinstance(parsed, (dict, list)):
            return json.dumps(parsed, ensure_ascii=False)
        if isinstance(parsed, str) and parsed.strip():
            return parsed.strip()
        nested_parts = [_content_text(value) for value in content.values()]
        return "\n".join(part for part in nested_parts if part).strip()
    if content is None:
        return ""
    text = getattr(content, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    nested = getattr(content, "content", None)
    if nested is not None:
        return _content_text(nested)
    return str(content).strip()


def _typed_stream_item_text(item: Any) -> str:
    if isinstance(item, tuple) and item:
        parts = [_typed_stream_item_text(part) for part in item]
        return " ".join(part for part in parts if part).strip()
    text = getattr(item, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    content = _message_content_text(item)
    if content:
        return content
    name = getattr(item, "name", None)
    status = getattr(item, "status", None)
    if name or status:
        return f"name={name or '-'} status={status or '-'}"
    return ""


def _log_subagent_stream_item_sync(
    log_cb: LogCallback | None,
    item: Any,
    *,
    label: str,
    node_id: str,
) -> None:
    name = getattr(item, "name", "") or "subagent"
    status = getattr(item, "status", "") or ""
    _emit_log_sync(log_cb, label, f"subagent> {name} status={status or '-'}", node_id=node_id)
    messages = getattr(item, "messages", None)
    if messages:
        formatted = _format_message_trace(list(messages))
        if formatted:
            _emit_log_sync(log_cb, label, f"subagent messages ({name}):\n{formatted}", node_id=node_id)
    output = getattr(item, "output", None)
    if output:
        _emit_log_sync(
            log_cb,
            label,
            f"subagent output ({name}): {_truncate_text(_stringify_tool_args(output), max_chars=2000)}",
            node_id=node_id,
        )


def _stringify_tool_args(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return repr(value)


def _truncate_text(text: str, *, max_chars: int) -> str:
    normalized = (text or "").strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 18].rstrip() + "\n...[truncated]"


def _indent_block(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines()) if text else prefix


async def _emit_log(
    log_cb: LogCallback | None,
    agent_name: str,
    message: str,
    *,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    if log_cb is None:
        return
    result = log_cb(agent_name, message, status, node_id)
    if inspect.isawaitable(result):
        await result


def _emit_log_sync(
    log_cb: LogCallback | None,
    agent_name: str,
    message: str,
    *,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    if log_cb is None:
        return
    result = log_cb(agent_name, message, status, node_id)
    if not inspect.isawaitable(result):
        return
    try:
        loop = None
        try:
            import asyncio

            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            loop.create_task(result)
            return
        close = getattr(result, "close", None)
        if callable(close):
            close()
    except Exception:
        return


def _should_stream(stream: bool | None) -> bool:
    if stream is not None:
        return stream
    return True


def _should_use_sync_stream_v3() -> bool:
    return False


def build_agent_config(thread_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": DEFAULT_RECURSION_LIMIT,
    }


def _json_candidates(text: str) -> list[str]:
    candidates: list[str] = []

    def add(candidate: str) -> None:
        stripped = candidate.strip()
        if stripped and stripped not in candidates:
            candidates.append(stripped)

    add(text)
    for match in re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE):
        add(match)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end != -1 and end > start:
            add(text[start : end + 1])
    return candidates
