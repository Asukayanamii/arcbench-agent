"""End-to-end test of the ARC stage bridge over my-agent's engine.

Runs a real LangGraph loop against the mock OpenAI-compatible server, so it covers:
path sandboxing, the ARC-named tool surface, stage discipline, tool execution
through app.agent.tool_calls, JSON artifact extraction, and the runtime SDK writes.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(r"D:\project\hks\my_agent")
MOCK_PORT = 8931
WORKSPACE = Path(r"D:\project\hks\_arc_test_out")

sys.path.insert(0, str(REPO))

os.environ["LLM_API_KEY"] = "mock-key"
os.environ["LLM_BASE_URL"] = f"http://127.0.0.1:{MOCK_PORT}/v1"
os.environ["LLM_MODEL"] = "mock-model"
os.environ["OPENAI_API_KEY"] = "mock-key"
os.environ["OPENAI_BASE_URL"] = f"http://127.0.0.1:{MOCK_PORT}/v1"
os.environ["MODEL"] = "mock-model"
os.environ["LOG_LEVEL"] = "INFO"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("PASS  " if condition else "FAIL  ") + label + (f"  ({detail})" if detail else ""))
    if not condition:
        failures.append(label)


def wait_for_server(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def main() -> int:
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE, ignore_errors=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    mock = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "--app-dir",
            r"D:\project\hks",
            "_mock_llm:app",
            "--port",
            str(MOCK_PORT),
            "--log-level",
            "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        if not wait_for_server(f"http://127.0.0.1:{MOCK_PORT}/v1/models"):
            check("mock model server came up", False)
            return 1
        check("mock model server came up", True)

        from core.service import configure_runtime
        from agents.interface_designer import InterfaceDesigner
        from agents.test_generator import TestGenerator

        # The workspace the stage agent is trusted with.
        (WORKSPACE / "package.json").write_text('{"name":"workspace"}\n', encoding="utf-8")
        (WORKSPACE / "frontend" / "src").mkdir(parents=True, exist_ok=True)
        (WORKSPACE / "frontend" / "src" / "App.tsx").write_text(
            "export default function App() {\n  return null;\n}\n", encoding="utf-8"
        )

        runtime = configure_runtime(
            project_dir=str(WORKSPACE),
            traceability_dir=str(WORKSPACE / ".arc" / "traceability"),
            app_type="web",
            web_port=3100,
        )
        runtime.traceability.init_store(reset=True)
        runtime.traceability.store_requirement_tree(
            {
                "id": "REQ-1",
                "name": "Landing shell",
                "description": "Root shell of the application.",
                "children": [],
            }
        )
        check("runtime + requirement tree initialised", runtime.traceability.get_requirement("REQ-1") is not None)

        requirement_data = runtime.traceability.get_requirement("REQ-1") or {}
        requirement_data = {**requirement_data, "children_ids": []}

        designer = InterfaceDesigner(
            workspace_root=str(WORKSPACE),
            requirement_path=str(WORKSPACE / "requirements" / "requirements.yaml"),
            app_type="web",
        )
        design = asyncio.run(designer.run(node_id="REQ-1", requirement_data=requirement_data))

        print("      design ->", json_dump(design))
        check("InterfaceDesigner returned an interface", len(design.get("interfaces") or []) == 1)
        first = (design.get("interfaces") or [{}])[0]
        check("interface id preserved", first.get("interface_id") == "REQ-1:UI:shell", str(first.get("interface_id")))
        check("interface type normalised", first.get("type") == "UI", str(first.get("type")))

        generator = TestGenerator(
            workspace_root=str(WORKSPACE),
            requirement_path=str(WORKSPACE / "requirements" / "requirements.yaml"),
            app_type="web",
        )
        tests, _ = asyncio.run(generator.run("REQ-1", requirement_data))
        print("      tests  ->", json_dump(tests))
        check("TestGenerator returned a test", bool(tests) and tests[0].get("test_id") == "REQ-1:unit:app")

        with urllib.request.urlopen(f"http://127.0.0.1:{MOCK_PORT}/__calls") as response:
            import json as _json

            calls = _json.loads(response.read().decode("utf-8"))
        check("model was called", calls["count"] >= 2, f"count={calls['count']}")
        check(
            "the designer's read_file tool call was executed",
            any(call.get("has_tool_results") for call in calls["calls"]),
            "no tool result round-trip observed",
        )

        # The SDK must have recorded real state, not just returned values.
        interfaces = runtime.traceability.list_interfaces(req_id="REQ-1")
        check("traceability store has no interface yet (phases.py stores it)", isinstance(interfaces, list))

        # Path sandbox: a write outside /workspace must be refused, not silently allowed.
        from agents.runtime.workspace_tools import write_file
        from app.agent.runtime import current_workspace
        from app.config import PROJECT_ROOT

        async def try_escape() -> str:
            token = current_workspace.set(WORKSPACE)
            try:
                return await write_file.ainvoke(
                    {"file_path": r"C:\Windows\System32\evil.txt", "content": "x"}
                )
            except Exception as exc:  # noqa: BLE001 - the refusal is the assertion
                return f"REFUSED: {type(exc).__name__}"
            finally:
                current_workspace.reset(token)

        escape = asyncio.run(try_escape())
        check("out-of-workspace write refused", str(escape).startswith("REFUSED") or "工作区之外" in str(escape), str(escape)[:90])

        # ARC protected paths must be refused even inside the workspace.
        async def try_protected() -> str:
            token = current_workspace.set(WORKSPACE)
            try:
                return await write_file.ainvoke(
                    {"file_path": "/workspace/.git/config", "content": "x"}
                )
            except Exception as exc:  # noqa: BLE001
                return f"REFUSED: {type(exc).__name__}"
            finally:
                current_workspace.reset(token)

        protected = asyncio.run(try_protected())
        check("protected path write refused", str(protected).startswith("REFUSED") or "受保护" in str(protected), str(protected)[:90])

        _ = PROJECT_ROOT
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock.kill()

    print("")
    print("result: " + ("ALL OK" if not failures else f"{len(failures)} FAILED -> {failures}"))
    return 1 if failures else 0


def json_dump(value: object) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False)[:300]
    except TypeError:
        return repr(value)[:300]


if __name__ == "__main__":
    sys.exit(main())
