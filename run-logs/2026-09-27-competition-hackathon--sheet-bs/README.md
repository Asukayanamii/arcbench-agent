# 比赛实跑归档：hackathon--sheet（**失败**）★ 守卫硬死锁

由用户从比赛平台导出（`bs log.txt`，5.25 MB / 25,040 行）。这是**比赛任务**，不是本地模拟。

## 被测版本

| | |
| --- | --- |
| 平台提交 id | `bec1591afadb` |
| 任务 | **Core Requirements for an Online Spreadsheet Data Workspace**（= `hackathon--sheet`） |
| 平台时长 | **201 分 47 秒** |
| 结果 | **失败** —— 卡在 `REQ-1-2-2` 烧完剩余时间 |

⚠️ **平台跑的是哪个 commit 未在日志中体现** —— 导出的是页面文本，不含 git 信息。因此本归档只能说明
「当时的代码有这个问题」，不能精确对应到某个提交。

## 结果：一个测试文件被拦 1142 次

```
ARC stage discipline 总命中 : 1157
  TestGenerator        1149      ← 98.7%
  InterfaceDesigner       5
  TestDrivenDeveloper     3

按原因:
  Repeated write blocked: /frontend/tests/workbook-rename.integration.test…  ← 1142 次
  Repeated read blocked:  /frontend/src/pages/WorkbookEditorPage.tsx             7
  Repeated write blocked: /frontend/src/pages/WorkbookEditorPage.tsx             3
  InterfaceDesigner may materialize at most 8 small skeleton files               1
  TestGenerator may write only test files…                                       1
```

## 根因（issue #44）

`_path_unlocked` 的两个解锁来源，在 `test_generation` 阶段**都不可能成立**：

1. `_validation_failed` 只由**验证工具失败**置位，而 `check()` 明确禁止该阶段调验证工具
2. `_guard_tool.runner` 在拦截时**直接 return、从不调用 `record`** → 路径进不了 `_failed_paths`

→ **TestGenerator 写过的路径永久锁死，第一次写就是最终稿。**

而拦截消息承诺的是「Wait for a file-operation or system-validation error before changing it
again」—— agent 照做了，被拦**就是**一次文件操作错误，守卫却没记账。**承诺与实现相反。**

## ★ 与同期另一道题的对照

| | 本归档（sheet，失败） | `…hackathon--github-bs`（github，仍在跑） |
| --- | --- | --- |
| 拦截总数 | **1157** | **55** |
| 分布 | **1142 集中在同一个文件** | 分散在三个 agent（20/18/17） |
| TDD 判定 | 0 | **11，全部 passed** |
| 预算占用 | — | 每层只花 **1–2 / 5** |
| 进度 | 卡在 `REQ-1-2-2` | 推进到 `REQ-1-3` |

**差别就是死锁的判据**：另一道题的主要拦截发生在 `TestDrivenDeveloper`，而**它允许调验证工具** ——
`run_tests` 一失败就置 `_validation_failed`、整阶段解锁，死锁自然化解。日志里能看到 agent 自己
意识到了这点：`model-final> The run_tests validation error has b…`。

**所以这个死锁是「没有验证工具的阶段」特有的**，`test_generation` 是唯一这样的阶段。

## 检索命令

```powershell
Select-String .\competition-log.txt -Pattern 'ARC stage discipline'   # 本轮 1157 次
Select-String .\competition-log.txt -Pattern 'Repeated write blocked' # 其中 1145 次
Select-String .\competition-log.txt -Pattern 'TDD batch'              # 本轮 0 条
```

## ⚠️ 归因限制

1. 导出的是**页面文本**（含 UI 文案），不是结构化日志；行号无意义，需靠模式检索。
2. 平台 commit 未知（见上）。
3. 这是**单次**实跑，不能据此判断其他任务或其他时段的表现 —— 同期另一道题的对照见上表。
