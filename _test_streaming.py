"""Verify the stage agent now streams, without changing the result.

Before: `runners.ainvoke_stage_agent` logged "agent streaming is unavailable; falling back
to ainvoke" and the whole stage ran as one blocking call -- no tool-call/tool-result lines
until everything finished, so a 4-7 minute stage looked identical to a hang.

After: `ArcStageAgent.astream_events` feeds the upstream streaming branch, which logs
`tool-call>` / `tool-result>` / `model-final>` live.

This test asserts both halves: the stream is actually used, AND the extracted payload is
unchanged (a streaming path that loses the final state would silently re-run the stage).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

REPO = Path(r"D:\project\hks\my_agent")
WORKSPACE = Path(r"D:\project\hks\_stream_test")
MOCK = "http://127.0.0.1:8931/v1"

sys.path.insert(0, str(REPO))

os.environ["LLM_API_KEY"] = "mock-key"
os.environ["LLM_BASE_URL"] = MOCK
os.environ["LLM_MODEL"] = "mock-model"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + label + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    try:
        with urllib.request.urlopen(f"{MOCK}/models", timeout=5):
            pass
    except Exception as exc:
        print(f"mock model server not reachable at {MOCK}: {exc}")
        return 1

    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE, ignore_errors=True)
    (WORKSPACE / "package.json").parent.mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "package.json").write_text('{"name":"ws"}\n', encoding="utf-8")
    (WORKSPACE / "frontend" / "src").mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "frontend" / "src" / "App.tsx").write_text("export default function App(){return null}\n", encoding="utf-8")

    from core.service import configure_runtime
    from agents.interface_designer import InterfaceDesigner

    runtime = configure_runtime(
        project_dir=str(WORKSPACE),
        traceability_dir=str(WORKSPACE / ".arc" / "traceability"),
        app_type="web",
        web_port=3301,
    )
    runtime.traceability.init_store(reset=True)
    runtime.traceability.store_requirement_tree(
        {"id": "REQ-1", "name": "shell", "description": "Root shell.", "children": []}
    )

    logs: list[str] = []

    def log_cb(agent: str, message: str, status=None, node_id=None):
        logs.append(f"{agent}|{status or ''}|{message}")
        return None

    designer = InterfaceDesigner(
        log_cb=log_cb,
        workspace_root=str(WORKSPACE),
        requirement_path=str(WORKSPACE / "requirements" / "requirements.yaml"),
        app_type="web",
    )
    requirement = runtime.traceability.get_requirement("REQ-1") or {}
    requirement = {**requirement, "children_ids": []}

    result = asyncio.run(designer.run(node_id="REQ-1", requirement_data=requirement))

    joined = "\n".join(logs)

    print("=== streaming path ===")
    check("agent stream start was logged", "agent stream start." in joined)
    check("did NOT fall back to ainvoke", "falling back to ainvoke" not in joined)
    check("did NOT report a stream fallback warning",
          "agent stream ended without final state" not in joined)

    print()
    print("=== live progress lines (the point of the change) ===")
    tool_calls = [line for line in logs if "tool-call>" in line]
    tool_results = [line for line in logs if "tool-result>" in line]
    model_final = [line for line in logs if "model-final>" in line]
    print(f"  tool-call>   lines: {len(tool_calls)}")
    print(f"  tool-result> lines: {len(tool_results)}")
    print(f"  model-final> lines: {len(model_final)}")
    check("tool calls were logged in flight", len(tool_calls) >= 1)
    check("tool results were logged in flight", len(tool_results) >= 1)
    if tool_calls:
        print("  first:", tool_calls[0][:150])

    print()
    print("=== result unchanged ===")
    interfaces = result.get("interfaces") or []
    print(f"  interfaces: {json.dumps(interfaces, ensure_ascii=False)[:160]}")
    check("payload still parsed from the stream", len(interfaces) == 1,
          f"got {len(interfaces)}")
    if interfaces:
        check("interface id preserved", interfaces[0].get("interface_id") == "REQ-1:UI:shell",
              str(interfaces[0].get("interface_id")))
        check("interface type normalised", interfaces[0].get("type") == "UI")

    # A streaming path that loses the final state re-runs the stage via ainvoke. The mock
    # counts its calls, so a doubled count reveals the silent re-run.
    with urllib.request.urlopen("http://127.0.0.1:8931/__calls") as response:
        calls = json.loads(response.read().decode("utf-8"))
    model_calls = calls["count"]
    tool_round_trips = sum(1 for c in calls["calls"] if c.get("has_tool_results"))
    print()
    print(f"  model calls recorded by the mock: {model_calls}")
    print(f"  calls that carried a tool result: {tool_round_trips}")
    check("stage ran exactly once (no silent re-run)", model_calls <= 3,
          f"{model_calls} model calls")

    print()
    print("result: " + ("ALL OK" if not failures else f"{len(failures)} FAILED -> {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
