# 本地判题模拟：hackathon--github（第 8 轮）

## 被测版本

| | |
| --- | --- |
| 仓库 | `Asukayanamii/arcbench-agent` |
| 分支 | `main` |
| **commit** | **`d75f7634371b5053556961094077a07f9d3f2651`**（`d75f763`） |
| 含 | 队友的 subagent + todo、本地全部修复、**修好的看门狗** |

## 结果

```
18:39:04  TDD batch `Unit`        did not pass after 5/5 run_tests call(s); budget exhausted.
18:39:04  TDD batch `Integration` passed       after 5/5 run_tests call(s).
18:39:04  TDD batch `E2E`         passed       after 3/5 run_tests call(s).
```

```
run_tests 调用   32
超时              0
TDD 判定          3（Unit 未过 / Integration 过 / E2E 过）
支付错误          0
运行时长          41 分钟，凑满 3 个判定即被看门狗收停
```

**Unit 这轮真的没过**（attempt 1/2/3 都是 `Exit Code: 1` 的真实失败，用满 5 次预算）。
与第 6、7 轮对照，这是**生成测试的逐轮差异** —— 每轮重新生成的测试不同，同一节点的难度随之变化。样本量太小（每轮 1–2 个节点），不足以谈趋势。

## ★ 修好的看门狗确认生效

第 7 轮因看门狗失效跑满 50 分钟（判定早在 17:20 就够 3 条）；本轮**在 3 个判定处正确收停**（41 分钟）。

## 本轮发现的缺陷（已修 / 已提）

**已修（本地那半）**：判题 stdout 转储被我写在 agent 的工作区里，agent 直接 grep 到它 —— 日志里出现 `runner-stdout.txt:7297: ...`。已移到工作区外。

**issue #36（产品那半，待做）**：`.arc/debug.log` 本来就在 `/workspace` 里（第 4 轮就见到 agent 读它）。第 7 轮该文件 **2.8 MB**，agent 一旦读入就是几万 token 的噪音；而且日志里全是**历史**测试输出，可能让它去修已经修好的东西。建议让 `grep`/`glob`/`find`/`ls` 默认跳过 `.arc/`。

## 队友工具的调用情况

```
add_todo      : 4 次
subagent      : 2 次
complete_todo : 0 次
list_todo     : 0 次   <- issue #34
update_todo   : 0 次   <- issue #34
```

## ⚠️ 保真度限制

仍只跑了 1 个节点（65 个中的第 1 个）。比赛测试隐藏，跑的是生成测试，**通过率无法本地自测**。
