# 本地判题模拟：hackathon--github（第 6 轮）★ **首次三层全过**

**这是六轮以来第一个测试工具链真正可用、并跑出真实测试结果的 run。** 三层判定全部 `passed`。

## 被测版本

| | |
| --- | --- |
| 仓库 | `Asukayanamii/arcbench-agent` |
| 分支 | `main` |
| **commit** | **`301235891490911b8e2f2d6a76018b0bcd65418f`**（`3012358`） |
| commit 主题 | `Merge pull request #30 from Asukayanamii/dev` |
| 含修复 | #13 / #14 / #21 / #24 / #26 / **#29（关键）** |
| 工作区 | 干净 |
| 本地超时配置 | `ARC_TEST_COMMAND_TIMEOUT=180` |

## 运行参数

```
started  : 2026-09-26 16:04:54 +0800
stopped  : 2026-09-26 16:35:57 +0800   （31 分钟，凑满 3 个层判定被看门狗收停）
command  : python -u main.py ..\_arcbench_tasks\hackathon--github -o ..\work\judge-hackathon--github-3012358 --port 3411
```

## 结果：三层全部通过

```
16:23:22  Unit        failed  exit=1  attempt 1/5
16:23:36  Unit        PASSED  exit=0  attempt 2/5
16:23:44  Unit        PASSED  exit=0  attempt 3/5
16:23:53  Unit        PASSED  exit=0  attempt 4/5
16:24:02  Unit        PASSED  exit=0  attempt 5/5

16:26:18  Integration failed  exit=1  attempt 1/5
16:27:42  Integration PASSED  exit=0  attempt 2/5
      ...  直到 attempt 5/5 全部 PASSED

16:35:00  E2E         failed  exit=1  attempt 1/5
16:35:47  E2E         PASSED  exit=0  attempt 2/5

16:35:57  TDD batch `Unit`        passed after 5/5 run_tests call(s).
16:35:57  TDD batch `Integration` passed after 5/5 run_tests call(s).
16:35:57  TDD batch `E2E`         passed after 2/5 run_tests call(s).
```

`REQ-1-1-1` 是第一个叶子节点，三层全过。

## ★ 与修复前对照（同一个任务、同一台机器）

| | 第 4 轮（`8792028`，修复前） | 第 6 轮（`3012358`） |
| --- | --- | --- |
| 超时 | **37** | **0** |
| `run_tests` 调用 | 30 | 46 |
| 真实 `Exit Code: 0/1` | **0 个（清一色 124）** | **12 个** |
| TDD 层判定 | **0 个** | **3 个，全部 passed** |
| 有效产出 | 无 | 三层通过 |

## ★ 根因链（前四条都是症状，第五条才是根因）

1. `NODE_ENV=production` 在宿主环境 → npm 推断 `omit=dev`
2. **`devDependencies` 从来没被安装** —— 而 vitest / vite / playwright 全在里面
3. 于是构建与测试在任何环境都跑不起来，只有 `'vite' is not recognized`
4. agent 自己诊断出了这一点（见第 4 轮 README），却没有安装的手段
5. 下游表现：`npx` 挂起 180 秒、37 次超时、0 个层判定

**修复 #29 之后，这四条症状同时消失。**

## ★ 本轮发现的新问题：通过之后仍在空烧预算

`Unit` 在 **attempt 2 就已经通过**，却一路跑到 **5/5**：

```
16:23:36  PASSED exit=0  attempt 2/5   <- 已经过了
16:23:44  PASSED exit=0  attempt 3/5   <- 冗余
16:23:53  PASSED exit=0  attempt 4/5   <- 冗余
16:24:02  PASSED exit=0  attempt 5/5   <- 冗余
```

`Integration` 同样：attempt 2 通过后仍用满 5 次。
**12 次有真实 exit code 的调用里，8 次是「已经通过之后的重跑」。**

代价直接体现在评分上 —— 排名规则里成本是次级比较项（score → pass rate → **lower cost** → 时间）。已立 issue。

## 日志导航

| 文件 | 看什么 |
| --- | --- |
| `debug.log` | 主日志（1.39 MB） |
| `node_sessions/REQ-1-1-1.json` | 该节点的接口、测试资产与最终状态 |
| `processing_queue.json` | `node_states` 在此文件内，字段名是 `state` |

```powershell
Select-String .\debug.log -Pattern 'with Exit Code'      # 本轮 12 条真实结果
Select-String .\debug.log -Pattern 'TDD batch'           # 本轮 3 条，全部 passed
Select-String .\debug.log -Pattern 'timed out after'     # 本轮 0 次
```

## ⚠️ 保真度限制

1. **只跑了 1 个叶子节点**（`REQ-1-1-1`），任务共 65 个节点。不能由「这个节点三层全过」推断整体通过率。
2. **比赛测试仍然隐藏**（`?catalog=competition` → 404），跑的是**生成的测试**，不是官方测试。**通过率依旧无法本地自测。**
3. 生成测试能与实现同时被 agent 修改，所以「全过」不等于「官方测试会过」。
4. 本地无 token 计费数据。
