# 本地判题模拟：hackathon--github（第 3 轮，受限于依赖缺失）

**这一轮的价值是「修复前的对照基线」，不是结果** —— 它跑在 #21 / #24 修复**之前**，
全程被挂起主导。通过率仍**无法**本地验证（见文末保真度限制）。

## 被测版本

| | |
| --- | --- |
| 仓库 | `Asukayanamii/arcbench-agent` |
| 分支 | `main` |
| **commit** | **`8792028c60e03799ff94f7d948c20bde2e74e79d`**（`8792028`） |
| commit 主题 | `Merge pull request #23 from Asukayanamii/dev` |
| 工作区 | 干净 |
| 本地超时配置 | `ARC_TEST_COMMAND_TIMEOUT=180`（**本轮实测：放宽超时没有帮助**） |

## 运行参数

```
started  : 2026-09-26 14:23:50 +0800
stopped  : 2026-09-26 15:53 前后（撞上 90 分钟上限，0 个判定）
command  : python -u main.py ..\_arcbench_tasks\hackathon--github -o ..\work\judge-hackathon--github-8792028 --port 3411
```

## 结果

```
run_tests 调用   30
超时             37
预算耗尽          1（Unit）
session 早停      1
TDD 层判定        0        ← 一个都没产出
支付错误          0
```

## ★ 本轮最重要的结论：推翻了 #21 的归因

#21 把「Unit 5/5 全灭」归因为「60 秒超时太紧」。**把超时放宽到 180 秒后依然每次超时** ——
两次相邻调用相隔 **3 分 12 秒**（≈180 秒 + 开销）。所以主因不是慢，是**挂起**，
放宽超时只是让它每次挂得更久。已在 #26 里更正。

## 根因（见 issue #26，含更正）

**`run_npm_install` 在依赖装不全的情况下报 success** —— 它只看 `returncode == 0`：

```
14:23:56  NPM install success in ...\backend
14:24:06  NPM install success in ...\frontend
```

但归档后的工作区显示：

```
frontend  .bin 42 项：vitest ✓  vite ✓             ← 最终装上了
backend   .bin 18 项：只有 node-gyp / prebuild-install / rimraf / semver...
                      vitest ✗  vite ✗             ← 测试与构建工具从未装上
```

backend 那 18 项恰好是**原生模块（sqlite3 类）的构建工具链** —— npm 装到原生依赖那步
出了问题（Windows 上常见），退出码却是 0。

## ★ agent 自己诊断出了根因，却没有杠杆去修

`run_build` 在 15:00:58 的输出已经很明确：

```
=== Frontend Build Result ===
Exit Code: 1
STDOUT:
> frontend@0.0.0 build
> vite build
STDERR:
'vite' is not recognized as an internal or external command, operable pro...
```

agent 在 15:17:15 的 `model-final` 里自己得出了正确结论（原文）：

> The environment's toolchain is the blocker: `run_build` shows `vite` is not installed in
> `frontend/node_modules`, so dependency installation is incomplete.
> **Let me re-check whether the runner has warmed up since.**

它随后在 15:01:28 与 15:23:32 两次写下 `npm install` —— **但它的工具集里没有能真正执行
安装的杠杆**（TDD 阶段的工具只有 read/write/edit/grep/ls/glob/find + run_tests/run_build，
提示词还明写 "do not run arbitrary test commands with shell"），只能寄望于 runner 自己热起来。

**这条印证了一个设计矛盾**：提示词要求它「就地修 harness」（"If the runner setup is wrong,
edit build/config. Then rerun tests."），但它没有修 harness 的手段。它照做了（往
`package.json` 加了 `axios` / `jsdom`），然后撞上一堵推不开的墙。

## 日志导航

| 文件 | 看什么 |
| --- | --- |
| `debug.log` | 主日志（1.75 MB） |
| `node_sessions/REQ-1-1-1.json` | 该层的失败摘要 |
| `processing_queue.json` | `node_states` 在此文件内，字段名是 `state` |
| `traceability/` | 需求 → 接口 → 测试映射 |

```powershell
Select-String .\debug.log -Pattern 'timed out after'        # 本轮 37 次
Select-String .\debug.log -Pattern 'TDD batch'              # 本轮 0 个
Select-String .\debug.log -Pattern 'not recognized'         # 依赖缺失的直接证据
Select-String .\debug.log -Pattern 'NPM install success'    # 「成功」的安装
```

## ⚠️ 保真度限制

1. 本轮**跑在 #21 / #24 修复之前**，所有超时行为都是修复前状态。下一轮才是修复后的对照。
2. 比赛测试隐藏（`?catalog=competition` → 404），**通过率无法本地自测**。
3. 本地无 token 计费数据。
4. `ARC_TEST_COMMAND_TIMEOUT=180` 是本轮特意设的，用来检验「放宽超时是否有帮助」—— 结论是没有。
