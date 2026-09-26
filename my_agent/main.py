"""ARC-Bench 提交入口。

平台按同一套固定 CLI 形状启动每一份上传的 agent：

    python3 /workspace/submission/main.py <requirements 目录> --output-dir <输出目录>

入口必须留在压缩包根目录（Upload Entry Contract：Python 用 `main.py` + `requirements.txt`）。
这个文件把平台的调用转成编译命令，再把平台的模型配置接到 my-agent 的配置上，
真正的编译流程在 `arc_main.py`。

**环境变量映射必须在导入任何 `app.*` 之前完成**：`app/config/__init__.py` 在导入时就把
`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` 读成模块级常量了，晚一步设置就读不到。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    # 平台不一定以包根为 cwd；把入口所在目录放进搜索路径，`app` / `agents` / `core` 才导入得到。
    sys.path.insert(0, str(_HERE))

# 提交包里必须存在的包目录与关键模块。
#
# 判据是"目录在不在"而不是"有没有 __init__.py"：`core/` 走的是隐式命名空间包
# （上游模板就没有给它 __init__.py），按 __init__.py 判会误报。
_REQUIRED_DIRS = (
    "app",
    "core",
    "agents",
    "app_type_handler",
    "arcbench_agent_runtime",
    "skills",
)
_REQUIRED_MODULES = (
    "arc_main.py",
    "agents/runtime/arc_agent.py",
    "agents/interface_designer.py",
    "app/agent/graph.py",
    "core/workflow.py",
    "core/files.py",
    "arc-template/templates/web-react-express/backend/package.json",
    "skills/leaf-full-design/SKILL.md",
)


def _preflight() -> None:
    """开跑之前先确认解压出来的目录结构是完整的。

    这一步存在的理由很具体：上传的 zip 曾经因为条目里没有 Unix 文件类型位
    （`external_attr=0`）被平台的解压器处理坏，表现是在 `import app_type_handler`
    那里抛 `ModuleNotFoundError`，日志里只有一行栈，看不出到底解压出了什么。
    现在先把清单打出来再退出，下一次任何结构性问题都能一眼定位。
    """

    missing_modules = [name for name in _REQUIRED_MODULES if not (_HERE / name).is_file()]
    missing_dirs = [name for name in _REQUIRED_DIRS if not (_HERE / name).is_dir()]

    if not missing_modules and not missing_dirs:
        return

    lines = [
        "=" * 72,
        "SUBMISSION LAYOUT PREFLIGHT FAILED",
        f"entrypoint dir : {_HERE}",
        f"cwd            : {os.getcwd()}",
        f"python         : {sys.version.split()[0]}",
        "",
        f"missing dirs    : {missing_dirs or 'none'}",
        f"missing modules : {missing_modules or 'none'}",
        "",
        "actual contents (depth 2):",
    ]
    base = len(_HERE.parts)
    for current, dirnames, filenames in os.walk(_HERE):
        current_path = Path(current)
        depth = len(current_path.parts) - base
        if depth >= 2:
            dirnames[:] = []
        indent = "  " * depth
        lines.append(f"{indent}{current_path.name or current_path}/")
        for name in sorted(filenames)[:20]:
            lines.append(f"{indent}  {name}")
        if len(filenames) > 20:
            lines.append(f"{indent}  ... {len(filenames) - 20} more")
    lines.append("=" * 72)

    # ARC runners expose stdout as the primary console stream. Keep stderr too
    # for local shell diagnostics, but ensure a failed upload is visible in the
    # platform's short output panel.
    message = "\n".join(lines)
    print(message, flush=True)
    print(message, file=sys.stderr, flush=True)
    raise SystemExit(2)


def _bridge_model_env() -> None:
    """把平台的模型配置接到 my-agent 的 LLM_* 上。

    平台下发的是 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL`（ARC 那一套命名），
    my-agent 读的是 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`。两边都认，
    显式设了 LLM_* 的时候以它为准，方便本地跑的时候直接写 .env。
    """

    for source, target in (
        ("OPENAI_API_KEY", "LLM_API_KEY"),
        ("OPENAI_BASE_URL", "LLM_BASE_URL"),
        ("MODEL", "LLM_MODEL"),
        ("ARCBENCH_MODEL", "LLM_MODEL"),
    ):
        value = os.environ.get(source, "").strip()
        if value and not os.environ.get(target, "").strip():
            os.environ[target] = value


def main() -> None:
    _preflight()
    _bridge_model_env()

    arguments = sys.argv[1:]
    if "--type" not in arguments:
        arguments.extend(["--type", os.environ.get("ARCBENCH_TASK_TYPE", "web").strip() or "web"])
    sys.argv = [sys.argv[0], "compile", *arguments]

    from arc_main import main as arc_main

    arc_main()


if __name__ == "__main__":
    main()
