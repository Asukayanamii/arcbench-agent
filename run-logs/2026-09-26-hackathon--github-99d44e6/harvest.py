"""Harvest one bounded judge run: accurate event accounting, no regex-by-eyeball.

Previous pass used the pattern `run_tests (Unit|...)` which silently matched nothing,
because the real log text is "`run_tests` Unit passed with Exit Code: ...". That produced a
false "0 successes" reading. Every pattern here is checked against a matched sample so a
silent zero can never be mistaken for a finding again.
"""

import collections
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUN_DIR = REPO / "work" / "judge-hackathon--github-99d44e6"
LOG = RUN_DIR / ".arc" / "debug.log"

lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
print(f"log        : {LOG}")
print(f"lines      : {len(lines):,}")


def count(pattern: str, label: str, *, sample: bool = True) -> int:
    rx = re.compile(pattern)
    hits = [ln for ln in lines if rx.search(ln)]
    print(f"  {label:<34} {len(hits):>4}")
    if sample and hits:
        text = hits[0].strip()
        print(f"      e.g. {text[:150]}")
    return len(hits)


print("\n=== run_tests 调用与结果 ===")
calls = count(r"tool-call> run_tests", "工具调用")
count(r"`run_tests` (Unit|Integration|E2E) passed with Exit Code:", "per-call 判定 passed")
count(r"`run_tests` (Unit|Integration|E2E) failed with Exit Code:", "per-call 判定 failed")

print("\n=== 每次 run_tests 回执里的退出码分布 ===")
codes: collections.Counter = collections.Counter()
for ln in lines:
    if "tool-result> run_tests" not in ln:
        continue
    found = re.findall(r"Exit Code:\s*(-?\d+)", ln)
    if found:
        codes[found[0]] += 1
    elif "timed out after" in ln:
        codes["<timeout, no exit code>"] += 1
    else:
        codes["<no exit code at all>"] += 1
for code, n in codes.most_common():
    print(f"  Exit Code {code:<26} {n}")

print("\n=== 超时 ===")
count(r"timed out after 60\.0 seconds", "超时日志行（含重复记录）")
timeouts = {ln.split("]")[0] + "]" for ln in lines if "timed out after 60.0 seconds" in ln}
print(f"  {'去重后的超时时刻':<34} {len(timeouts):>4}")

print("\n=== 修复验证 ===")
count(r"agent stream stopped early", "session 早停（#13 修复）")
count(r"TDD batch", "TDD 层判定（此前永远为 0）")
count(r"ARC stage discipline", "守卫拦截")
count(r"Exit Code: 124", "超时被分类为 124（#14 修复）")
count(r"insufficient_balance|status_code=402", "支付错误")

print("\n=== TDD 判定明细 ===")
for ln in lines:
    if "TDD batch" in ln:
        print("  " + ln.strip()[:170])

print("\n=== 交付侧状态 ===")
queue = RUN_DIR / "processing_queue.json"
if queue.is_file():
    data = json.loads(queue.read_text(encoding="utf-8"))
    nodes = data if isinstance(data, list) else data.get("nodes", [])
    state: collections.Counter = collections.Counter()
    for node in nodes if isinstance(nodes, list) else []:
        state[str((node or {}).get("status", "?"))] += 1
    print(f"  节点状态: {dict(state)}")
else:
    print("  processing_queue.json 缺失")
