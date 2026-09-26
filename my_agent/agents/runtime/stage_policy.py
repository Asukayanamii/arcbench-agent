"""ARC 的阶段纪律：把 `stage_discipline` 那套规则搬到 my-agent 的执行模型上。

上游模板用 LangChain 的 `AgentMiddleware` 在工具调用前后插钩子；my-agent 没有中间件层，
工具由 `app.agent.tool_calls.run_tools` 直接按名字取出来执行。所以这里改成**包一层工具**：
每个工具外面套一个同名的壳，壳里先查纪律、再执行、后记账。规则本身逐条照搬，不重新设计。

拦下来的调用不走异常，而是返回一条 `Error: ARC stage discipline: ...` 的正文——
与上游一致：模型需要读到这句话才能纠正方向，直接抛异常会让它退化成"工具坏了"。
"""

from __future__ import annotations

from typing import Any, Iterable

from langchain_core.tools import BaseTool, StructuredTool

from agents.runtime.workspace_tools import (
    MAX_READ_LIMIT,
    READ_TOOL_NAME,
    WORKSPACE_PREFIX,
    WRITE_TOOL_NAMES,
)

VALIDATION_TOOL_NAMES = frozenset({"run_build", "run_tests"})
MAX_DESIGN_WRITES = 8
MAX_SKELETON_LINES = 160

BLOCKED_PREFIX = "Error: ARC stage discipline: "


class StageDiscipline:
    """单次阶段调用的纪律状态。一次 stage run 一个实例，不跨阶段复用。"""

    def __init__(self, *, stage: str) -> None:
        self.stage = stage
        self._read_ranges: dict[str, list[tuple[int, int]]] = {}
        self._written_paths: set[str] = set()
        self._failed_paths: set[str] = set()
        self._validation_failed = False
        self._design_write_count = 0

    def check(self, name: str, args: dict[str, Any]) -> str | None:
        """返回拦截原因；None 表示放行。"""

        if name in {"execute", "delete"}:
            return f"`{name}` is disabled in ARC's staged file workflow."
        if self.stage == "test_generation" and name in VALIDATION_TOOL_NAMES:
            return "TestGenerator only creates tests and its manifest; it must not run validation."
        if name == READ_TOOL_NAME:
            return self._validate_read(args)
        if name in WRITE_TOOL_NAMES:
            return self._validate_write(args)
        return None

    def bounded_args(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """在工作区读取上强制分页上限，避免一次把大文件整个吞进上下文。"""

        if name != READ_TOOL_NAME:
            return args
        path = _discipline_path(args)
        if not path.startswith("/workspace/"):
            return args
        bounded = dict(args)
        # offset 是"跳过的行数"（0 基），与 workspace_tools.read_file 一致。
        bounded["offset"] = _as_int(bounded.get("offset"), default=0, minimum=0)
        bounded["limit"] = min(_as_int(bounded.get("limit"), default=100, minimum=1), MAX_READ_LIMIT)
        return bounded

    def record(self, name: str, args: dict[str, Any], *, ok: bool, content: str = "") -> None:
        path = _discipline_path(args)
        if name in VALIDATION_TOOL_NAMES:
            if not ok:
                self._validation_failed = True
                self._failed_paths.update(self._written_paths)
            return
        if not ok:
            if path:
                self._failed_paths.add(path)
            return
        if name == READ_TOOL_NAME and path:
            offset = _as_int(args.get("offset"), default=0, minimum=0)
            limit = _as_int(args.get("limit"), default=100, minimum=1)
            self._read_ranges.setdefault(path, []).append((offset, offset + limit))
        if name in WRITE_TOOL_NAMES and path:
            if path not in self._written_paths and self.stage == "interface_design":
                self._design_write_count += 1
            self._written_paths.add(path)

    def _validate_read(self, args: dict[str, Any]) -> str | None:
        path = _discipline_path(args)
        if not path:
            return None
        if path in self._written_paths and not self._path_unlocked(path):
            return (
                f"Read blocked: {path} was already written in this stage. Continue to the next action; "
                "re-read only after a file-operation or system-validation error."
            )
        offset = _as_int(args.get("offset"), default=0, minimum=0)
        limit = min(_as_int(args.get("limit"), default=100, minimum=1), MAX_READ_LIMIT)
        previous = self._read_ranges.get(path, [])
        if not previous or self._path_unlocked(path):
            return None
        if any(_overlap(offset, offset + limit, start, end) for start, end in previous):
            return (
                f"Repeated read blocked: {path} is already in this stage's read cache. "
                "Use the earlier result; only a non-overlapping paginated range or a failure may justify another read."
            )
        return None

    def _validate_write(self, args: dict[str, Any]) -> str | None:
        path = _discipline_path(args)
        if not path:
            return None
        if path in self._written_paths and not self._path_unlocked(path):
            return (
                f"Repeated write blocked: {path} was already changed in this stage. "
                "Wait for a file-operation or system-validation error before changing it again."
            )
        if self.stage == "test_generation" and not _is_test_asset(path):
            return (
                "TestGenerator may write only test files, test helpers/configuration, and the returned manifest; "
                f"{path} is not a test asset."
            )
        if self.stage == "interface_design":
            if path not in self._written_paths and self._design_write_count >= MAX_DESIGN_WRITES:
                return (
                    f"InterfaceDesigner may materialize at most {MAX_DESIGN_WRITES} small skeleton files. "
                    "Record remaining interfaces in the response for TDD."
                )
            content = str(args.get("content", args.get("new_string", "")) or "")
            if content.count("\n") + 1 > MAX_SKELETON_LINES:
                return (
                    f"InterfaceDesigner may only materialize small skeletons (at most {MAX_SKELETON_LINES} lines per write). "
                    "Record the complete business contract for TDD instead of implementing it now."
                )
        return None

    def _path_unlocked(self, path: str) -> bool:
        return self._validation_failed or path in self._failed_paths


def guard_tools(tools: Iterable[BaseTool], discipline: StageDiscipline) -> list[BaseTool]:
    """给每个工具套上纪律外壳，保持名字、描述与参数 schema 不变。"""

    guarded: list[BaseTool] = []
    for tool in tools:
        guarded.append(_guard_tool(tool, discipline))
    return guarded


def _guard_tool(tool: BaseTool, discipline: StageDiscipline) -> BaseTool:
    name = tool.name

    async def runner(**kwargs: Any) -> str:
        blocked = discipline.check(name, kwargs)
        if blocked is not None:
            return f"{BLOCKED_PREFIX}{blocked}"
        args = discipline.bounded_args(name, kwargs)
        try:
            output = await tool.ainvoke(args)
        except Exception as exc:
            discipline.record(name, args, ok=False)
            raise exc
        discipline.record(name, args, ok=not _tool_result_failed(output), content=_tool_text(output))
        return output

    return StructuredTool.from_function(
        coroutine=runner,
        name=name,
        description=tool.description,
        args_schema=tool.args_schema,
    )


def _discipline_path(args: dict[str, Any]) -> str:
    """把工具参数里的路径归一成**同一个键**，好让去重真正生效。

    模型会混着写 `/workspace/backend/src/app.js` 和 `backend/src/app.js` 指向同一个文件。
    只补前导斜杠的话这是两个键，读缓存与写入记录就被绕过 —— 实测同一个
    `init_db.js` 被不同写法读了 6 次，每次都是一整个模型往返。
    所以先把虚拟根前缀去掉再归一。
    """

    raw = str(args.get("file_path", "") or "").replace("\\", "/").strip()
    if not raw:
        return ""
    if raw == WORKSPACE_PREFIX or raw.startswith(f"{WORKSPACE_PREFIX}/"):
        raw = raw[len(WORKSPACE_PREFIX) :]
    raw = raw.lstrip("/")
    while raw.startswith("./"):
        raw = raw[2:]
    return f"/{raw}" if raw else ""


def _tool_text(output: Any) -> str:
    """取工具返回的正文。`run_tests` 这类工具返回的是裸 `str`，不是 `ToolMessage`。"""

    content = getattr(output, "content", None)
    if isinstance(content, str):
        return content
    return output if isinstance(output, str) else ""


def _tool_result_failed(output: Any) -> bool:
    """失败写在工具正文里，不能只看有没有抛异常。

    上游 `StageDisciplineMiddleware` 用 `_tool_result_failed` 读正文里的 `Exit Code:`；
    搬到 my-agent 时被换成"看调用有没有抛异常"。但 `run_tests`/`run_build` 失败时
    **返回** `Exit Code: 1` 的正文、从不抛异常，于是 `_validation_failed` 永远是假、
    `_path_unlocked` 永远为假 —— 一个文件在本阶段被写过一次就再也改不回、也读不回，
    守卫消息承诺的解锁成了不可达的死代码。线上实测因此死锁 133 次工具调用
    （REQ-1.1 被拦 98 次、REQ-1.2 被拦 35 次），agent 修不了自己刚生成的坏测试文件。
    """

    if getattr(output, "status", None) == "error":
        return True
    text = _tool_text(output)
    return "Exit Code: 0" not in text and ("Exit Code:" in text or text.lstrip().startswith("Error:"))


def _as_int(value: Any, *, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _overlap(start: int, end: int, other_start: int, other_end: int) -> bool:
    return start < other_end and other_start < end


def _is_test_asset(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    segments = ("/test/", "/tests/", "/__tests__/", "/e2e/", "/__mocks__/", "/test-e2e/")
    markers = (
        ".test.",
        ".spec.",
        "playwright.config.",
        "vitest.config.",
        "jest.config.",
        "setup-tests.",
        "setuptests.",
    )
    return any(segment in normalized for segment in segments) or any(
        marker in name for marker in markers
    )


def render_stage_limits(stage: str) -> str:
    """把本阶段会被拦下的硬约束渲染成提示词片段。

    **谁执行规则，谁就得公布规则。** 这些上限原先只写在 `_validate_*` 的拦截分支里，
    提示词与技能一个字都没提，模型只能撞上去才知道 —— 实测它写了一个 158 行的文件、
    被拦回、再写一遍，白烧一个完整往返（那一轮要重发整段历史）。

    文案由这里的常量拼出来，所以改上限时提示词跟着变，不会两边分叉。
    """

    common = [
        "You may not write the same path twice in this stage, and you may not re-read a path "
        "you already wrote; re-reading is allowed only after a tool or validation call reported "
        "an error for that path.",
        "File reads are paginated: `offset` is the number of lines to skip (0-based) and `limit` "
        "is how many lines to return. Do not re-read a range you already have.",
        "Use only the file tools and the stage-specific tools listed above; `execute` and `delete` "
        "do not exist in this workflow.",
    ]

    by_stage: dict[str, list[str]] = {
        "interface_design": [
            f"You may materialize at most {MAX_DESIGN_WRITES} skeleton files in this stage.",
            f"Each single write must be at most {MAX_SKELETON_LINES} lines. If a contract needs "
            "more, record it in the returned `interfaces` entry instead of writing the code now — "
            "implementation belongs to the TDD stage.",
        ],
        "test_generation": [
            "You may write only test files, test helpers/configuration, and the returned manifest; "
            "writes to application source are rejected.",
            "You must not run validation tools in this stage; `run_tests` and `run_build` are not available.",
        ],
        "implementation": [
            "Read the failing test output before editing, then change only the owning file.",
        ],
    }

    lines = [*by_stage.get(stage, []), *common]
    if not lines:
        return ""
    return "\n".join(["### Stage Limits (enforced)", *[f"- {line}" for line in lines]])


__all__ = [
    "BLOCKED_PREFIX",
    "MAX_DESIGN_WRITES",
    "MAX_SKELETON_LINES",
    "StageDiscipline",
    "VALIDATION_TOOL_NAMES",
    "guard_tools",
    "render_stage_limits",
]
