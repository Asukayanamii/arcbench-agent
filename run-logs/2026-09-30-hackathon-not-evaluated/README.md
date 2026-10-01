# 跑完了、96% 的节点是绿的，但退出码 1 → 平台一个测试都没跑（0/200）

提交 `594cec9bc2c9`（09-30 06:15 UTC 起跑）的两题都在 **12.2 小时**内把整棵需求树跑完，
但两条 run 的 `failure_reason` 都是 `… returned non-zero exit status 1`：
`start_agent: failed` → `run_tests: pending - Not reached` → 0/200、score 0。

| run | 任务 | 节点状态 | 调用 | tokens | 花费 | 平台结局 |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `829d9669d118` | hackathon--github | **63/65 test-passed**（失败 REQ-1-1-1、REQ-2-1-1） | 1,188 | 48.0M | ¥56.90 | `run_tests: Not reached` |
| `f84071f7b84d` | hackathon--sheet | **39/42 test-passed**（3 个失败） | ≥773 | 54.7M | ¥50.40 | `run_tests: Not reached` |

agent 侧其实跑得很好：github 的日志末尾有完整 Compilation Summary、墙钟 12.2h 远小于 23h
限值、rss 峰值只有 266MB（09-28 那轮 1.31GB）。**0 分的唯一原因是退出码**：

```
[Compiler][error] Compilation finished with 2 failed node(s): REQ-1-1-1, REQ-2-1-1
✗ Failed:  REQ-1-1-1, REQ-2-1-1
（main.py 退出 1）→ 平台：start_agent failed → run_tests pending → 0/200
```

平台的评测**只看进程退出码**（我们自己的两条先例：`5e447a97` start_agent completed →
评测跑了、评出 30 个失败；`7d895aac11f4` 非零退出 → `Not reached`）。于是 2 个失败节点
把 63 个绿节点一起带进了 0 分。

## 修法（本次提交）

- `core/workflow._build_compile_result` 新增 `finished`：队列排空（所有任务到终态，可能
  含失败节点）
- `arc_main.compile_exit_code()`：`ok` / `soft_stop` / `finished` → **退出 0**；
  只有"没跑完"（初始化失败、异常、队列未排空）才退 1
- 行为检查 `check_a_failed_node_does_not_block_evaluation`（旧代码上红）

## 同轮的其它发现

- **15:55–18:27（约 2.5h）**卡在 `REQ-2-2-3` 的 TestGenerator：反复
  `摘要没带来净收益，不写入`（中段 663–3865），期间没有一次成功的模型调用
- **18:27 起网关 429**：`insufficient_quota` / "Free quota exhausted and balance too low,
  please recharge compute credits" —— 最后几个节点（REQ-2-2-4、REQ-6-6 …）是降级完成的
  （0 interface / 0 test 也记成 test-passed）
- 每节点往返数大幅下降：github 全程 1,188 次调用 / 65 节点（09-28 那轮是 3,435 次 / 12 节点）

## 归档内容

| 文件 | 是什么 |
| --- | --- |
| `github-stderr.log` / `sheet-stderr.log` | 两题 stderr 全量（143 / 77 行：失败、压缩空转、429） |
| `github-tail.log` / `sheet-tail.log` | 被杀前最后 50 行 stdout（github 含完整 Compilation Summary） |
| `run-829d9669d118.json` / `run-f84071f7b84d.json` | `/api/runs/{id}` 原始响应（steps / node_states） |

注意：sheet 的日志只抓到最后 8MB（`--keep-mb 8`），github 是全文 15.5MB。
