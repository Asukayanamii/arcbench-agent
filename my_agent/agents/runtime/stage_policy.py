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
# 检索类工具：输出在一个阶段内是常量（目录结构不会自己变），所以和 read cache 一样按
# 「同一个查询只回一次」处理。`find` 目前没有暴露成工具（`glob` 内部用的是它），
# 名字留在这里，免得以后加了它却漏在覆盖之外。
SEARCH_TOOL_NAMES = frozenset({"find", "glob", "grep", "ls"})
MAX_DESIGN_WRITES = 8
MAX_SKELETON_LINES = 160

BLOCKED_PREFIX = "Error: ARC stage discipline: "


class StageDiscipline:
    """单次阶段调用的纪律状态。一次 stage run 一个实例，不跨阶段复用。

    **两个缓存在 `check()` 放行时就记账，不是等工具跑成功再记**：同一批工具调用是并发
    执行的（`run_calls` 给每个调用建一个 task），只在成功后记账的话，同一批里的孪生调用
    会**都**通过 `check()`、都去执行 —— 实测一次受限运行里 55 次重复查询**全部**落在同一批，
    那种缓存在这批上等于不存在。失败（工具报错）时把这次认领撤销，所以「报错仍可重试」
    这条规则不受影响。
    """

    def __init__(self, *, stage: str) -> None:
        self.stage = stage
        # 两个缓存里存的都是「已经问过的东西」：读用 (path, [start, end))，检索用查询键。
        self._read_ranges: dict[str, list[tuple[int, int]]] = {}
        self._search_calls: set[str] = set()
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
        if name in SEARCH_TOOL_NAMES:
            return self._validate_search(name, args)
        return None

    def bounded_args(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """在工作区读取上强制分页上限，避免一次把大文件整个吞进上下文。

        ⚠️ 归一后的键（`_discipline_path`）**已经去掉了 `/workspace` 前缀**，所以「是不是
        工作区里的读」不能用它来判断 —— 原先写的是 `path.startswith("/workspace/")`，
        那是**恒假**的，上限从来没生效过：`read_file(path, limit=500)` 按 (0, 500) 记账，
        而工具实际只返回 200 行（`MAX_READ_LIMIT`），于是合法的续读 `offset=200` 会被
        判成重叠而拦下 —— 白白烧掉一个往返，守卫的理由还是错的。

        技能库（`/skills/<名字>/SKILL.md`）由 `read_file` 整篇放行，忽略这两个参数，
        所以这里一并按工作区口径处理即可。
        """

        if name != READ_TOOL_NAME:
            return args
        if not _discipline_path(args):
            return args
        bounded = dict(args)
        # offset 是"跳过的行数"（0 基），与 workspace_tools.read_file 一致。
        bounded["offset"] = _as_int(bounded.get("offset"), default=0, minimum=0)
        bounded["limit"] = min(_as_int(bounded.get("limit"), default=100, minimum=1), MAX_READ_LIMIT)
        return bounded

    def record(self, name: str, args: dict[str, Any], *, ok: bool, content: str = "") -> None:
        path = _discipline_path(args)
        key = _search_key(name, args)
        if name in VALIDATION_TOOL_NAMES:
            if not ok:
                self._validation_failed = True
                self._failed_paths.update(self._written_paths)
            return
        if not ok:
            # 工具报错：撤销 `check()` 时的认领并解锁，这一路径 / 这一查询可以重试
            # （与既有规则一致）。
            self._release_read_claim(args)
            if key:
                self._search_calls.discard(key)
            if path:
                self._failed_paths.add(path)
            if key:
                self._failed_paths.add(key)
            return
        if name in WRITE_TOOL_NAMES and path:
            if path not in self._written_paths and self.stage == "interface_design":
                self._design_write_count += 1
            self._written_paths.add(path)

    def _release_read_claim(self, args: dict[str, Any]) -> None:
        path = _discipline_path(args)
        if not path:
            return
        window = _read_window(args)
        claimed = self._read_ranges.get(path) or []
        if window in claimed:
            claimed.remove(window)

    def note_blocked(self, name: str, args: dict[str, Any]) -> None:
        """被纪律拦下的调用也要留下痕迹：把**该路径**记进 `_failed_paths`。

        守卫的拦截消息写着「Wait for a file-operation or system-validation error before
        changing it again」—— 而被拦**就是**一次文件操作错误。原先 `_guard_tool.runner`
        在拦截时直接 return，从不调用 `record`，于是路径进不了 `_failed_paths`；
        `_validation_failed` 又只在验证工具失败时置位，而 `test_generation` 在 `check()`
        里被明确禁止调用验证工具。

        两条叠加 → **TestGenerator 写过的路径永久锁死，第一次写就是最终稿，改不回来**。
        线上比赛实测：一个测试文件被 "Repeated write blocked" 拦了 **1142 次**，整轮
        烧掉 201 分钟（`run-logs/2026-09-27-competition-hackathon--sheet-bs`）。

        只记当前路径，**不置 `_validation_failed`** —— 后者是整阶段解锁，语义不同，
        不该被一次普通的写入拦截顺手打开。

        检索类工具同理：被拦的是**那一个查询**，不是整个搜索缓存。
        """

        path = _discipline_path(args)
        if path:
            self._failed_paths.add(path)
        key = _search_key(name, args)
        if key:
            self._failed_paths.add(key)

    def _validate_read(self, args: dict[str, Any]) -> str | None:
        path = _discipline_path(args)
        if not path:
            return None
        if path in self._written_paths and not self._path_unlocked(path):
            return (
                f"Read blocked: {path} was already written in this stage. Continue to the next action; "
                "re-read only after a file-operation or system-validation error."
            )
        window = _read_window(args)
        previous = self._read_ranges.get(path, [])
        if previous and not self._cache_unlocked(path):
            if any(_overlap(*window, start, end) for start, end in previous):
                return (
                    f"Repeated read blocked: {path} is already in this stage's read cache. "
                    "Use the earlier result; only a non-overlapping paginated range or a failure may justify another read."
                )
        self._read_ranges.setdefault(path, []).append(window)
        return None

    def _validate_search(self, name: str, args: dict[str, Any]) -> str | None:
        """同一个检索查询在一个阶段内只回一次（issue #63 方案 A）。

        实测（`run-logs/2026-09-28-hackathon--github-01fcacc`）：一次受限运行里检索类调用
        占全部工具调用的 21%（`ls` 34 次、`glob` 9 次、`grep` 4 次），其中约四成是同一会话内
        的重复查询 —— 目录结构与匹配结果在一个阶段内基本是常量，重复跑只是把同一份输出
        再灌一次上下文，而每一次都是一整个模型往返、且要重发整段阶段历史。
        read cache 早就在拦重复读，检索类工具只是不在它的覆盖里。

        不做「写过文件就作废检索缓存」：真要重列一次时，被拦的那一次本身就解锁了该查询
        （`note_blocked`），代价至多一个往返。
        """

        key = _search_key(name, args)
        if not key:
            return None
        if key in self._search_calls and not self._cache_unlocked(key):
            return (
                f"Repeated {name} blocked: this exact query is already in this stage's search cache. "
                "Use the earlier result; only a different query or a failure may justify another call."
            )
        self._search_calls.add(key)
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

    def _cache_unlocked(self, key: str) -> bool:
        """重复读 / 重复检索的解锁：只看**这个键自己**有没有出过错。

        不能跟着 `_validation_failed` 一起整阶段解锁。一次测试失败不会让已经拿到的文件内容
        或目录清单变旧，但它会把两个缓存整个关掉 —— 而 TDD 阶段**就是**由失败驱动的：
        实测一个会话里同区间重复读 190 次（`run-logs/2026-09-28-hackathon--github-01fcacc`），
        守卫总共只拦下 1 次，就是被这个过宽的解锁吃掉的。

        写入侧与「读回自己写过的文件」仍然用 `_path_unlocked` —— 那里整阶段解锁是承重的
        （#4.14：一次失败之后必须能改，也必须能读回自己刚写的文件）。
        """

        return key in self._failed_paths


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
            # 拦截本身就是一次"文件操作错误"，必须记账 —— 否则被拦的路径永远解不开，
            # 而测试生成阶段又没有验证工具能触发整阶段解锁。见 note_blocked。
            discipline.note_blocked(name, kwargs)
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


def _normalise_spelling(raw: Any) -> str:
    """去掉虚拟根 `/workspace` 与 `./` 前缀，返回工作区相对写法（空串 = 没给路径）。"""

    text = str(raw or "").replace("\\", "/").strip()
    if text == WORKSPACE_PREFIX or text.startswith(f"{WORKSPACE_PREFIX}/"):
        text = text[len(WORKSPACE_PREFIX) :]
    text = text.lstrip("/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _discipline_path(args: dict[str, Any]) -> str:
    """把工具参数里的路径归一成**同一个键**，好让去重真正生效。

    模型会混着写 `/workspace/backend/src/app.js` 和 `backend/src/app.js` 指向同一个文件。
    只补前导斜杠的话这是两个键，读缓存与写入记录就被绕过 —— 实测同一个
    `init_db.js` 被不同写法读了 6 次，每次都是一整个模型往返。
    所以先把虚拟根前缀去掉再归一。
    """

    raw = _normalise_spelling(args.get("file_path"))
    return f"/{raw}" if raw else ""


def _read_window(args: dict[str, Any]) -> tuple[int, int]:
    """把一次读折算成半开区间 `[start, end)`。offset 是"跳过的行数"（0 基）。

    `check()` 与 `record()` 都要用同一个折算：前者认领、后者在失败时撤销认领，
    两边算得不一样就会撤销失败，缓存里留下一条永远命中不了的账单。
    """

    offset = _as_int(args.get("offset"), default=0, minimum=0)
    limit = min(_as_int(args.get("limit"), default=100, minimum=1), MAX_READ_LIMIT)
    return offset, offset + limit


def _search_key(name: str, args: dict[str, Any]) -> str:
    """检索类查询的缓存键；非检索工具返回空串。

    同一个查询的**不同写法必须共用一把键**，否则缓存等于没有 —— 模型两种都写：
    `glob(pattern="frontend/tests/*")` 与 `glob(pattern="frontend/tests/*", path=None)`
    各出现 2 次，`ls()` 与 `ls("/workspace")` 也是一回事。所以路径按 `_normalise_spelling`
    归一，缺省 / `null` / `.` / `/workspace` 都落成同一个「工作区根」。
    """

    if name not in SEARCH_TOOL_NAMES:
        return ""
    base = _normalise_spelling(args.get("path"))
    location = f"/{base}" if base and base != "." else "/"
    parts = [name, str(args.get("pattern", "") or "").strip(), location]
    if name == "grep":
        parts.append(str(args.get("glob", "") or "").strip())
    return "|".join(parts)


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
        "Search results are cached per query: `ls` / `glob` / `grep` answer each distinct query "
        "once in this stage. Reuse the earlier result instead of repeating the same call.",
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
    "SEARCH_TOOL_NAMES",
    "StageDiscipline",
    "VALIDATION_TOOL_NAMES",
    "guard_tools",
    "render_stage_limits",
]
