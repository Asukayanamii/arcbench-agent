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

### 4.3 省钱：去掉提示词里的重复（已定位，收益明确）

每个阶段的上下文里，**同一份接口清单出现了两次**：

| 块 | 来源 | 实测大小 |
| --- | --- | --- |
| `<interfaces>` | `ContextPipeline._get_node_session_layers`（node session 层） | 11.1 KB |
| `<current_interface_contract>` | `ContextPipeline.get_interface_contract_context`（阶段 agent 单独再传一次） | 11.2 KB |

实测（10 个接口的节点、TestDrivenDeveloper 阶段）：**总上下文 26.7 KB，其中 11.2 KB 是纯重复**，
两份的字段值**逐字段完全相同**。**去掉一份就省掉约 42% 的上下文**。

- 受影响的是 **TestGenerator** 与 **TestDrivenDeveloper**（InterfaceDesigner 只拿一次，没有重复）
- 而 TestDrivenDeveloper 恰恰是调用最密的：每层最多 5 次 × 3 层 = 每节点最多 15 次
- 改法：`get_interface_contract_context` 要么不再单独传，要么让它返回空，
  由 `<interfaces>` 承担；两边只留一处

### 4.4 省钱：接口清单也做截断

`_get_source_file_cards` / `_get_existing_interface_cards` 已经做了截断
（responsibility 180 字符、specification 220 字符），但**上面那两个块没有**，
它们 `json.dumps` 整份接口对象。实测全量 11.1 KB、截断成卡片视图 6.3 KB —— **再省 43%**。

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

### 4.7 省钱：把压缩阈值调到真实窗口

`app/config/__init__.py` 的 `LLM_CONTEXT_WINDOW` **默认 64000**，压缩线 = 窗口 × `COMPACT_AT`(0.8)。
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

---

## 5. 行动清单

**提交前**
- [ ] 完整跑一次单任务，记录 `p` / `b` / `E` / `r`
- [ ] 确认两个 hackathon 任务都在计划内（综合榜要求全跑）
- [ ] 提交时勾选"使用比赛额度评测"（否则不进排行榜）
- [ ] 确认 zip 的 `main.py` / `requirements.txt` 在根目录，且用
      `python tools/build_submission_zip.py` 打包（不要用 `Compress-Archive`）

**第一批改动（低风险、收益明确）**
- [ ] 去掉 `<interfaces>` / `<current_interface_contract>` 的重复（省约 42% 上下文）
- [ ] 接口清单改用截断卡片视图（再省 43%）
- [ ] `LLM_CONTEXT_WINDOW` 按真实窗口设置
- [ ] 设计产物加字数上限
- [x] 把阶段硬约束写进提示词（避免试错往返）
- [x] 路径写法归一（同一个文件不再被读 4–6 遍）

**第二批（需要验证再上）**
- [ ] 预读接口契约里点名的文件，减少工具往返
- [ ] TDD 阶段换便宜模型
- [ ] 收紧 `run_tests` 输出长度
- [x] 给 `ArcStageAgent` 加 `astream_events`（长阶段调用能看见进度了）

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
