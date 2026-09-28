# ArcBench 评分规则与运行时优化建议

这份文档回答两件事：**平台怎么算分**（公式已用排行榜真实数据验证），以及**据此该优化运行时的哪里**。

> 结论先行：**通过率的权重远大于开销**，但开销有一道"合理开销"线，
> 越过它惩罚翻倍。所以正确的顺序是 **先保正确率，再把总 token 压到线内**，
> 而不是为了省钱牺牲通过率。

---

## 1. 评分公式

来源：排行榜的 "Leaderboard scoring"（查看计分方式）弹窗原文。

> Rankings balance test pass rate with model cost. Let **p** be the pass-rate percentage,
> **b** the cost in CNY, and **N** the total number of tests.
>
> - **Expected cost** = `0.4 × N × p ÷ 100`
> - **Final score** = `S = p ÷ (b ÷ expected cost)^γ`
> - When actual cost is at or below the expected cost, **γ = 0.1** rewards efficiency.
>   Above it, **γ = 0.2** applies a cost penalty.
> - Cost has a **￥0.10 floor** and the cost ratio is **clamped to 0.01–100**.

整理成可直接算的形式：

```
E     = 0.4 × N × p / 100          # 合理开销（人民币）
r     = clamp(b / E, 0.01, 100)    # 成本比率，b ≥ ￥0.10
γ     = 0.1  (b ≤ E, 奖励)  |  0.2  (b > E, 惩罚)
S     = p / r^γ
```

**排行榜规则**（同样出自弹窗）：

- **综合榜**：同一份提交必须**完成全部任务**，把通过数、测试总数、开销汇总后**统一算一次分**
- **单任务榜**：取每位用户/团队在该任务上的最高分运行
- **排序**：得分 → 通过率 → 较低开销 → 较早提交时间
- **缺人民币开销数据的运行不参与排名**

### 用真实数据验证

三个数据点都能精确复现（误差 < 1e-6）：

| 用户 | p | b(￥) | N | E | r | 机制 | 榜单分 | 公式算 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| VOLO-AI | 50.5 | 16.3888 | 200 | 40.4 | 0.4057 | reward | 55.268144 | **55.268144** |
| Iris | 40.5 | 55.7107 | 200 | 32.4 | 1.7195 | penalty | 36.339281 | **36.339281** |
| jackyjiang | 100 | 0.0 | 478 | 191.2 | 0.01 | reward | 158.489319 | **158.489319** |

**理论满分** = `100 / 0.01^0.1` = **158.4893**（p=100 且成本比率触到 0.01 下限）。
注意 `avg_feature_implementation_rate` 在榜单上**只显示、不进公式**。

---

## 2. 排行榜实况（hackathon 赛道，N=200）

| # | 用户 | 模型 | 通过率 | 通过/总数 | 开销(￥) | E | 比率 | 机制 | 得分 | 运行时长 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | VOLO-AI | deepseek-v4-flash | 50.5% | 101/200 | 16.39 | 40.4 | 0.41 | reward | **55.27** | 108 min |
| 2 | Iris | deepseek-v4-pro | 40.5% | 81/200 | 55.71 | 32.4 | 1.72 | penalty | 36.34 | 89 min |
| 3 | 尻名山… | glm-5.3-flash | 29.5% | 59/200 | 24.98 | 23.6 | 1.06 | penalty | 29.17 | 11 h |
| 4 | bzt | deepseek-v4-flash | 33.0% | 66/200 | 49.40 | 26.4 | 1.87 | penalty | 29.11 | 177 min |

两个很值得注意的事实：

- **最高通过率才 50.5%** —— 这个赛道很难，不是"做到 90% 才够看"。**每提高一个百分点都是实打实的排名**
- **上榜的 4 支队里有 3 支踩进了惩罚区**（开销超过 E）。**控制开销比多数人以为的更值钱**
- 运行时长 89 分钟到 11 小时都有，**时长不进公式**（只做同分时的靠后比较）

---

## 3. 敏感度：什么值钱、什么不值钱

以 p=50、N=200（E=￥40）为基准：

**开销的影响**（通过率固定 50%）

| 开销(￥) | 比率 | 机制 | 得分 | 相对基准 |
| --- | --- | --- | --- | --- |
| 0.4 | 0.01 | reward | 79.24 | +58.5% |
| 4 | 0.10 | reward | 62.95 | +25.9% |
| 16 | 0.40 | reward | 54.80 | +9.6% |
| 40 | 1.00 | reward | 50.00 | 0% |
| 60 | 1.50 | penalty | 46.11 | −7.8% |
| 80 | 2.00 | penalty | 43.53 | −12.9% |
| 200 | 5.00 | penalty | 36.24 | −27.5% |
| 400 | 10.00 | penalty | 31.55 | −36.9% |

**通过率的影响**（开销固定 ￥16）

| 通过率 | 得分 | 相对 p=50 |
| --- | --- | --- |
| 30% | 31.24 | −43.0% |
| 40% | 42.87 | −21.8% |
| 50% | 54.80 | 0% |
| 60% | 66.97 | +22.2% |
| 80% | 91.90 | +67.7% |
| 100% | 117.46 | +114.4% |

**三条结论：**

1. **通过率是主导项。** 指数是 `1+γ`（奖励区 1.1、惩罚区 1.2），所以通过率涨 10% 相对值，
   得分涨约 11–12%；而开销对半砍只涨 7%（奖励区）或 15%（惩罚区）。
   **任何"为了省钱而降低正确率"的取舍都不划算。**
2. **开销的真正意义是"别越线"。** 在奖励区里继续省钱收益递减（￥40→￥4 才 +26%），
   但一旦越过 E，斜率翻倍。**目标不是"越便宜越好"，是"稳稳待在 E 以内"。**
3. **通过率还顺带放宽开销线**：E 正比于 p。通过率从 50% 提到 80%，E 从 ￥40 涨到 ￥64 ——
   分子变大、分母同时变松，双重收益。

### 开销目标怎么定

E 正比于任务总测试数 N：

| 场景 | N | p=50% 时 E | p=100% 时 E |
| --- | --- | --- | --- |
| hackathon 综合榜（两任务合计） | 200 | ￥40 | ￥80 |
| hackathon 单任务（github / sheet） | 100 | ￥20 | ￥40 |
| ticketbooking（playground） | 30 | ￥6 | ￥12 |

实测的 token 单价差异很大（同一份真实数据里）：

| 用户 | tokens | 开销(￥) | 单价 |
| --- | --- | --- | --- |
| VOLO-AI | 7.14 M | 16.39 | ￥2.30 / M |
| Iris | 70.29 M | 55.71 | ￥0.79 / M |
| bzt | 215.77 M | 49.40 | ￥0.23 / M |

所以**不要凭感觉估 token 预算**：先跑一次完整的单任务，用自己那条 run 记录里的
`token_count` 和 `token_cost_usd` 算出**自己的 ￥/M**，再倒推总预算。
参考量级：夺冠那次是 **7.1 M tokens / ￥16.4**；踩惩罚的 bzt 是 **215.8 M / ￥49.4**。

---

## 4. 运行时优化建议

按"收益 ÷ 风险"排序。**前两条是必修，中间是省钱，最后是提分。**

### 4.1 必修：保证"完成全部任务"

综合榜硬性要求**同一份提交跑完全部任务**，只跑一个任务 = 不进综合榜。
hackathon 有两个任务（github / sheet，各 100 测试），**预算和时间要按两个都跑来留**。

另外：**只有勾选"使用比赛额度评测"的运行才进排行榜**
（平台为每次运行创建临时 Key，从队伍共享的 ￥500 预算扣）。

### 4.2 必修：先测量，再优化

第一次跑完，从 run 记录里抄下三个数：`passed_count`、`test_total`、`token_cost_usd`。
算出 `p`、`E`、`r`，就知道自己在奖励区还是惩罚区 —— **这决定了后面该优化什么**：

- `r < 1`：已在线内，**全力提通过率**，省钱收益很小
- `r > 1`：先压开销（惩罚区斜率翻倍），但**不能以掉通过率为代价**

### 4.3 省钱：去掉提示词里的重复（✅ 已完成）

每个阶段的上下文里，**同一份接口清单出现了两次**：

| 块 | 来源 | 实测大小 |
| --- | --- | --- |
| `<interfaces>` | `ContextPipeline._get_node_session_layers`（node session 层） | 11.1 KB |
| `<current_interface_contract>` | `ContextPipeline.get_interface_contract_context`（阶段 agent 单独再传一次） | 11.2 KB |

实测（10 个接口的节点、TestDrivenDeveloper 阶段）：**总上下文 26.7 KB，其中 11.2 KB 是纯重复**，
两份的字段值**逐字段完全相同**。

**已修复**：`test_generator.py` 与 `test_driven_developer.py` 现在只在
`"<interfaces>" not in dynamic_context` 时才发 `### Current Interface Contract`；
`tools/check_stage_bridge.py::check_interface_contract_is_not_duplicated` 守着它。
**约 42% 已拿到。**

### 4.4 省钱：接口清单瘦身（✅ 已完成，但幅度只有文档原先估的一半）

`_get_source_file_cards` / `_get_existing_interface_cards` 早就做了截断
（responsibility 180 字符、specification 220 字符），而 `<interfaces>` 与
`<current_interface_contract>` 是整份 `json.dumps`，没有截断。

> ⚠️ **这里原先的建议是错的，别照做。**
> 原文说「截断成卡片视图可再省 43%」—— 那个数字是把 **180/220 的文本上限整套套上去**测的，
> 而这份 JSON 的体积大头正是 `responsibility` / `specification`，也就是 **agent 要照着写代码的
> 实现契约**。`<interfaces>` 是本节点自己的契约，`<existing_interfaces>` 才是别的节点的。
> 截断前者会丢实现细节 → 伤通过率，而**通过率是评分主导项**（见本文档开头）。

**实际做法**（`pipeline.py::_interface_contract_view`）：

- 只裁**对实现无用**的列表类字段：`callers` / `callees`（各 3 条 × 80 字符）、`test_focus`（4 条 × 120 字符）
- `responsibility` / `specification` **原样保留**
- 顺带：`<existing_interfaces>` 不再重复列出本节点自己的接口（那些已在 `<interfaces>` 里全量给出）；
  只跳过 `interface_id` 确实对得上的，避免两份存储不一致时丢接口

**实测降幅（合成数据：10 个接口、散文式 responsibility/specification、各 10 个 callers/callees）**：

| | 字节 |
| --- | --- |
| 全量 `_compact_json` | 35,856 |
| 契约视图 | 28,006 |
| **节省** | **7,850（21.9%）** |

**21.9%，不是 43%。** 差的那一半就是实现契约本身，那部分不该省。

回归检查：`tools/check_stage_bridge.py::check_interface_contract_keeps_its_load_bearing_text`
—— 钉住「`responsibility` / `specification` 不被截断」，防止以后有人照搬 180/220 把它一起砍了。

### 4.5 省钱：约束设计产物的篇幅

`InterfaceDesigner` 产出的 `responsibility` / `specification` 是长散文，
**并且会成为后续每个节点的输入**（写进追溯表 → 又被 `_get_source_file_cards` /
`existing_interfaces` 读回来）。所以它的输出 token 是**一次性成本**，
但它的**长度是乘法成本**。

- 改法：在 `agents/context/prompts/interface_designer.py` 的返回要求里给
  `responsibility` / `specification` 写明字数上限（例如各 ≤ 200 / 300 字符）
- 这会同时减少：本次输出 token + 所有下游节点的输入 token

### 4.6 省钱：减少工具往返

每次 `read_file` 都是一次完整的模型调用，**且要重发截至此刻的全部历史**。
阶段内历史越长，单次调用越贵（O(n²) 增长）。

- 现在：模型自己一个个 `read_file`（实测 REQ-1 阶段读了约 15 个文件）
- 建议：把接口契约里已经写明 `file_path` 的文件**预读进上下文**，
  省掉这些往返的模型调用（预读是拼接，不花模型 token）
- 注意 `agents/runtime/stage_policy.py` 已经在拦"重复读"和"重叠区间读"，可以再收紧

### 4.7 省钱：把压缩阈值调到真实窗口（✅ 默认值已改）

`app/config/__init__.py` 的 `LLM_CONTEXT_WINDOW` **默认已从 64000 改成 1000000**
（= 当前提交模型 deepseek 系在网关上的真实窗口）。**平台不下发 `LLM_*`**，所以这个默认值
就是平台实际用的那个 —— 原先的 64k 让压缩线落在 51.2K，而实测 input 到过 93K，
压缩被过早、反复触发。现在压缩线是 800K。

副作用有界：`summary_max_tokens` 是 `min(window × 5%, 8000)`，被 8000 兜住（3200 → 8000）。

⚠️ **但有一个尚未处理的副作用**：`summary_input_chars` = `window × 0.6 / OTHER_PER_TOKEN`，
随窗口线性放大 —— 1M 窗口下是 **2,000,000 字符（≈66 万 token）**。也就是说**压缩次数少了，
但每次压缩的摘要器输入上限放大了 15 倍**，而摘要输出被 8000 兜住。
喂 66 万 token 去产 ≤8000 token 的摘要，性价比很差。
**建议给摘要器输入加一个与窗口无关的绝对上限**（例如 20 万字符 ≈ 6.6 万 token 就足够），
不要让它跟着窗口一起涨。在那之前，大窗口下的压缩虽然更少、但单次更贵。

**换模型时要跟着改这个默认值** —— 真正的风险是撞 provider 超窗。

原分析（保留作背景）：

`app/config/__init__.py` 的 `LLM_CONTEXT_WINDOW` 原为默认 64000，压缩线 = 窗口 × `COMPACT_AT`(0.8)。
平台那条 run 里出现过：

```
摘要没带来净收益，不写入 thread=REQ-1.1:DESIGN:TestGenerator 中段=78 摘要块=204
```

这条本身是**正确行为**（压了不划算就跳过，不花冤枉钱）。但它说明**压缩确实被触发了**，
只是没压成。要真正省钱：

- 把 `LLM_CONTEXT_WINDOW` **按网关模型的真实窗口填**。填大了永远压不动、一路顶到 provider 报错；
  填小了偏早压缩（多一次摘要调用）。`main.py` 里可以用 `ARCBENCH_*` 环境变量覆盖
- 考虑调低 `COMPACT_AT`（例如 0.6–0.7），在阶段内更早压掉中段

### 4.8 省钱：分阶段用不同模型

`app/agent/models.py::build_model(name)` 已经支持指定模型名，
但 `agents/runtime/arc_agent.py` 现在对所有阶段都用同一个 `LLM_MODEL`。

- **设计阶段**（InterfaceDesigner / TestGenerator）决定正确率，值得用强模型
- **TDD 修错阶段**大多是机械改错，可以换便宜模型
- 平台在提交表单里给了 `Model` / `Visual Model` 两个字段；**多模型路由需要在代码里做**
  （例如按 stage 读一个环境变量，如 `ARC_MODEL_TDD`）
- ⚠️ 这条是**提分+省钱**，但也是**最容易改坏正确率**的一条 —— 建议先把 4.1–4.7 做完再动

### 4.9 提分：把技能正文真正送到模型手里（已修）

第一版有个 bug：技能被选中了但读不到正文（`技能文件不存在：/skills/...`），
**ARC 那五个调好的阶段技能等于全程没用上**。已修复（`_skills_root()` 统一到包内 `skills/`），
并由 `tools/check_stage_bridge.py` 守住。**这条对通过率的影响可能比上面所有省钱项加起来还大。**

### 4.10 提分：TDD 预算

`core/phases.py` 里 `TDD_RUN_TESTS_BUDGET = 5`（每层），顺序 Unit → Integration → E2E。
预算越大越可能修好，但每次 `run_tests` 的输出（截断到 4000 字符）都会留在历史里。

- 通过率优先的前提下，**不要盲目调小**
- 但可以：让 `run_tests` 的输出更短（只保留失败用例名 + 关键断言），
  减少历史膨胀 —— **在不牺牲修复信息的前提下省钱**

### 4.11 可观测性：让长阶段调用看得见（已修）

`agents/runtime/arc_agent.py` 的 `ArcStageAgent` 原先没暴露 `astream_events`，
上游 `runners.ainvoke_stage_agent` 就会打印 `agent streaming is unavailable; falling
back to ainvoke`，然后整段阶段调用变成**一次阻塞调用**。

后果不是慢，是**分不清在跑还是挂了**：实测一轮 design 要 3.8 分钟、TDD 一轮要 6.6 分钟，
那段时间日志一个字都不打，工具批次要等全部结束才一次性吐出来。
（平台上真的因此来问过"是不是卡住了"。）

已加上 `astream_events`，上游的流式分支直接接上，产出实时日志：

| 日志行 | 含义 |
| --- | --- |
| `agent stream start.` | 走的是流式路径（没有这行说明又退回阻塞了） |
| `tool-call> 工具名 args=...` | 每次工具调用**发起时**就打出 |
| `tool-result> 工具名 result=...` | 每次工具返回时打出 |
| `model-final> ...` | 每次模型调用结束时打出完整回复 |

**注意时长本身不进评分公式**，所以这条不直接提分 —— 它的价值是让人能判断进度、
及时止损。回归测试见 `_test_streaming.py`（同时验证结果不变、且**没有静默重跑**：
流式路径若丢掉终态会退回去 `ainvoke` 再跑一遍，那是双倍开销）。

### 4.12 省钱：把执行中的约束写进提示词（已修）

`stage_policy.py` 拦下的硬约束原先**只存在于拦截分支里**，提示词没提，
模型只能靠试错发现。实测：模型写了一个 158 行文件 → 被拦回「超过 160 行」→ 又写一遍。
**白烧一个完整往返**（那一轮要重发整段阶段历史）。

已改为由 `render_stage_limits(stage)` 从同一组常量渲染提示词片段，
`factory.py` 追加到系统提示词。同类风险：任何在工具层拦下的规则，
只要提示词没说，就会变成一次试错往返。

### 4.13 省钱：同一文件的不同路径写法必须算同一个键（已修）

阶段纪律靠「路径 → 已读/已写记录」去重，但**同一个文件会被模型用两种写法访问**：

```
/workspace/backend/src/database/init_db.js
backend/src/database/init_db.js
```

只补一个前导斜杠的话这是两个键，去重直接失效。实测一轮里：

| 文件 | 被读次数 |
| --- | --- |
| `init_db.js` | **6 次** |
| `db_runtime.js` | 4 次 |
| `test_harness.js` | 5 次 |

每次重复读都是一整个模型往返，而且那一轮要重发整段阶段历史。

已改为先把 `/workspace` 虚拟根前缀和 `./` 去掉再归一，
`tools/check_stage_bridge.py` 里加了 4 组写法等价断言 + 1 条「换个写法重读必须被拦」。

### 4.14 提分：阶段纪律的失败判定必须读工具正文（已修）

守卫（`agents/runtime/stage_policy.py`）用「本次调用成没成」决定要不要解锁已写过的文件。
搬到 my-agent 时判定被换成了**看有没有抛异常**，但 `run_tests` / `run_build` 失败时是
**返回** `Exit Code: 1` 的正文、**从不抛异常** —— 于是 `_validation_failed` 永远是假、
`_path_unlocked` 永远为假：**一个文件在本阶段被写过一次，就再也改不回、也读不回**，
守卫消息承诺的那个解锁成了不可达的死代码。

平台实测代价：**死锁 133 次工具调用**（REQ-1.1 被拦 98 次、REQ-1.2 被拦 35 次）。
四个单测红点全来自这条路径 —— agent 已经明确诊断出自己生成的测试文件要由
`require('vitest')` 改成 ESM `import`，却改不动它，只能反复重试到超时。

**为什么 demo 能过、ticket 过不了**：锁是**每轮 stage run 一份**。demo 被锁的是源码
（不在该轮测试的关键路径上，可拖到下一轮解锁），ticket 被锁的**就是测试文件本身**
（绕不过，且读也被锁）。

已按上游 `StageDisciplineMiddleware._tool_result_failed` 的语义改成读正文。
**注意这条依赖 `Exit Code:` 这个正文格式**，所以另加了两道独立见证：
`check_guard_unlocks_after_a_failing_validation`（行为）与
`check_guard_reads_the_real_validation_format`（生产者仍发出该标记 + 守卫与独立解析器结论一致），
防止格式改名后守卫**静默**停止解锁。
### 4.15 省钱：压缩摘要不得进 system 提示词（已修）

平台上真实 run 的 usage_metadata 里，缓存命中率**不是普遍偏低，而是局部塌陷**：
多数调用 98–99%，但个别调用 `cache_read` **恒等于 4096**、命中率 5% 左右，
且那几次的 input 比上一次**变小**（93K→81K、96K→87K）。

- **机制**：塌陷调用 = **一次成功的上下文压缩之后的第一次模型调用**。
  旧实现（`with_summary`）把摘要块拼进 system 提示词；system 位于请求前缀的最头部，
  它一变，这次请求与此前所有请求的共同前缀就在 system 末尾断掉——
  只剩「工具定义 + 静态 system 头部」约 4K tokens 命中，其后整段历史按全价计费。
  input 变小是重写的指纹：摘要顶替了中段。与"stage 结束"相关只是因为那是上下文最大、
  最先越过压缩线的地方（E2E 收尾正是历史最长处）。
- **改法**：摘要改为**一条独立的用户消息**，插在首条用户消息之后
  （`compaction.policy.summary_message`），system 逐字节不变。
  压缩前后请求的共同前缀由此延长到「工具定义 + system + 首条用户消息」——
  阶段编译里首条消息是完整需求上下文，正是前缀的大头。
- **量级**：一次 8–9 万 input 的调用，塌陷后约 8.5 万 tokens 从缓存价变全价。
  评分里开销是惩罚项（越过 E 后 γ 从 0.1 变 0.2），这类"不改变行为、纯抬 b"的浪费
  不修，等于每次压缩都白送一次惩罚区冲刺。
- **回归**：`_test_compaction_prefix.py`（真实图 + 脚本假模型，不花钱）断言
  压缩前后 system 逐字节一致、共同前缀延伸过首条用户消息。
  真实 run 的前后对比用 `_diag_cache.py <日志>`：塌陷签名（大 input + 4096 地板）
  在修复后的 run 里应当绝迹。

与 §4.3/§4.4 的两处上下文重复相互独立、可叠加：那两条是"少发 token"，
这条是"发出去的 token 命中缓存价"。

---

## 5. 行动清单

**提交前**
- [ ] 完整跑一次单任务，记录 `p` / `b` / `E` / `r`
- [ ] 确认两个 hackathon 任务都在计划内（综合榜要求全跑）
- [ ] 提交时勾选"使用比赛额度评测"（否则不进排行榜）
- [ ] 确认 zip 的 `main.py` / `requirements.txt` 在根目录，且用
      `python tools/build_submission_zip.py` 打包（不要用 `Compress-Archive`）

**第一批改动（低风险、收益明确）**
- [x] 去掉 `<interfaces>` / `<current_interface_contract>` 的重复（省约 42% 上下文）
      —— 由 `current_interface_contract` 的条件发射实现，`check_interface_contract_is_not_duplicated` 守住
- [x] 接口清单瘦身（**实测 21.9%，不是原先估的 43%** —— 见 §4.4 的更正）
      —— 只裁 callers/callees/test_focus，`responsibility`/`specification` 全留
- [x] `LLM_CONTEXT_WINDOW` 按真实窗口设置（默认 64000 → 1000000，见 §4.7）
- [ ] 设计产物加字数上限
- [x] 把阶段硬约束写进提示词（避免试错往返）
- [x] 路径写法归一（同一个文件不再被读 4–6 遍）
- [x] 阶段纪律的失败判定读工具正文（否则解锁永不触发，见 §4.14）
- [x] 压缩摘要改成独立用户消息（压缩不再打塌前缀缓存，见 §4.15）
- [x] 检索类工具接入查询缓存（`ls` / `glob` / `grep`），并把两个缓存与"失败即整阶段解锁"解耦
      —— **实测收益≈0**，而且顺手更正了 issue #63 的计数（那是日志行数，见 §4.17）

**第二批（需要验证再上）**
- [ ] 预读接口契约里点名的文件，减少工具往返
- [ ] TDD 阶段换便宜模型
- [ ] 收紧 `run_tests` 输出长度
- [x] 给 `ArcStageAgent` 加 `astream_events`（长阶段调用能看见进度了）

### 4.16 Issue #6：todo 与只读 subagent 的成本测量

Issue #6 的新增工具只接入 `implementation`。工具 schema 与阶段提示词都用
`tiktoken` 的 `cl100k_base` 做了同一份 requirements、同一轮数的静态对照；这是
不调用外部模型的可复现实验，避免把本地 mock 的固定响应误报成真实 API 价格：

| implementation 工具面 | `token_count`（schema 代理） | `token_cost_usd` |
| --- | ---: | ---: |
| 原文件工具面（6 个） | 1,864 | N/A（未调用计费 API） |
| 加入 subagent + todo 四件套（11 个） | 2,294 | N/A（未调用计费 API） |
| 增量 | **+430** | N/A |

阶段限制之后公布新增工具的文案从 142 增至 271 tokens（+129）。本仓库的
`_mock_llm.py` 是 SSE 流且不返回 provider `usage_metadata`，所以 mock E2E 的
`token_count=0`、`token_cost_usd=0` 只能作为功能回归，不能当作费用结论。接入真实
网关后，用 run 记录的 `token_count` 与 `token_cost_usd` 复跑同一 requirements、同一
轮数，并把两行真实值补到此表；若 subagent 实际未被调用，工具 schema 的 +430
tokens 是其可见上限，todo 的拉取式清单不会改变 system 前缀。

### 4.17 ⚠️ 更正 issue #63 的计数：检索类工具**没有**会话内重复查询（改动已做，收益≈0）

**先把数更正过来。** issue #63 的「727 次工具调用 / 71% 是查找 / 同一文件被读 11–16 次」
**数的是日志行，不是调用次数**。`runners._log_stream_event` 给每一次 `on_tool_start` 打一行，
而**一个逻辑调用会打出 2–3 行**：

| 行 | 是什么 |
| --- | --- |
| 1 | 守卫包的 `StructuredTool`（模型给的原始参数） |
| 2 | 被包的 `workspace_tools.*` 工具（schema 补全后的参数 —— 所以 `read_file` 那行会多出 `offset/limit: null`） |
| 3 | 它内部委托的 my-agent 工具：名字是 `find` / `write` / `edit`，或者 `path` 是**解析后的绝对路径** |

按「同一 (工具, 规范化参数) 在一个会话内的事件数 ÷ 2」折回（`ls` / `grep` 除以 3，它们多一层委托），
`run-logs/2026-09-28-hackathon--github-01fcacc` 的真实构成是：

| | issue #63（日志行数） | 实际调用数 |
| --- | --- | --- |
| `read_file` | 378 | **189** |
| `ls` | 103 | **34** |
| `glob` / `grep` | 17 / 11 | **9 / 4**（另有内层 `find` 9） |
| `write_file` / `edit_file` | 52 / 50 | **26 / 25**（另有内层 `write` / `edit` 25 / 23） |
| 查找 ÷ 动手 | 3.5:1 | **235 ÷ 51 = 4.6:1** |

**而且一个会话内几乎没有重复查询。** 188 个 (会话, `read_file`, 规范化参数) 组里，
**182 组的行数正好是 2**（= 恰好一次调用）；只有 4 组是 3 行 —— 其中
`/backend/src/index.js` 正是那次被旧守卫拦下的重复读。`ls` / `glob` / `grep` 的会话内重复是 **0**
（「`ls` 被调 102 次」是 102 **行** ≈ 34 次调用）。

> 同一个文件被读十几次**是真的，但发生在会话之间**：每个 TDD 层是一个新会话、上下文干净，
> 于是各自把要用的文件重读一遍。App.tsx 在五个会话里各读一次 = 10 行事件，
> 看起来就像"被读了 12 次"。

**所以方案 A 没有可打的目标**，但仍然做了 —— 它便宜、正确，而且顺手修掉一个真 bug：

- `ls` / `glob` / `grep`（含工具面里的 `find`）按 (工具, 规范化参数) 只放行一次，
  重复回「用之前的结果」；工具报错或被拦则解锁该查询（与读/写的既有规则一致）
- **两个缓存在 `check()` 放行时就认领**，失败时撤销 —— 一批工具调用是**并发**跑的
  （`run_calls` 给每个调用建 task），只在成功后记账的缓存看不见同批孪生。而实测确实出现过
  同批重复：2026-09-26 的归档里同一个文件在一次会话内被拦**两次**，那只能是同批孪生
- 重复读 / 重复检索不再跟着 `_validation_failed` 整阶段解锁：一次测试失败不会让已经拿到的
  文件内容或目录清单变旧，却会把两个缓存整个关掉，而 TDD 阶段**就是**由失败驱动的
  （原先实测同区间重复读 190 行、守卫只拦下 1 次）。写入侧与「读回自己写过的文件」保持
  整阶段解锁 —— #4.14 / #43 的解锁是承重的
- **真 bug**：`bounded_args` 拿归一后的键去判 `startswith("/workspace/")`，而那个前缀已经被
  归一吃掉了 → 判断恒假、上限从来没生效：`read_file(path, limit=500)` 按 (0, 500) 记账，
  工具却只返回 200 行（`MAX_READ_LIMIT`），于是合法的续读 `offset=200` 被判成"重叠"拦下，
  还附一句"用之前的结果" —— 而 201–300 行从来没给过

**实测收益 = 0**：第一次受限运行（`run-logs/2026-09-28-hackathon--github-c847c43`，
51 次模型调用 / 12 个会话）里检索缓存**一次都没拦到**，只有 2 次写拦截 ——
与上面「没有会话内重复查询」一致。

**该打哪里**：既然重读发生在**会话之间**，省它的方向是让新会话不必重新读同样的文件 ——
§4.6 的预读、方案 C 的仓库地图，或者把上一个会话的读摘要带过去（上游 `_template` 里有
`arc_read_summaries`，这一版没搬；归档里每个会话的 `resume_context` 都是 `{}`）。
这比继续收紧会话内守卫有用得多。

关于"被拦一次会不会变成硬循环"：翻了三个归档，**11 次读拦截之后模型一次都没有重发**
同一个读（0/11），所以「拦住一次 → 解锁 → 仍要问就再问」这条软规则在实践中是收敛的，
不需要更硬的拦法。

回归检查：`tools/check_stage_bridge.py` 里新增三条 ——
`check_repeat_searches_are_served_from_the_cache`、
`check_a_failing_validation_does_not_switch_off_the_lookup_cache`、
`check_the_read_cache_records_the_window_the_tool_serves`。
**在旧代码上这组是红的（10 条断言失败）**，改完之后全绿。

### 4.18 ⚠️ 待修：模型调用没有整段超时，一次网络抖动就挂死整轮（实跑遇到）

`run-logs/2026-09-28-hackathon--github-c847c43` 的时间线：

```
14:10:27  运行开始
14:54:10  第一次模型调用报 Connection error（retry.py 记「第 1/3 次尝试，N 秒后重试」）
14:54:11  …然后 46 分钟里日志一个字都没有
~15:40    run_bounded.py 的 90 分钟看门狗把进程杀掉，归档
```

同一时刻这台机器到 `api.commandcode.ai` 的网络是断的（同日 `git fetch` 也报
`Failed to connect to github.com port 443 via 127.0.0.1`）。**但三层现成机制都没兜住它**：

| 机制 | 值 | 为什么没兜住 |
| --- | --- | --- |
| `app/agent/retry.py` | 3 次、退避 2/4/8s | 它只重试**已经失败**的调用；第 2 次尝试**根本没返回** |
| `stream_chunk_timeout` | 300s | 那是**分片之间**的看门狗；响应头都没到，它不启动 |
| SDK 默认读超时 | 600s | 连接「连着但不回」时要 10 分钟才第一次失败，还会被重试放大 |

**平台上那两次 SIGKILL 很可能属于同一类**（7h45m / 9h05m 跑完却被杀，`died with
<Signals.SIGKILL: 9>`）—— 平台只给总时长，看不出中间有多少时间耗在这种静默挂起上。
这一条比任何 token 优化都值钱：**它决定"跑不跑得完"，而跑不完就是 0 分。**

建议（各自可验证）：

- 给每次模型调用一个**整段上限**：`asyncio.wait_for(model.ainvoke(...), LLM_CALL_DEADLINE)`
  （默认 600s 量级），超时算瞬态失败、进现有重试 —— 静默挂起就再也吃不满预算
- 重试那条 warning 把**延迟值单独打一行**（这次导出的 stdout 里那个数字是乱码，无法复盘）
- 归档里记下「最后一个模型调用的时间戳」，让"挂了多少分钟"一眼可见

**每次改完**
- [ ] `python tools/check_stage_bridge.py`（守住技能根与分页语义）
- [ ] `python arc_main.py doctor`
- [ ] 重新打包并**从解压后的包里**跑一次

---

## 6. 参考：数据来源

| 数据 | 来源 |
| --- | --- |
| 评分公式与弹窗文案 | `https://arc-bench.com/competition` → Leaderboard → "View scoring rules" |
| 排行榜真实行 | `GET https://arc-bench.com/api/competitions/leaderboard?competition_id=hackathon` |
| run 记录字段 | `GET https://arc-bench.com/api/runs/{run_id}` |
| run 日志 | `GET https://arc-bench.com/api/runs/{run_id}/logs`（`events` / `stdout` / `stderr` / `console`） |
| 提示词体积与重复 | `_measure_prompt.py` 实测（10 接口节点、TestDrivenDeveloper） |

公式验证脚本与提示词测量脚本见交付说明（不在本包内，位于开发机的
`_verify_formula.py` / `_measure_prompt.py`）。
