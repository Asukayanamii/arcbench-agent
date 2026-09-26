# 本地判题模拟：hackathon--github（第 4 轮，**被主动中止**）

这一轮只跑了 **4 分钟就被人工杀掉**，不是失败，是**判定为无价值后主动止损**。

## 被测版本

| | |
| --- | --- |
| 仓库 | `Asukayanamii/arcbench-agent` |
| **commit** | **`56ba2127bc6c006f01a0378d269850ee2cf6bb36`**（`56ba212`） |
| commit 主题 | `Merge pull request #28 from Asukayanamii/dev` |
| 备注 | 含 #13 / #14 / #21 / #24 / #26 修复，**但不含 #29** |

## 运行参数

```
started  : 2026-09-26 15:59:54 +0800
stopped  : 2026-09-26 16:04     （4 分钟，人工中止）
command  : python -u main.py ..\_arcbench_tasks\hackathon--github -o ..\work\judge-hackathon--github-56ba212 --port 3411
超时配置  : ARC_TEST_COMMAND_TIMEOUT=180
```

## 为什么中止

跑到 4 分钟时已经确认：**这一轮的测试工具链永远不会可用**，因为：

```
16:00:00  NPM install did NOT complete in ...\backend  (attempt 1/2):
          6 declared package(s) missing from node_modules
16:00:10  NPM install did NOT complete in ...\frontend (attempt 1/2):
          17 declared package(s) missing from node_modules

frontend/node_modules/.bin/vitest.cmd  → 不存在
```

而这正是即将被 #29 修掉的根因（宿主 `NODE_ENV=production` → npm `omit=dev` → devDependencies 全不装）。既然已知它不可能产出任何关于被测代码的信息，继续跑满 90 分钟只是烧钱 —— 省下约 86 分钟。

## ★ 这一轮真正的价值：它是 #29 的证据来源

**#26 新增的「安装后校验依赖」在这里第一次开火**，把此前完全静默的事实打印了出来：

```
6 declared package(s) missing   ← backend，恰好等于 backend 的 devDependencies 数量
17 declared package(s) missing  ← frontend，恰好等于 frontend 的 devDependencies 数量
```

而 npm 自己的输出说明它**没有失败**：

```
added 182 packages, and audited 183 packages in 2s
34 packages are looking for funding
13 vulnerabilities (3 low, 1 moderate, 8 high, 1 critical)
```

⚠️ **注意这条日志是本次会话加上的**（#26 里为「失败时带上 npm 输出尾部」而写）。没有它，就看不到 `added 182 packages`，也就无从判断 npm 到底做没做事。

数字精确吻合（6 / 17 = devDeps 个数）+ npm 自认成功 → 直接指向 `NODE_ENV=production`：

```
NODE_ENV = production
npm config get omit = dev
```

## 这个发现推翻了什么

| 曾经的归因 | 实际情况 |
| --- | --- |
| 「60 秒超时太紧」（#21） | 放宽到 180 秒无效 —— 不是慢，是工具根本没装 |
| 「agent 加了依赖后没有重装」（#26 初版） | frontend 最终装上了运行时依赖；**devDependencies 从来没装过** |
| 「npx 找不到包会挂起」（#24） | 是真的（把 180 秒变成秒级失败），但只是**症状** |

**真正的根因是 #29**：安装继承了宿主的 `NODE_ENV=production`，而全部测试与构建工具都在 `devDependencies`。

## 目录内容

| 文件 | 说明 |
| --- | --- |
| `debug.log` | 4 分钟的完整日志，**含上面那两条 `did NOT complete` 与 npm 输出**，是 #29 的原始证据 |
| `processing_queue.json` / `node_sessions/` / `traceability/` | 只到 DESIGN 阶段 |

## ⚠️ 保真度限制

本轮不代表任何通过率 —— 它 4 分钟就被中止了，且测试从未运行。
