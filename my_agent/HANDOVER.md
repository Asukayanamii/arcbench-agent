# 提交包交接说明

这份文档说明：**改动在哪些文件里、每个文件是从哪来的、要完善 agent 运行时该动哪里、改完怎么重新打包。**

包本身是「my-agent 的引擎 + 官方 ARC 模板的骨架 + 一层桥接」，所以先分清哪些是官方的、
哪些是我们的 —— 改官方文件之前值得想一下。

---

## 0. 先看这一条：改完必须用脚本重新打包

```bash
python tools/build_submission_zip.py
# 默认输出到仓库上一级的 my_agent-arcbench-submission.zip
```

**不要用 `Compress-Archive`、也不要右键「发送到 → 压缩文件夹」。** 原因很具体：

| | 官方模板 zip | Windows 自带压缩 |
| --- | --- | --- |
| `create_system` | `3`（Unix） | `0`（MS-DOS） |
| `external_attr` | `0x81B40000`（mode `100664`） | `0x00000000` |

Unix 模式下 `0` **连 `S_IFREG`（普通文件）位都没有**，平台的解压器按 Unix mode 判断条目类型时
无法把它认成文件，目录结构会落不全。上一版就是这样挂的：

```
File "/workspace/submission/arc_main.py", line 12, in <module>
    from app_type_handler import list_app_types, normalize_app_type
ModuleNotFoundError: No module named 'app_type_handler'
```

`tools/build_submission_zip.py` 会写出和官方归档一致的属性，并在结尾自检
（`create_system`、`external_attr`、`main.py` 是否在根、有没有 `.pyc` 泄漏），
自检不过会返回非零退出码。

---

## 1. 包的三层结构

平台约定（Upload Entry Contract）：Python 提交用 `main.py` + `requirements.txt`，
**入口必须在压缩包根目录**。调用形状是固定的一条命令：

```
python3 /workspace/submission/main.py /tmp/arcbench/requirements-source --output-dir /workspace/template
```

| 目录 / 文件 | 来源 | 说明 |
| --- | --- | --- |
| `app/` | **my-agent 原仓库** | LangGraph 图、模型客户端、重试、工具、沙箱、压缩 |
| `app/agent/` | 同上 | 阶段 agent 真正跑的就是这张图 |
| `core/` | 官方模板 | 需求树展开、阶段编排、调试日志、会话 |
| `app_type_handler/` | 官方模板 | web 脚手架、npm 安装、测试执行器（vitest / playwright） |
| `agents/context/` | 官方模板 | 每个节点的提示词上下文组装 |
| `agents/tools/` | 官方模板 | 只读追溯工具、`run_build` |
| `agents/skills/selection.py` | 官方模板 | 按节点挑技能 |
| `agents/*.py` | 官方模板 | 三个阶段 agent（接口设计 / 测试生成 / TDD） |
| `agents/runtime/` | **混合** | 见第 4 节 —— 桥接层在这里 |
| `skills/` | 官方模板 | 5 个阶段技能（`SKILL.md`） |
| `arc-template/` | 官方模板 | web app 脚手架（react + express + sqlite） |
| `arcbench_agent_runtime/` | 官方模板 | **平台 SDK，不要改** |
| `arc_main.py` | 官方模板 | 编译 CLI（改了 `_get_repo_root`） |
| `main.py` | **本包新增** | 平台入口 |
| `requirements.txt` | **本包新增** | 依赖（已去 `deepagents`） |
| `SCORING_AND_OPTIMIZATION.md` | **本包新增** | 平台评分公式（已验证）+ 运行时优化建议 |
| `tools/build_submission_zip.py` | **本包新增** | 打包脚本（必须用它） |
| `tools/check_stage_bridge.py` | **本包新增** | 技能根 / 分页语义回归测试 |
| `tools/score_calculator.py` | **本包新增** | 按公式算分、算开销上限 |

**核对过的数字**：相对官方模板，**173 个文件原样照搬，4 个被改动**（见第 2 节）。

---

## 2. 相对官方模板改动的 4 个文件

### `core/config.py` — `doctor` 自检

- 原来 `check_config()` 里 `import deepagents` 检查 agent 运行时；本包不含 `deepagents`，改成检查
  `langgraph`、`my_agent` 的图能否导入、以及 `arcbench_agent_runtime` 是否在。
- `ARC_OPENAI_API_MODE` 从「必填且必须为 responses/chat_completions」降为**告警**：本包的阶段
  agent 走 OpenAI 兼容的 chat-completions 路径，这个变量用不上，不该卡住启动。

### `core/processes.py` — `finalize_subprocess` 改成收整棵进程树

原实现只 `terminate()` 直接子进程。但 `_execute_web_test_command` 起的是
`sh -c "npx vitest run ..."`，真正干活的是再往下几层的 `node`。
**实测一次单元测试超时留下 10 个 `npx vitest` 孤儿进程**，继续占端口和文件句柄，
后面的测试层跟着失败。

现在：Windows 用 `taskkill /PID <pid> /T /F`，POSIX 用 `killpg`（子进程以
`start_new_session=True` 起，自成一个进程组，不会波及自己）。

### `app_type_handler/web.py` — 3 处加 `start_new_session=True`

配合上一项，让子进程自成进程组。改了 `run_npm_install`、`_execute_web_test_command`、
`_start_backend_runtime` 三处 spawn。行为本身不变。

### `arc_main.py` — `_get_repo_root()` 返回自身目录

原来返回上一级（那是官方自己的打包布局）。本包的包根就是 `arc_main.py` 所在目录，
改成同级才能找到旁边的 `.env`。

---

## 3. 相对 my-agent 原仓库改动的 6 个文件

用 `git diff` 可查，全部改动都是**加法**，不影响原来的聊天功能。

### `app/agent/graph.py` — `build_graph()` 多了两个注入口

```python
def build_graph(checkpointer, model=None, summary_model=None,
                tools=None, system_prompt=None):
```

- `tools`：本次运行绑定哪些工具，默认 `None` = 原来的 8 个编码工具，行为完全不变
- `system_prompt`：注入了就用它，不再拼项目约定与技能索引

**这是编译器接入引擎的唯一入口。** 聊天侧不传这两个参数，走的还是原来的路。

### `app/agent/tool_calls.py` — 每次运行的工具表

新增 `_tool_map(config)`：`configurable.tools` 有值就以它为准（**不合并**）。ARC 明确要求
`execute` 不对模型开放，把全局表并进来等于又把它放回去了。

### `app/agent/retry.py` — 修了一个回调合并的 bug

`callbacks` 可能是 LangChain 的 `CallbackManager`。原代码
`getattr(existing, "handlers", []) or [existing]` 在 `handlers` 为空时会把**管理器本身**
当成 handler 塞进列表，于是 `on_chat_model_start` 去读它的 `run_inline`：

```
AttributeError: 'AsyncCallbackManager' object has no attribute 'run_inline'
```

判据改成「有没有 `handlers` 属性」而不是「`handlers` 是不是空的」，见新的 `_callback_handlers()`。

同文件还修了一个更贵的 bug（一次卡流打死整轮运行），见第 7 节「流卡住」。

### `app/config/__init__.py` + `app/agent/models.py` — 流式停顿看门狗

新增配置 `LLM_STREAM_CHUNK_TIMEOUT`，默认 **300** 秒（SDK 默认 120s 太紧），填 `0`/`off`
关掉。`build_model()` 把它透传给 `ChatOpenAI(stream_chunk_timeout=...)`。

这个参数是 langchain-openai 的真实字段（不是 `model_kwargs`），**不会**混进请求体 ——
已确认过版本是 1.6.2，和 `requirements.txt` 钉的同一个。

### `core/workflow.py` — 阶段抛异常不再带走整轮编译

`compile_requirement_tree` 的任务循环里，`await self._run_task(task)` 原先**没有保护**：
阶段里任何异常（模型调用失败、工具层报错）都会冲出整个 `while` 循环直到 `exit 1`，
**这个节点没做完、排在后面的节点也一起不做了**。

改成降级处理：记一条 `... raised for node ...` 错误日志，然后走原有的 `TASK_FAILED` 分支
（节点标记失败、记账、提交 `-FAILED` 检查点），循环继续下一个节点。
取消（`CancelledError` 是 `BaseException`）不经过这里，照旧往上抛。

### 其余小改动

`.env.example`（加了平台变量说明）、`.gitignore`（忽略编译产物）、`requirements.txt`
（合并依赖、去掉 `deepagents`）、`README.md`（加了提交包说明）。

---

## 4. 要完善 agent 运行时，改这几个文件

`agents/runtime/` 是桥接层。**`contracts.py` 和 `runners.py` 是官方原文件，没动**：

| 文件 | 状态 | 行数 | 职责 |
| --- | --- | --- | --- |
| `agents/runtime/contracts.py` | 官方原样 | 21 | `AgentRuntimeContext` 数据类 |
| `agents/runtime/runners.py` | 官方原样 | 776 | 调 agent、解析结构化产物、日志。**桥接只要满足它的接口就不用动它** |
| `agents/runtime/factory.py` | **重写** | 574 → 146 | 原来是 `deepagents.create_deep_agent`，现在是 my-agent 的图 |
| `agents/runtime/arc_agent.py` | **新增** | 126 → 158 行 | `ArcStageAgent`：一个阶段 = 一张 my-agent 图。**必须暴露 `astream_events`**，否则长阶段调用会退化成阻塞调用、日志全程静默 |
| `agents/runtime/workspace_tools.py` | **新增** | 316 | ARC 那套文件工具面，实现在 my-agent 的工具+沙箱上 |
| `agents/runtime/stage_policy.py` | **新增** | 214 → 260 行 | 阶段纪律（读缓存、写去重、骨架行数上限）+ `render_stage_limits` 把约束公布进提示词 |

**关键设计：只改名不改行为。** ARC 的提示词、5 个技能、阶段纪律全是按
`read_file` / `write_file` / `edit_file` / `glob` / `grep` 和 `/workspace` 虚拟根写死的。
所以桥接层让 my-agent 的工具去适配 ARC 的工具面，而不是反过来改提示词 —— 改提示词等于把上游
调好的东西推倒。

### 想改什么，看哪里

| 目标 | 改哪 |
| --- | --- |
| 换阶段 agent 的执行循环 / 状态管理 | `agents/runtime/arc_agent.py`（`ArcStageAgent`） |
| 换工具集、加工具 | `agents/runtime/workspace_tools.py` 的 `FILE_TOOLS` |
| 改阶段纪律（次数上限、写保护） | `agents/runtime/stage_policy.py` |
| 改提示词组装 / 结构化输出形状 | `agents/runtime/factory.py` |
| 改模型的构造（超时、温度、并发） | `app/agent/models.py` |
| 改重试策略 | `app/agent/retry.py` |
| 改路径沙箱 | `app/agent/sandbox.py` |
| 改上下文压缩 | `app/agent/compaction/` |
| 改阶段编排（先后顺序、预算） | `core/phases.py`、`core/workflow.py` |
| 改 web 脚手架 / 测试执行 | `app_type_handler/web.py` |

### 三个文件不要改

- `arcbench_agent_runtime/` —— 平台 SDK。规则明确：**不要手写事件负载**，只调高层方法。
  改了会导致追溯数据、刷新信号、git 事件对不上。
- `agents/runtime/runners.py` —— 它只要求 `agent.ainvoke(inputs, context=, config=)` 返回
  `{"messages": [...]}`。满足这个形状就能用，不用动它。
- `agents/runtime/contracts.py` —— 只是个数据类。

---

## 5. 必须维持的平台契约

改动时别破坏这些（坏了平台收不到追溯数据 / 判定失败）：

1. **CLI 形状**：`main.py <requirements 目录> --output-dir <out>`，可带 `--port` / `--type` / `--clean`
2. **入口位置**：`main.py`、`requirements.txt` 在压缩包根目录
3. **环境变量**：平台下发 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL`，
   `main.py` 桥接到本项目的 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`
   （`app/config/__init__.py` 在导入时就读成常量，**桥接必须在导入 `app.*` 之前完成**）
4. **SDK 产物**：`.arc/traceability/*.json`、`.arc/runner-events.jsonl`、git checkpoint，
   全由 `arcbench_agent_runtime` 写，不要绕过它手写
5. **工作区边界**：agent 的文件工具只能碰 `/workspace`（编译输出目录），
   `main.py` 里的 `_preflight()` 会在结构不完整时先报清单再退出

---

## 6. 本地怎么验证

```bash
# 1. 依赖
pip install -r requirements.txt

# 2. 桥接自检（不花钱、不联网）——改完 agents/runtime/ 或 skills/ 后务必跑一次
python tools/check_stage_bridge.py

# 2b. 卡流回归（不花钱、不联网）——改完 app/agent/retry.py、app/agent/models.py
#     或 core/workflow.py 后跑一次。22 条断言，覆盖第 7 节那三层修复，
#     外加两个反例（普通错误吐过字仍然不许重试；永久卡流要能放弃）
python _test_stall_recovery.py        # 在开发机上，不在这个包里

# 3. 环境自检
python arc_main.py doctor

# 4. 算一次分：通过率 50.5%、开销 ￥16.39、200 个测试
python tools/score_calculator.py --pass-rate 50.5 --cost 16.39 --tests 200
python tools/score_calculator.py --tables          # 敏感度表
python tools/score_calculator.py --target-score 60 --tests 200 --pass-rate 55   # 开销上限

# 5. 用真实 key 编译一个小需求
LLM_API_KEY=sk-xxx \
LLM_BASE_URL=https://api.deepseek.com/v1 \
LLM_MODEL=deepseek-chat \
python main.py /path/to/requirements-dir -o /path/to/output --port 3301
```

> **评分规则与优化建议见 `SCORING_AND_OPTIMIZATION.md`。** 一句话版本：
> 得分 `S = p ÷ (b ÷ E)^γ`，`E = 0.4 × N × p ÷ 100`；开销在 E 以内 γ=0.1（奖励），
> 超出则 γ=0.2（惩罚翻倍）。**通过率是主导项，开销的意义是"别越线"。**

编译产物在输出目录的 `.arc/` 下：`debug.log`（全过程日志）、`traceability/*.json`（追溯表）、
`processing_queue.json`（任务队列与节点状态）、`node_sessions/*.json`（每个节点的接口/测试/失败摘要）。

### 跑平台的官方测试

`run_official_tests.py` 不在这个包里（它在开发机上）。要复现平台评分，
从 arc-bench 拉任务材料后自己起 Playwright：

- 需求：`GET https://arc-bench.com/api/requirements/<task-id>?catalog=competition` → `requirements_yaml`
- 测试：`GET https://arc-bench.com/api/requirements/<task-id>/tests?catalog=competition` → `files[]`
- 公开任务不用登录；`hackathon--github` / `hackathon--sheet` 的测试是**隐藏的**（404），
  只能提交后在平台上跑

跑测试时：前端 `npm run build` → 后端 `PORT=<port> npm run start` →
`npx playwright test`，`baseURL` 指向 `http://127.0.0.1:<port>`，**单 worker 顺序执行**
（平台就是这么跑的，状态变更类场景才能确定）。

---

## 7. 上一个版本踩过的坑（别再踩）

1. **打包**：`Compress-Archive` 的条目属性不对 → 上传后 `ModuleNotFoundError`（见第 0 节）
2. **进程树**：测试超时留下孤儿 `npx vitest`，拖垮后面的测试层
3. **回调合并**：`retry.py` 把 `CallbackManager` 当 handler 传下去
4. **技能库两个根目录**：见下面「技能读取」，选中的技能读不到正文
5. **`offset` 语义不一致**：见下面「分页约定」，续读被误判成重复读取
6. **执行规则却没公布规则**：见下面「阶段上限」，模型只能撞上去，白烧往返
7. **禁止手写 SDK 事件**：规则明确要求只用高层方法
8. **不要把 `execute` / `bash` 暴露给 ARC 阶段 agent**：ARC 的提示词明确说
   `execute` 是关闭的，只用 `run_tests` / `run_build`
9. **流卡住会打死整轮运行**：见下面「流卡住」，一次 120s 停顿让 exit code 变成 1

### 流卡住：一次停顿 = 整轮白跑

平台实测（2026-09-25 12:04 那次，节点 `REQ-1.1` 的 IMPLEMENT）：

```
langchain_openai...StreamChunkTimeoutError: No streaming chunk received for 120.0s
(model=deepseek-v4-flash, chunks_received=5164)
```

网关不再往下发分片了，但 TCP 连接还活着。然后**三个独立缺陷串在一起**把它变成了整轮失败：

| # | 位置 | 问题 |
|---|---|---|
| 1 | `retry.py` | 「吐过字就不重试」把**被掐断的流**也挡住了。该规则防的是把一句**完整**回答拉两遍；残句本来就要丢 |
| 2 | `models.py` | SDK 的停顿看门狗停在默认 120s，对这个网关太紧 |
| 3 | `core/workflow.py` | 异常冲出编译循环，**后面的节点全部不再执行** |
|   |   | 第 3 条最贵：得分是**通过率**，一个节点崩掉不该让整棵树陪葬 |

第 1 条的判断依据值得记一下 —— 重试是**安全**的，因为抛异常时那个 `AIMessage` 根本没构造出来、
工具也没执行，所以重试不会重复任何副作用，只会补上本来拿不到的结果。

还有一个坑：`is_retryable` 当时**只是因为类名里恰好有 "timeout"** 才放行。
消息本身（`No streaming chunk received for 120.0s`）一个 `RETRYABLE_PATTERN` 认识的词都没有。
换个类名、同样的措辞就会当场失败 —— 现在 `is_retryable` 会**显式**调
`stream_was_interrupted()`，不再靠这个巧合。

回归测试：`_test_stall_recovery.py`（22 条断言，覆盖三层 + 「非流式错误仍然不重试」的反例）。

### 阶段上限：谁执行规则，谁就得公布规则

`stage_policy.py` 里拦了这些硬约束，但**原先只写在拦截分支里**，提示词与技能一个字都没提：

- InterfaceDesigner 最多写 `MAX_DESIGN_WRITES`（8）个骨架文件
- 单次写入最多 `MAX_SKELETON_LINES`（160）行
- 同一路径不许写两次 / 写完不许再读
- TestGenerator 只能写测试资产，且不许跑校验工具

实测后果：模型写了一个 **158 行**的文件、被拦回「超过 160 行」、又写了一遍才通过 ——
白烧一个完整往返，而那一轮要重发整段阶段历史。

现在 `render_stage_limits(stage)` 从**同一组常量**渲染出提示词片段，
由 `agents/runtime/factory.py` 追加到系统提示词。**改上限时文案跟着变，不会两边分叉**；
`tools/check_stage_bridge.py` 会验证「enforced 的数字都出现在提示词里」。

> 教训可以推广：任何在工具层拦下的约束，如果提示词里没说，模型就只能靠试错发现。
> 试错的代价是一整个模型往返 —— 通常比多写几行提示词贵得多。

### 平台侧已知限制：Diff 页签永远是空的

判题系统的 **Diff 页签读的是 `GET /api/runs/{id}/commit-history`**，
而该接口对**所有** run 都返回：

```json
{ "availability": "workspace_unavailable", "commits": [] }
```

原因是平台从容器里收集工作区时**不含 `.git/`**。实测某次 run 收集到 75 个条目
（`backend/`、`frontend/`、`requirements/`、各种 `.gitignore`），
**`.git` 目录一个条目都没有**，`node_modules` / `dist` / `.arc` 同样被过滤。

**这不是我们这边的 bug。** 容器里 git 是好的 —— 同一次 run 的日志里：

```
[System] Initializing Git repository...
[Compiler][ROOT] Running git checkpoint for DESIGN on node ROOT...
[Compiler][REQ-1] Running git checkpoint for DESIGN on node REQ-1...
[Compiler][REQ-1.1] Running git checkpoint for DESIGN on node REQ-1.1...
```

3 次 checkpoint、0 次 "No file changes detected"，说明每个阶段都真的提交了。
`git` 与 `arcbench_agent_runtime` 的 `commit_history` 刷新信号也都正常发出。

**要看生成结果就用这两个**：

| 想干什么 | 用什么 |
| --- | --- |
| 浏览生成的文件树 / 看单个文件 | 运行页的 **File** 标签（走 `/runs/{id}/workspace/files`） |
| 整体下载 | 运行页的 **Download code**，或 `GET /api/runs/{id}/workspace/template-bundle` |
| 需求 → 接口 → 测试的映射 | **Traceability** 标签（走 `/runs/{id}/traceability`） |
| 全过程日志 | `GET /api/runs/{id}/logs` 的 `stdout` / `console` |

> 如果哪天 Diff 能用了，`change_type` 用的是 git 的 `A`/`D`/`R`/`M`，
> 空状态文案会区分 `workspace_unavailable`（工作区没收到）和
> `git_unavailable`（收到了但 `.git` 不在）。现在一律是前者。

平台上的真实症状（第一版）：

```
skill-permitted: leaf-full-design, auth-session-consistency
工具失败 name=read_file：技能文件不存在：/skills/leaf-full-design/SKILL.md
```

技能**被选中了但读不到正文**。原因是同一个概念用了两个根：

| 位置 | 用的根 |
| --- | --- |
| `agents/skills/selection.py::available_skill_names` | `Path(__file__).parents[2] / "skills"` → **包内 `skills/`** |
| `agents/runtime/workspace_tools.py::_skills_root`（旧） | `app.config.SKILLS_DIR` → `~/.my_agent/skills` ❌ |

`_skills_root()` 现在也以包内 `skills/` 为准（`SKILLS_DIR` 仅作回退）。
**改这两个文件里任何一个的路径推导时，两边必须一起改** ——
`_test_skills.py` 就是专门盯这个的。

### 分页约定：`offset` 是「跳过的行数」，0 基

`read_file(file_path, offset, limit)` 的 `offset` 是**要跳过的行数**，不是行号：

- 读第 1-100 行：`offset` 省略（或 0），`limit=100`
- 接着读：`offset=100`（返回的第 1 行是文件第 101 行）

这个约定必须和 `agents/runtime/stage_policy.py` 里的读取记账一致 —— 那份是照搬
上游 `StageDisciplineMiddleware` 的（`_as_nonnegative_int(args.get("offset"), default=0)`，
默认就是 0）。两边算法不同的时候，模型按 0 基续读会被判成「与已读范围重叠」而被拦下：

```
Error: ARC stage discipline: Repeated read blocked: /backend/src/database/init_db.js
```

`read_file` 的返回值里会带上 `[后面还有 N 行，续读用 offset=X]`，X 用同一套语义。
