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
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Simulate the runner container, where my-agent's own skill library does not exist,
# so a wrong root cannot be masked by a stray directory on a dev machine.
os.environ.setdefault("SKILLS_DIR", str(REPO.parent / "_no_such_skills_dir"))

from langchain_core.tools import StructuredTool  # noqa: E402

from agents import test_driven_developer  # noqa: E402
from agents.context.pipeline import context_pipeline  # noqa: E402
from agents.runtime import stage_policy, workspace_tools  # noqa: E402
from agents.runtime.contracts import AgentRuntimeContext  # noqa: E402
from agents.skills import selection  # noqa: E402
from app.agent.runtime import current_workspace  # noqa: E402
from app_type_handler import test_results, web as web_handler  # noqa: E402

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


def main() -> int:
    check_skill_roots()
    check_pagination()
    check_path_keys_are_normalised()
    check_stage_limits_are_published()
    check_interface_contract_is_not_duplicated()
    check_guard_unlocks_after_a_failing_validation()
    check_guard_reads_the_real_validation_format()
    check_timeout_output_is_classified_as_failure()
    check_interface_contract_keeps_its_load_bearing_text()
    check_published_budget_matches_the_enforced_one()
    check_clean_survives_readonly_git_objects()
    check_a_stage_can_stop_its_own_agent_session()
    print()
    print("result: " + ("ALL OK" if not failures else f"{len(failures)} FAILED -> {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
