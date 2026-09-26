# 本地判题模拟：hackathon--github（第 9 轮）★ **#33 修复生效，收敛达成**

## 被测版本

| | |
| --- | --- |
| 仓库 | `Asukayanamii/arcbench-agent` |
| 分支 | `main` |
| **commit** | **`0285ab00fe55b1d68409cdf13e895e02f75deb45`**（`0285ab0`） |
| 含 | 队友的 subagent + todo、全部修复、**#33（层通过后拒绝重跑）** |

## 运行参数

```
started : 2026-09-26 19:01:34 +0800
stopped : 2026-09-26 19:40:32 +0800   （39 分钟，凑满 3 个判定即被看门狗收停）
```

## 结果

```
19:40:31  TDD batch `Unit`        passed       after 1/5 run_tests call(s).
19:40:31  TDD batch `Integration` passed       after 3/5 run_tests call(s).
19:40:31  TDD batch `E2E`         did not pass after 5/5 run_tests call(s); budget exhausted.
```

超时 **0**、支付错误 **0**。

## ★ #33 修复的量化效果

**日志证据**：`` `run_tests` Unit already passed; refusing to repeat it. `` 出现 **2 次**。

| 层 | 第 6 轮（修复前） | 第 9 轮（修复后） | 说明 |
| --- | --- | --- | --- |
| Unit | **5/5** | **1/5** | 一次就过，不再重跑 |
| Integration | **5/5** | **3/5** | 第三次过，随后停止 |
| E2E | 2/5（过了） | **5/5（没过）** | 真没过，预算给足 |

```
usage 分布（修复后）：Unit 1 · Integration 1,2,3 · E2E 1,2,3,4,5
```

**这正是想要的形状**：通过的层只花它需要的次数，而**真正没过的那层用满了 5 次** ——
预算花在该花的地方。被拒的调用不产生 usage 行，所以这份分布就是不含浪费的真实次数。

**Unit + Integration 合计：10 次 → 4 次（省 60%）。**

## 与前几轮对照

| | 第 4 轮 `8792028`（修复前） | 第 6 轮 `3012358` | 第 9 轮 `0285ab0` |
| --- | --- | --- | --- |
| 超时 | 37 | 0 | **0** |
| 真实 `Exit Code 0/1` | 0（清一色 124） | 12 | 9 |
| TDD 判定 | 0 | 3 | **3** |
| Unit / Integration 预算占用 | — | 5/5 · 5/5 | **1/5 · 3/5** |

E2E 本轮未过属**逐轮差异** —— 每轮重新生成的测试不同；每轮只有 1–2 个节点，样本量不足以谈趋势。

## 日志导航

```powershell
Select-String .\debug.log -Pattern 'TDD batch'                       # 本轮 3 条
Select-String .\debug.log -Pattern 'refusing to repeat'              # 本轮 2 次（#33）
Select-String .\debug.log -Pattern '`run_tests` \w+ usage'           # 真实调用次数
Select-String .\debug.log -Pattern 'timed out after'                 # 本轮 0 次
```

## ⚠️ 保真度限制

仍只跑了 1 个节点（65 个中的第 1 个）。比赛测试隐藏，跑的是生成测试，**通过率无法本地自测**。
