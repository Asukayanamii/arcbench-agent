# 本地判题模拟：hackathon--github（未跑完）

用**自己的 key** 在本地跑真实比赛题目，目的是回答「能不能 pass」。本次**中途停止**，
没有拿到完整的通过率 —— 原因与两个真实发现见下。

## 被测版本

| | |
| --- | --- |
| 仓库 | `Asukayanamii/arcbench-agent` |
| 分支 | `main` |
| **commit** | **`7e1ba30aabd88914599f2a663c6b303a8cc48a1a`**（`7e1ba30`） |
| commit 主题 | `Merge pull request #8 from Asukayanamii/dev` |
| commit 时间 | 2026-09-26 11:11:41 +0800 |
| 工作区 | **干净** —— 被测代码就是这个提交，没有未提交改动混入 |
| 交付包 | `my_agent-arcbench-submission.zip`，SHA256 `ff64949adb57152444a73345a3519bf0afcce14a34f8e10fb717bfd2f372e900` |

## 运行参数

```
started : 2026-09-26 11:23:54 +0800   stopped: 12:17 (+08:00)，约 53 分钟
command : python -u main.py ..\_arcbench_tasks\hackathon--github -o ..\work\judge-github --port 3400
model   : deepseek/deepseek-v4.1-flash   （网关 https://api.commandcode.ai/provider/v1）
task    : hackathon--github   （100 个测试 / 47 个模块 / 130 个任务 / 65 个节点）
config  : my_agent/.env（不入库；LLM_CONTEXT_WINDOW 保持默认 64000，与平台历次 run 可比）
```

## 结果：没跑完，卡在第一个叶子节点

```
53 分钟   3 个 stage run / 24 次模型调用 / 508 次工具调用
节点状态   3 DESIGNED / 1 IMPLEMENTING / 61 UNSEEN
TDD 判定   0 个（没有任何 TDD batch passed/failed）
```

`REQ-1-1-1` 的 Unit 层 5/5 预算用尽后，agent 既拿不到判定也不停手，继续盲改实现
（那一轮 stage run 单独跑了 43 分钟）。**没有回答「能不能 pass」这个问题。**

## ★ 发现 1：守卫解锁修复得到实跑验证（正面）

精确统计（只数 `tool-result>` 里的真实拦截，不含模型在推理中引用拦截文案）：

```
真实拦截总数              13 次
第一次 run_tests 失败      11:51:09
该时刻之后的拦截           0 次
```

13 次拦截全部发生在第一次测试失败**之前**，之后归零 —— 正是修复预测的行为
（校验一失败，`_validation_failed` 置真、所有已写路径解锁）。

对照平台那次：同样条件下被拦 **133 次**并一路死锁到余额耗尽（REQ-1.1 98 次 / REQ-1.2 35 次）。
**这次是 0 次。**

## ★ 发现 2：超时返回串击穿了「Exit Code:」格式契约（真实缺陷）

`app_type_handler/web.py`：

```python
async def _execute_web_test_command(command, cwd, timeout: float = 60.0, ...):
    ...
    except asyncio.TimeoutError:
        return f"Command timed out after {timeout} seconds."     # ← 没有 Exit Code: 前缀
```

**实测证据**（本日志内）：

| | |
| --- | --- |
| `run_tests` 返回总数 | 34 |
| **其中不含 `Exit Code:` 的** | **5** |
| 超时串原文 | `Command timed out after 60.0 seconds.` |

后果有**两处**，都会被同一个格式缺口绕过：

1. **守卫**：`stage_policy._tool_result_failed()` 的判据是「含 `Exit Code:` 且不是 0」。
   超时串两条都不满足 → **判定为"没失败" → 解锁不触发**。也就是说：
   *一次超时不会解锁，只有真正带 Exit Code 的失败才会。*
2. **TDD 层**：`test_driven_developer._extract_exit_code()` 找不到 `Exit Code:` → 返回 `None`，
   这一层无从分类这次失败。

而且 `timeout=60.0` 是**硬编码**、不可配。

### 为什么这个缺口没被我们的检查挡住

`tools/check_stage_bridge.py::check_guard_reads_the_real_validation_format` 目前只做
**静态扫描**：断言 8 个生产/解析模块的**文件里**出现 `Exit Code:`。`web.py` 当然有 —— 在非超时
分支里。**它扫不出「某个分支绕过了这个前缀」。** 这是那条检查的已知盲区，需要补：
要么按失败路径逐一断言，要么加一条行为检查（喂真实超时串，断言守卫判为失败）。

## 日志导航

| 文件 | 看什么 |
| --- | --- |
| `debug.log` | 主日志（1.4 MB）。全过程的 `tool-call>` / `tool-result>` / `model-final>` / 守卫拦截 / TDD 预算 |
| `runner-events.jsonl` | SDK 事件流：需求状态、运行状态、刷新信号 |
| `processing_queue.json` | 130 个任务、65 个节点的最终状态 |
| `node_sessions/*.json` | 每个节点的接口、测试资产、失败摘要。**`REQ-1-1-1.json` 里能看 Unit 层为什么失败** |
| `traceability/*.json` | 需求 → 接口 → 测试的映射（`interfaces.json` 25 KB、`requirements.json` 294 KB） |

常用检索（在 `debug.log` 上）：

```powershell
Select-String .\debug.log -Pattern 'ARC stage discipline'          # 守卫拦截（应当只在早期出现）
Select-String .\debug.log -Pattern 'budget exhausted'              # 预算耗尽
Select-String .\debug.log -Pattern 'timed out after'               # 超时（发现 2）
Select-String .\debug.log -Pattern 'TDD batch'                     # TDD 判定（本次为 0）
```

## ⚠️ 本地模拟的保真度限制（读日志前务必知道）

1. **`run_tests` 超时 60 秒是硬编码的**，而本地是 Windows + 冷启动 node/vitest，
   比平台容器（Linux）慢得多 —— 本次 Unit 的 5 次预算里有一次就是被超时吃掉的。
   **平台上同样的测试很可能不会超时**，所以这里的失败模式有一部分是本地特有的。
2. 本次**没有跑官方测试**：比赛任务的测试是隐藏的（`GET /api/requirements/hackathon--github/tests?catalog=competition` → 404）。
   所以只能验证「编译能不能跑、跑得贵不贵」，**通过率无法本地自测**。
3. 本地跑**没有 token 计费数据** —— 平台会给 `token_count` / `token_cost_usd`，本地 run 什么都不给，
   网关上也没有用量端点。要控成本只能靠数模型调用次数估算。

## 复现方式

```powershell
# 1. 写 my_agent/.env（已被 .gitignore 与打包器 SKIP_NAMES 双重排除）
#    LLM_API_KEY / LLM_BASE_URL=https://api.commandcode.ai/provider/v1 / LLM_MODEL=deepseek/deepseek-v4.1-flash
# 2. 跑
cd my_agent
python -u main.py ..\_arcbench_tasks\hackathon--github -o ..\work\judge-github --port 3400
```

**坑**：不要加 `--clean`。`arc_main.py:263` 用 `shutil.rmtree` 删输出目录，而上次留下的
`.git/objects/*` 在 Windows 上是**只读**的 → `PermissionError: [WinError 5]`。
要先清只读属性再删，或者干脆换一个全新的输出目录。（平台跑 Linux 不受影响。）
