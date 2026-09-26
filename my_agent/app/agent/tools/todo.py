"""Pull-based todo tools backed by the graph state.

The tools follow the LangGraph convention used by ``ToolNode``: hidden graph
arguments are injected into the tool, while the model only sees the useful
task fields.  A ``Command`` carries both the state replacement and the
``ToolMessage`` that closes the tool call.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from app.agent.tools.common import fail

TODO_STATUSES = frozenset({"pending", "in_progress", "completed"})
MAX_TODOS = 50


def _state_todos(state: dict[str, Any]) -> list[dict[str, str]]:
    values = state.get("todo_list", []) if isinstance(state, dict) else []
    if not isinstance(values, list):
        return []
    return [dict(item) for item in values if isinstance(item, dict)]


def _message(tool_call_id: str, content: str) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id)


def _command(tool_call_id: str, todos: list[dict[str, str]], content: str) -> Command:
    return Command(
        update={
            "todo_list": todos,
            "messages": [_message(tool_call_id, content)],
        }
    )


def _find(todos: list[dict[str, str]], todo_id: str) -> dict[str, str]:
    requested = str(todo_id or "").strip()
    for item in todos:
        if item.get("id") == requested:
            return item
    fail(f"todo 不存在：{requested or '<empty>'}")


def _validate_status(status: str | None) -> str | None:
    if status is None:
        return None
    value = str(status).strip()
    if value not in TODO_STATUSES:
        fail(f"status 必须是 pending、in_progress 或 completed：{value}")
    return value


def _check_single_in_progress(todos: list[dict[str, str]]) -> None:
    active = [item for item in todos if item.get("status") == "in_progress"]
    if len(active) > 1:
        fail("同时最多只能有一条 in_progress todo")


@tool
async def add_todo(
    task_desc: str,
    deadline: str | None = None,
    *,
    state: Annotated[dict[str, Any], InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Add one pending task to the todo list."""

    description = str(task_desc or "").strip()
    if not description:
        fail("task_desc 不能为空")
    todos = _state_todos(state)
    if len(todos) >= MAX_TODOS:
        fail(f"todo 最多只能有 {MAX_TODOS} 条")
    used = {str(item.get("id", "")) for item in todos}
    index = 1
    while f"t{index}" in used:
        index += 1
    item = {
        "id": f"t{index}",
        "task_desc": description,
        "status": "pending",
        "progress": "",
        "deadline": str(deadline or "").strip(),
    }
    todos.append(item)
    return _command(tool_call_id, todos, json.dumps(item, ensure_ascii=False))


@tool
async def list_todo(
    *,
    state: Annotated[dict[str, Any], InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """List the current todo items; the list is intentionally pull-based."""

    todos = _state_todos(state)
    return _command(tool_call_id, todos, json.dumps(todos, ensure_ascii=False))


@tool
async def update_todo(
    id: str,
    status: str | None = None,
    progress: str | None = None,
    task_desc: str | None = None,
    deadline: str | None = None,
    *,
    state: Annotated[dict[str, Any], InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Update selected fields of one todo item."""

    todos = _state_todos(state)
    item = _find(todos, id)
    next_status = _validate_status(status)
    if next_status is not None:
        item["status"] = next_status
    if progress is not None:
        item["progress"] = str(progress).strip()
    if task_desc is not None:
        description = str(task_desc).strip()
        if not description:
            fail("task_desc 不能为空")
        item["task_desc"] = description
    if deadline is not None:
        item["deadline"] = str(deadline).strip()
    _check_single_in_progress(todos)
    return _command(tool_call_id, todos, json.dumps(item, ensure_ascii=False))


@tool
async def complete_todo(
    id: str,
    *,
    state: Annotated[dict[str, Any], InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Mark one todo item completed."""

    todos = _state_todos(state)
    item = _find(todos, id)
    item["status"] = "completed"
    item["progress"] = item.get("progress", "") or "完成"
    return _command(tool_call_id, todos, json.dumps(item, ensure_ascii=False))


TODO_TOOLS = [add_todo, list_todo, update_todo, complete_todo]
TODO_TOOL_NAMES = frozenset(tool_.name for tool_ in TODO_TOOLS)

__all__ = [
    "MAX_TODOS",
    "TODO_STATUSES",
    "TODO_TOOLS",
    "TODO_TOOL_NAMES",
    "add_todo",
    "list_todo",
    "update_todo",
    "complete_todo",
]
