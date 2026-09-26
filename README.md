# arcbench-agent

ARC-Bench 比赛工作区：把 `my_agent/` 编译成提交包，交给平台。

> **这个仓库不是 my-agent 的仓库。**
> 它是一个独立的工作区仓库，装着比赛相关的全部内容（提交树、打包脚本、本地验证工具、任务素材）。
> 上游仓库 `Asukayanamii/my_agent` 与 `Asukayanamii/R-agent` 保持独立，本仓库不向其推送任何东西 ——
> 见下面「和上游仓库的关系」。

---

## 目录结构

| 路径 | 是什么 |
| --- | --- |
| `my_agent/` | **提交树**。真正会被打包上传的东西：上游引擎 `app/` + ARC 层（`core/` `agents/` `skills/` `app_type_handler/` `arcbench_agent_runtime/` `arc-template/`）+ 入口 `main.py` / `arc_main.py`。**改代码基本都在这里** |
| `my_agent/HANDOVER.md` | 包内交接文档：分层结构、对官方模板改了什么、想改 X 该动哪个文件、必须维持的平台契约 |
| `my_agent/README.md` | 提交包本身的设计说明 |
| `my_agent/SCORING_AND_OPTIMIZATION.md` | 平台评分公式与优化建议 |
| `my_agent/tools/check_stage_bridge.py` | **打包前必跑**的自检（技能根、分页语义、路径归一、阶段上限公布、接口契约去重、守卫解锁） |
| `_package.py` | 打包脚本，写 Unix 条目属性并自检格式 |
| `_mock_llm.py` | OpenAI 兼容的 mock（说 SSE），让编译路径不花钱就能跑通 |
| `_test_bridge.py` | 阶段桥接端到端测试：真实 LangGraph 循环打 mock 模型，覆盖沙箱、工具面、阶段纪律、SDK 落盘 |
| `_test_stall_recovery.py` | 卡流回归：覆盖三层修复（重试判定、看门狗配置、编译循环不被打断），外加两个反例 |
| `_test_streaming.py` | 验证阶段 agent 是流式的、且结果不变 |
| `_test_proctree.py` | 超时命令必须收掉整棵进程树（防孤儿 `npx vitest`） |
| `_test_spawn_proof.py` | 上面那条的哨兵，证明真的产生了孙进程（否则可能空过） |
| `_fetch_tasks.py` | 从 arc-bench 拉官方任务材料（requirements + Playwright 套件） |
| `run_official_tests.py` | 本地跑官方 Playwright 套件，复现平台评分 |
| `_diag_payload.py` | 诊断入口：不调模型，只报告平台解压出了什么。配 `_package.py --diagnostic` 用 |
| `_arcbench_tasks/` | 拉下来的任务材料（`smoke--*`、`hackathon--*`、`arc-bench-lite--*`） |
| `_arcbench_harness/` | 跑官方套件用的 Playwright 环境（`node_modules/` 不入库） |
| `_arc_req/` | 本地编译用的 requirements 源 |
| `_template/` | 官方模板的解包副本，用来对照 diff |
| `my_agent-arcbench-submission.zip` | **交付产物**。本仓库唯一入库的 zip |
| `new log.txt` | 平台 run 的日志，仅本地参考（不入库） |

---

## 交付流程

```bash
# 1. 改完先跑自检 —— 不花钱、不联网
cd my_agent && python tools/check_stage_bridge.py

# 2. 打包（两个打包器产出逐字节一致，用哪个都行）
python _package.py                                   # 仓库根，默认源=my_agent/
python my_agent/tools/build_submission_zip.py        # 包内那份，输出同一个文件

# 3. 提交
git add my_agent-arcbench-submission.zip && git commit -m "..."
```

**不要用 `Compress-Archive`、也不要右键「发送到 → 压缩文件夹」。**
Windows 自带压缩写的是 `create_system=0` / `external_attr=0`，Unix 模式下 mode `0` 连 `S_IFREG`
都没有，平台的解压器认不出条目类型，目录结构会落不全 —— 上一版就是这样挂在
`ModuleNotFoundError: No module named 'app_type_handler'` 上的。

两个打包器都会在结尾自检格式（`create_system=3`、`external_attr=0x81B40000`、
`main.py` 在根、无 `.pyc` 泄漏），不过会返回非零退出码。

### 交付产物为什么要入库

`my_agent-arcbench-submission.zip` 是唯一被 `.gitignore` 开白名单的 zip
（`*.zip` 仍排除其余 zip）。理由：它才是真正交给平台的那份东西，出问题时得能取回
「当时提交的到底是哪一个」。

当前这份：

```
SHA256 : d1f3e5da43e7475333c09c2c73e734218a8af843faded431e6d92f7f84ab8e95
size   : 587931 bytes
entries: 244
```

每次提交交付包时，把当时的 SHA256 写进提交信息。

---

## 改了什么就跑什么

| 你动了 | 跑这个 |
| --- | --- |
| `agents/runtime/`、`skills/`、`tools/` | `cd my_agent && python tools/check_stage_bridge.py` |
| `app/agent/retry.py`、`app/agent/models.py`、`core/workflow.py` | `python _test_stall_recovery.py` |
| `agents/runtime/`（较大改动） | `python _test_bridge.py` |
| `agents/runtime/arc_agent.py`、`runners.py` | `python _test_streaming.py` |
| `core/processes.py`、`app_type_handler/web.py` | `python _test_proctree.py` |
| 打包相关 | 打包后看脚本的自检输出 |
| 任何改动 | 打包前跑一次 `check_stage_bridge.py` |

这些测试**不花钱、不联网**（mock 模型打本地）。真正要花 key 的是
`python my_agent/main.py <requirements 目录> -o <输出目录>`。

---

## 和上游仓库的关系

本仓库的 `my_agent/` 最初是上游 `Asukayanamii/my_agent` 的克隆。但 ARC 层文件就长在那个克隆的
工作副本里，而 **git 无法跟踪嵌套仓库内的文件**（`fatal: Pathspec ... is in submodule`，加 `-f` 也不行）。
所以：

- 那个克隆的 `.git` 被**原样移动**到 `.my_agent_upstream_git/`（保留，未删除），
  这样本仓库才能把 `my_agent/` 当普通目录跟踪
- 想恢复成独立克隆：把 `.my_agent_upstream_git` 移回 `my_agent/.git`

```powershell
Move-Item .my_agent_upstream_git my_agent\.git
```

- 本仓库配了 `upstream` remote 指向上游仓库，**但推送 URL 被禁用**
  （`DISABLED_DO_NOT_PUSH`），只用于 fetch / diff：

```bash
git fetch upstream
git diff upstream/main -- my_agent/app/     # 看我们对上游引擎改了什么
```

---

## 维护约定

- **不要改 `arcbench_agent_runtime/`**：那是平台 SDK，规则明确要求只调高层方法、
  不手写事件负载，否则追溯数据与刷新信号会对不上。
- **不要改 `agents/runtime/runners.py`、`contracts.py`**：只要满足它们的接口就不用动。
- **改了执行规则，就要把规则公布进提示词**：`agents/runtime/stage_policy.py` 里的硬上限
  由 `render_stage_limits()` 从同一组常量渲染进系统提示词，`check_stage_bridge.py` 会验证
  两边不分叉。模型撞上限的代价是一整个往返，比多写几行提示词贵得多。
- **阶段纪律的失败判定必须读工具正文**：`run_tests` / `run_build` 失败时是**返回**
  `Exit Code: 1` 的正文、从不抛异常。只看异常会让解锁永不触发，一个文件在本阶段写过一次
  就再也改不回来 —— 平台实测因此死锁 133 次工具调用，四个单测红点全来自这条路径。
  回归检查在 `check_stage_bridge.py` 的 `check_guard_unlocks_after_a_failing_validation`。

更多踩过的坑见 `my_agent/HANDOVER.md` 第 7 节。

---

## 协作

仓库为私有。协作者：`Asukayanamii`（owner）、`chenglei6-arch`（write）。

### 分支约定

`main` 只接受合并，不直接往上推。改动一律先落 `dev`，再开 PR 合进 `main`：

```bash
git checkout dev && git pull
# ... 改代码、跑验证、打包 ...
git add -A
git commit -m "..."            # 提交信息只描述改动本身，不加任何工具/AI 署名
git push origin dev
gh pr create --base main --head dev --title "..." --body "..."
gh pr merge <PR 号> --merge
git checkout main && git pull  # 回到 main 跟上合并结果
```

提交信息里**不要**出现 `Co-authored-by:`、`Signed-off-by:`、`Generated with` 这类
工具署名或生成声明，只写人类作者的改动内容。

### 改动落在哪个分支

| 内容 | 分支 |
| --- | --- |
| 交付包 `my_agent-arcbench-submission.zip` | 跟源码同一个 PR 一起提，或单独 `build:` 提交 |
| `my_agent/` 提交树、ARC 层 | `dev` → PR → `main` |
| 外层工具链、本地测试、README | 同上 |

日志（`new log.txt`、`new ticket log.txt`）不入库，只在本地做诊断用。
