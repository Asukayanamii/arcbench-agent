"""Pre-flight checks for the ARC stage bridge.

Four invariants that only fail with a *real* model (the mock never exercised them, so
the first platform run was where they surfaced):

1. **The skill selector and the skill reader must use the same root.** The selector
   resolved skills under the package's `skills/`, while `read_file` resolved them under
   my-agent's `SKILLS_DIR` (`~/.my_agent/skills`). A stage was told
   "skill-permitted: leaf-full-design" and then got
   "技能文件不存在：/skills/leaf-full-design/SKILL.md" - the tuned guidance never
   reached the model.

2. **`read_file`'s `offset` must mean "lines to skip" (0-based), matching the discipline
   accounting in `agents/runtime/stage_policy.py`** (ported from the upstream
   `StageDisciplineMiddleware`, which defaults `offset` to 0). When the two disagreed, a
   legitimate continuation read after the first page was rejected as an overlap:
   `Error: ARC stage discipline: Repeated read blocked: ...`

3. **A failing `run_tests` must unlock the paths the stage already wrote.** Failure has to
   be read from the tool's *text* (`Exit Code: 1`), exactly as upstream's
   `_tool_result_failed` does - these tools never raise. Judging by exception alone made
   the documented unlock unreachable: one write to a path locked it for the rest of the
   stage, so an agent that generated a broken test file could not repair it. That
   deadlocked 133 tool calls in a single platform run (REQ-1.1: 98, REQ-1.2: 35).

4. **That unlock rests on a text contract nothing else guards.** Fixing (3) moved the
   failure test into string matching on `Exit Code:`. Rename that marker in any producer
   and the guard stops unlocking again - silently, with every other check still green.
   So the marker is pinned from both sides: the producer modules must still emit it, and
   the guard must agree with an independent parser on the same bytes.

Run before packaging:

    python tools/check_stage_bridge.py
"""

from __future__ import annotations

import asyncio
import json
import re
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Simulate the runner container, where my-agent's own skill library does not exist,
# so a wrong root cannot be masked by a stray directory on a dev machine.
os.environ.setdefault("SKILLS_DIR", str(REPO.parent / "_no_such_skills_dir"))

from langchain_core.tools import StructuredTool  # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.tools import ToolException, tool  # noqa: E402
from langgraph.types import Command  # noqa: E402

from agents import test_driven_developer  # noqa: E402
from agents.context.pipeline import context_pipeline  # noqa: E402
from agents.runtime import stage_policy, workspace_tools  # noqa: E402
from agents.runtime.contracts import AgentRuntimeContext  # noqa: E402
from agents.skills import selection  # noqa: E402
from app.agent.runtime import current_workspace  # noqa: E402
from app_type_handler import test_results, web as web_handler  # noqa: E402
from app.agent.tool_calls import _inject_tool_args, run_calls, run_tools  # noqa: E402
from app.agent.tools.todo import (  # noqa: E402
    MAX_TODOS,
    TODO_TOOLS,
    add_todo,
    complete_todo,
    list_todo,
    reduce_todos,
    update_todo,
)
from agents.runtime.extra_tools import (  # noqa: E402
    EXTRA_TOOLS_BY_STAGE,
    build_extra_tools,
    render_extra_tools,
)
from agents.runtime.workspace_tools import FILE_TOOLS  # noqa: E402

failures: list[str] = []

# The text contract the guard's unlock depends on. Declared here **independently**
# of the guard on purpose: this file is the witness, so it must not read the
# marker back out of `stage_policy`.
EXIT_CODE_MARKER = "Exit Code:"

# Every module that writes or parses that contract. An explicit list, not a glob -
# a renamed file must break the check, not quietly drop out of its coverage.
VALIDATION_OUTPUT_SOURCES = (
    "app_type_handler/base.py",
    "app_type_handler/web.py",
    "app_type_handler/cli.py",
    "app_type_handler/android.py",
    "app_type_handler/test_results.py",
    "core/phases.py",
    "agents/tools/build.py",
    "agents/test_driven_developer.py",
)


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + label + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


def _read(path: str, **kwargs: object) -> str:
    async def run() -> str:
        token = current_workspace.set(REPO)
        try:
            return await workspace_tools.read_file.ainvoke({"file_path": path, **kwargs})
        finally:
            current_workspace.reset(token)

    return asyncio.run(run())


def check_skill_roots() -> None:
    print("--- skill selector vs skill reader ---")
    selector_root = (REPO / "skills").resolve()
    reader_root = workspace_tools._skills_root()
    print(f"selector root : {selector_root}")
    print(f"reader root   : {reader_root}")

    check("reader root matches the selector root",
          reader_root is not None and reader_root.resolve() == selector_root,
          f"{reader_root} != {selector_root}")
    if reader_root is None or reader_root.resolve() != selector_root:
        return

    shipped = sorted(p.parent.name for p in selector_root.glob("*/SKILL.md"))
    print(f"skills on disk: {shipped}")
    check("skills are shipped", len(shipped) >= 5, f"found {len(shipped)}")
    check("selector accepts every shipped skill",
          set(selection.available_skill_names(shipped)) == set(shipped))

    for name in shipped:
        try:
            body = _read(f"/skills/{name}/SKILL.md")
        except Exception as exc:  # noqa: BLE001 - the failure text is the assertion
            check(f"read /skills/{name}/SKILL.md", False, f"{type(exc).__name__}: {exc}")
            continue
        check(f"read /skills/{name}/SKILL.md", "---" in body and len(body) > 200, f"{len(body)} chars")


def check_pagination() -> None:
    print()
    print("--- read_file pagination vs stage discipline ---")

    probe = REPO / "docs" / "_pagination_probe.txt"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("\n".join(f"line {i}" for i in range(1, 301)) + "\n", encoding="utf-8")
    rel = "docs/_pagination_probe.txt"
    discipline = stage_policy.StageDiscipline(stage="implementation")

    try:
        first = _read(rel)
        check("page 1 reports lines 1-100", "显示 1-100" in first, first.splitlines()[0])
        check("page 1 advertises the continuation offset", "offset=100" in first)

        args1 = {"file_path": "/workspace/" + rel}
        check("page 1 passes discipline", discipline.check(workspace_tools.READ_TOOL_NAME, args1) is None)
        discipline.record(workspace_tools.READ_TOOL_NAME, args1, ok=True, content=first)

        args2 = {"file_path": "/workspace/" + rel, "offset": 100, "limit": 100}
        blocked = discipline.check(workspace_tools.READ_TOOL_NAME, args2)
        check("continuation at offset=100 is allowed", blocked is None, blocked or "")

        second = _read(rel, offset=100, limit=100)
        check("page 2 reports lines 101-200", "显示 101-200" in second, second.splitlines()[0])
        check("page 2 does not repeat page 1", "line 100" not in second and "line 101" in second)

        discipline.record(workspace_tools.READ_TOOL_NAME, args2, ok=True, content=second)
        args3 = {"file_path": "/workspace/" + rel, "offset": 50, "limit": 10}
        check("a genuinely overlapping re-read is still blocked",
              discipline.check(workspace_tools.READ_TOOL_NAME, args3) is not None)
    finally:
        probe.unlink(missing_ok=True)


def check_stage_limits_are_published() -> None:
    """Every hard limit the discipline enforces must be stated in the stage prompt.

    The interface-design caps (max skeleton files, max lines per write) used to live only in
    the blocking branches of `stage_policy`. A real run showed the model write a 158-line file,
    get rejected for exceeding the undocumented cap, then write it again -- one full round-trip
    wasted, and that round-trip resends the whole stage history.
    """

    print()
    print("--- stage limits are published to the model ---")

    from agents.runtime import factory

    for stage in ("interface_design", "test_generation", "implementation"):
        text = stage_policy.render_stage_limits(stage)
        check(f"{stage}: renders a limits block", bool(text.strip()), text[:60])

    design = stage_policy.render_stage_limits("interface_design")
    check("design caps mention the max skeleton files",
          str(stage_policy.MAX_DESIGN_WRITES) in design)
    check("design caps mention the max lines per write",
          str(stage_policy.MAX_SKELETON_LINES) in design)

    for stage in ("interface_design", "test_generation", "implementation"):
        assembled = factory._with_response_shape(
            "BASE PROMPT",
            response_format=None,
            skills=None,
            permitted_skill_names=None,
            stage=stage,
        )
        check(f"{stage}: limits reach the assembled system prompt",
              "### Stage Limits (enforced)" in assembled and "BASE PROMPT" in assembled)


def check_path_keys_are_normalised() -> None:
    """The same file written two ways must share one cache key.

    A real run showed `init_db.js` read six times because the model alternates between
    `/workspace/backend/src/database/init_db.js` and `backend/src/database/init_db.js`.
    Keyed separately, the read cache never matched and every repeat cost a full
    model round-trip (which resends the whole stage history).
    """

    print()
    print("--- path keys collapse to one spelling ---")

    pairs = [
        ("/workspace/backend/src/app.js", "backend/src/app.js"),
        ("/workspace/frontend/src/App.tsx", "./frontend/src/App.tsx"),
        ("/workspace/package.json", "package.json"),
        ("backend\\src\\app.js", "/workspace/backend/src/app.js"),
    ]
    for left, right in pairs:
        a = stage_policy._discipline_path({"file_path": left})
        b = stage_policy._discipline_path({"file_path": right})
        check(f"{left!r} == {right!r}", a == b, f"{a} vs {b}")

    # And the discipline must actually refuse the second spelling as a repeat.
    probe = REPO / "docs" / "_path_key_probe.txt"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("\n".join(f"line {i}" for i in range(1, 60)) + "\n", encoding="utf-8")
    try:
        discipline = stage_policy.StageDiscipline(stage="implementation")
        first = {"file_path": "/workspace/docs/_path_key_probe.txt"}
        check("first read passes", discipline.check(workspace_tools.READ_TOOL_NAME, first) is None)
        discipline.record(workspace_tools.READ_TOOL_NAME, first, ok=True, content="x")

        second = {"file_path": "docs/_path_key_probe.txt"}
        blocked = discipline.check(workspace_tools.READ_TOOL_NAME, second)
        check("re-reading via the other spelling is blocked", blocked is not None,
              blocked or "not blocked")
    finally:
        probe.unlink(missing_ok=True)


def check_repeat_searches_are_served_from_the_cache() -> None:
    """同一阶段内的同一个检索查询只回一次（issue #63 方案 A）。

    一次受限运行里（`run-logs/2026-09-28-hackathon--github-01fcacc`）检索类调用占全部
    工具调用的 21%，其中约四成是同一会话内的重复查询：`ls backend/src` 问过又问一遍、
    `glob` 加不加 `path` 各问一遍。目录清单在一个阶段内是常量，重复问只是把同一份输出
    再灌一次上下文，而且**每一次都是一整个模型往返**（要重发整段阶段历史）。

    这里的关键是**同一批**：`run_calls` 把一批调用并发跑，只在成功后记账的缓存看不见
    同批的孪生调用（实测那 55 次重复全部落在同一批里），所以两个缓存在 `check()` 放行时
    就认领，失败时撤销。
    """

    print()
    print("--- repeated searches are served from the cache ---")

    discipline = stage_policy.StageDiscipline(stage="implementation")

    check("the first ls is allowed",
          discipline.check("ls", {"path": "/workspace/backend/src"}) is None)
    blocked = discipline.check("ls", {"path": "/workspace/backend/src"})
    check("the same ls in the same batch is blocked", blocked is not None, blocked or "")
    check("the block tells the model to reuse the earlier result",
          "Use the earlier result" in (blocked or ""), blocked or "")
    check("another spelling of the same directory is the same query",
          discipline.check("ls", {"path": "backend/src"}) is not None)
    check("a different directory is not",
          discipline.check("ls", {"path": "/workspace/backend/tests"}) is None)
    check("a different tool is not the same query",
          discipline.check("glob", {"pattern": "backend/src"}) is None)

    spelling = stage_policy.StageDiscipline(stage="implementation")
    check("glob without `path` is allowed",
          spelling.check("glob", {"pattern": "frontend/tests/*"}) is None)
    check("glob with `path: null` is the same query",
          spelling.check("glob", {"pattern": "frontend/tests/*", "path": None}) is not None)
    check("grep with `glob: null` equals `glob: ''`",
          spelling.check("grep", {"pattern": "TODO", "path": "/workspace"}) is None
          and spelling.check("grep", {"pattern": "TODO", "path": None, "glob": ""}) is not None)

    retry = stage_policy.StageDiscipline(stage="implementation")
    retry.check("ls", {"path": "/workspace/backend"})
    retry.record("ls", {"path": "/workspace/backend"}, ok=False, content="Error: 目录不存在")
    check("a search that errored can be retried",
          retry.check("ls", {"path": "/workspace/backend"}) is None,
          "the tool error must release the claim")

    selfheal = stage_policy.StageDiscipline(stage="implementation")
    selfheal.check("ls", {"path": "/workspace/backend"})
    selfheal.check("ls", {"path": "/workspace/backend"})
    selfheal.note_blocked("ls", {"path": "/workspace/backend"})
    check("a blocked search does not become a hard loop",
          selfheal.check("ls", {"path": "/workspace/backend"}) is None,
          "a model that insists would burn a round-trip on every retry")

    # 谁执行规则谁就得公布规则（与 render_stage_limits 的既有约定一致）。
    limits = stage_policy.render_stage_limits("implementation")
    check("the search cache is published in the stage limits",
          "`ls` / `glob` / `grep`" in limits, limits.splitlines()[-1][:100])


def check_a_failing_validation_does_not_switch_off_the_lookup_cache() -> None:
    """一次测试失败不能把两个查询缓存整个关掉 —— 但写入侧必须照旧解锁。

    实测（`run-logs/2026-09-28-hackathon--github-01fcacc`）：一次受限运行里同区间重复读
    190 次（按事件计），守卫总共只拦下 **1** 次。原因是 TDD 会话**就是**由失败驱动的：
    第一次 `run_tests` 报 `Exit Code: 1` 就把 `_validation_failed` 置上，而
    `_path_unlocked` 从此对**任何**路径都返回真 —— 两个缓存一起停摆。

    但「整阶段解锁」对**写入**是承重的（#4.14：一次失败之后必须能改，也必须能读回自己
    刚写的文件；#43：没有验证工具的阶段靠 `note_blocked` 解锁）。所以这条检查同时钉住
    两侧：缓存不再被无关的失败关掉，写入与「读回自己写过的文件」照旧解锁。
    """

    print()
    print("--- a failing validation does not switch off the lookup cache ---")

    discipline = stage_policy.StageDiscipline(stage="implementation")
    read_args = {"file_path": "/workspace/backend/src/app.js"}
    search_args = {"path": "/workspace/backend/src"}

    check("the first read is allowed", discipline.check(workspace_tools.READ_TOOL_NAME, read_args) is None)
    check("the first search is allowed", discipline.check("ls", search_args) is None)
    discipline.record("run_tests", {}, ok=False, content="Runner: Vitest\nExit Code: 1\nFAIL x\n")

    check("a failing run_tests still flags the stage",
          discipline._validation_failed is True, "the write-side unlock depends on this")
    check("a repeated read is still blocked afterwards",
          discipline.check(workspace_tools.READ_TOOL_NAME, read_args) is not None,
          "one failing test used to switch the whole read cache off")
    check("a repeated search is still blocked afterwards",
          discipline.check("ls", search_args) is not None)

    # 写入侧的整阶段解锁照旧（#4.14 / #43）。用一份干净的状态验：上面那次失败已经把
    # 整个阶段解锁了（那时"读回自己写的文件"本来就该放行）。
    written = {"file_path": "/workspace/backend/src/owned.js", "content": "x"}
    repair = stage_policy.StageDiscipline(stage="implementation")
    repair.record("edit_file", written, ok=True)
    check("the read-after-write gate holds while nothing failed",
          repair.check(workspace_tools.READ_TOOL_NAME, written) is not None)
    repair.record("run_tests", {}, ok=False, content="Runner: Vitest\nExit Code: 1\nFAIL x\n")
    check("...and a failing validation still unlocks that read",
          repair.check(workspace_tools.READ_TOOL_NAME, written) is None,
          "#4.14: the agent must be able to read back what it wrote")
    check("...and still unlocks the rewrite",
          repair.check("write_file", written) is None,
          "#43/#4.14: the repair path must stay open")


def check_the_read_cache_records_the_window_the_tool_serves() -> None:
    """守卫记账的区间必须等于工具**真正返回**的区间，否则合法的续读会被当成重叠拦下。

    `read_file` 的 `limit` 上限是 `MAX_READ_LIMIT`(200)，而 `bounded_args` 原先拿**归一后的
    键**去判 `startswith("/workspace/")` —— 归一已经把这个前缀去掉了，判定恒假，上限从没
    生效：`read_file(path, limit=500)` 按 (0, 500) 记账，工具只返回 1-200 行，于是再读
    `offset=200` 被判成"重叠"拦下，附一句"用之前的结果" —— 而 201-300 行从来没给过。

    这条走**真的守卫外壳**（`guard_tools`），所以断言的是端到端行为：工具看到的参数、
    以及拦不拦下一次续读。
    """

    print()
    print("--- the read cache records the window the tool serves ---")

    seen: list[dict] = []

    async def fake_read_file(
        file_path: str, offset: int | None = None, limit: int | None = None
    ) -> str:
        seen.append({"file_path": file_path, "offset": offset, "limit": limit})
        return "line one\n"

    discipline = stage_policy.StageDiscipline(stage="implementation")
    tools = {
        tool.name: tool
        for tool in stage_policy.guard_tools(
            [StructuredTool.from_function(coroutine=fake_read_file, name="read_file", description="read")],
            discipline,
        )
    }

    async def run() -> tuple[str, str]:
        first = await tools["read_file"].ainvoke(
            {"file_path": "/workspace/backend/src/big.js", "limit": 500}
        )
        second = await tools["read_file"].ainvoke(
            {"file_path": "/workspace/backend/src/big.js", "offset": 200, "limit": 100}
        )
        return first, second

    first, second = asyncio.run(run())

    check("the tool is asked for at most MAX_READ_LIMIT lines",
          bool(seen) and seen[0]["limit"] == stage_policy.MAX_READ_LIMIT,
          f"tool saw {seen[0] if seen else None}")
    check("the first read is served", not first.startswith(stage_policy.BLOCKED_PREFIX), first[:60])
    check("a continuation past the served window is allowed",
          not second.startswith(stage_policy.BLOCKED_PREFIX),
          f"the guard billed {stage_policy.MAX_READ_LIMIT}..500 as if it had been delivered")


def check_a_model_call_cannot_hang_forever() -> None:
    """一次静默挂起的模型调用不能吃掉整轮预算（SCORING_AND_OPTIMIZATION §4.18）。

    实测（`run-logs/2026-09-28-hackathon--github-c847c43-outage`）：14:54:10 报
    `Connection error` 之后 **46 分钟**日志一个字都没有，最后靠本地看门狗才停下。
    三层机制都没兜住：`retry.py` 只重试**已经失败**的调用（第 2 次尝试根本没返回）、
    `stream_chunk_timeout` 只管**分片之间**（响应头都没到它不启动）、SDK 读超时要 600s
    才第一次失败。

    这条不扫源码，而是**拿一个永不返回的假模型**去撞整段上限：它必须被掀翻，
    而且要在上限附近回来 —— 否则下一次网络抖动还是会白烧掉半轮。
    """

    print()
    print("--- a model call cannot hang forever ---")

    from app import config as app_config  # noqa: PLC0415
    from app.agent import retry  # noqa: PLC0415

    class _HangingModel:
        async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003, ANN201
            await asyncio.sleep(60)
            return "never returned"

    class _OkModel:
        async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003, ANN201
            return "fine"

    async def probe() -> tuple[bool, float, str]:
        saved = retry.LLM_CALL_DEADLINE
        try:
            retry.LLM_CALL_DEADLINE = 0.2
            started = time.perf_counter()
            try:
                await retry._ainvoke_with_deadline(_HangingModel(), [])
            except retry.ModelCallDeadlineExceeded:
                raised = True
            else:
                raised = False
            elapsed = time.perf_counter() - started
            retry.LLM_CALL_DEADLINE = 0  # 关掉就该回到"直接调"的老行为
            unchanged = await retry._ainvoke_with_deadline(_OkModel(), [])
        finally:
            retry.LLM_CALL_DEADLINE = saved
        return raised, elapsed, unchanged

    try:
        raised, elapsed, unchanged = asyncio.run(asyncio.wait_for(probe(), 20))
    except asyncio.TimeoutError:
        raised, elapsed, unchanged = False, float("inf"), "<the probe itself hung>"

    check("a hanging model call is cut off", raised, "the call was allowed to run on")
    check("it comes back at the deadline, not minutes later",
          elapsed < 5, f"elapsed={elapsed:.2f}s (deadline was 0.2s)")
    check("a call without a deadline is unchanged", unchanged == "fine", str(unchanged))

    error = retry.ModelCallDeadlineExceeded("单次模型调用超过 600s 未返回")
    check("the retry layer treats the deadline as transient",
          retry.is_retryable(error),
          "a non-retryable verdict would kill the node instead of retrying it")
    check("a non-zero default deadline is configured",
          bool(app_config.LLM_CALL_DEADLINE), f"got {app_config.LLM_CALL_DEADLINE!r}")
    check("it is longer than the stream-chunk watchdog",
          (app_config.LLM_CALL_DEADLINE or 0) > (app_config.LLM_STREAM_CHUNK_TIMEOUT or 0),
          f"deadline={app_config.LLM_CALL_DEADLINE} chunk={app_config.LLM_STREAM_CHUNK_TIMEOUT}")


def check_interface_contract_is_not_duplicated() -> None:
    """Ensure normal shared context does not repeat the full interface JSON."""

    print()
    print("--- interface contract is not duplicated ---")

    from agents.context.prompts import test_driven_developer, test_generator

    dynamic = "<interfaces>\n[{\"interface_id\": \"IF-1\"}]\n</interfaces>"
    contract = "[{\"interface_id\": \"IF-1\"}]"
    generator_prompt = test_generator.get_user_prompt(
        node_id="REQ-1",
        requirement_data={"id": "REQ-1"},
        dynamic_context=dynamic,
        interface_contract=contract,
    )
    check(
        "TestGenerator does not repeat a contract already in dynamic context",
        "### Current Interface Contract" not in generator_prompt,
    )

    developer_prompt = test_driven_developer.get_user_prompt(
        node_id="REQ-1",
        dynamic_context=dynamic,
        interface_contract=contract,
        test_files=[],
        test_type="Unit",
        node_tests=[],
    )
    check(
        "TestDrivenDeveloper does not repeat a contract already in dynamic context",
        "### Current Interface Contract" not in developer_prompt,
    )

    fallback_prompt = test_generator.get_user_prompt(
        node_id="REQ-1",
        requirement_data={"id": "REQ-1"},
        dynamic_context="<requirement_focus/>",
        interface_contract=contract,
    )
    check(
        "prompt keeps the explicit contract fallback",
        "### Current Interface Contract" in fallback_prompt,
    )


def check_guard_unlocks_after_a_failing_validation() -> None:
    """A failing `run_tests` must unlock the paths this stage already wrote.

    Regression: the port replaced upstream's content-based `_tool_result_failed`
    with an exception-based flag. `run_tests`/`run_build` report failure in their
    *text* (`Exit Code: 1`) and never raise, so the unlock never fired - once a
    test file was written, the agent could not repair it for the rest of the
    stage. On the platform that deadlocked 133 tool calls (REQ-1.1: 98,
    REQ-1.2: 35) and left four unit tests red that the agent had already
    diagnosed.
    """

    print()
    print("--- stage discipline unlocks after a failing validation ---")

    async def fake_run_tests(test_type: str | None = None, test_files: list[str] | None = None) -> str:
        return "Runner: Vitest\nExit Code: 1\nSTDERR:\nFAIL tests/authSchema.test.js\n"

    async def fake_edit_file(file_path: str, content: str) -> str:
        return "written"

    async def fake_read_file(file_path: str, offset: int = 0, limit: int = 100) -> str:
        return "line one\nline two\n"

    discipline = stage_policy.StageDiscipline(stage="implementation")
    tools = {
        tool.name: tool
        for tool in stage_policy.guard_tools(
            [
                StructuredTool.from_function(coroutine=fake_run_tests, name="run_tests", description="run tests"),
                StructuredTool.from_function(coroutine=fake_edit_file, name="edit_file", description="write"),
                StructuredTool.from_function(coroutine=fake_read_file, name="read_file", description="read"),
            ],
            discipline,
        )
    }

    target = "backend/tests/authSchema.test.js"

    async def run() -> tuple[str, str, str, str]:
        first = await tools["edit_file"].ainvoke({"file_path": target, "content": "require('vitest')"})
        failure = await tools["run_tests"].ainvoke({})
        repair = await tools["edit_file"].ainvoke({"file_path": target, "content": "import { it } from 'vitest'"})
        reread = await tools["read_file"].ainvoke({"file_path": target})
        return first, failure, repair, reread

    first, failure, repair, reread = asyncio.run(run())

    check("first write passes", not first.startswith(stage_policy.BLOCKED_PREFIX), first[:60])
    check("run_tests reports Exit Code: 1", "Exit Code: 1" in failure, failure[:60])
    check("the repair write is allowed after that failure",
          not repair.startswith(stage_policy.BLOCKED_PREFIX), repair[:120])
    check("re-reading the repaired file is allowed",
          not reread.startswith(stage_policy.BLOCKED_PREFIX), reread[:120])


def check_guard_reads_the_real_validation_format() -> None:
    """The guard's unlock rests on a text contract - prove the two sides still agree.

    `_tool_result_failed` decides success by looking for `Exit Code:` in the tool's
    *text*. Nothing else ties the guard to the code that writes that text: rename the
    marker and the guard simply stops unlocking, silently re-arming the deadlock while
    every other check stays green.

    Two independent witnesses:
      1. every producer/parser module still emits the literal marker;
      2. the guard and the real parser (`test_results.parse_test_results`) reach the
         same verdict on the same bytes.
    """

    print()
    print("--- guard reads the real validation output format ---")

    for relative in VALIDATION_OUTPUT_SOURCES:
        source = REPO / relative
        text = source.read_text(encoding="utf-8", errors="replace") if source.is_file() else ""
        found = EXIT_CODE_MARKER in text
        check(
            f"{relative} still emits `{EXIT_CODE_MARKER}`",
            found,
            "" if found else "marker renamed - update stage_policy._tool_result_failed "
                             "and this list together",
        )

    failing = "Runner: Vitest\nExit Code: 1\nSTDERR:\nFAIL tests/authSchema.test.js\n"
    passing = "Runner: Vitest\nExit Code: 0\nSTDOUT:\nTest Files  3 passed (3)\n"
    unmarked = "Runner: Vitest\nrunner crashed before reporting\n"

    check("guard: a non-zero exit reads as failure", stage_policy._tool_result_failed(failing))
    check("guard: a zero exit does not", not stage_policy._tool_result_failed(passing))
    check("guard: output without the marker does not unlock",
          not stage_policy._tool_result_failed(unmarked))

    parsed_failing = test_results.parse_test_results(failing)["exit_code"]
    parsed_passing = test_results.parse_test_results(passing)["exit_code"]
    check("parser reads the failing output as non-zero",
          parsed_failing != 0, f"exit_code={parsed_failing}")
    check("parser reads the passing output as zero",
          parsed_passing == 0, f"exit_code={parsed_passing}")
    check(
        "guard and parser reach the same verdict",
        stage_policy._tool_result_failed(failing) == (parsed_failing != 0)
        and stage_policy._tool_result_failed(passing) == (parsed_passing != 0),
    )


def check_interface_contract_keeps_its_load_bearing_text() -> None:
    """`<interfaces>` 只许裁列表类噪音，`responsibility` / `specification` 必须全留。

    这份清单是本节点**自己的实现契约** —— agent 要照着它写代码。它同时是这份 JSON 的
    体积大头，所以「省上下文」的直觉会去截断它；但通过率是评分主导项，丢实现细节的代价
    比省下的 token 贵得多。这条检查就是钉住这个取舍，防止以后有人照搬别的节点的
    180/220 上限把它一起砍了。
    """

    print()
    print("--- interface contract keeps its load-bearing text ---")

    long_responsibility = "职责说明。" * 800      # 远大于 180
    long_specification = "实现细节。" * 800       # 远大于 220
    view = context_pipeline._interface_contract_view(
        [
            {
                "interface_id": "IF-1",
                "type": "API",
                "content": {
                    "responsibility": long_responsibility,
                    "specification": long_specification,
                    "callers": [f"caller-{i}" for i in range(10)],
                    "callees": [f"callee-{i}" for i in range(10)],
                    "test_focus": [f"focus-{i}" for i in range(10)],
                },
            }
        ]
    )

    check("the view keeps the interface", len(view) == 1, f"{len(view)} entries")
    if not view:
        return
    content = view[0].get("content") or {}
    check("responsibility survives untruncated",
          content.get("responsibility") == long_responsibility,
          f"{len(str(content.get('responsibility', '')))} chars")
    check("specification survives untruncated",
          content.get("specification") == long_specification,
          f"{len(str(content.get('specification', '')))} chars")
    check("callers are capped", len(content.get("callers") or []) == 3,
          f"{len(content.get('callers') or [])} kept")
    check("callees are capped", len(content.get("callees") or []) == 3,
          f"{len(content.get('callees') or [])} kept")
    check("test_focus is capped", len(content.get("test_focus") or []) == 4,
          f"{len(content.get('test_focus') or [])} kept")
    check("the identity fields are untouched",
          view[0].get("interface_id") == "IF-1" and view[0].get("type") == "API")


def check_timeout_output_is_classified_as_failure() -> None:
    """超时也是失败 —— 而且这条必须用**真实生产者**的返回值来断言。

    `_tool_result_failed` 的判据是「含 `Exit Code:` 且不是 `Exit Code: 0`」。
    `app_type_handler/web.py` 的超时分支原先返回 `Command timed out after 60.0 seconds.`，
    两条都不满足 → **判为「没失败」→ 解锁不触发**；而超时恰恰是最需要「让我改改再试一次」
    的那类失败。`test_results.parse_test_results` 同样解析不出退出码。

    实测：那次本地 run 里 34 次 `run_tests` 返回有 5 次如此
    （`run-logs/2026-09-26-hackathon-github-7e1ba30/debug.log`）。

    这里刻意**不写死字符串**，而是真的调一次 `_execute_web_test_command` 让它超时 ——
    上一条检查只扫「文件里有没有这个字面量」，而 `web.py` 的非超时分支当然有，
    所以它扫不出「同一个文件的另一个分支绕过了它」。只有从真实生产者取返回值，
    格式真的变了才会红。
    """

    print()
    print("--- timeout output is classified as failure ---")

    async def probe() -> str:
        return await web_handler._execute_web_test_command(
            f'"{sys.executable}" -c "import time; time.sleep(5)"',
            cwd=str(REPO),
            timeout=0.5,
        )

    output = asyncio.run(probe())
    detail = " ".join(output.split())[:95]

    check("the probe really timed out", "timed out" in output, detail)
    check(f"timeout output carries `{EXIT_CODE_MARKER}`", EXIT_CODE_MARKER in output, detail)
    check("guard treats a timeout as failure",
          stage_policy._tool_result_failed(output), detail)
    parsed = test_results.parse_test_results(output)["exit_code"]
    # 注意断言「正」而不是「非零」：解析不出来时 parse_test_results 返回哨兵 -1，
    # 那也是非零 —— 用 != 0 会让这条在格式坏掉时"蒙对"。要的是真的解析出了退出码。
    check("parser reads a timeout as a real positive exit code",
          parsed > 0, f"exit_code={parsed}（-1 表示没解析出来）")


def check_published_budget_matches_the_enforced_one() -> None:
    """提示词里公布的 `run_tests` 预算，必须等于执行层**真正强制**的那个数。

    原先提示词里硬编码 "a fixed `run_tests` budget of 10 calls"，而
    `core/phases.py` 的 `TDD_RUN_TESTS_BUDGET` 是 **5**（见 issue #10）。模型以为还有余量，
    于是收到 "budget exhausted at 5/5" 时不信、当成瞬时故障继续试 —— 实测盲改了 40 多分钟
    （见 issue #13）。

    这和 `render_stage_limits` 是同一类问题：**谁执行规则谁就得公布规则，而且文案必须由
    常量派生**。这条检查就是钉住那个派生关系，防止以后有人改了一边忘了另一边。
    """

    print()
    print("--- published run_tests budget matches the enforced one ---")

    from agents.context.prompts import test_driven_developer as tdd_prompt  # noqa: PLC0415
    from core.phases import TDD_RUN_TESTS_BUDGET  # noqa: PLC0415

    prompt = tdd_prompt.get_user_prompt(
        node_id="REQ-1",
        dynamic_context="<requirement_focus/>",
        test_files=[],
        test_type="Unit",
        node_tests=[],
        run_tests_budget=TDD_RUN_TESTS_BUDGET,
    )

    check(
        f"the prompt publishes the enforced budget ({TDD_RUN_TESTS_BUDGET})",
        f"budget of {TDD_RUN_TESTS_BUDGET} calls" in prompt,
        "prompt text must be derived from TDD_RUN_TESTS_BUDGET",
    )
    check(
        "the prompt no longer hardcodes a different number",
        "budget of 10 calls" not in prompt,
        "found the stale hardcoded '10 calls'",
    )


def check_clean_survives_readonly_git_objects() -> None:
    """`--clean` 必须能在 Windows 上删掉带只读属性的 git 对象（issue #12）。

    Windows 上 `.git/objects/*` 带 `FILE_ATTRIBUTE_READONLY`，默认的 `shutil.rmtree` 会抛
    `PermissionError: [WinError 5]`，让 `--clean` 整个失败 —— 复跑同一个输出目录时必踩。
    Linux 不受影响（unlink 的权限看父目录），所以「plain rmtree 会失败」这个前提只在
    Windows 上成立，那一步在别人机器上跳过、但 `_rmtree_force` 那一步两边都验。
    """

    print()
    print("--- --clean survives read-only git objects ---")

    import shutil as _shutil  # noqa: PLC0415
    import stat as _stat  # noqa: PLC0415
    import sys as _sys  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    import arc_main  # noqa: PLC0415

    def seed_readonly_tree(root: Path) -> None:
        obj = root / ".git" / "objects" / "ab" / "cdef0123456789"
        obj.parent.mkdir(parents=True, exist_ok=True)
        obj.write_text("object\n", encoding="utf-8")
        obj.chmod(_stat.S_IREAD)  # 模拟 git 对象的只读属性

    plain_failed = False
    with tempfile.TemporaryDirectory(prefix="arc-clean-probe-", ignore_cleanup_errors=True) as tmp:
        seed_readonly_tree(Path(tmp))
        try:
            _shutil.rmtree(tmp)
        except PermissionError:
            plain_failed = True

        if _sys.platform == "win32":
            check("plain rmtree fails on a read-only git object (reproduces #12)",
                  plain_failed, "it unexpectedly succeeded - probe is not reproducing")
        else:
            print("  SKIP  plain-rmtree reproduction (POSIX unlink ignores the read-only bit)")

    with tempfile.TemporaryDirectory(prefix="arc-clean-probe-", ignore_cleanup_errors=True) as tmp:
        seed_readonly_tree(Path(tmp))
        try:
            arc_main._rmtree_force(tmp)
            removed, detail = not Path(tmp).exists(), ""
        except Exception as exc:  # noqa: BLE001 - 异常本身就是结论
            removed, detail = False, f"{type(exc).__name__}: {exc}"
        check("_rmtree_force removes it", removed, detail)


def check_a_stage_can_stop_its_own_agent_session() -> None:
    """预算耗尽必须能**终止 session**，而不只是让工具拒绝（issue #13）。

    工具层说「不」是拦不住模型的：它会把 ``run_tests budget exhausted`` 当成一次普通失败，
    接着改、接着试。实测那次它收到 9 次拒绝仍空转了 **18.6 分钟**、写下 **27 个再也无法验证**
    的文件，最后是人工杀的 —— ``BUDGET_EXHAUSTED`` 和 ``TDD batch`` 都没有出现在日志里，
    也就是说这一层连判定都没产出。

    这条检查覆盖整条链：谓词能被置位 → runner 真的中断流 → **不回落到 ainvoke**
    （回落的后果是把整个 agent 重跑一遍，正是我们刚刚决定不要做的事）。
    最后两条源码断言防止接线被悄悄拆掉。
    """

    print()
    print("--- a stage can stop its own agent session ---")

    from agents.runtime import runners  # noqa: PLC0415

    tdd = test_driven_developer.TestDrivenDeveloper()
    check("a fresh agent has no exhausted budget", not tdd._test_budget_exhausted)
    tdd.note_test_budget_exhausted("Unit")
    check("the executor's callback flips it", tdd._test_budget_exhausted)
    check("it remembers which layer", tdd._test_budget_exhausted_type == "Unit",
          f"got {tdd._test_budget_exhausted_type!r}")

    class _FakeStreamingAgent:
        """只会一直吐事件的假 agent，用来观察流是不是被中断。"""

        def __init__(self) -> None:
            self.events = 0
            self.ainvoke_called = False

        async def astream_events(self, *_args, **_kwargs):
            for _ in range(50):
                self.events += 1
                yield {"event": "on_tool_start", "name": "run_tests", "data": {"input": {}}}

        async def ainvoke(self, *_args, **_kwargs):
            self.ainvoke_called = True
            return {"messages": []}

    async def drive() -> tuple[_FakeStreamingAgent, object]:
        agent = _FakeStreamingAgent()

        def should_stop() -> bool:
            return agent.events >= 3

        payload = await runners.ainvoke_stage_agent(
            agent,
            message="probe: do not actually run anything",
            context=AgentRuntimeContext(
                node_id="REQ-1",
                phase="IMPLEMENT",
                app_type="web",
                workspace_root=str(REPO),
                requirement_path="",
            ),
            thread_id="stop-predicate-probe",
            label="probe",
            should_stop=should_stop,
        )
        return agent, payload

    agent, payload = asyncio.run(drive())

    check("the stream stopped long before its natural end",
          agent.events < 50, f"consumed {agent.events}/50 events")
    check("it did NOT fall back to ainvoke",
          not agent.ainvoke_called,
          "falling back would re-run the whole agent - exactly what the stop was for")
    check("the early stop still returns a usable payload",
          isinstance(payload, dict) and bool(payload.get("summary")), f"payload={payload!r}")

    tdd_source = (REPO / "agents" / "test_driven_developer.py").read_text(encoding="utf-8", errors="replace")
    check("the TDD agent wires the predicate into its agent call",
          "should_stop=lambda: self._test_budget_exhausted" in tdd_source,
          "the wiring was removed, so nothing would stop the session")
    phases_source = (REPO / "core" / "phases.py").read_text(encoding="utf-8", errors="replace")
    check("the executor that enforces the budget reports it back",
          "note_test_budget_exhausted(selected_type)" in phases_source,
          "the enforcing side must flip the flag, or the predicate never fires")


def check_timeouts_are_configurable_and_distinguishable() -> None:
    """测试命令的超时既要**可配**，又要**可区分**（issue #21）。

    两件事都是实跑逼出来的。`run-logs/2026-09-26-hackathon--github-99d44e6` 里
    `REQ-1-1-1` 的 Unit 5/5 与 Integration 5/5 被判「失败」，实际全是 60 秒超时：

    * **不可配**：`timeout=60.0` 硬编码，本地 Windows 冷启动 node/vitest 稳定超过它。
      agent 拿到的是一次「失败」，可超时值在它自己的配置里、不在 `/workspace`，
      提示词却要求它就地修 harness —— 它没有任何杠杆，只能把整层预算耗在重试上。
    * **不可区分**：`run_test_group` 把任何非零塌缩成 1，而外层信封的退出码是
      `parse_test_results` 唯一会读的那个 → 「测试跑了且失败」和「测试没跑完」
      在层判定里长得一模一样。

    这条检查对两者都做行为验证，而不是扫源码：真的让它超时、真的比退出码。
    """

    print()
    print("--- timeouts are configurable and distinguishable ---")

    saved = os.environ.get("ARC_TEST_COMMAND_TIMEOUT")

    try:
        os.environ.pop("ARC_TEST_COMMAND_TIMEOUT", None)
        check("default timeout stays 60.0 (platform behaviour unchanged)",
              web_handler._default_test_command_timeout() == 60.0,
              f"got {web_handler._default_test_command_timeout()}")

        for raw, expected in (("90", 90.0), ("0.5", 0.5)):
            os.environ["ARC_TEST_COMMAND_TIMEOUT"] = raw
            check(f"ARC_TEST_COMMAND_TIMEOUT={raw} is honoured",
                  web_handler._default_test_command_timeout() == expected,
                  f"got {web_handler._default_test_command_timeout()}")

        for raw in ("", "  ", "abc", "0", "-5"):
            os.environ["ARC_TEST_COMMAND_TIMEOUT"] = raw
            check(f"garbage {raw!r} falls back to 60.0",
                  web_handler._default_test_command_timeout() == 60.0,
                  f"got {web_handler._default_test_command_timeout()}")

        # 端到端：不传 timeout 时走环境变量。这条同时证明「配置真的到达了执行点」，
        # 而不只是「那个读 env 的函数返回了对的值」。
        os.environ["ARC_TEST_COMMAND_TIMEOUT"] = "0.5"

        async def probe() -> tuple[str, float]:
            started = time.perf_counter()
            out = await web_handler._execute_web_test_command(
                f'"{sys.executable}" -c "import time; time.sleep(5)"',
                cwd=str(REPO),
            )
            return out, time.perf_counter() - started

        output, elapsed = asyncio.run(probe())
        check("the env-var timeout reaches the real execution point",
              "Exit Code: 124" in output and elapsed < 4.0,
              f"elapsed={elapsed:.2f}s output={output[:80]!r}")
    finally:
        if saved is None:
            os.environ.pop("ARC_TEST_COMMAND_TIMEOUT", None)
        else:
            os.environ["ARC_TEST_COMMAND_TIMEOUT"] = saved

    merge = web_handler._batch_exit_code
    check("all sub-batches passing -> 0", merge([0, 0]) == 0, f"got {merge([0, 0])}")
    check("a plain failure stays 1", merge([1, 1]) == 1, f"got {merge([1, 1])}")
    check("a timeout is NOT collapsed to 1", merge([124]) == 124, f"got {merge([124])}")
    check("a timeout among failures still wins", merge([1, 124]) == 124, f"got {merge([1, 124])}")
    check("no sub-batches -> 1", merge([]) == 1, f"got {merge([])}")

    envelope = "Exit Code: 124\n\n=== Backend Vitest Batch ===\nExit Code: 124\nSTDERR:\nCommand timed out after 0.5 seconds.\n"
    parsed = test_results.parse_test_results(envelope)["exit_code"]
    check("the parser reads a timed-out envelope as 124, not 1",
          parsed == 124, f"exit_code={parsed}")
    check("the parser still reads a passing envelope as 0",
          test_results.parse_test_results("Exit Code: 0\n\n=== Backend Vitest Batch ===\nExit Code: 0\n")["exit_code"] == 0)


def check_test_runners_fail_fast_instead_of_hanging() -> None:
    """测试 runner 缺失时要**秒级失败**，超时也要**留住现场**（issue #24）。

    第 4 轮本地判题把超时从 60 秒放宽到 180 秒后，Unit 层**依然**每次超时（两次相邻调用间隔
    3 分 12 秒）。这否掉了「60 秒太紧」的归因 —— 不是慢，是挂起。根因是
    `node_modules` 里**没有 vitest**（`package.json` 声明了），而 `npx` 找不到本地包时会
    转去下载并在有 TTY 时停下来问 `Ok to proceed? (y)`，永远等不到输入。

    放大它的两件事：`try_install_dependencies` 只在阶段早期跑过一次（backend 3 秒、
    frontend 10 秒就「成功」，正常装不完），之后 agent 为修 runner 加的依赖永不安装；
    而超时分支把子进程已经打印的内容整个丢掉，导致现场无法复盘。

    这条检查三样都验：本地二进制解析、缺失信息的质量、以及超时是否留住输出。
    """

    print()
    print("--- test runners fail fast instead of hanging ---")

    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="arc-runner-probe-") as tmp:
        check("a missing binary resolves to None",
              web_handler._local_node_binary(tmp, "vitest") is None)

        bin_dir = Path(tmp) / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "vitest").write_text("#!/bin/sh\n", encoding="utf-8")
        resolved = web_handler._local_node_binary(tmp, "vitest")
        check("an installed binary resolves to its local path",
              resolved is not None and "node_modules" in resolved, f"got {resolved!r}")

    message = web_handler._missing_runner_message("/workspace/frontend", "vitest")
    check("the missing-runner reply fails instead of hanging",
          message.startswith("Exit Code: 1"), message.splitlines()[0])
    check("it names the missing binary", "vitest" in message)
    check("it says how to fix it", "npm install" in message)
    check("the guard reads it as a failure", stage_policy._tool_result_failed(message))

    async def probe() -> str:
        return await web_handler._execute_web_test_command(
            f'"{sys.executable}" -c "import time; print(\'MARKER_BEFORE_HANG\', flush=True); time.sleep(5)"',
            cwd=str(REPO),
            timeout=0.5,
        )

    output = asyncio.run(probe())
    check("a timeout still reports Exit Code: 124",
          "Exit Code: 124" in output, output.splitlines()[0] if output else "")
    check("a timeout keeps what the process had already printed",
          "MARKER_BEFORE_HANG" in output,
          "partial output was discarded - that is what made the npx hang undiagnosable")


def check_install_is_verified_not_assumed() -> None:
    """「装完了」必须是**校验过**的，不能只看 `returncode == 0`（issue #26）。

    实跑证据：backend 的 `node_modules/.bin` 里最终只有 node-gyp / prebuild-install /
    rimraf 这些原生模块的构建工具链，**测试与构建工具一个都没有** —— 而 `run_npm_install`
    打印的是 `NPM install success`，因为它只检查退出码。于是问题一路藏到
    `run_build` 打出 `'vite' is not recognized` 才暴露，那时 TDD 已经把整层预算耗光。

    这条检查针对 `_missing_declared_dependencies` 做行为验证：它就是要用来在安装后
    点名「声明了但没装上」的包。
    """

    print()
    print("--- the install result is verified, not assumed ---")

    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="arc-install-probe-") as tmp:
        root = Path(tmp)
        check("no package.json -> nothing to check",
              web_handler._missing_declared_dependencies(tmp) == [])

        (root / "package.json").write_text(
            json.dumps(
                {
                    "dependencies": {"react": "^19.0.0"},
                    "devDependencies": {"vitest": "^3.0.0", "vite": "^7.0.0"},
                    "optionalDependencies": {"fsevents": "^2.0.0"},
                }
            ),
            encoding="utf-8",
        )
        missing = web_handler._missing_declared_dependencies(tmp)
        check("declared-but-absent packages are all reported",
              sorted(missing) == ["fsevents", "react", "vite", "vitest"],
              f"got {sorted(missing)}")

        # 装上一个，它就该从缺失清单里消失。
        (root / "node_modules" / "vitest").mkdir(parents=True)
        missing_after = web_handler._missing_declared_dependencies(tmp)
        check("an installed package drops out of the list",
              "vitest" not in missing_after and "vite" in missing_after,
              f"got {sorted(missing_after)}")

        # 作用域包要按两级路径展开，否则永远误报缺失。
        (root / "node_modules" / "@scope" / "pkg").mkdir(parents=True)
        (root / "package.json").write_text(
            json.dumps({"devDependencies": {"@scope/pkg": "1.0.0"}}), encoding="utf-8"
        )
        check("a scoped package resolves through its two-level path",
              web_handler._missing_declared_dependencies(tmp) == [],
              f"got {web_handler._missing_declared_dependencies(tmp)}")

        # 空目录下 `npm install` 的经典产物：exit 0 + node_modules 只有构建工具链。
        (root / "package.json").write_text(
            json.dumps({"devDependencies": {"vite": "^7.0.0"}}), encoding="utf-8"
        )
        (root / "node_modules" / "node-gyp").mkdir(parents=True, exist_ok=True)
        check("exit-0-but-incomplete is exactly what this catches",
              web_handler._missing_declared_dependencies(tmp) == ["vite"],
              f"got {web_handler._missing_declared_dependencies(tmp)}")


def check_install_requests_dev_dependencies() -> None:
    """安装必须显式要 dev 依赖，不能被宿主的 `NODE_ENV=production` 决定（issue #27）。

    这是「测试与构建工具永远不可用」的真正根因：这个项目的测试与构建工具链**全部声明在
    `devDependencies`**（vitest / playwright / supertest / vite / tailwind…），而宿主的
    `NODE_ENV=production` 让 npm 推断出 `omit=dev`，一个都不装 —— **npm 仍退出 0、
    仍报 `added N packages`**，所以从退出码到日志都看不出问题。实测 backend 的 6 个、
    frontend 的 17 个 devDeps 全部缺失。

    这条检查刻意做**真行为验证**而不是扫源码：在 `NODE_ENV=production` 下跑一次真实的
    `npm install`，断言一个 devDependency 确实落到了 `node_modules`。依赖用本地
    `file:` 路径声明，所以不碰网络、也不需要已有缓存。npm 不可用时跳过而不是误报。
    """

    print()
    print("--- the install requests dev dependencies ---")

    import shutil as _shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    if _shutil.which("npm") is None:
        print("  SKIP  npm is not on PATH")
        return

    with tempfile.TemporaryDirectory(prefix="arc-devdep-probe-") as tmp:
        root = Path(tmp)
        probe = root / "probe-dep"
        probe.mkdir()
        (probe / "package.json").write_text(
            json.dumps({"name": "arc-probe-dep", "version": "1.0.0"}), encoding="utf-8"
        )
        (root / "package.json").write_text(
            json.dumps(
                {
                    "name": "arc-probe-root",
                    "version": "1.0.0",
                    "dependencies": {"arc-probe-runtime": "file:./probe-dep"},
                    "devDependencies": {"arc-probe-dev": "file:./probe-dep"},
                }
            ),
            encoding="utf-8",
        )

        messages: list[str] = []

        def collect(_name: str, message: str, *_rest) -> None:
            messages.append(str(message))

        saved = os.environ.get("NODE_ENV")
        # 关键：复刻宿主环境。没有修复时，这一行会让 devDependencies 被整个跳过。
        os.environ["NODE_ENV"] = "production"
        try:
            asyncio.run(web_handler.run_npm_install(tmp, collect))
        finally:
            if saved is None:
                os.environ.pop("NODE_ENV", None)
            else:
                os.environ["NODE_ENV"] = saved

        installed = (root / "node_modules" / "arc-probe-dev").exists()
        check("a devDependency is installed even with NODE_ENV=production",
              installed,
              f"missing; npm said: {' | '.join(messages)[:300]}")

        check("the install reports success for a complete tree",
              any("NPM install success" in m for m in messages),
              " | ".join(messages)[:300])
        check("it did not report a failure",
              not any("did NOT complete" in m for m in messages),
              " | ".join(m for m in messages if "did NOT complete" in m)[:300])


def check_extra_tools_are_published_and_wired() -> None:
    """Tool assembly and prompt publication must share one stage configuration."""

    print()
    print("--- extra tools are published and wired ---")
    from agents.runtime import factory

    for stage in ("interface_design", "test_generation", "implementation"):
        configured = set(EXTRA_TOOLS_BY_STAGE.get(stage, ()))
        published = set(re.findall(r"`([a-zA-Z_][a-zA-Z0-9_]*)`", render_extra_tools(stage)))
        check(f"{stage}: published names match configuration", published == configured,
              f"published={sorted(published)} configured={sorted(configured)}")
        if stage != "implementation":
            continue
        built = build_extra_tools(stage, read_tools=FILE_TOOLS, workspace_root=str(REPO))
        check("implementation exposes all five extra tools",
              {item.name for item in built} == configured,
              str([item.name for item in built]))
        from agents.runtime.factory import build_stage_agent
        probe = build_stage_agent(
            name="implementation_bridge_probe",
            stage=stage,
            system_prompt="BASE PROMPT",
            workspace_root=str(REPO),
        )
        todo_objects = {id(item) for item in TODO_TOOLS}
        check("injected todo tools bypass guard wrapping",
              all(id(item) in todo_objects for item in probe.tools if item.name in configured - {"subagent"}))
        assembled = factory._with_response_shape(
            "BASE PROMPT", response_format=None, skills=None,
            permitted_skill_names=None, stage=stage,
        )
        check("extra tool publication follows stage limits",
              assembled.index("### Extra Tools (available)") > assembled.index("### Stage Limits (enforced)"))


def check_subagent_read_only_boundary() -> None:
    """The subgraph constructor rejects write tools and exposes a small schema."""

    print()
    print("--- subagent read-only boundary ---")
    from app.agent.tools.subagent import build_subagent_tool
    from agents.runtime.workspace_tools import edit_file, read_file, write_file

    for label, forbidden in (("write_file", write_file), ("edit_file", edit_file)):
        try:
            build_subagent_tool(read_tools=[forbidden], workspace_root=str(REPO))
        except AssertionError:
            rejected = True
        else:
            rejected = False
        check(f"subagent rejects {label}", rejected)

    child = build_subagent_tool(read_tools=[read_file], workspace_root=str(REPO))
    check("subagent public schema has only description and prompt",
          set(child.tool_call_schema.model_fields) == {"description", "prompt"})


def check_todo_and_command_behaviour() -> None:
    """Todo validation and Command updates must survive the handwritten tool node."""

    print()
    print("--- todo and Command behaviour ---")

    def invoke(tool_obj, args, state, call_id):
        injected = _inject_tool_args(tool_obj, args, call_id, state)
        return asyncio.run(tool_obj.ainvoke({
            "name": tool_obj.name, "args": injected, "id": call_id, "type": "tool_call",
        }))

    state: dict = {"messages": [], "todo_list": []}
    command = invoke(add_todo, {"task_desc": "first"}, state, "todo-1")
    check("add_todo returns Command", isinstance(command, Command))
    check("add_todo returns only the new item", command.update["todo_list"]["item"]["task_desc"] == "first")
    state["todo_list"] = reduce_todos(state["todo_list"], command.update["todo_list"])
    first_id = state["todo_list"][0]["id"]
    command = invoke(update_todo, {"id": first_id, "status": "in_progress"}, state, "todo-2")
    check("update_todo returns only the changed item", command.update["todo_list"]["item"]["id"] == first_id)
    state["todo_list"] = reduce_todos(state["todo_list"], command.update["todo_list"])
    check("update_todo enters in_progress", state["todo_list"][0]["status"] == "in_progress")

    command = invoke(list_todo, {}, state, "todo-3")
    check("list_todo does not write a todo patch", "todo_list" not in command.update)
    listed = json.loads(command.update["messages"][0].content)
    check("list_todo returns the complete list", listed == state["todo_list"])

    command = invoke(complete_todo, {"id": first_id}, state, "todo-4")
    check("complete_todo returns only the completed item", command.update["todo_list"]["item"]["id"] == first_id)
    state["todo_list"] = reduce_todos(state["todo_list"], command.update["todo_list"])
    check("complete_todo marks the item completed", state["todo_list"][0]["status"] == "completed")

    try:
        invoke(update_todo, {"id": "missing", "status": "completed"}, state, "todo-bad-id")
    except ToolException:
        bad_id = True
    else:
        bad_id = False
    check("invalid todo id uses fail()", bad_id)

    state["todo_list"][0]["status"] = "in_progress"
    state["todo_list"].append({"id": "t2", "task_desc": "second", "status": "pending", "progress": "", "deadline": ""})
    try:
        invoke(update_todo, {"id": "t2", "status": "in_progress"}, state, "todo-bad-active")
    except ToolException:
        two_active = True
    else:
        two_active = False
    check("two in_progress todos use fail()", two_active)

    state["todo_list"] = [
        {"id": f"t{i}", "task_desc": str(i), "status": "pending", "progress": "", "deadline": ""}
        for i in range(1, MAX_TODOS + 1)
    ]
    try:
        invoke(add_todo, {"task_desc": "overflow"}, state, "todo-overflow")
    except ToolException:
        overflow = True
    else:
        overflow = False
    check("todo limit uses fail()", overflow)

    batch = [
        {"name": "add_todo", "args": {"task_desc": "batch one"}, "id": "todo-batch-1", "type": "tool_call"},
        {"name": "add_todo", "args": {"task_desc": "batch two"}, "id": "todo-batch-2", "type": "tool_call"},
    ]
    batch_results = asyncio.run(run_calls(
        batch, {"configurable": {"tools": TODO_TOOLS}}, {}, {"messages": [], "todo_list": []},
    ))
    batch_state = []
    for result in batch_results:
        batch_state = reduce_todos(batch_state, result.update["todo_list"])
    final_ids = [item["id"] for item in batch_state]
    check("same-batch add_todo calls allocate distinct ids", final_ids == ["t1", "t2"])

    @tool
    async def command_probe(value: str) -> Command:
        """Return a state update and its matching tool message."""
        return Command(update={
            "todo_list": [{"id": "t1", "task_desc": value, "status": "pending", "progress": "", "deadline": ""}],
            "messages": [ToolMessage(content="probe-ok", tool_call_id="probe-1")],
        })

    from langchain_core.runnables import RunnableLambda
    output = asyncio.run(RunnableLambda(run_tools).ainvoke(
        {"messages": [AIMessage(content="", tool_calls=[{
            "name": "command_probe", "args": {"value": "state"}, "id": "probe-1", "type": "tool_call",
        }])], "todo_list": []},
        config={"configurable": {"tools": [command_probe]}},
    ))
    check("run_tools preserves Command state update", output.get("todo_list", [{}])[0].get("task_desc") == "state")
    check("run_tools preserves Command ToolMessage", isinstance(output.get("messages", [None])[0], ToolMessage))


def check_a_passed_layer_is_not_rerun() -> None:
    """层已通过后必须拒绝重跑，且**拒绝的判定要排在预算检查之前**（issue #33）。

    实测（第 6 轮）：Unit 在 attempt 2 就已通过，却一路跑到 5/5；Integration 同样。
    **12 次带真实退出码的调用里 8 次是「已通过之后的重跑」。** 代价直接计入评分
    （score -> pass rate -> **lower cost** -> 提交时间），而且挤占真正需要修复时的预算
    （第 6 轮 Unit 的 attempt 1 就是真实失败）。

    这条检查盯的是**顺序**，那是承重的一点：守卫若排在预算检查之后，一个已经 5/5 通过的层
    会先被判成「预算耗尽」并走 #13 的硬停路径 —— 回执语义完全不同（一个说「本层完成，
    返回 IMPLEMENTED」，一个说「别再试了」）。执行器是闭包、要驱动它得搭整套 TDD 环境，
    所以这里断言源码里的相对位置，而不是硬造一个假执行器去测。
    """

    print()
    print("--- a passed layer is not rerun ---")

    source = (REPO / "core" / "phases.py").read_text(encoding="utf-8", errors="replace")

    guard = "prior_exit_code == 0:"
    budget = "if used >= TDD_RUN_TESTS_BUDGET:"
    increment = "usage_by_type[selected_type] = used + 1"
    check("the repeat guard exists", guard in source, "guard removed - #33 would regress")
    check("the budget check still exists", budget in source)

    if guard in source and budget in source:
        check("the guard runs BEFORE the budget check",
              source.index(guard) < source.index(budget),
              "a layer that passed at 5/5 would be misreported as budget-exhausted")
    if guard in source and increment in source:
        check("the guard runs BEFORE the usage counter increments",
              source.index(guard) < source.index(increment),
              "a refused repeat would still consume layer budget")

    check("the refusal says the layer is complete",
          "already passed for this node and is recorded as complete" in source)
    check("the refusal reports Exit Code: 0, matching a completed layer",
          "already passed for this node" in source and '"Exit Code: 0\\n"' in source)


def check_engine_state_dir_is_not_searchable() -> None:
    """`.arc/` 必须在检索工具的默认排除里（issue #36）。

    ARC 引擎把自己的状态写进 agent 的 `/workspace` 的 `.arc/` 下（debug.log、
    processing_queue.json、node_sessions/、traceability/）。这些对 agent 没有任何可读价值，
    却是实打实的噪音：

    * 第 4 轮 grep 命中 `.arc/debug.log:10460:`
    * 第 8 轮 grep 命中我们自己的 stdout 转储 `runner-stdout.txt:7297:`
    * 第 7 轮的 `debug.log` 有 **2.8 MB**，读进来就是几万 token
    * 更糟的是日志里全是**历史**测试输出，可能让 agent 去修已经修好的东西

    `grep` 与 `find` 都经 `common.walk_files` 枚举 —— 所以排除加在 `SKIP_DIRS` 里就能
    同时覆盖两者。这条检查做行为验证：真造一棵含 `.arc/` 的树，看它会不会被枚举出来。
    """

    print()
    print("--- the engine state dir is not searchable ---")

    import tempfile  # noqa: PLC0415

    from app.agent.tools import common as tool_common  # noqa: PLC0415

    check(".arc is in the default skip set", ".arc" in tool_common.SKIP_DIRS)

    with tempfile.TemporaryDirectory(prefix="arc-skip-probe-") as tmp:
        root = Path(tmp)
        (root / ".arc").mkdir()
        (root / ".arc" / "debug.log").write_text("noise\n", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src" / "app.js").write_text("code\n", encoding="utf-8")

        found = {p.relative_to(root).as_posix() for p in tool_common.walk_files(root)}

    check("a file under .arc/ is not enumerated",
          ".arc/debug.log" not in found, f"got {sorted(found)}")
    check("a real source file still is",
          "src/app.js" in found, f"got {sorted(found)}")

    grep_src = (REPO / "app" / "agent" / "tools" / "grep.py").read_text(encoding="utf-8", errors="replace")
    find_src = (REPO / "app" / "agent" / "tools" / "find.py").read_text(encoding="utf-8", errors="replace")
    check("grep enumerates through walk_files", "walk_files(" in grep_src)
    check("find enumerates through walk_files", "walk_files(" in find_src)


def check_a_blocked_write_unlocks_its_path() -> None:
    """被纪律拦下的写入必须让**该路径**解锁，否则测试生成阶段会永久锁死（issue #43）。

    线上比赛实测（`run-logs/2026-09-27-competition-hackathon--sheet-bs`，201 分钟）：
    一个测试文件被 "Repeated write blocked" 拦了 **1142 次**，占全部拦截的 98.7%。

    死锁的构造：
    1. `_guard_tool.runner` 在拦截时**直接 return，从不调用 record** → 路径进不了 `_failed_paths`
    2. `_path_unlocked` = `_validation_failed or path in _failed_paths`
    3. `_validation_failed` 只由**验证工具失败**置位，而 `check()` 明确禁止
       `test_generation` 调用验证工具

    → TestGenerator 写过的路径**永久锁死，第一次写就是最终稿**。而守卫的拦截消息
    却写着「Wait for a file-operation or system-validation error」—— 被拦恰恰就是
    一次文件操作错误，承诺与实现相反。

    这条检查直接构造那个场景：写一次 → 被拦 → **再写必须放行**。
    """

    print()
    print("--- a blocked write unlocks its path ---")

    from agents.runtime import stage_policy  # noqa: PLC0415

    args = {"file_path": "/frontend/tests/workbook-rename.integration.test.tsx"}
    path = stage_policy._discipline_path(args)

    discipline = stage_policy.StageDiscipline(stage="test_generation")

    check("the first write is allowed", discipline.check("write_file", args) is None)
    discipline.record("write_file", args, ok=True)

    blocked = discipline.check("write_file", args)
    check("the second write is blocked (the rule still exists)",
          blocked is not None and "Repeated write" in blocked, f"got {blocked!r}")

    # 关键：被拦之后必须解锁 —— 否则就是线上那 1142 次循环。
    discipline.note_blocked("write_file", args)
    check("after being blocked, the same path can be written again",
          discipline.check("write_file", args) is None,
          "the deadlock is back: this path can never be corrected")
    # 注意：这个阶段还要求路径必须是**测试资产**（另一条规则），所以这里要用测试文件形态的
    # 路径。我第一版随手写了 /other.tsx，被那条规则拦下，断言误报成 FAIL。
    check("a test asset that was never blocked stays writable",
          discipline.check("write_file", {"file_path": "/frontend/tests/other.test.tsx"}) is None)
    check("note_blocked does NOT unlock the whole stage",
          discipline._validation_failed is False,
          "a plain write block must not flip the stage-wide unlock")
    check("the blocked path is the one recorded as failed",
          path in discipline._failed_paths, f"failed_paths={sorted(discipline._failed_paths)}")

    # 说明为什么这个阶段没法靠验证解锁：它被明确禁止调验证工具。
    validation_block = discipline.check("run_tests", {})
    check("test_generation is still forbidden from running validation",
          validation_block is not None, f"got {validation_block!r}")

    source = (REPO / "agents" / "runtime" / "stage_policy.py").read_text(encoding="utf-8", errors="replace")
    runner_start = source.index("async def runner(")
    guard_body = source[runner_start : source.index("args = discipline.bounded_args", runner_start)]
    check("the guard records the block instead of returning silently",
          "note_blocked(" in guard_body,
          "removing it would restore the 1142-retry deadlock")


def check_an_unattempted_layer_is_retried() -> None:
    """一次「没动手」的 session 不能把整个节点判死（issue #44 之后的新问题）。

    最新代码交的 sheet 题里 `REQ-1-1-1` 变红，收尾是：

        TDD batch `E2E` did not pass after **0/5** run_tests call(s)
        IMPLEMENT failed for node REQ-1-1-1.

    `0/5` 说明这一层**一次都没跑过** —— agent 在那一层的最后一条消息是一句计划
    （*"Let me check the shell components…"*），零工具调用就结束了 session。

    原先 `used_after == used_before` 时直接 `break`，把「从未被尝试过的层」判成「没通过」。
    但此刻**预算一次都没动**，`max_sessions` 也远未用尽 —— 重试完全免费。
    而 pass rate 是评分主导项：没被尝试 = 尝试后失败，得分一样。

    这条检查盯三件事：**重试而非判死**、**有次数上限**、**下一轮被告知上一轮没跑测试**。
    层循环要驱动起来得搭整套 TDD 环境，所以这里断言源码结构而不是硬造一个假循环。
    """

    print()
    print("--- an unattempted layer is retried ---")

    source = (REPO / "core" / "phases.py").read_text(encoding="utf-8", errors="replace")

    marker = "if used_after == used_before:"
    check("the empty-session branch exists", marker in source)

    if marker in source:
        start = source.index(marker)
        # 取这一段的正文：到下一个同级判断为止
        rest = source[start:]
        end = rest.find("\n            active_test_type = None")
        branch = rest[: end if end != -1 else 2000]

        check("the branch retries instead of concluding the layer",
              "continue" in branch,
              "a layer that was never attempted would be scored as failed")
        check("it is bounded, so the model cannot loop forever",
              "max_empty_sessions" in branch and "> max_empty_sessions" in branch)
        check("giving up says WHY, not just did-not-pass",
              "gave up after" in branch and "never actually attempted" in branch)
        check("the retry tells the next session it ran no test",
              "ended WITHOUT running any test" in branch,
              "the next session would repeat the same non-action")
        check("the retry is logged with the untouched budget",
              "budget untouched at" in branch)

    check("the bound is declared before the loop", "max_empty_sessions = 2" in source)


def check_suite_load_failure_is_distinguishable() -> None:
    """「套件没加载起来」必须与「断言失败」可区分（issue #49）。

    实测（`run-logs/2026-09-27-competition-sheet-log4`）：生成的测试里相对导入多了一层
    `../` → `Failed to resolve import "../../src/components/workbook/..."` → 套件加载失败
    → `(0 test)`。但回执里显示的仍是 `Test Files 1 failed` + `FAIL <file>` ——
    和断言失败**一模一样**，于是 agent 一路去修实现，5 次预算全败，一次都没碰那行 import。

    这条检查盯三件事：能识别、不误报、以及**插了提示之后退出码仍被正确解析**
    （提示插在 `Exit Code:` 行之后，不能插在它之前）。
    """

    print()
    print("--- a suite load failure is distinguishable ---")

    real = (
        "RUN  v4.1.11 /workspace/template/frontend\n"
        "\n"
        " ? tests/workbookListView.test.tsx (0 test)\n"
        "\n"
        " Test Files  1 failed (1)\n"
        "      Tests  no tests\n"
        "\n"
        " Failed Suites 1\n"
        " FAIL  tests/workbookListView.test.tsx [ tests/workbookListView.test.tsx ]\n"
        'Error: Failed to resolve import "../../src/components/workbook/WorkbookListView" '
        'from "tests/workbookListView.test.tsx". Does the file exist?\n'
        "  Plugin: vite:import-analysis\n"
    )
    note = test_results.describe_suite_load_failure(real)
    check("the real load-failure signature is recognised", bool(note), "got empty string")
    check("it says zero tests ran", "zero tests were executed" in note)
    check("it says this is NOT an assertion failure", "NOT an assertion failure" in note)
    check("it tells the caller to fix the TEST FILE", "Fix the TEST FILE" in note)
    check("it quotes the first Error: line",
          "Failed to resolve import" in note, note[:160])
    check("it points at the relative-path depth (the real generator defect)",
          "`../` count" in note and "tests/workbookListView.test.tsx" in note,
          "the agent is told to fix the test file but not which direction to look")

    assertion_failure = (
        " Test Files  1 failed (1)\n"
        "      Tests  2 failed | 3 passed (5)\n"
        " FAIL  tests/x.test.tsx > adds two numbers\n"
        "AssertionError: expected 3 to be 4\n"
    )
    check("a plain assertion failure is NOT misreported",
          test_results.describe_suite_load_failure(assertion_failure) == "",
          "an assertion failure would be relabelled as a load failure")

    # 提示插在 Exit Code 行之后：解析器取的是第一个 `Exit Code:`，插在前面会读错。
    envelope = f"Exit Code: 1\n\n[ARC] {note}\n\n=== Frontend Vitest Batch ===\nExit Code: 1\n"
    check("the parser still reads the right exit code with the note inserted",
          test_results.parse_test_results(envelope)["exit_code"] == 1,
          f"got {test_results.parse_test_results(envelope)['exit_code']}")

    web_src = (REPO / "app_type_handler" / "web.py").read_text(encoding="utf-8", errors="replace")
    check("run_test_group actually prepends the note",
          "describe_suite_load_failure(" in web_src,
          "the helper exists but nothing surfaces it to the agent")


def check_host_exceptions_are_distinguishable_from_failures() -> None:
    """宿主异常与「层未通过」在结果里必须可区分（issue #51）。

    实测（`run-logs/2026-09-27-competition-sheet-log4`）：`REQ-1-2-2` 的 Unit **已经通过**，
    随后一个 `JSONDecodeError` 把节点判死。但结果里只打印 `JSONDecodeError: ...`，
    与「层未通过」并排出现 —— **读者分不出哪个是真实成绩、哪个该修 harness**。

    而区分所需的数据**本来就有**：`core/workflow.py` 记 `kind=phase_exception`（带
    `error_type` 与 `traceback`）与 `kind=phase_failure`（带 `summary`）两种。
    问题只是从没被读出来 —— `cli.py` 的摘要两者一视同仁。

    这条检查钉住：产出侧两种 kind 都在、消费侧真的按 kind 分开说。
    """

    print()
    print("--- host exceptions are distinguishable from failures ---")

    workflow = (REPO / "core" / "workflow.py").read_text(encoding="utf-8", errors="replace")
    check("a raised phase exception is recorded as its own kind",
          '"kind": "phase_exception"' in workflow)
    check("a normal layer failure is recorded as a different kind",
          '"kind": "phase_failure"' in workflow)

    cli = (REPO / "core" / "cli.py").read_text(encoding="utf-8", errors="replace")
    check("the summary reads the kind field",
          'detail.get("kind")' in cli,
          "the two kinds exist but nothing tells them apart in the output")
    check("it marks the infrastructure ones visibly", "[INFRA]" in cli)
    check("it counts them separately from real failures",
          "died from a HOST" in cli and "EXCEPTION rather than from failing tests" in cli,
          "a reader still cannot tell how many failures were ours vs the agent's")
    check("it says they are not a pass-rate signal",
          "not a pass-rate signal" in cli)

    # #51 的第二半：回退到 ainvoke 的那次调用原先没有保护，异常直接穿到节点级处理。
    runners = (REPO / "agents" / "runtime" / "runners.py").read_text(encoding="utf-8", errors="replace")
    marker = "result = await agent.ainvoke("
    check("the non-streaming fallback exists", marker in runners)
    if marker in runners:
        before = runners[max(0, runners.index(marker) - 700) : runners.index(marker)]
        check("it is wrapped so a failure degrades instead of killing the node",
              "try:" in before,
              "an exception here reaches workflow.py and the node is marked FAILED")
        check("the degraded payload says it was an infrastructure failure",
              "_agent_call_failed" in runners)


def check_model_usage_is_logged() -> None:
    """每次模型调用的用量与**缓存命中**必须能落盘（issue #58）。

    一次提交烧 5.06 亿 tokens（榜首 872 万，约 58×，#57），最后被 SIGKILL。
    而在此之前，ARC 这条路（`runners._log_stream_event`）**连 usage 字段都没碰过** ——
    本地只能靠数模型调用次数估算，平台只给一个总数，看不到花在哪一步。

    `cached` 是这条里最贵的字段：它决定「命中率 0% 还是 90%」，
    而这两种情况的修法完全相反。所以两种 provider 的字段约定都要认 ——
    只认一种的话，换个网关就会静默变成 0。
    """

    print()
    print("--- model usage is logged ---")

    from langchain_core.messages import AIMessage  # noqa: PLC0415

    from agents.runtime import runners  # noqa: PLC0415

    langchain_style = AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": 85_231,
            "output_tokens": 412,
            "total_tokens": 85_643,
            "input_token_details": {"cache_read": 78_900, "cache_creation": 0},
        },
    )
    line = runners._usage_line(langchain_style)
    check("a langchain-shaped usage_metadata is read", bool(line), "got empty string")
    check("input / output are reported", "in=85,231" in line and "out=412" in line, line[:120])
    check("cache_read is reported", "cached=78,900" in line, line[:120])
    check("a hit ratio is computed", "cache_hit=92%" in line, line[:120])

    openai_style = AIMessage(
        content="ok",
        usage_metadata={"input_tokens": 10_000, "output_tokens": 100, "total_tokens": 10_100},
        response_metadata={"token_usage": {"prompt_tokens_details": {"cached_tokens": 0}}},
    )
    openai_line = runners._usage_line(openai_style)
    check("the OpenAI-shaped fallback is read too",
          "cached=0" in openai_line and "cache_hit=0%" in openai_line,
          openai_line[:120])

    check("a message without usage yields nothing (no fake zeros)",
          runners._usage_line(AIMessage(content="ok")) == "")

    source = (REPO / "agents" / "runtime" / "runners.py").read_text(encoding="utf-8", errors="replace")
    marker = 'if event_name in {"on_chat_model_end", "on_llm_end"}:'
    check("the stream event handler exists", marker in source)
    if marker in source:
        branch = source[source.index(marker) : source.index(marker) + 900]
        check("it actually logs the usage line",
              "_usage_line(" in branch,
              "the helper exists but nothing calls it - the data still never lands")


def check_test_receipt_is_compacted() -> None:
    """测试回执只留信号行，完整输出落盘（issue #57）。

    一次提交烧 5.06 亿 tokens（榜首 872 万，约 58×）。测试回执里**整段 STDOUT/STDERR**
    （堆栈、边框、分隔线）每次都进上下文，乘以约 2400 次调用就是亿级 —— 而模型真正需要的是
    「哪一层、退几、第一个错误是什么」。

    **本检查最重要的一条不是「变小了」，而是「压完解析结果不能变」**：
    `parse_test_results` 取第一个 `Exit Code:` 填外层、靠子批次的 `=== ... ===` 与
    各自的 `Exit Code:` 填 `sub_batches`。压掉这些就等于悄悄改了判定。
    """

    print()
    print("--- the test receipt is compacted ---")

    from app_type_handler import web as web_handler  # noqa: PLC0415

    # 仿真实回执：两个子批次，正文很长，信号只有几行。
    body = "\n".join(f"  padding line {i}" for i in range(400))
    raw = (
        "=== Backend Vitest Batch ===\n"
        "Exit Code: 1\n"
        "STDOUT:\n" + body + "\n"
        "STDERR:\n"
        " FAIL  tests/workbookRename.api.test.js > renames the workbook\n"
        "Error: expected 2 to be 3\n"
        "\n=== Frontend Vitest Batch ===\n"
        "Exit Code: 0\n"
        "STDOUT:\n" + body + "\n"
    )
    compact = web_handler._compact_test_output(raw)

    check("the sub-batch boundaries survive", compact.count("=== ") == 2, compact[:120])
    check("each sub-batch keeps its exit code", compact.count("Exit Code:") == 2)
    check("the failure signal survives", "FAIL " in compact and "Error: expected" in compact)
    check("the padding is dropped", "padding line" not in compact)
    check("it is dramatically smaller",
          len(compact) < len(raw) / 10,
          f"{len(compact)} vs {len(raw)} chars")

    # 判定不能变：把压缩后的正文按 run_test_group 的写法包成信封再解析。
    envelope = f"Exit Code: 1\n\n{compact}"
    parsed = test_results.parse_test_results(envelope)
    raw_parsed = test_results.parse_test_results(f"Exit Code: 1\n\n{raw}")
    check("the outer exit code is unchanged", parsed["exit_code"] == raw_parsed["exit_code"],
          f'{parsed["exit_code"]} vs {raw_parsed["exit_code"]}')
    check("both sub-batches are still found",
          len(parsed["sub_batches"]) == len(raw_parsed["sub_batches"]) == 2,
          f'{len(parsed["sub_batches"])} vs {len(raw_parsed["sub_batches"])}')
    check("the sub-batch exit codes are unchanged",
          [b["exit_code"] for b in parsed["sub_batches"]]
          == [b["exit_code"] for b in raw_parsed["sub_batches"]])

    source = (REPO / "app_type_handler" / "web.py").read_text(encoding="utf-8", errors="replace")
    check("run_test_group actually compacts before returning",
          "_compact_test_output(sections_text)" in source,
          "the helper exists but the receipt is still the raw dump")
    check("the full output is written somewhere the model can ask for",
          "_write_full_test_output(" in source)
    check("the full output goes under .arc/ (already excluded from search, issue #36)",
          '".arc" / "test-output"' in source)


def check_compaction_trigger_is_not_tied_to_the_window() -> None:
    """压缩触发点必须是**绝对量**，不能跟着模型窗口变（issue #61）。

    触发点原先是 `LLM_CONTEXT_WINDOW * COMPACT_AT`。issue #11 把窗口从 64K 调到 1M
    「防止过早压缩」—— 于是压缩线被动地从 **51K 抬到 800K** ✗。而 issue #58 的日志线
    实测上下文最大只到 **53K**，也就是说**压缩从不触发**，历史一路涨到会话结束，
    一次提交烧 5.06 亿 tokens（榜首 872 万，约 58×），最后被 SIGKILL。

    这里检查两件事：
    1. 绝对量存在，且**远低于**窗口 —— 否则它等于没设
    2. 源码里触发点**优先用绝对量**，不再以「窗口 × 比例」为主
    """

    print()
    print("--- the compaction trigger is not tied to the window ---")

    from app import config  # noqa: PLC0415

    trigger = int(getattr(config, "COMPACT_AT_TOKENS", 0) or 0)
    window = int(getattr(config, "LLM_CONTEXT_WINDOW", 0) or 0)
    keep = int(getattr(config, "COMPACT_KEEP_TOKENS", 0) or 0)

    check("an absolute trigger exists", trigger > 0, f"got {trigger}")
    check("it is far below the model window",
          window > 0 and trigger < window / 5,
          f"trigger={trigger:,} window={window:,} - a trigger near the window never fires")
    check("it leaves room above the kept tail",
          trigger > keep * 2,
          f"trigger={trigger:,} keep={keep:,} - compacting would immediately re-trigger")

    runtime = (REPO / "app" / "agent" / "compaction" / "runtime.py").read_text(encoding="utf-8", errors="replace")
    check("the trigger prefers the absolute value",
          "COMPACT_AT_TOKENS or int(LLM_CONTEXT_WINDOW * COMPACT_AT)" in runtime,
          "the window product is back in charge - #11's mistake")
    check("the window product survives only as a fallback",
          runtime.count("LLM_CONTEXT_WINDOW * COMPACT_AT") == 1,
          "more than one place still scales the trigger by the window")


def main() -> int:
    check_skill_roots()
    check_pagination()
    check_path_keys_are_normalised()
    check_repeat_searches_are_served_from_the_cache()
    check_a_failing_validation_does_not_switch_off_the_lookup_cache()
    check_the_read_cache_records_the_window_the_tool_serves()
    check_a_model_call_cannot_hang_forever()
    check_stage_limits_are_published()
    check_interface_contract_is_not_duplicated()
    check_guard_unlocks_after_a_failing_validation()
    check_guard_reads_the_real_validation_format()
    check_timeout_output_is_classified_as_failure()
    check_interface_contract_keeps_its_load_bearing_text()
    check_published_budget_matches_the_enforced_one()
    check_clean_survives_readonly_git_objects()
    check_a_stage_can_stop_its_own_agent_session()
    check_timeouts_are_configurable_and_distinguishable()
    check_test_runners_fail_fast_instead_of_hanging()
    check_install_is_verified_not_assumed()
    check_install_requests_dev_dependencies()
    check_a_passed_layer_is_not_rerun()
    check_engine_state_dir_is_not_searchable()
    check_a_blocked_write_unlocks_its_path()
    check_an_unattempted_layer_is_retried()
    check_suite_load_failure_is_distinguishable()
    check_host_exceptions_are_distinguishable_from_failures()
    check_model_usage_is_logged()
    check_test_receipt_is_compacted()
    check_compaction_trigger_is_not_tied_to_the_window()
    check_extra_tools_are_published_and_wired()
    check_subagent_read_only_boundary()
    check_todo_and_command_behaviour()
    print()
    print("result: " + ("ALL OK" if not failures else f"{len(failures)} FAILED -> {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
