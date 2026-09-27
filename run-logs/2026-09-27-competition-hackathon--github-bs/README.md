# 比赛实跑归档：hackathon--github（**仍在运行中导出**）

由用户从比赛平台导出（`bs log 2.txt`，8.75 MB / 46,547 行）。这是**比赛任务**，不是本地模拟。

## 被测版本

| | |
| --- | --- |
| 平台提交 id | `1b1fcb3de768` |
| 任务 | **GitHub Collaboration Platform Core Requirements**（= `hackathon--github`） |
| 平台时长 | **226 分 48 秒**（导出时仍显示 Running / Pause / Cancel run） |
| 结果 | 导出时仍在跑，已推进到 `REQ-1-3` |

⚠️ **平台跑的是哪个 commit 未在日志中体现** —— 导出的是页面文本，不含 git 信息。

## ★ 这是一份「多项修复在生产环境生效」的正面样本

```
守卫拦截     : 55 次，分散在三个 agent
  TestDrivenDeveloper 20 / InterfaceDesigner 18 / TestGenerator 17

TDD 判定     : 11 条，全部 passed
  Unit         passed 1/5 ×1 · 2/5 ×1
  Integration  passed 1/5 ×3 · 2/5 ×1
  E2E          passed 1/5 ×3 · 2/5 ×1

超时 0 · 预算耗尽 0 · session 早停 0 · NPM install 失败 0 · 支付/额度 0
```

**每层只花 1–2 次预算、从不接近 5 次** —— 这是 issue #33（层通过后拒绝重跑）在生产环境的直接证据。
对照修复前的本地第 6 轮：Unit 5/5、Integration 5/5。

**没有任何一层被预算耗尽**，也没有一次超时 —— 说明 #29（依赖安装）与 #24 / #26（runner 缺失、
安装不校验）在平台上同样不再发作。

## ★ 它同时说明了 #44 死锁的边界

拦截 55 次而不死锁，是因为主要拦截发生在 **`TestDrivenDeveloper`**，而**它允许调验证工具**：
`run_tests` 一失败就置 `_validation_failed`、整阶段解锁，被拦的路径随之解开。

日志里能看到 agent 自己意识到了这条路径：

```
[TestDrivenDeveloper][REQ-1-3] model-final> The run_tests validation error has b…
```

而 `test_generation` 阶段没有验证工具可用，所以那边（见
`run-logs/2026-09-27-competition-hackathon--sheet-bs`）就走成了 1142 次死循环。
两份 log 放在一起，正好构成「有验证工具 vs 没有验证工具」的对照。

## 检索命令

```powershell
Select-String .\competition-log.txt -Pattern 'ARC stage discipline'   # 本轮仅 55 次
Select-String .\competition-log.txt -Pattern 'TDD batch'              # 本轮 11 条，全 passed
Select-String .\competition-log.txt -Pattern 'run_tests validation error'  # agent 的自我诊断
```

## ⚠️ 归因限制

1. 导出时**仍在运行**，所以这不是最终结果；后续可能变化。
2. 导出的是**页面文本**（含 UI 文案），不是结构化日志。
3. 平台 commit 未知（见上）。
4. 11 条判定全部通过**不等于官方测试会过** —— 跑的是生成测试。
