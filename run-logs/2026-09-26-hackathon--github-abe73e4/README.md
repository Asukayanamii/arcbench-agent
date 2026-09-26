# 本地判题模拟：hackathon--github（第 7 轮）★ **两个叶子节点，5 条判定全过**

**第一次跑到队友的 subagent 与 todo 工具**，并第一次在一轮里处理**两个**叶子节点。

## 被测版本

| | |
| --- | --- |
| 仓库 | `Asukayanamii/arcbench-agent` |
| 分支 | `dev`（**注意：不是 main**） |
| **commit** | **`abe73e4`** —— 含队友的 subagent + todo（`805e8fa`）与本地全部修复 |
| 说明 | 启动时停在 `dev` 上而没切到 `main`，所以标签是 `abe73e4` 而非 `3d4a6d6`。两者树内容等价 |
| 本地超时配置 | `ARC_TEST_COMMAND_TIMEOUT=180` |

## 运行参数

```
started  : 2026-09-26 16:53:33 +0800
stopped  : 2026-09-26 17:43:38 +0800   （50 分钟，进程自行结束）
command  : python -u main.py ..\_arcbench_tasks\hackathon--github -o ..\work\judge-hackathon--github-abe73e4 --port 3411
```

## 结果：两个节点、5 条判定、全部 passed

```
REQ-1-1-1
  17:20:44  TDD batch `Integration` passed after 5/5 run_tests call(s).
  17:20:44  TDD batch `E2E`         passed after 2/5 run_tests call(s).
  （该节点本轮没有 Unit 层 —— 测试生成每轮不同，这轮没给它生成 Unit 测试，
    所以没有 Unit 的判定与 usage 行，不是缺陷）

REQ-1-1-2
  17:43:38  TDD batch `Unit`        passed after 5/5 run_tests call(s).
  17:43:38  TDD batch `Integration` passed after 3/5 run_tests call(s).
  17:43:38  TDD batch `E2E`         passed after 1/5 run_tests call(s).
```

**超时 0 次、支付错误 0 次。**

## ★ 队友的工具在实跑中被调用

```
add_todo      : 被调用（回执正常）
complete_todo : 被调用
subagent      : 被调用
list_todo     : 0 次   <- 见 issue #34
update_todo   : 0 次   <- 见 issue #34
```

工具本身工作正常。但 `add_todo` 的返回值里带着**完整的 `todo_list`**，因此 agent 永远不需要调 `list_todo` —— 与当初评审定的「agent 通过 `list_todo` 自己拉取」不符，且每加一条待办都要重发整个列表。已开 issue #34 并指派给队友。

## ★ 另发现：本轮工具链的一个自身缺陷（非产品问题）

`work/run_bounded.py` 的看门狗按 stdin/stdout 检测 `TDD batch` 行来决定何时收停 —— **但那些行写在 `.arc/debug.log` 里，不在 stdout**。所以判定计数**从来没生效过**，每轮都要等进程自行结束或撞 90 分钟上限。本轮因此跑了 50 分钟（5 条判定早就在 17:20 就够 3 条了）。

已修：改为轮询 `.arc/debug.log`。

## 与前几轮对照

| | 第 4 轮 `8792028`（修复前） | 第 6 轮 `3012358` | 第 7 轮 `abe73e4` |
| --- | --- | --- | --- |
| 叶子节点数 | 0（未进入） | 1 | **2** |
| 超时 | 37 | 0 | **0** |
| TDD 判定 | 0 | 3 | **5** |
| 判定结果 | — | 全 passed | **全 passed** |

## 日志导航

| 文件 | 看什么 |
| --- | --- |
| `debug.log` | 主日志（2.8 MB，本会话最大） |
| `node_sessions/` | REQ-1-1-1 与 REQ-1-1-2 各自的接口、测试资产与失败摘要 |
| `traceability/` | 需求 → 接口 → 测试映射 |

```powershell
Select-String .\debug.log -Pattern 'TDD batch'                    # 本轮 5 条，全 passed
Select-String .\debug.log -Pattern 'tool-call> (add_todo|subagent)' # 队友的工具
Select-String .\debug.log -Pattern 'budget exhausted'             # 2 次（末次尝试均通过）
```

## ⚠️ 保真度限制

1. **仍只跑了 2 / 65 个节点**。不能由「这两个节点全过」推断整体通过率。
2. **比赛测试仍然隐藏**，跑的是**生成的测试**；而生成测试与实现可被 agent 同时修改，所以「全过」不等于官方测试会过。
3. 本轮 Unit 只出现在 REQ-1-1-2 上，样本量极小。
4. 本地无 token 计费数据。
