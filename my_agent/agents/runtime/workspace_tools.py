"""ARC 文件工具面，实现在 my-agent 的工具与沙箱之上。

提示词、技能与阶段纪律都按 ARC 的既有名字（`read_file` / `write_file` / `edit_file` /
`glob` / `grep`）与虚拟根 `/workspace` 写死，所以这一层的职责只有两件事：

1. **改名与改口径**，不改行为：`read_file` 返回**不带行号**的原文（ARC 明确要求，带行号
   会被模型当成缩进抄进 `edit_file` 的锚点里），`glob` / `grep` 的参数名对齐 ARC 的 schema。
2. **补 ARC 的禁区**。my-agent 的沙箱只拦 `.env` / `.git` / `data`，ARC 另外要求把
   `.arc`、`requirements`、依赖目录、构建产物与 lockfile 一并挡住。两套规则叠加，
   取更严的那个。

写操作本身的落盘、换行风格保留、结构化差异都由 my-agent 的 `write` / `edit` 负责，
这里只做路径归一与准入判断，不重复实现。

**`/workspace` 是虚拟根**：模型会写 `/workspace/backend/src/app.js`，也会写
`backend/src/app.js`，两者都必须落到同一个真实文件上。`/skills/` 例外，它指向编译器
自己的工作区之外（见 `_SKILLS_PREFIX`）。
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from langchain_core.tools import tool

from app.agent.runtime import current_workspace
from app.agent.sandbox import READ, WRITE, guard_path
from app.agent.tools.common import MAX_LINES, fail, looks_binary, rel, truncate_head
from app.agent.tools.edit import edit as _edit
from app.agent.tools.grep import grep as _grep
from app.agent.tools.ls import ls as _ls
from app.agent.tools.write import write as _write
from app.exceptions import SandboxDenied
from core.path_compat import normalize_windows_extended_prefix_text

WORKSPACE_PREFIX = "/workspace"
SKILLS_PREFIX = "/skills"

_ARC_DENY = (
    ".arc",
    ".arc/*",
    ".arc/**",
    ".git",
    ".git/*",
    ".git/**",
    "requirements",
    "requirements/*",
    "requirements/**",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "**/node_modules",
    "**/node_modules/**",
    "**/dist",
    "**/dist/**",
    "**/dist-ssr",
    "**/dist-ssr/**",
    "**/build",
    "**/build/**",
    "**/coverage",
    "**/coverage/**",
    "**/.vite",
    "**/.vite/**",
    "**/package-lock.json",
    "**/yarn.lock",
    "**/pnpm-lock.yaml",
)

_MAX_READ_LIMIT = 200


def _workspace() -> Path:
    return current_workspace.get()


def _skills_root() -> Path | None:
    """ARC 技能库根目录 = **提交包里的 `skills/`**。

    必须和 `agents/skills/selection.py::available_skill_names` 用同一个根，否则会出现
    "挑中了技能、却读不到正文"：那边用包内 `skills/` 挑，这边却曾经读 my-agent 的
    `SKILLS_DIR`（默认 `~/.my_agent/skills`），于是每个技能都报"文件不存在"，
    技能正文永远送不到模型手里。实测在平台上就是这样挂的。

    回退到 `SKILLS_DIR` 只为本地跑 my-agent 自己的聊天 agent 时仍然可用。
    """

    packaged = Path(__file__).resolve().parents[2] / "skills"
    if packaged.is_dir():
        return packaged

    from app.config import SKILLS_DIR, SKILLS_ENABLED

    if not SKILLS_ENABLED or not SKILLS_DIR:
        return None
    return Path(SKILLS_DIR).resolve()


def _strip_virtual_prefix(raw: str) -> str:
    """把 `/workspace/...` 还原成工作区相对路径；其余原样返回。"""

    text = normalize_windows_extended_prefix_text(raw).replace("\\", "/").strip()
    if text == WORKSPACE_PREFIX:
        return "."
    if text.startswith(f"{WORKSPACE_PREFIX}/"):
        return text[len(WORKSPACE_PREFIX) + 1 :]
    return text


def _workspace_relative_label(target: Path) -> str:
    try:
        return target.resolve().relative_to(_workspace().resolve()).as_posix()
    except ValueError:
        return target.as_posix()


def _denied_by_arc(target: Path) -> bool:
    """ARC 额外禁区判定。命中即拒，且与 my-agent 的硬阻断一样不可授权绕过。"""

    label = _workspace_relative_label(target)
    name = target.name
    return any(
        fnmatch(label, pattern) or fnmatch(name, pattern) for pattern in _ARC_DENY
    )


def _resolve(raw: str, mode: str) -> Path:
    """归一虚拟路径 → ARC 越界判定 → my-agent 沙箱守卫 → ARC 附加禁区。

    **越界在这一层就拒掉**，不能留给 `guard_path`：那是会话里的行为，工作区之外的路径它会
    `interrupt()` 弹一张授权卡片等人点。编译是无人值守的批处理，弹卡片等于把这一轮挂死。
    ARC 对 `/workspace` 之外本来就是"一律拒绝"，所以这里直接给一句明确的拒绝。
    """

    text = normalize_windows_extended_prefix_text(raw)
    if not text or text in {".", "./"}:
        text = "."
    stripped = _strip_virtual_prefix(text)

    workspace = _workspace().resolve()
    candidate = Path(stripped).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if resolved != workspace and workspace not in resolved.parents:
            fail(
                f"{text} 在工作区之外。ARC 编译只允许访问 /workspace 内的路径，"
                "该限制是硬性的。"
            )

    target = guard_path(stripped, mode)
    if _denied_by_arc(target):
        fail(
            f"{rel(target)} 属于 ARC 的受保护路径，不允许"
            f"{'写入' if mode == WRITE else '读取'}。"
            "该限制是硬性的，无法通过授权绕过。"
        )
    return target


def _skill_file(raw: str) -> Path | None:
    """`/skills/<名字>/SKILL.md` 的只读放行。返回 None 表示不是技能路径。"""

    text = normalize_windows_extended_prefix_text(raw).replace("\\", "/").strip()
    if not text.startswith(f"{SKILLS_PREFIX}/"):
        return None
    root = _skills_root()
    if root is None:
        return None
    relative = text[len(SKILLS_PREFIX) + 1 :].strip("/")
    if not relative:
        return None
    candidate = (root / relative).resolve()
    if root != candidate and root not in candidate.parents:
        fail(f"{text} 越出了技能库根目录。")
    return candidate


@tool
async def read_file(file_path: str, offset: int | None = None, limit: int | None = None) -> str:
    """读取工作区内的文本文件，返回**不带行号**的原文。

    - offset 是**要跳过的行数**（从 0 开始，省略即从头读），不是行号
    - limit 是最多读取的行数（默认 100，上限 200）
    - 只读 `/workspace` 内的文件；`/skills/<名字>/SKILL.md` 是唯一例外
    - 超出范围时只保留开头，并说明下次该用什么 offset 接着读
    - 二进制文件会被拒绝
    - 返回的是原文，**不要**把行号写进 `edit_file` 的匹配文本里
    """

    skill_target = _skill_file(file_path)
    if skill_target is not None:
        if not skill_target.is_file():
            fail(f"技能文件不存在：{file_path}")
        try:
            return skill_target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            fail(f"读取失败：{exc}")

    try:
        target = _resolve(file_path, READ)
    except SandboxDenied as exc:
        fail(str(exc))

    if not target.exists():
        fail(f"文件不存在：{rel(target)}")
    if target.is_dir():
        fail(f"{rel(target)} 是目录，请用该目录下的具体文件路径")
    if looks_binary(target):
        fail(f"{rel(target)} 看起来是二进制文件，read_file 只处理文本")

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        fail(f"读取失败：{exc}")

    lines = text.splitlines()
    total = len(lines)

    # offset = 要跳过的行数（0 基）。这与 `stage_policy` 里那份照搬模板的读取记账
    # （`_as_nonnegative_int(args.get("offset"), default=0)`）保持一致——两边算法不同的话，
    # 模型按 0 基续读会被判成"重叠"而被纪律拦下（实测踩到）。
    skip = max(0, int(offset) if offset is not None else 0)
    if skip >= total and total:
        fail(f"{rel(target)} 共 {total} 行，offset={skip} 超出范围")

    count = max(1, min(int(limit) if limit is not None else 100, _MAX_READ_LIMIT))
    window = lines[skip : skip + count]
    if not window:
        return f"{rel(target)}（共 {total} 行，该范围为空）"

    first = skip + 1
    last = skip + len(window)

    # `truncate_head` 只负责行数/字节上限，它自带的续读提示只在**真的截断**时才会出现。
    # 正常分页（文件 300 行、这次只读 100 行）没有截断，那条提示就丢了——所以分页提示
    # 单独拼，只要后面还有行就一定给出来，且 offset 与入参语义一致（跳过的行数）。
    clipped = truncate_head("\n".join(window))
    tail = f"\n[后面还有 {total - last} 行，续读用 offset={skip + len(window)}]" if last < total else ""
    return f"{rel(target)}（共 {total} 行，显示 {first}-{last}）\n{clipped}{tail}"


@tool
async def write_file(file_path: str, content: str) -> str:
    """创建或整体覆盖一个文件。新文件用这个，改已有文件用 `edit_file`。

    - 父目录不存在会自动创建
    - content 是**完整**内容，不是片段；用片段覆盖会把其余内容丢掉
    - 只允许写 `/workspace` 内、且不属于 ARC 受保护路径（`.arc`、`.git`、`requirements`、
      依赖目录、构建产物、lockfile、环境文件）的文件
    """

    try:
        target = _resolve(file_path, WRITE)
    except SandboxDenied as exc:
        fail(str(exc))
    return await _write.ainvoke({"path": str(target), "content": content})


@tool
async def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """在已有文件里精确替换一段文本。

    - old_string 必须与文件内容一致（含缩进与空行），且默认要求在文件中**唯一**
    - 要多处一起改时传 replace_all=true
    - 找不到完全一致的片段时退化为忽略行尾空白的匹配
    - 改已有文件就用这个，不要用 `write_file` 整体覆盖
    """

    try:
        target = _resolve(file_path, WRITE)
    except SandboxDenied as exc:
        fail(str(exc))
    return await _edit.ainvoke(
        {
            "path": str(target),
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": replace_all,
        }
    )


@tool
async def glob(pattern: str, path: str | None = None) -> str:
    """按 glob 匹配查找文件，返回工作区相对路径。

    - pattern 里的 `*` 会跨目录，所以 `*.tsx` 能匹配任意层级
    - 只支持简单 glob，**不要**用 `**/*.{ts,tsx}` 这种花括号展开
    - path 限定搜索起点，省略即工作区根目录
    """

    from app.agent.tools.find import find as _find

    try:
        base = _resolve(path or ".", READ)
    except SandboxDenied as exc:
        fail(str(exc))
    return await _find.ainvoke({"pattern": pattern, "path": str(base), "max_results": 200})


@tool
async def grep(pattern: str, path: str | None = None, glob: str | None = None) -> str:
    """在文件内容里搜索，返回 `文件:行号: 内容`。

    - pattern 是**正则**；path 限定搜索起点；glob 按文件名过滤
    - 命中行数超过上限时会截断并说明
    - 自动跳过 `.git`、`node_modules`、`__pycache__` 这类目录与二进制文件
    """

    try:
        base = _resolve(path or ".", READ)
    except SandboxDenied as exc:
        fail(str(exc))
    return await _grep.ainvoke(
        {"pattern": pattern, "path": str(base), "glob": glob or "", "max_results": 200}
    )


@tool
async def ls(path: str | None = None) -> str:
    """列出目录内容，目录在前，附带文件大小与修改时间。

    ARC 不鼓励用目录清点代替定位，只在已知目录下确认文件名时使用。
    """

    try:
        target = _resolve(path or ".", READ)
    except SandboxDenied as exc:
        fail(str(exc))
    return await _ls.ainvoke({"path": str(target)})


FILE_TOOLS = [read_file, write_file, edit_file, glob, grep, ls]

FILE_TOOL_NAMES = frozenset(tool.name for tool in FILE_TOOLS)
READ_TOOL_NAME = read_file.name
WRITE_TOOL_NAMES = frozenset({write_file.name, edit_file.name})
MAX_READ_LIMIT = _MAX_READ_LIMIT
MAX_LINES_HINT = MAX_LINES
