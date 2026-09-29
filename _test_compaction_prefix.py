"""Regression test: compaction must not invalidate the request prefix cache.

Production evidence (issue #4, real platform runs): most calls hit 98-99%
cache, but the first model call after a successful compaction collapsed to
cache_read == 4096 while input shrank (93K -> 81K, 96K -> 87K). The old
implementation appended the summary to the system prompt (`with_summary`), so
the system message changed the moment compaction fired and the cached prefix
broke at the end of the static system prompt -- only tool definitions plus the
system head (about 4K tokens) still hit, everything after was billed at full
price. The shrink in input tokens is the fingerprint of a rewrite: the summary
replaced a large middle section of the history.

The fix: the summary enters the model view as a standalone HumanMessage
inserted after the first user message (`summary_message`). The system message
stays byte-identical across compaction, so the common prefix of the pre- and
post-compaction requests extends to "tool definitions + system + first user
message" -- and in ARC stage runs the first user message carries the whole
requirements context, which is the bulk of the prefix.

Drives the real graph with scripted fake models: no network, no cost.
  call 1 -> tool call (small result)
  call 2 -> tool call (huge result: pushes the estimate over the compaction line)
  call 3 -> final answer (compaction fires before this call, as in production:
            the collapse landed on the call that closed out a stage)
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent / "my_agent"
sys.path.insert(0, str(REPO))

# app.config reads these at import time; they must be in place first.
os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:1/v1"
os.environ["LLM_CONTEXT_WINDOW"] = "2000"
os.environ["COMPACT_AT"] = "0.5"
os.environ["COMPACT_KEEP_TOKENS"] = "50"
os.environ["COMPACT_ENABLED"] = "1"
# #62 起压缩触发点是**绝对量**优先（COMPACT_AT_TOKENS，默认 60000），窗口 × 比例只是回退。
# 不显式给它一个小值，这条测试的整段会话（约 1.8K tokens）永远到不了 60K 的线，
# 压缩一次都不会发生 —— 测试会静默空转（实测：summarizer calls=0，四个断言全红）。
os.environ["COMPACT_AT_TOKENS"] = "800"

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from app.agent.compaction.policy import (  # noqa: E402
    estimate_view,
    message_tokens,
    rough_tokens,
    summary_block_tokens,
    summary_message,
)
from app.agent.graph import build_graph  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + label + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


def content_of(message) -> str:  # noqa: ANN001
    """call_model 发请求时 system 是 dict、其余是消息对象，两种都归一到正文。"""
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(message.content)


SYSTEM = "SYS: stage system prompt, must never change across compaction."


class _ScriptedModel:
    """Duck-typed chat model: records every request, replays a fixed script."""

    def __init__(self, script: list[AIMessage]) -> None:
        self.script = list(script)
        self.requests: list[list] = []

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self

    async def ainvoke(self, messages, config=None, **kwargs):  # noqa: ANN001
        self.requests.append(list(messages))
        return self.script.pop(0)


@tool
def step(n: int) -> str:
    """Do one step of the task."""
    # n=1 的返回要保持在中段最小 token 线（MIN_MIDDLE_TOKENS=512，≈1700 字符）之上，
    # 否则压缩会在"中段太小不值得调摘要器"那一步被跳过，本测试就不再走真实压缩路径了。
    return "x" * (2048 if n == 1 else 4000)


def _ai_tool_call(call_id: str, n: int) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "step", "args": {"n": n}, "id": call_id, "type": "tool_call"}],
    )


async def run_graph() -> tuple[_ScriptedModel, _ScriptedModel, dict]:
    main = _ScriptedModel(
        [
            _ai_tool_call("c1", 1),
            _ai_tool_call("c2", 2),
            AIMessage(content="done"),
        ]
    )
    summarizer = _ScriptedModel([AIMessage(content="第1步完成。")])
    graph = build_graph(
        InMemorySaver(),
        model=main,
        summary_model=summarizer,
        tools=[step],
        system_prompt=SYSTEM,
    )
    config = {"configurable": {"thread_id": "prefix-test", "tools": [step]}}
    state = await graph.ainvoke({"messages": [HumanMessage(content="begin the task")]}, config=config)
    return main, summarizer, state


def check_unit_parts() -> None:
    print("=== 1. the summary travels as a message, not inside the system prompt ===")
    message = summary_message("关键结论")
    check("summary_message is a HumanMessage", isinstance(message, HumanMessage))
    check("summary text is inside the message body", "关键结论" in message.content)
    check("framing marks it as background, not new instructions",
          "早前对话的摘要" in message.content and "不是新指令" in message.content)
    check("summary block token estimate is positive", summary_block_tokens("关键结论") > 0)
    kept = [HumanMessage(content="a" * 100)]
    size = estimate_view(SYSTEM, "关键结论", kept)
    expected = (rough_tokens(SYSTEM) + summary_block_tokens("关键结论")
                + sum(message_tokens(item) for item in kept))
    check("estimate_view = system + summary block + kept",
          size == expected, f"estimate={size} expected={expected}")


async def check_prefix_stability() -> None:
    print()
    print("=== 2. the graph keeps the prefix stable across a real compaction ===")
    main, summarizer, state = await run_graph()
    requests = main.requests
    check("the loop made three model calls", len(requests) == 3,
          f"calls={len(requests)}")
    if len(requests) != 3:
        return
    check("compaction actually fired (one summarizer call)",
          len(summarizer.requests) == 1,
          f"summarizer calls={len(summarizer.requests)}")
    check("compaction was written into state",
          (state.get("compaction") or {}).get("count") == 1,
          f"compaction={state.get('compaction')!r}")

    print()
    print("--- the fix itself: system message is byte-identical across compaction ---")
    systems = [content_of(request[0]) for request in requests]
    check("system prompt identical in all three requests",
          systems[0] == systems[1] == systems[2] == SYSTEM,
          f"systems={systems!r}")
    check("summary never leaks into the system message",
          "早前对话的摘要" not in systems[2])

    print()
    print("--- the payoff: common prefix extends past the first user message ---")
    pre, post = requests[1], requests[2]
    check("request 3 keeps the system message of request 2",
          content_of(post[0]) == content_of(pre[0]))
    check("request 3 keeps the first user message of request 2",
          content_of(post[1]) == content_of(pre[1]),
          f"post[1]={content_of(post[1])[:40]!r}")
    summary_slot = post[2]
    check("the summary sits at view index 2 as a user message",
          isinstance(summary_slot, HumanMessage) and "早前对话的摘要" in content_of(summary_slot),
          f"role={type(summary_slot).__name__}")
    check("the kept tail keeps tool pairing (the calling AIMessage precedes its result)",
          isinstance(post[3], AIMessage)
          and bool(post[3].tool_calls)
          and content_of(post[4]) == "x" * 4000,
          f"post[3]={type(post[3]).__name__} post[4]={content_of(post[4])[:20]!r}")


def main() -> int:
    check_unit_parts()
    print()
    asyncio.run(check_prefix_stability())
    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
