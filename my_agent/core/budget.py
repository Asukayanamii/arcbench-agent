"""本轮编译的用量账本：累计 token + 本进程内存。

两件事共用这一处，因为它们都只需要**一个**记账点 —— 每次模型调用结束时的
`runners._usage_line`：

* **token 预算软停止**：平台按 token 计量（`initial_budget_cny` 那个共享池），而被 SIGKILL
  的那两次是 210M / 294M token、评测根本没开始（`passed_count 0`）→ 0 分。到预算就不再接
  新节点、把当前节点收尾后**正常退出**，让已生成的部分还能被评（见
  SCORING_AND_OPTIMIZATION.md §4.20）。
* **内存**：那两次的日志尾部都是正常干活、没有异常，时长还差 80 分钟（7h44m vs 9h04m）——
  最像被按内存杀掉。把 RSS 跟在每次调用的用量行后面，下一轮日志就能直接看出内存是平的
  还是无界增长，不用猜。
"""

from __future__ import annotations

import os
import sys

_spent_tokens = 0


def note_tokens(input_tokens: int, output_tokens: int) -> None:
    """记一次模型调用的用量。平台计的是"所有调用之和"，所以输入输出都算上。"""

    global _spent_tokens
    _spent_tokens += max(0, int(input_tokens or 0)) + max(0, int(output_tokens or 0))


def spent_tokens() -> int:
    return _spent_tokens


def reset() -> None:
    """把账本清零（测试用：同一个进程里要反复起预算场景）。"""

    global _spent_tokens
    _spent_tokens = 0


def budget_tokens() -> int:
    from app.config import TOKEN_BUDGET

    return int(TOKEN_BUDGET)


def exceeded() -> bool:
    limit = budget_tokens()
    return bool(limit) and _spent_tokens >= limit


def stop_message() -> str:
    return (
        f"Token budget reached: {_spent_tokens:,} >= {budget_tokens():,} tokens "
        "(ARC_TOKEN_BUDGET). Stopping before the next node so this run finishes normally "
        "and the generated work can still be evaluated, instead of being killed."
    )


def rss_bytes() -> int | None:
    """本进程常驻内存；取不到就返回 None（不编造数字）。"""

    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/statm", "r", encoding="ascii") as handle:
                resident_pages = int(handle.read().split()[1])
            return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError, IndexError):
            return None
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            # 必须显式声明签名：默认 restype 是 c_int，`GetCurrentProcess()` 的伪句柄 (-1)
            # 会被当成无效句柄传下去，调用直接失败返回 0（实测踩到，静默变成"取不到"）。
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_ProcessMemoryCounters),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            handle = kernel32.GetCurrentProcess()
            ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
            return int(counters.WorkingSetSize) if ok else None
        except Exception:  # noqa: BLE001 - 诊断信息，取不到就算了
            return None
    return None


def rss_human() -> str:
    value = rss_bytes()
    if value is None:
        return ""
    return f"{value / (1024 ** 3):.2f}GB"
