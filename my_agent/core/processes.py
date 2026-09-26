from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import signal
import subprocess
from typing import Any, Awaitable, Callable


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


async def finalize_subprocess(process: Any, *, force_kill: bool = False) -> None:
    """停掉一条命令，**连同它下面那一整棵进程树**。

    只 terminate 直接子进程会漏掉真正在干活的那个：`_execute_web_test_command` 起的是
    `sh -c "npx vitest run ..."`，跑测试的是再往下几层的 `node`。实测一次单元测试超时
    就在机器上留下 10 个 `npx vitest` 孤儿进程——它们继续占端口、占文件句柄，
    后面几层测试跟着失败。所以这里整棵树一起收。

    直接子进程已经退出时（`returncode` 已定）不动手，避免误杀被复用的 pid。
    """

    if process is None or getattr(process, "returncode", None) is not None:
        return

    await asyncio.to_thread(_kill_process_tree, process, force_kill)

    try:
        await asyncio.wait_for(process.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


def _kill_process_tree(process: Any, force_kill: bool) -> None:
    """同步的树杀。动作很短（一次 taskkill 或一次 killpg），丢到线程里不占事件循环。"""

    pid = getattr(process, "pid", None)
    if not pid or pid <= 0:
        return

    if os.name == "nt":
        # taskkill /T 递归子进程树，/F 强制。terminate() 只发到 cmd.exe，够不着 npx/node。
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        except Exception:
            _safe_terminate(process, force_kill)
        return

    # POSIX：这些命令是以 start_new_session 起的（见 app_type_handler/web.py），
    # 子进程自成一个进程组，killpg 只打那一棵树，不会波及我们自己。
    try:
        group = os.getpgid(pid)
        if group == os.getpgid(0):
            # 不在自己的进程组里（有调用方没按约定起进程）：退回只杀直接子进程，
            # 绝不能 killpg——那会把自己这一组一起干掉。
            _safe_terminate(process, force_kill)
            return
        os.killpg(group, signal.SIGKILL if force_kill else signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        _safe_terminate(process, force_kill)


def _safe_terminate(process: Any, force_kill: bool) -> None:
    try:
        if force_kill:
            process.kill()
        else:
            process.terminate()
    except (ProcessLookupError, OSError):
        return


async def check_prerequisites(app_type: str, log_cb: LogCallback | None = None) -> bool:
    normalized = (app_type or "web").strip().lower()
    from app_type_handler import get_app_type_handler_class

    required = get_app_type_handler_class(normalized).prerequisite_commands()
    missing = [command for command in required if shutil.which(command) is None]
    if missing:
        await _emit_log(
            log_cb,
            "System",
            f"Missing required command(s) for app_type={normalized}: {', '.join(missing)}",
            status="error",
        )
        return False
    await _emit_log(log_cb, "System", f"Prerequisite check passed for app_type={normalized}.")
    return True


async def _emit_log(
    log_cb: LogCallback | None,
    agent_name: str,
    message: str,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    if log_cb is None:
        return
    result = log_cb(agent_name, message, status, node_id)
    if inspect.isawaitable(result):
        await result
