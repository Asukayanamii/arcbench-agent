"""Verify that a timed-out system command takes its whole process tree with it.

Regression test for the orphan leak: `_execute_web_test_command` spawns
`sh -c "npx vitest ..."`, and `npx` spawns the real `node` runner. Killing only the
direct child left the grandchildren running (10 orphaned `npx vitest` after one run),
which then holds ports and file handles and breaks later test layers.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(r"D:\project\hks\my_agent")
sys.path.insert(0, str(REPO))

os.environ["ARC_WEB_PORT"] = "3399"
os.environ["ARC_APP_TYPE"] = "web"

from app_type_handler.web import _execute_web_test_command  # noqa: E402

# A command whose real work happens one level below the shell: the outer node
# stays alive while it spawns an inner node that also stays alive.
SPAWN_SCRIPT = (
    "const{spawn}=require('child_process');"
    "const c=spawn(process.execPath,['-e','setTimeout(()=>{},120000)'],{stdio:'ignore'});"
    "console.log('spawned',c.pid);"
    "setTimeout(()=>{},120000);"
)


def node_children() -> list[tuple[int, str]]:
    """Every node.exe whose command line mentions the sentinel marker."""

    output = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    raw = (output.stdout or "").strip()
    if not raw:
        return []
    if raw.startswith("{"):
        raw = f"[{raw}]"
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return []
    found: list[tuple[int, str]] = []
    for row in rows:
        command = str(row.get("CommandLine") or "")
        if "setTimeout(()=>{},120000)" in command:
            found.append((int(row.get("ProcessId")), command))
    return found


async def main() -> int:
    before = node_children()
    print(f"node processes matching the sentinel before: {len(before)}")

    result = await _execute_web_test_command(
        f'node -e "{SPAWN_SCRIPT}"',
        cwd=str(REPO),
        timeout=6.0,
    )
    print("command result head:", result.splitlines()[0] if result else "(empty)")
    print("timed out as expected:", "timed out" in result.lower())

    await asyncio.sleep(3)

    after = node_children()
    pids = ", ".join(str(pid) for pid, _ in after) or "none"
    print(f"node processes matching the sentinel after: {len(after)}  ({pids})")

    leaked = len(after)
    print("")
    print("result: " + ("NO LEAK" if leaked == 0 else f"LEAKED {leaked} PROCESS(ES)"))
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
