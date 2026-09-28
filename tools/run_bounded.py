"""Bounded local judge run: start the agent, stop after N TDD verdicts or a wall-clock cap.

Why a watchdog instead of a flag: main.py has no "stop after N nodes" option. `--rerun-tdd`
can target one leaf node but needs an already-populated workspace. So we start a normal run
and stop it once enough leaf nodes have produced a real TDD verdict - which is the signal
that costs money and produces findings.

Usage: python work/run_bounded.py <task-dir-name> [max_tdd_verdicts] [max_minutes]
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MY_AGENT = REPO / "my_agent"
PYTHON = sys.executable
sys.path.insert(0, str(MY_AGENT))

from arc_main import _rmtree_force  # noqa: E402 - 需要先有上面的 sys.path

VERDICT = re.compile(r"TDD batch [`']([^`']+)[`'] (passed|did not pass)")
TS = re.compile(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)")

task = sys.argv[1] if len(sys.argv) > 1 else "hackathon--github"
max_verdicts = int(sys.argv[2]) if len(sys.argv) > 2 else 3
max_minutes = float(sys.argv[3]) if len(sys.argv) > 3 else 90.0

commit = subprocess.run(
    ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True
).stdout.strip()
dirty = bool(
    subprocess.run(
        ["git", "status", "--short"], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()
)

stamp = datetime.now().strftime("%Y-%m-%d")
out_dir = REPO / "work" / f"judge-{task}-{commit}"
log_dir = REPO / "run-logs" / f"{stamp}-{task}-{commit}"
log_path = out_dir / ".arc" / "debug.log"

print(f"task        : {task}")
print(f"commit      : {commit}{'  (WORKING TREE DIRTY)' if dirty else ''}")
print(f"output dir  : {out_dir}")
print(f"stop after  : {max_verdicts} TDD verdicts or {max_minutes:.0f} min")
print(flush=True)

if out_dir.exists():
    # 同一个 commit 会复用同一个 out_dir，所以这里必须先删干净。
    #
    # ⚠️ 不能用 `shutil.rmtree(..., ignore_errors=True)`：Windows 上 `.git/objects/*`
    # 带只读属性，默认的 rmtree 删不掉，而 ignore_errors=True 会把失败**吞掉**，
    # 于是留下一个残缺的 `.git`。下一轮的 `ensure_repo` 只看「.git 存不存在」，
    # 会跳过 `git init`，之后所有 git 命令**静默落到宿主仓库**（`work/` 在宿主的
    # .gitignore 里）—— `git add .` 直接抛 "paths are ignored ... work"，
    # 运行在初始化阶段就死（实测 2026-09-28 第二轮，17 秒）。
    # 复用 `arc_main._rmtree_force`：它清只读位，而且 `check_clean_survives_readonly_git_objects`
    # 守着它（#12）。
    _rmtree_force(out_dir)

env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
started = time.time()
# stdout 写文件而不是 PIPE：没人读的 PIPE 会被 64KB 缓冲填满，**子进程会卡死在写 stdout 上**。
#
# 关键：这个文件必须放在**输出工作区之外**。第一版放在 out_dir 里，结果 agent 的 grep
# 直接命中它（日志里出现 `runner-stdout.txt:7297: ...`），于是它开始读我们自己的 stdout 转储 ——
# 既污染搜索结果，又白烧上下文。工作区是 agent 的 /workspace，往里丢宿主的诊断文件就是给它添噪音。
out_dir.mkdir(parents=True, exist_ok=True)
stdout_path = out_dir.parent / f"{out_dir.name}-runner-stdout.txt"
proc = subprocess.Popen(
    [
        PYTHON, "-u", "main.py",
        str(REPO / "_arcbench_tasks" / task),
        "-o", str(out_dir),
        "--port", "3411",
    ],
    cwd=str(MY_AGENT),
    env=env,
    stdout=stdout_path.open("w", encoding="utf-8", errors="replace"),
    stderr=subprocess.STDOUT,
)

verdicts: list[str] = []
stop_reason = ""
tail: list[str] = []

while True:
    # 不要用 stdout=PIPE + readline()：那是阻塞的，main.py 长时间不打印时
    # 日志轮询根本轮不到执行 —— 这正是第一版失效的另一种形态。
    if proc.poll() is not None:
        stop_reason = f"process exited with code {proc.returncode}"
        break

    # 判定行写在 .arc/debug.log 里，**不在 stdout**。第一版读 stdout，于是判定计数
    # 从来没生效过，每轮都要等进程自行结束或撞 90 分钟上限（第 7 轮因此跑了 50 分钟，
    # 而 3 条判定早在 17:20 就凑齐了）。改成纯轮询日志文件。
    if log_path.is_file():
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        found = VERDICT.findall(text)
        if len(found) > len(verdicts):
            for test_type, outcome in found[len(verdicts):]:
                verdicts.append(f"{test_type}: {outcome}")
                print(
                    f"  [{time.time() - started:6.1f}s] TDD verdict #{len(verdicts)}: {verdicts[-1]}",
                    flush=True,
                )
            if len(verdicts) >= max_verdicts:
                stop_reason = f"reached {len(verdicts)} TDD verdicts"
                break

    if time.time() - started > max_minutes * 60:
        stop_reason = f"hit the {max_minutes:.0f} min cap with {len(verdicts)} verdicts"
        break

    time.sleep(5)

elapsed = time.time() - started
if proc.poll() is None:
    subprocess.run(
        ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
        capture_output=True,
    )
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()

print(f"\nstopped     : {stop_reason}")
print(f"elapsed     : {elapsed / 60:.1f} min")
print(f"verdicts    : {len(verdicts)}")
for v in verdicts:
    print(f"  - {v}")

log_dir.mkdir(parents=True, exist_ok=True)
copied = []
# 注意：processing_queue.json / node_sessions / traceability 都在输出目录的 **.arc/** 下，
# 不在根目录。第一版去根目录找，结果每轮自动归档都是空跑、只能手工补一次。
arc_dir = out_dir / ".arc"
if log_path.is_file():
    shutil.copy2(log_path, log_dir / "debug.log")
    copied.append(("debug.log", (log_dir / "debug.log").stat().st_size))
for name in ("processing_queue.json", "project.json", "runner-events.jsonl"):
    src = arc_dir / name
    if src.is_file():
        shutil.copy2(src, log_dir / name)
        copied.append((name, src.stat().st_size))
for sub in ("node_sessions", "traceability", "screenshots"):
    src = arc_dir / sub
    if src.is_dir():
        shutil.copytree(src, log_dir / sub, dirs_exist_ok=True)
        copied.append((sub + "/", sum(f.stat().st_size for f in src.rglob("*") if f.is_file())))

print(f"\narchived to : {log_dir}")
for name, size in copied:
    print(f"  {name:<24} {size:,} bytes")

state = {}
queue_path = arc_dir / "processing_queue.json"
if queue_path.is_file():
    try:
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        # node_states 是 {node_id: {...}} 的字典，键里那个字段叫 state（不是 status）。
        node_states = queue.get("node_states") if isinstance(queue, dict) else None
        if isinstance(node_states, dict):
            for entry in node_states.values():
                key = str((entry or {}).get("state", "?") or "?")
                state[key] = state.get(key, 0) + 1
            tasks = queue.get("tasks")
            print(f"tasks       : {len(tasks) if isinstance(tasks, list) else '?'}")
        else:
            print("  (queue has no node_states dict)")
    except Exception as exc:  # noqa: BLE001
        print(f"  (queue unreadable: {exc})")
print(f"node states : {state}")
print(f"\nNOTE: pass rate is NOT measurable locally - competition tests are 404 until submission.")
print(f"      This run only tells us whether the pipeline compiles, what it costs, and where it breaks.")
