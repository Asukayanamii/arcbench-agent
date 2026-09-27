# 比赛实跑归档：hackathon--sheet（第三份导出，提交 id `a1b3591d6000`）

由用户从比赛平台导出（`bs log3.txt`，1.93 MB / 9,850 行）。**这是用「最新代码」跑的那一份**
（用户原话），与我之前归档的 `…-sheet-bs` 是**同一个 task 的另一次提交**。

## 结果

```
时长          85 分 58 秒        （第一份 sheet log 是 201 分 47 秒）
守卫拦截      12 次              （第一份是 1157 次）
TDD 判定      3 条
超时 / 预算耗尽 / 安装失败 / 支付   全 0
```

**`REQ-1-1-1` 红了。**

## ★ 守卫死锁（#44）确认消失

第一份 sheet log 里一个测试文件被 "Repeated write blocked" 拦了 **1142 次**、烧掉 201 分钟。
本份只有 **12 次**拦截，时长也跟着降到 86 分钟 —— **#44 的修复在生产上生效。**

## ★ 但 `REQ-1-1-1` 的红是一个假红（issue #46）

```
10:11:39  agent call end: duration_ms=349367.2
10:11:39  "summary": "Implementation looks substantially present.
                      Let me check the shell components, configs, and package scripts."
10:11:39  TDD agent session ended without calling run_tests for `E2E`; …
10:11:39  TDD batch `E2E` did not pass after **0/5** run_tests call(s); agent session ended before this layer passed.
10:11:39  IMPLEMENT failed for node REQ-1-1-1.
```

**`0/5` 说明 E2E 层一次都没跑过。** agent 在那一层的最后一条消息是一句计划
（*"Let me check the shell components…"*），零工具调用就结束了 session。

`core/phases.py` 里 `used_after == used_before` 时**直接 `break`** —— 但此刻预算一次都没动
（`0/5`）、`max_sessions` 允许 15 次而只用了 4 次，**重试完全免费**。代码却把
「从未被尝试过的层」判成「没通过」。而 pass rate 是评分主导项：**没被尝试 = 尝试后失败。**

→ 已修（#46 / PR #47）：重试本层（同层最多 2 次空 session），并把「上一轮一次测试都没跑」
写进 `previous_failure_summary` 带进下一轮。

## 因此：**这次的红不正常**，预期修复后会变成「重试一次并真的跑起 E2E」

如果重试后 E2E 仍失败，那才是真实的通过率信号；那时该看的是 E2E 层的失败断言，而不是层判定本身。

## 检索命令

```powershell
Select-String .\competition-log.txt -Pattern 'ARC stage discipline'          # 本轮 12 次
Select-String .\competition-log.txt -Pattern 'TDD batch'                    # 本轮 3 条
Select-String .\competition-log.txt -Pattern 'ended without calling run_tests'  # 假红的证据
Select-String .\competition-log.txt -Pattern 'timed out after'              # 本轮 0 次
```

## ⚠️ 归因限制

1. 导出的是**页面文本**（含 UI 文案），不是结构化日志；行号无意义，需靠模式检索。
2. **平台跑的是哪个 commit 在日志里没有体现** —— 用户说「最新代码」，但无法从日志核实到具体提交。
   建议下次在平台上记下显示的 commit，归档才能精确对应。
3. 单次实跑，不能据此判断通过率趋势。
