"""Verify the committed delivery package matches the code it is supposed to ship.

Run from the repo root, after `python _package.py`:

    python tools/verify_package.py

Why this exists: the zip is committed alongside the code, so it goes stale the moment anyone
edits `my_agent/` without re-packing. That happened repeatedly during this project, and the only
thing that caught it was someone remembering to check. This makes it one command.

It asserts the STRONGEST form available on this machine:
  1. every entry in the zip byte-matches the working tree (the real invariant)
  2. the zip is byte-identical to the one committed at HEAD, when the working tree is clean
  3. a list of named fixes is actually present (a readable summary, not the guarantee)

Exit code 0 = the package is current; 1 = it is not.
"""

import hashlib
import io
import subprocess
import sys
import zipfile
from pathlib import Path

ZIP = "my_agent-arcbench-submission.zip"
ROOT = Path("my_agent")

# 可读的摘要：每个 issue 的修复是否在包里。真正的保证是下面的逐条字节比对。
NAMED_FIXES = [
    ("#14 超时返回 Exit Code: 124", "app_type_handler/web.py", "Exit Code: 124"),
    ("#21 超时可配 ARC_TEST_COMMAND_TIMEOUT", "app_type_handler/web.py", "ARC_TEST_COMMAND_TIMEOUT"),
    ("#24 runner 缺失时秒级失败", "app_type_handler/web.py", "_missing_runner_message"),
    ("#26 安装后校验依赖真的装上了", "app_type_handler/web.py", "_missing_declared_dependencies"),
    ("#29 安装显式要 dev 依赖", "app_type_handler/web.py", "--include=dev"),
    ("#33 层通过后拒绝重跑", "core/phases.py", "prior_exit_code == 0:"),
    ("#36 检索默认跳过 .arc/", "app/agent/tools/common.py", '".arc"'),
    ("#44 被拦写入解锁该路径", "agents/runtime/stage_policy.py", "def note_blocked"),
    ("#44 runner 真的调用了它", "agents/runtime/stage_policy.py", "discipline.note_blocked("),
    ("#46 空 session 重试本层", "core/phases.py", "max_empty_sessions"),
    ("#49 套件加载失败可区分", "app_type_handler/test_results.py", "describe_suite_load_failure"),
    ("#49 回执里真的带上了", "app_type_handler/web.py", "describe_suite_load_failure("),
    ("#11 上下文窗口默认 1M", "app/config/__init__.py", "1_000_000"),
]

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


if not Path(ZIP).is_file():
    sys.exit(f"{ZIP} does not exist - run `python _package.py` first")

z = zipfile.ZipFile(ZIP)
names = z.namelist()
print(f"package : {ZIP}")
print(f"entries : {len(names)}")
print()

print("--- the zip matches the working tree ---")
missing = [n for n in names if not (ROOT / n).is_file()]
differs = [
    n
    for n in names
    if (ROOT / n).is_file()
    and hashlib.sha256(z.read(n)).hexdigest() != hashlib.sha256((ROOT / n).read_bytes()).hexdigest()
]
check("every entry exists on disk", not missing, f"missing {missing[:5]}")
check("every entry byte-matches the tree", not differs, f"differs {differs[:5]}")

print()
print("--- named fixes are present ---")
for label, path, needle in NAMED_FIXES:
    try:
        text = z.read(path).decode("utf-8", "replace")
    except KeyError:
        check(label, False, f"{path} not in the zip")
        continue
    check(label, needle in text)

print()
print("--- the committed zip at HEAD ---")
head = subprocess.run(["git", "show", f"HEAD:{ZIP}"], capture_output=True).stdout
local = Path(ZIP).read_bytes()
if not head:
    check("HEAD has the zip committed", False, "not in git - run `git add` + commit")
else:
    if head == local:
        check("the working zip is byte-identical to HEAD", True)
    else:
        hz = zipfile.ZipFile(io.BytesIO(head))
        hn = {i.filename: hashlib.sha256(hz.read(i)).hexdigest() for i in hz.infolist()}
        ln = {i.filename: hashlib.sha256(z.read(i)).hexdigest() for i in z.infolist()}
        entry_diff = sorted(k for k in set(hn) & set(ln) if hn[k] != ln[k])
        check(
            "the committed zip has the same CONTENT as the freshly built one",
            not entry_diff and set(hn) == set(ln),
            f"{len(entry_diff)} differing entries - commit the rebuilt zip"
            if entry_diff
            else "same content, different container bytes (compression only)",
        )

print()
print("result: " + ("ALL OK - the package is current" if not failures else f"{len(failures)} FAILED -> {failures}"))
sys.exit(1 if failures else 0)
