# 墙钟软停止冒烟：150s 上限，进程按预期自己收尾（退出 0）

`ARC_RUN_DEADLINE_SECONDS=150` + `python -u tools/run_bounded.py hackathon--github 99 5`，
代码 = `018df66` + 同一次改动（工作树 dirty，尚未提交）。跑到 **150.2s**（`REQ-1-1-1` 的
Design 阶段）时：

- `.arc/debug.log`：`Run deadline reached while a task was still running: 150s >= 150s
  (ARC_RUN_DEADLINE_SECONDS)…`
- CLI 打印 `SOFT STOP: …` 与 `Exiting 0 on purpose so the platform still evaluates the
  generated workspace.` → **退出码 0**
- **没有**写 `submission_failure_report.*`（软停止不算失败）

对照：同一条命令在改动前的 150s 冒烟里是 `Compilation returned ok=false` + 退出 1；
平台对非零退出是 `start_agent: failed` → `run_tests: pending - Not reached`（run
`7d895aac11f4`，35.5M token 白跑）—— 这正是这次要修掉的东西。

这里的产物只作"机制在真实流程里跑得通"的凭证，不是通过率口径（本地没有比赛测试集）。
