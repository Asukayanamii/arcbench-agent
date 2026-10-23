# 平台 24h 软时限：两题都在 **86,401s** 被 `SoftTimeLimitExceeded()` 杀掉（评测没开始）

2026-09-28 13:21:52（UTC）提交的 `be2190adb5ed`（包 = 当时的 dev tip，含 #66/#67/#68）
在平台上并行跑两题。**09-29 13:21:53（UTC）两题同时被杀**，`failure_reason =
SoftTimeLimitExceeded()`（celery soft time limit = 1440 分钟），`passed_count 0 /
failed_count 0 / test_pass_rate 0`、`steps.run_tests = Evaluation pending`。

| run | 任务 | 时长 | 输入 tokens | 花费 | 模型调用 | 判定过的节点 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `343cf77b3bfc` | hackathon--github | 86,401s | 219,200,022 | ¥124.18 | 3,435 | 12 / 65 |
| `702881a10479` | hackathon--sheet | 86,401s | 262,146,112 | ¥120.31 | 3,754 | 18 / 42 |

两题合计 **¥244.49**，池子从 ￥363.73 掉到 **￥119.24**（页面 MY REMAINING BUDGET 实测），
而 E(p=50%) 才 ¥40 —— 这一轮把 1/5 的池子烧在了一个**不会被评测**的运行上。

## 三个待判问题

**1）用的是哪个包？** 当时的 dev tip（`018df66`）：

- 日志里有 `rss=` 用量行 → 至少含 #67（`2cf1de8`）
- 报错原话「单次模型调用超过 **1800s** 未返回（LLM_CALL_DEADLINE）」→ 1800s 默认值是 #68
  （`018df66`）才改的（#66 的 `e4c272d` 是 600s）
- `Token budget reached` **0 次** ✓（`ARC_TOKEN_BUDGET` 默认关，符合预期）

**2）有没有可评结果？** 没有。评测根本没开始；被杀那一刻：

- github 停在 `TestDrivenDeveloper REQ-2-2-3`（最后一行 13:21:44，8 秒后被杀）
- sheet 停在 `InterfaceDesigner REQ-3-1-1`（最后一行 13:13:47，8 分钟后被杀）

**3）rss 是平的还是无界增长？** **有界、锯齿**：github 102MB → 峰值 **1.31GB**
（09-29 07–08 点）→ 收尾 ~510MB；sheet 102MB → 788MB → 348MB。24h 这轮不是内存死的。

## 24 小时为什么没跑完

- 停顿：github **110 次 >2 分钟、合计 9.5h**（最长 74.5 / 69.7 分钟）；sheet 119 次 / 8.0h
  （最长 51.1 分钟）
- 单节点最长 **7.6 小时**（github REQ-2-2-2：InterfaceDesigner 320min + TestGenerator 306min
  + TDD 138min…）；sheet 的 REQ-2 从头到尾占了 19.8h 还没完
- 按 12 节点/24h 外推：github 65 节点要 ~130 小时；sheet 42 节点要 ~56 小时

停顿的三种签名（`github-stderr.log` / `sheet-stderr.log` 里逐条可查）：

1. **流中断 → 整段 agent 调用回退重跑**（`Unterminated string` → `falling back to ainvoke`，
   github 4 次）。74.5 分钟那个坑：12:05:40 之后直到 13:19:25 才有下一批工具结果 ——
   回退那次调用静默跑了 70+ 分钟
2. **`LLM_CALL_DEADLINE`（1800s）真的触发**（github 2 次：15:55:43、17:35:34；sheet 1 次：
   17:35:34），其中一次伴随 `langchain_openai.stream_chunk_timeout fired`
3. **压缩空转**：`摘要没带来净收益，不写入`（中段 78–2128 tokens，摘要块反而更大）在同一
   thread 上反复十几次（08:48–09:57 最密），每次都是一次完整摘要调用

## 顺带量到的两件事（都进了代码）

- **非零退出 = 平台不评测**：run `7d895aac11f4`（35.5M token、2.3h）退出码非零 →
  `start_agent: failed`、`run_tests: pending - Not reached`。所以"主动收尾"必须**退出 0**
  （本次改动：`soft_stop` 标记 + CLI 退出 0）
- **中段 <512 tokens 的压缩是纯浪费**：见上面第 3 类停顿（本次改动：`MIN_MIDDLE_TOKENS`）

## 归档内容与取证方式

| 文件 | 是什么 |
| --- | --- |
| `github-stderr.log` / `sheet-stderr.log` | 两题 stderr 全量（298 / 285 行：失败、回退、压缩空转、工具拒绝） |
| `github-tail.log` / `sheet-tail.log` | 被杀前最后 50 行 stdout |
| `run-343cf77b3bfc.json` / `run-702881a10479.json` | `/api/runs/{id}` 原始响应（steps / node_states / 计数） |

日志本体 36MB / 38MB 没有入库，用 `work/` 下的脚本按需重取（全部走 API，不碰页面）：

```
python work/arc_tail.py 343cf77b3bfc 702881a10479 --keep-mb 40   # 下载 + 保存
python work/unescape.py work/_tails/<id>.tail.txt work/_tails/<id>.txt
python work/analyze_24h.py  work/_tails/<id>.tail.txt             # 调用/停顿/rss/缓存曲线
python work/tool_mix.py     work/_tails/<id>.txt                  # 工具构成
python work/node_spans.py   work/_tails/<id>.txt                  # 每个节点跑了多久
python work/make_archive.py work/_tails/<id>.txt <本目录> <前缀>   # 生成上面两个切片
```

结论写进 `my_agent/SCORING_AND_OPTIMIZATION.md` §4.22（并更新了 §4.20/§4.21 里被推翻的部分）。
