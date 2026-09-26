"""Archive one judge run into run-logs/.

Usage: python work/archive.py <output-dir-name> <log-dir-name>

Everything the platform-side report needs lives under <out>/.arc/ - not at the run root.
That mistake made the first watchdog archive silently empty.
"""

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
src = REPO / "work" / sys.argv[1]
dst = REPO / "run-logs" / sys.argv[2]
arc = src / ".arc"

if not arc.is_dir():
    sys.exit(f"no .arc directory under {src}")

dst.mkdir(parents=True, exist_ok=True)
copied: list[tuple[str, int]] = []
for name in ("debug.log", "processing_queue.json", "project.json", "runner-events.jsonl"):
    if (arc / name).is_file():
        shutil.copy2(arc / name, dst / name)
        copied.append((name, (dst / name).stat().st_size))
for sub in ("node_sessions", "traceability", "screenshots"):
    if (arc / sub).is_dir():
        shutil.copytree(arc / sub, dst / sub, dirs_exist_ok=True)
        copied.append(
            (sub + "/", sum(f.stat().st_size for f in (dst / sub).rglob("*") if f.is_file()))
        )

print(f"archived {src.name} -> {dst}")
for name, size in copied:
    print(f"  {name:<26} {size:>12,} bytes")
print(f"  {'TOTAL':<26} {sum(s for _, s in copied):>12,} bytes")
print(f"  files                      {len(list(dst.rglob('*')))}")
