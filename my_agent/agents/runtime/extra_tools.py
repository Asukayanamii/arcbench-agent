"""Stage-specific tools shared by ARC tool assembly and prompt publication."""

from __future__ import annotations

from typing import Iterable

from langchain_core.tools import BaseTool

from app.agent.tools.todo import TODO_TOOLS
from app.agent.tools.subagent import build_subagent_tool

EXTRA_TOOLS_BY_STAGE: dict[str, tuple[str, ...]] = {
    "implementation": (
        "subagent",
        "add_todo",
        "list_todo",
        "update_todo",
        "complete_todo",
    ),
}

EXTRA_TOOL_DESCRIPTIONS: dict[str, str] = {
    "subagent": "delegate a focused read-only exploration and receive a short summary",
    "add_todo": "start a pending implementation task",
    "list_todo": "pull the current todo list and progress",
    "update_todo": "update one todo task or its progress",
    "complete_todo": "mark one todo task completed",
}


def _by_name(tools: Iterable[object]) -> dict[str, object]:
    return {str(getattr(item, "name", "")): item for item in tools}


def build_extra_tools(
    stage: str,
    *,
    read_tools: list[BaseTool],
    workspace_root: str,
) -> list[BaseTool]:
    """Build the extra tools listed for one ARC stage."""

    names = EXTRA_TOOLS_BY_STAGE.get(stage, ())
    if not names:
        return []
    subagent = build_subagent_tool(read_tools=_read_only_tools(read_tools), workspace_root=workspace_root)
    available = {"subagent": subagent, **_by_name(TODO_TOOLS)}
    missing = [name for name in names if name not in available]
    if missing:
        raise RuntimeError(f"extra tool configuration references missing tools: {missing}")
    return [available[name] for name in names]


def _read_only_tools(tools: list[BaseTool]) -> list[BaseTool]:
    allowed = {"read_file", "grep", "glob", "ls", "read", "find"}
    selected = [tool for tool in tools if str(getattr(tool, "name", "")) in allowed]
    forbidden = {"write_file", "edit_file", "write", "edit", "subagent"}
    assert not ({str(getattr(tool, "name", "")) for tool in selected} & forbidden)
    return selected


def render_extra_tools(stage: str) -> str:
    """Render the exact configured tool names and their workflow guidance."""

    names = EXTRA_TOOLS_BY_STAGE.get(stage, ())
    if not names:
        return ""
    lines = ["### Extra Tools (available)"]
    lines.extend(f"- `{name}`: {EXTRA_TOOL_DESCRIPTIONS[name]}" for name in names)
    if stage == "implementation":
        lines.extend(
            [
                "- Begin planning with `add_todo`; call `list_todo` at the start of each round to review progress; call `complete_todo` when a task is finished.",
                "- `subagent` is read-only and returns a bounded summary; use it for focused exploration before editing.",
            ]
        )
    return "\n".join(lines)


__all__ = [
    "EXTRA_TOOLS_BY_STAGE",
    "EXTRA_TOOL_DESCRIPTIONS",
    "build_extra_tools",
    "render_extra_tools",
]
