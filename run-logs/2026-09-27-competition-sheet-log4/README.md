# 比赛实跑归档：hackathon--sheet（第四份导出，`bs log4.txt`）

由用户从比赛平台导出（4.24 MB / 22,087 行）。**这是当前最新一次的提交。**

## 这一份最重要的作用：确认 #46 生效，并把一个被它掩盖的缺陷暴露出来

```
11:14:56  Unit failed   ┐
11:19:08  Unit failed   │
11:22:47  Unit failed   ├─ 5 次全部真的跑了、真的失败
11:24:56  Unit failed   │
11:30:55  Unit failed   ┘
11:37:36  Integration failed
11:41:00  Integration failed
11:43:44  Integration failed
11:45:06  Integration PASSED   ← 4/5
11:47:40  E2E failed
11:49:00  E2E PASSED           ← 2/5
```

| | 第一份（失败） | log3 | **本份 log4** |
| --- | --- | --- | --- |
| 守卫拦截 | 1157 ✗ | 12 | **7** ✓ |
| `ended without calling run_tests` | — | 1 次 ✗ | **0 次** ✓ |
| E2E 层 | — | **0/5 假红** ✗ | **2/5 真实通过** ✓ |
| 超时 / 安装失败 / 支付 | — | 0 | **全 0** |
| 进度 | 卡在 REQ-1-2-2 | REQ-1-1-1 | **REQ-1-2-1** ✓ |

**#46 确认生效**（空 session 零命中、E2E 真的跑起来了）。

## 但 `REQ-1-1-1` 仍红 —— 这一次是真实失败，根因精确到一行（issue #49）

`BEGIN RAW TEST OUTPUT` 段：

```
RUN  v4.1.11 /workspace/template/frontend
 ? tests/workbookListView.test.tsx (0 test)          <- 0 个测试！

 Test Files  1 failed (1)
      Tests  no tests
 Failed Suites 1
 FAIL  tests/workbookListView.test.tsx [ tests/workbookListView.test.tsx ]
Error: Failed to resolve import "../../src/components/workbook/Workboo…
  Plugin: vite:import-analysis
  File: /workspace/template/frontend/tests/workbookListView.test.tsx:4
```

**生成的测试里相对导入多了一层 `../`：**

| | 路径 |
| --- | --- |
| 测试文件 | `frontend/tests/workbookListView.test.tsx` |
| 组件（InterfaceDesigner 写的） | `frontend/src/components/workbook/WorkbookListView.tsx` |
| 测试里的导入 | `../../src/components/...` ✗ |
| **应为** | **`../src/components/...`** ✓ |

从 `frontend/tests/` 向上两级是 `template/`，那里没有 `src/`。→ 套件**加载失败**、**0 个测试被执行** → `Exit Code: 1`。

## 真正的问题是它「长得像断言失败」

回执里显示的是 `Test Files 1 failed` + `FAIL <file>` —— 与断言失败**完全一样**。于是 agent 去修**实现**，5 次预算全败，**一次都没碰那行 import**。而提示词其实要求它就地修生成测试的缺陷 —— 信号不区分，它就无从知道该改哪边。

与 #14（超时被当成没失败）、#21（内层 124 被外层 1 掩盖）是同一类病。

→ 已修（#49）。

## 检索命令

```powershell
Select-String .\competition-log.txt -Pattern 'TDD batch'
Select-String .\competition-log.txt -Pattern 'Failed to resolve import'
Select-String .\competition-log.txt -Pattern 'BEGIN RAW TEST OUTPUT'
```

## ⚠️ 归因限制

1. 导出的是**页面文本**（含其它页面的 UI 文案，开头有一段乱码的中文侧栏），不是结构化日志。
2. **平台跑的是哪个 commit 未在日志中体现** —— 无法从日志核实。
3. 单次实跑，不能据此判断通过率趋势。赛场基准：榜首通过率 51.5%，多数队伍 20–50%。
