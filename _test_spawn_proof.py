"""Sanity check: confirm the sentinel command really does create a grandchild.

Without this, the tree-kill test could pass vacuously (nothing spawned, nothing to leak).
Also shows the OLD behaviour leaked, so the fix is demonstrably doing something.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

SENTINEL = "setTimeout(()=>{},120000)"
SPAWN_SCRIPT = (
    "const{spawn}=require('child_process');"
    f"const c=spawn(process.execPath,['-e','{SENTINEL}'],{{stdio:'ignore'}});"
    "console.log('spawned',c.pid);"
    f"{SENTINEL};"
)


def sentinel_procs() -> list[int]:
    out = subprocess.run(
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
    raw = (out.stdout or "").strip()
    if not raw:
        return []
    if raw.startswith("{"):
        raw = f"[{raw}]"
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [
        int(row["ProcessId"])
        for row in rows
        if SENTINEL in str(row.get("CommandLine") or "")
    ]


async def main() -> int:
    print("1) starting the spawning command raw (no tree-kill), to prove it spawns")
    process = await asyncio.create_subprocess_shell(
        f'node -e "{SPAWN_SCRIPT}"',
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    await asyncio.sleep(4)
    alive = sentinel_procs()
    print(f"   sentinel processes while running: {len(alive)} -> {alive}")
    spawned_ok = len(alive) >= 2  # outer node + the grandchild it spawned
    print(f"   grandchild really spawned: {spawned_ok}")

    print("2) killing only the direct child (the OLD behaviour)")
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()
    await asyncio.sleep(3)
    leaked = sentinel_procs()
    print(f"   sentinel processes after direct-child kill only: {len(leaked)} -> {leaked}")
    print(f"   old behaviour leaked: {len(leaked) > 0}")

    print("3) cleaning up whatever leaked")
    for pid in leaked:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    await asyncio.sleep(2)
    remaining = sentinel_procs()
    print(f"   remaining after cleanup: {len(remaining)}")

    ok = spawned_ok and len(leaked) > 0 and not remaining
    print("")
    print("result: " + ("CONFIRMED (spawn happens, old path leaks, cleanup works)" if ok else "INCONCLUSIVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
