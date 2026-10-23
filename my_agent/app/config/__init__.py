"""
环境配置。换模型服务只改 .env，不动代码。

代码直接放在包的 `__init__.py` 里，和 `app/exceptions/` 一致：对外只有一个
`from app.config import X` 的入口，不再套一层 `config/config.py` 的同名模块。

**`PROJECT_ROOT` 是按目录层级数出来的，挪文件必须同步改。**
这个文件在 `app/config/` 下，所以是往上第三层。算错不会报错，只会让相对路径的
配置静默写到别处去——曾经因此凭空多出一个 `app/data/checkpoints.db`，
用户那边表现为"会话丢了"。下面的断言让这类错误在启动时立刻暴露。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if not (PROJECT_ROOT / "requirements.txt").is_file():
    raise RuntimeError(f"PROJECT_ROOT 算错了，指向 {PROJECT_ROOT}")

CONFIG_DIR = ".my_agent"
"""应用在工作区/用户目录下用的隐藏目录名。

沙箱授权（`sandbox.json`）、技能库（`skills/`）、项目约定（`AGENTS.md`）都住在这个名字下，
所以它只能有一处定义——写两处迟早会分叉，而分叉的表现是"某个功能静默看不到文件"。
"""

# 显式指定 .env 位置，不用 load_dotenv() 的上溯查找：
# 上溯是按调用方文件位置找的，行为和路径解析不一致，容易踩坑。
load_dotenv(PROJECT_ROOT / ".env")

LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat").strip()

# 显式启用桩实现（复读 + 演练工具卡片 / 人工确认），用于没有模型时调前端交互。
# 它**不是**兜底：没配 key 时对话接口直接报错，见 app/agent/langgraph_runner.py 的 _UnavailableModel。
STUB_ENABLED = os.getenv("AGENT_STUB", "").strip() == "1"

# 日志级别。约定见 README「日志」：ERROR 是需要人处理的失败，WARNING 是降级与拒绝，
# INFO 是主线里程碑（每轮对话、工具调用、迁移），DEBUG 是每次写库这类细节。
# 排查问题时置 DEBUG 重启即可；级别名写错时退回 INFO。
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()

CONFIG_WARNINGS: list[str] = []
"""取值非法的配置项记录（谁退回了默认值、原值是什么），启动时由组合根打出来。

静默退回默认是"改坏了不报错"的那类：窗口填成 "64k" 会被当成默认值用，
行为跟预期不同却没有任何提示。
"""


def _int_or(raw: str, default: int) -> int:
    """数值配置：写错、写非正数一律退回默认，并把这件事记下来。"""
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        CONFIG_WARNINGS.append(f"配置值不是整数，已退回默认：{raw!r} → {default}")
        return default
    if value <= 0:
        CONFIG_WARNINGS.append(f"配置值必须为正，已退回默认：{value} → {default}")
        return default
    return value


def _ratio_or(raw: str, default: float) -> float:
    """比例配置：只接受 (0, 1]；其余退回默认。"""
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        CONFIG_WARNINGS.append(f"配置值不是小数，已退回默认：{raw!r} → {default}")
        return default
    if not 0 < value <= 1:
        CONFIG_WARNINGS.append(f"配置值必须落在 (0, 1]，已退回默认：{value} → {default}")
        return default
    return value


# ---- 流式调用的停顿看门狗 ----
# langchain-openai 默认 120s：连续这么久收不到新分片就抛 StreamChunkTimeoutError。
# 实测网关会静默停顿超过 120s，而一次停顿会让整个阶段（乃至整轮编译）失败——它太紧。
# 放宽到 300s；填 0/off/none 关掉看门狗，交给内核级 TCP 超时兜底。
_RAW_CHUNK_TIMEOUT = os.getenv("LLM_STREAM_CHUNK_TIMEOUT", "").strip().lower()
LLM_STREAM_CHUNK_TIMEOUT: int | None = (
    None
    if _RAW_CHUNK_TIMEOUT in {"0", "off", "none", "disable", "disabled"}
    else _int_or(_RAW_CHUNK_TIMEOUT, 300)
)

# ---- 单次模型调用的整段上限 ----
# 上面那个看门狗只管**分片之间**：响应头都没到的时候它不启动，而 SDK 的默认读超时要 600s
# 才第一次失败 —— 实测一次网络抖动就让整轮卡了 46 分钟，最后靠本地看门狗才停下
# （run-logs/2026-09-28-hackathon--github-c847c43-outage/README.md §4.18）。
#
# 值取 1800s：平台实测单次最长 143–146s（文档 §3），**12× 余量** —— 只有真挂起才会触发，
# 不会把"只是慢"的调用截断（那条限制是我们自己加的，砍掉本来能跑完的活最不划算）。
# 想彻底关掉：`LLM_CALL_DEADLINE=0`。
_RAW_CALL_DEADLINE = os.getenv("LLM_CALL_DEADLINE", "").strip().lower()
LLM_CALL_DEADLINE: int | None = (
    None
    if _RAW_CALL_DEADLINE in {"0", "off", "none", "disable", "disabled"}
    else _int_or(_RAW_CALL_DEADLINE, 1800)
)

# ---- 本轮编译的 token 预算（默认关闭）----
# 机制在 `core/budget.py` + `core/workflow.py`：到线就不再接新节点、当前节点收尾后**正常退出**，
# 让已生成的部分还能被评（被 SIGKILL 是 0 分）。
#
# **默认 0 = 不限制**，理由：任务量大（65 个节点），而平台侧的 token 上限**没有文档化**
# （赛事 FAQ 只说"Token 配额对所有队一视同仁"，没给数值；平台唯一显式资源是共享 ￥500 池）。
# 我们已知的两次被杀是 210M / 294M token，但那是"被杀的用量"、不等于"上限"。
# 所以先让它跑满，用每次调用的用量行（`in=`/`cached=`/`rss=`）把真实曲线量出来，
# 再把线设在实测值下面。要现在就防一手：`ARC_TOKEN_BUDGET=250000000`。
_RAW_TOKEN_BUDGET = os.getenv("ARC_TOKEN_BUDGET", "").strip().lower()
TOKEN_BUDGET: int = (
    0
    if _RAW_TOKEN_BUDGET in {"", "0", "off", "none", "disable", "disabled"}
    else _int_or(_RAW_TOKEN_BUDGET, 0)
)

# ---- 本轮编译的墙钟上限（默认 23h，对着平台实测的 24h 软时限）----
# 这不是我们猜的上限，是**平台自己给的数**：2026-09-28 那次运行从 13:21:52 跑到
# 2026-09-29 13:21:52 整（86,401s），failure_reason 是 `SoftTimeLimitExceeded()`
# —— Celery 的 soft time limit = 1,440 分钟。被杀时 `passed_count 0 / failed_count 0`，
# **评测根本没开始**（run-logs/2026-09-28-hackathon-softlimit-24h/，§4.22）。
# 所以到线就自己收尾：不再接新节点，**在跑的那个任务也一起停**（节点实测最长 7.6 小时，
# "等它做完"赶不上墙），然后正常退出，让平台还有时间评测已经生成的部分。
# 留 1 小时余量：进程收尾、平台评测都要时间。想关掉：`ARC_RUN_DEADLINE_SECONDS=0`。
_RAW_RUN_DEADLINE = os.getenv("ARC_RUN_DEADLINE_SECONDS", "").strip().lower()
RUN_DEADLINE_SECONDS: int = (
    0
    if _RAW_RUN_DEADLINE in {"0", "off", "none", "disable", "disabled"}
    else _int_or(_RAW_RUN_DEADLINE, 82_800)
)


# ---- 上下文自动压缩 ----
# 窗口这个数只能由配置给：provider 一般不报（DeepSeek 的 /v1/models 只有 id/owned_by），
# 我们也学不来 Pi 那种"生成式模型目录"（没有构建步骤，base_url 可指向任意兼容端点）。
# 启动日志会写明实际用的是哪个值、来自哪里。
#
# 默认按**当前提交模型的真实窗口**填：网关上的 deepseek 系是 1M，而平台不下发 LLM_*，
# 所以这个默认值就是平台实际会用的那个（见 issue #11）。
# 原先的默认 64k 让压缩线落在 51.2K，而实测 input 到过 93K —— 压缩被过早、反复触发，
# 每次都是纯抬 b、不改行为：一次摘要调用 + 压缩后前缀缓存失效 + 重写中段视图。
# 填大了的退化方向是"少省 token"；真正的风险是撞 provider 超窗，所以**换模型时要跟着改**。
_RAW_WINDOW = os.getenv("LLM_CONTEXT_WINDOW", "").strip()
LLM_CONTEXT_WINDOW = _int_or(_RAW_WINDOW, 1_000_000)
CONTEXT_WINDOW_SOURCE = ".env" if _RAW_WINDOW else "默认值（未配置 LLM_CONTEXT_WINDOW）"

# 关掉就完全回到没有压缩时的行为。
COMPACT_ENABLED = os.getenv("COMPACT_ENABLED", "1").strip() != "0"

# 触发线 = 窗口 × 该比例。研究共识是 85–90% 偏晚、95% 太晚；Hermes 的 in-loop 默认 50%
# （为省 token，偏早），0.8 是折中。
COMPACT_AT = _ratio_or(os.getenv("COMPACT_AT", "").strip(), 0.8)

# 压缩触发的**绝对** token 数。设了它就以它为准，`LLM_CONTEXT_WINDOW × COMPACT_AT` 不再参与。
#
# 为什么要解耦：触发点 = 窗口 × COMPACT_AT，而窗口在 issue #11 里从 64K 调到了 1M ——
# 于是压缩线被动地从 51K 抬到 **800K**，而实测上下文最大只到 53K（issue #58 的日志线），
# **压缩实际上从不触发**，历史一路涨到会话结束。一次提交因此烧 5.06 亿 tokens
# （榜首 872 万，约 58×），最后被 SIGKILL。
#
# 这两个数管的是不同的东西：
#   * 模型窗口 = **物理上限**，超了 provider 报错
#   * 这条线   = **我们的工作集预算**，与用哪个模型无关
# 绑在一起的话，换个模型就会连带改掉压缩行为。
#
# 默认 60000：实测会话从约 21K 起步、每次调用涨 1-3K，60K 意味着长会话大约压一次；
# 而 `COMPACT_KEEP_TOKENS`(20000) 保证最近 20K 原文不动，所以正在改的那部分不会被摘掉。
COMPACT_AT_TOKENS = _int_or(os.getenv("COMPACT_AT_TOKENS", "").strip(), 60_000)
COMPACT_TRIGGER_SOURCE = (
    "环境变量 COMPACT_AT_TOKENS" if os.getenv("COMPACT_AT_TOKENS", "").strip() else "默认值（绝对量 60000）"
)

# 保留的最近原文预算（Pi 的默认值），其余中段摘要掉。
COMPACT_KEEP_TOKENS = _int_or(os.getenv("COMPACT_KEEP_TOKENS", "").strip(), 20_000)

# 摘要用哪个模型：留空 = 用主模型。填了就单独建一个（小模型更快更便宜）。
COMPACT_MODEL = os.getenv("COMPACT_MODEL", "").strip()


def _resolve(raw: str) -> str:
    """
    把配置里的相对路径锚定到项目根。

    否则 ./data/checkpoints.db 会随启动目录漂移：从 app/ 启动就会写到 app/data/，
    凭空多出一个库，会话看起来像"丢了"。
    """
    if not raw:
        return ""
    path = Path(raw).expanduser()
    return str(path if path.is_absolute() else (PROJECT_ROOT / path).resolve())


SQLITE_PATH = _resolve(os.getenv("SQLITE_PATH", "./data/checkpoints.db"))

# ---- 技能（Skills）与项目约定（AGENTS.md） ----
# 技能是"索引常驻 + 正文按需读取"的能力包（`SKILL.md`），约定文件是 `AGENTS.md` / `CLAUDE.md`。
# 两者都只做**提示词注入**，没有任何检索：索引全量进系统提示词，匹不匹配交给模型判断。
SKILLS_ENABLED = os.getenv("SKILLS_ENABLED", "1").strip() != "0"

# 全局技能库。工作区技能库固定在 `<工作区>/.my_agent/skills`，不用配。
# 它同时是沙箱的"受信只读根"：模型读这里的正文不弹授权卡片（只读，写仍要授权）。
SKILLS_DIR = _resolve(os.getenv("SKILLS_DIR", "~/.my_agent/skills"))

AGENTS_MD_ENABLED = os.getenv("AGENTS_MD_ENABLED", "1").strip() != "0"

# 是否向上遍历祖先目录找约定文件（到含 .git 的目录为止）。关掉就只读全局与工作区根。
AGENTS_ANCESTORS = os.getenv("AGENTS_ANCESTORS", "1").strip() != "0"


def llm_configured() -> bool:
    """没配 key 时对话接口直接说明情况；列表、浏览、删除这些不依赖模型的接口照常可用。"""
    return bool(LLM_API_KEY)
