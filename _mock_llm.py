"""A tiny OpenAI-compatible mock used to exercise the compile path without a real model.

my-agent builds its chat model with `streaming=True`, so this mock must speak SSE
for `/v1/chat/completions` when `stream` is set -- that is the path the real gateway
will be hit on too.

Reply selection is keyed off marker strings the ARC stage prompts contain.
"""

from __future__ import annotations

import json
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

CALL_LOG = []


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def _decide(messages: list[dict], tool_names: list[str]) -> tuple[str, list[dict]]:
    """Return (assistant_text, tool_calls)."""

    joined = "\n".join(_text_of(m) for m in messages)
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    already_called_tool = bool(tool_messages)

    if "Implementation probe." in joined and "add_todo" in tool_names:
        if not already_called_tool:
            return "", [
                {
                    "id": "call_add_todo_1",
                    "type": "function",
                    "function": {
                        "name": "add_todo",
                        "arguments": json.dumps({"task_desc": "mock implementation task"}),
                    },
                }
            ]
        if "list_todo" in tool_names and not any(
            isinstance(message.get("content"), str)
            and message["content"].lstrip().startswith("[")
            for message in tool_messages
        ):
            return "", [
                {
                    "id": "call_list_todo_1",
                    "type": "function",
                    "function": {
                        "name": "list_todo",
                        "arguments": json.dumps({}),
                    },
                }
            ]
        return "IMPLEMENTATION_COMPLETE", []

    if "InterfaceDesigner Role" in joined:
        if not already_called_tool and "read_file" in tool_names:
            return "", [
                {
                    "id": "call_read_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"file_path": "/workspace/package.json"}),
                    },
                }
            ]
        return (
            json.dumps(
                {
                    "summary": "mock design: one UI interface for the shell",
                    "interfaces": [
                        {
                            "interface_id": "REQ-1:UI:shell",
                            "req_id": "REQ-1",
                            "type": "UI",
                            "name": "Shell",
                            "file_path": "frontend/src/App.tsx",
                            "first_line": "export default function App() {",
                            "responsibility": "Render the application shell.",
                            "specification": "Root component that hosts the routed pages.",
                            "callers": [],
                            "callees": [],
                            "test_focus": ["renders a heading"],
                        }
                    ],
                    "files_written": [],
                }
            ),
            [],
        )

    if "TestGenerator Role" in joined:
        return (
            json.dumps(
                {
                    "summary": "mock tests: one frontend unit test",
                    "tests": [
                        {
                            "test_id": "REQ-1:unit:app",
                            "req_id": "REQ-1",
                            "interface_ids": ["REQ-1:UI:shell"],
                            "type": "Unit",
                            "file_path": "frontend/tests/app.test.tsx",
                            "first_line": "import { describe, it, expect } from 'vitest';",
                        }
                    ],
                    "files_written": [],
                }
            ),
            [],
        )

    if "TestDrivenDeveloper Role" in joined:
        if not already_called_tool and "run_tests" in tool_names:
            return "", [
                {
                    "id": "call_tests_1",
                    "type": "function",
                    "function": {
                        "name": "run_tests",
                        "arguments": json.dumps(
                            {"test_type": "Unit", "test_files": ["frontend/tests/app.test.tsx"]}
                        ),
                    },
                }
            ]
        return "IMPLEMENTED", []

    return json.dumps({"summary": "mock default", "interfaces": [], "tests": [], "files_written": []}), []


def _chunks(completion_id: str, text: str, tool_calls: list[dict]):
    def envelope(delta: dict, finish_reason: str | None = None) -> str:
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "mock-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(payload)}\n\n"

    yield envelope({"role": "assistant", "content": ""})
    if tool_calls:
        for index, call in enumerate(tool_calls):
            yield envelope(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call["id"],
                            "type": "function",
                            "function": call["function"],
                        }
                    ]
                }
            )
    elif text:
        # Emit in a few pieces so the consumer sees a real stream.
        step = max(1, len(text) // 4)
        for start in range(0, len(text), step):
            yield envelope({"content": text[start : start + step]})
    yield envelope({}, "stop")
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    messages = body.get("messages") or []
    tool_names = [t.get("function", {}).get("name") for t in body.get("tools") or []]
    text, tool_calls = _decide(messages, tool_names)

    CALL_LOG.append(
        {
            "has_tool_results": any(m.get("role") == "tool" for m in messages),
            "tool_names": tool_names,
            "tool_calls_returned": [c["function"]["name"] for c in tool_calls],
            "stream": bool(body.get("stream")),
        }
    )

    completion_id = "chatcmpl-" + uuid.uuid4().hex[:12]

    if body.get("stream"):
        return StreamingResponse(
            _chunks(completion_id, text, tool_calls),
            media_type="text/event-stream",
        )

    message: dict = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "mock-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


@app.get("/v1/models")
async def models() -> dict:
    return {"object": "list", "data": [{"id": "mock-model", "object": "model"}]}


@app.get("/__calls")
async def calls() -> dict:
    return {"count": len(CALL_LOG), "calls": CALL_LOG}
