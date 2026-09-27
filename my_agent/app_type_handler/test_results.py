from __future__ import annotations

import re
from typing import Any


def parse_test_results(test_output: str) -> dict[str, Any]:
    """Parse ARC test-run output into a compact status structure."""

    result: dict[str, Any] = {"passed": [], "failed": [], "exit_code": -1, "sub_batches": []}
    output = test_output or ""
    for line in output.splitlines():
        if "Exit Code:" not in line:
            continue
        try:
            result["exit_code"] = int(line.split("Exit Code:", 1)[1].strip())
        except ValueError:
            result["exit_code"] = -1
        break

    test_file_sections = re.findall(
        r"Test File:\s*(.+?)\r?\nTest Results:\r?\n(.*?)(?=\r?\nTest File: |\Z)",
        output,
        re.DOTALL,
    )
    for file_path, raw_section in test_file_sections:
        result["sub_batches"].append(
            {
                "requested_files": [file_path.strip().replace("\\", "/")],
                "exit_code": _extract_exit_code(raw_section),
                "raw_output": raw_section.strip(),
            }
        )

    if not result["sub_batches"]:
        requested_files = [
            line.split("-", 1)[1].strip().replace("\\", "/")
            for line in output.splitlines()
            if line.startswith("- ")
        ]
        for label in ("Backend Vitest Batch", "Frontend Vitest Batch", "Playwright E2E Batch"):
            section = _extract_labeled_section(output, label)
            if not section:
                continue
            result["sub_batches"].append(
                {
                    "requested_files": requested_files,
                    "exit_code": _extract_exit_code(section),
                    "raw_output": section.strip(),
                }
            )

    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("PASS ", "✓", "√", "✔")):
            result["passed"].append(stripped)
        elif stripped.startswith(("FAIL ", "✗", "×", "✕")) or " FAILED" in stripped:
            result["failed"].append(stripped)
    return result


SUITE_LOAD_FAILURE_MARKERS = (
    "Failed to resolve import",
    "Failed Suites",
    "No test files found",
    "Failed to load",
)


def describe_suite_load_failure(output: str) -> str:
    """套件**没加载起来**时返回一句可执行的说明；否则返回空串。

    必须与「测试跑了但断言失败」区分开 —— 两者在回执里长得一样
    （都是 `Test Files 1 failed` + `FAIL <file>`），而该修的东西**正好相反**：

    * 加载失败 → 修**测试文件**的导入/配置
    * 断言失败 → 修**实现**

    实测（`run-logs/2026-09-27-competition-sheet-log4`）：生成的测试里相对导入多了一层
    `../`，报 `Failed to resolve import "../../src/components/workbook/..."` → 套件加载失败
    → `(0 test)` → Unit 层 5 次预算全败，agent 一直在修实现，**一次都没碰那行 import**。
    而提示词其实要求它就地修生成测试的缺陷 —— 信号不区分，它就无从知道该改哪边。

    与 #14（超时被当成没失败）、#21（内层 124 被外层 1 掩盖）是同一类病。
    """

    text = output or ""
    if not any(marker in text for marker in SUITE_LOAD_FAILURE_MARKERS):
        return ""

    detail = ""
    importer = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Error:"):
            detail = stripped[:300]
        if "Failed to resolve import" in stripped:
            # `Failed to resolve import "<spec>" from "<importer>"` —— 把出问题的那个文件
            # 抠出来，好在下面说清「相对路径是相对它自己所在目录算的」。
            match = re.search(r'from\s+"([^"]+)"', stripped)
            if match:
                importer = match.group(1).strip()
            if not detail:
                detail = stripped[:300]
        if detail and importer:
            break

    hint = ""
    if importer or "Failed to resolve import" in text:
        hint = (
            "\nRelative imports resolve from the importing file's OWN directory, so re-check the "
            "`../` count against the layout"
            + (f" (`{importer}` lives under the test directory, while the module it needs is usually "
               "a sibling of that directory)" if importer else "")
            + ". A generated test very often gets this one level wrong."
        )

    return (
        "The test suite failed to LOAD - zero tests were executed, so this is NOT an assertion "
        "failure and the implementation has not been disproved. Fix the TEST FILE (imports, paths, "
        "or test configuration) before touching the implementation."
        + (f"\nFirst error: {detail}" if detail else "")
        + hint
    )


def _extract_exit_code(output: str) -> int:
    for line in (output or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("Exit Code:"):
            continue
        try:
            return int(stripped.split("Exit Code:", 1)[1].strip())
        except ValueError:
            return -1
    return -1


def _extract_labeled_section(output: str, label: str) -> str:
    pattern = rf"=== {re.escape(label)} ===\r?\n(.*?)(?=\r?\n=== |\Z)"
    match = re.search(pattern, output or "", re.DOTALL)
    return match.group(1).strip() if match else ""
