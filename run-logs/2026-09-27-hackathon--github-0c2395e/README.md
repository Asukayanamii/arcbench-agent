# 本地判题模拟：hackathon--github（最终验证轮）★ **五项修复同时实测通过**

## 被测版本

| | |
| --- | --- |
| 仓库 | `Asukayanamii/arcbench-agent` |
| 分支 | `main` |
| **commit** | **`0c2395e`**（`Merge pull request #41 from Asukayanamii/dev`） |
| 含 | 全部 17 条 issue 的修复，含队友的 todo 增量语义（`4976d55`） |

## 运行参数

```
started : 2026-09-27 12:12:50 +0800
stopped : 2026-09-27 12:58:59 +0800   （46 分钟，凑满 3 个判定即被看门狗收停并自动归档）
command : python -u ..\tools\run_bounded.py hackathon--github 3 90
```

## 结果

```
12:58:58  TDD batch `Unit`        passed       after 2/5 run_tests call(s).
12:58:58  TDD batch `Integration` passed       after 2/5 run_tests call(s).
12:58:58  TDD batch `E2E`         did not pass after 5/5 run_tests call(s); budget exhausted.
```

逐次明细（`REQ-1-1-2`）：

```
Unit          failed 1/5  ->  passed 2/5   ✓
Integration   failed 1/5  ->  passed 2/5   ✓
E2E           failed 1/5, 2/5, 3/5, 4/5, 5/5   ✗ 真难，不是浪费
```

**超时 0、支付错误 0。**

## ★ 五项修复在同一次实跑里同时生效

| issue | 修复前 | 本轮实测 |
| --- | --- | --- |
| **#29** 依赖安装 | backend 缺 6 个 / frontend 缺 17 个 devDeps | **缺失 0 个**，vitest / vite 可用 |
| **#33** 层通过后拒绝重跑 | Unit 5/5、Integration 5/5 | **2/5、2/5**；拒绝重跑 **4 次** |
| **#34** todo 增量语义 | `list_todo` 永远 0 次 | **`list_todo` 被调用 4 次** |
| **#36** `.arc` 检索污染 | grep 命中 `.arc/debug.log:10460:` | **命中 0 条** |
| #21 / #24 / #26 | 清一色 124 超时、0 判定 | 真实 `Exit Code 0/1`、完整判定 |

这五项分布在四个不同位置（`phases.py` 的预算判定、`web.py` 的依赖安装、
`common.py` 的检索排除、`todo.py` 的接口语义），彼此无耦合，却同时生效 ——
说明各自的行为检查没有互相掩盖。

## ★ 看门狗与自动归档确认可用

前几轮修的两处（判定检测改读 `.arc/debug.log`、stdout 重定向到文件）在本轮兑现：
**46 分钟就准确在 3 个判定处收停，并自动完成归档**（17 文件 / 2.56 MB），无需人工补。

## 与前几轮对照

| | 第 4 轮 `8792028`（修复前） | 第 9 轮 `0285ab0` | 本轮 `0c2395e` |
| --- | --- | --- | --- |
| 超时 | **37** | 0 | **0** |
| 真实 `Exit Code 0/1` | 0（清一色 124） | 9 | **9** |
| TDD 判定 | **0** | 3 | **3** |
| 通过的层预算占用 | — | 1/5 · 3/5 | **2/5 · 2/5** |
| `list_todo` | — | 0 | **4** |
| grep 命中 `.arc` | — | — | **0** |

## ⚠️ 保真度限制（仍然成立）

1. **只跑了 1 个节点**（`REQ-1-1-2`），任务共 65 个。
2. **比赛测试隐藏**，跑的是**生成的测试**；生成测试与实现可被 agent 同时修改，
   所以「两层通过」不等于官方测试会过。**通过率依然无法本地自测。**
3. E2E 连续 5 次失败属本轮具体情形，与前几轮的「E2E 通过」不构成趋势 ——
   每轮重新生成的测试不同，而每轮样本只有 1–2 个节点。
4. 本地无 token 计费数据。

## 归档来源

本轮由 `tools/run_bounded.py` **自动归档**（这是它第一次真正跑通全流程）。
