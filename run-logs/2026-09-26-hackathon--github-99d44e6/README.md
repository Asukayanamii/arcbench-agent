# 本地判题模拟：hackathon--github（第 2 轮，受限运行）

用**自己的 key** 在本地跑真实比赛题目。本轮跑满 **3 个 TDD 层判定**即由看门狗收停，
**65 分钟**（未到 90 分钟上限）。通过率仍然**无法**本地验证（见文末保真度限制）。

## 被测版本

| | |
| --- | --- |
| 仓库 | `Asukayanamii/arcbench-agent` |
| 分支 | `main` |
| **commit** | **`99d44e6237cccc88724fa54fc09d64a01ae9a318`**（`99d44e6`） |
| commit 主题 | `Merge pull request #20 from Asukayanamii/dev` |
| 工作区 | **干净** —— 被测代码就是这个提交 |
| 交付包 | `my_agent-arcbench-submission.zip`，SHA256 `36f3540ec653b10457e8ebb9ad680067d6938c13e42c0f42b89895c9c909`（见包内实际值） |

## 运行参数

```
started  : 2026-09-26 12:49:59 +0800
stopped  : 2026-09-26 13:55:19 +0800   （65.3 分钟，因凑满 3 个 TDD 判定被收停）
command  : python -u main.py ..\_arcbench_tasks\hackathon--github -o ..\work\judge-hackathon--github-99d44e6 --port 3411
model    : deepseek/deepseek-v4.1-flash
task     : hackathon--github（130 个任务 / 65 个节点）
config   : my_agent/.env（不入库）；LLM_CONTEXT_WINDOW 走代码默认值 1M
```

## ★ 三个修复全部实跑验证通过

| 修复 | 上一轮（`7e1ba30`） | 本轮（`99d44e6`） |
| --- | --- | --- |
| #13 预算耗尽终止 session | 0 次 | **3 次** `agent stream stopped early`，精确落在每次预算耗尽时刻 |
| 「产不出层判定」 | **0 个判定** | **3 个判定**（Unit / Integration / E2E） |
| #14 超时分类为 124 | 超时串无退出码 | **`Exit Code: 124` 出现 52 次** |
| 层推进 | 卡死在 Unit 后再无动静 | Unit → Integration → E2E **三层全走完** |
| 守卫拦截 | 133 次（平台那次） | **6 次**，无死锁 |

节点最终状态：**3 DESIGNED / 1 FAILED(`REQ-1-1-1`) / 61 UNSEEN**，130 个任务。

## ★ 本轮真正的 bug：超时被误报成「测试失败」

`node_sessions/REQ-1-1-1.json` 的 `recent_failure_summary` 是决定性证据：

```
Unit:  Requested: backend/tests/auth/auth_service_registration.test.js
       Exit Code: 1                                     ← 外层信封报告的退出码
       === Backend Vitest Batch ===
       Exit Code: 124                                   ← 内层批次真实的退出码
       STDERR: Command timed out after 60.0 seconds.     ← 真正的原因

Integration: frontend/tests/auth/registration_page.test.tsx
       Exit Code: 1
       === Frontend Vitest Batch ===
       Exit Code: 124
       STDERR: Command timed out after 60.0 seconds.
```

**Unit 5/5 和 Integration 5/5 不是测试失败，是测试从来没跑起来。**
`parse_test_results` 读第一个 `Exit Code:` 行 → 拿到外层的 `1` → 整层被判为「失败」。

代价可量化：

```
15 个预算槽位的真实去向
Unit         exit=1  ×5    ← 实为超时，被掩盖
Integration  exit=1  ×5    ← 实为超时，被掩盖
E2E          exit=124 ×2, exit=1 ×2, exit=0 ×1   ← E2E 第 5 次真的通过
超时事件 32 次 × 60s = 32 分钟 ≈ 本轮 65 分钟墙钟的一半
```

agent 于是把整层预算花在「修」一个可能本来就正确的实现上，而它拿到的信号
（`Exit Code: 1`）无法区分「测试跑了且失败」和「测试根本没跑」。

## 日志导航

| 文件 | 看什么 |
| --- | --- |
| `debug.log` | 主日志（1.7 MB / 12,196 行） |
| `processing_queue.json` | 130 个任务；`node_states` 在**这个**文件里，键是 `state` 不是 `status` |
| `node_sessions/REQ-1-1-1.json` | **失败的根因在这里**（`recent_failure_summary`） |
| `traceability/requirements.json` | 需求树（300 KB） |
| `harvest.py` | 本轮的精确统计脚本 —— 可复跑 |

常用检索：

```powershell
Select-String .\debug.log -Pattern 'TDD batch'                 # 层判定（本轮 3 个）
Select-String .\debug.log -Pattern 'stopped early'             # 早停（本轮 3 次）
Select-String .\debug.log -Pattern 'timed out after'           # 超时（本轮 36 行 / 32 个时刻）
Select-String .\debug.log -Pattern 'Exit Code: 124'            # 超时被正确分类
Select-String .\debug.log -Pattern 'ARC stage discipline'      # 守卫拦截（本轮仅 6 次）
```

## ⚠️ 本地模拟的保真度限制（读日志前务必知道）

1. **`run_tests` 超时 60 秒是硬编码的**，本地 Windows + 冷启动 node/vitest 明显慢于平台
   Linux 容器，所以本轮的 Unit/Integration 全军覆没**很可能带有本地放大效应**。
   但「超时被外层 `Exit Code: 1` 掩盖」这个缺陷本身与平台无关。
2. **比赛测试是隐藏的**（`GET /api/requirements/hackathon--github/tests?catalog=competition` → 404），
   所以**通过率无法本地自测**。本轮只能回答「管线能不能跑通、贵不贵、在哪断」。
3. 本地跑**没有 token 计费数据**，只能靠数模型调用次数估算。
4. `traceability/call_edges.json`、`node_contracts.json`、`scenarios.json` 均为 4 字节（空）。
   只处理了 1 个叶子节点，未确认是否属正常；留待下一轮观察。

## 复现方式

```powershell
# 1. my_agent/.env 需要有 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL（已被 .gitignore 与打包器双重排除）
# 2. 受限运行（3 个判定或 90 分钟即停），并自动归档
python work\run_bounded.py hackathon--github 3 90
```

注意 `run_bounded.py` 的归档路径按 `.arc/` 取文件；若手工归档，`processing_queue.json`
等都在输出目录的 **`.arc/`** 下，不在根目录。
