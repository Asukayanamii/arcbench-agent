"""Read-only subagent tool backed by an independent LangGraph subgraph."""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import RECURSION_LIMIT, build_graph
from app.agent.messages import text_of
from app.agent.models import build_model, build_summary_model
from app.agent.tools.common import fail

SUBAGENT_MAX_CALLS = 3
SUBAGENT_MAX_CHARS = 4000
SUBAGENT_RECURSION_LIMIT = 60
READ_ONLY_FORBIDDEN = frozenset({"write_file", "edit_file", "write", "edit", "subagent"})


def build_subagent_tool(
    *,
    read_tools: list[BaseTool],
    workspace_root: str,
    system_prompt: str | None = None,
    max_calls: int = SUBAGENT_MAX_CALLS,
) -> BaseTool:
    """Build a model-facing tool whose implementation is a separate graph."""

    tools = list(read_tools)
    names = {str(item.name) for item in tools}
    forbidden = sorted(names & READ_ONLY_FORBIDDEN)
    assert not forbidden, f"subagent read tool set contains forbidden tools: {forbidden}"
    assert max_calls > 0
    checkpointer = InMemorySaver()
    child_graph = build_graph(
        checkpointer,
        build_model(),
        build_summary_model(),
        tools=tools,
        system_prompt=system_prompt
        or "You are a read-only exploration subagent. Inspect the workspace and return a concise, evidence-based summary. Do not modify files.",
    )
    calls = 0

    @tool
    async def subagent(description: str, prompt: str) -> str:
        """Delegate a focused read-only exploration task and return its summary."""

        nonlocal calls
        if calls >= max_calls:
            fail(f"subagent 本阶段最多调用 {max_calls} 次")
        calls += 1
        thread_id = f"subagent-{uuid.uuid4().hex}"
        child_config: dict[str, Any] = {
            "configurable": {
                "thread_id": thread_id,
                "workspace": workspace_root,
                "tools": tools,
            },
            "recursion_limit": SUBAGENT_RECURSION_LIMIT,
            # Nested callbacks/tags are deliberately isolated from the parent stream.
            "callbacks": [],
            "tags": ["subagent"],
        }
        request = f"Task: {str(description or '').strip()}\n\nInstructions:\n{str(prompt or '').strip()}"
        result = await child_graph.ainvoke(
            {"messages": [{"role": "user", "content": request}]},
            config=child_config,
        )
        summary = ""
        for message in reversed(result.get("messages") or []):
            if isinstance(message, AIMessage):
                summary = text_of(message).strip()
                if summary:
                    break
        if not summary:
            summary = "子 agent 未返回摘要。"
        return summary[:SUBAGENT_MAX_CHARS]

    return subagent


__all__ = [
    "SUBAGENT_MAX_CALLS",
    "SUBAGENT_MAX_CHARS",
    "SUBAGENT_RECURSION_LIMIT",
    "build_subagent_tool",
]
