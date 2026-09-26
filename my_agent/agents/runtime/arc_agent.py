"""把 my-agent 的图当成 ARC 的阶段 agent 来跑。

上游模板用 `deepagents.create_deep_agent` 造一个带文件工具、技能与结构化输出的 agent。
这里换成 my-agent 的引擎：`app.agent.graph.build_graph` 出的 LangGraph 图（agent ↔ tools 循环
+ 上下文压缩），模型来自 `app.agent.models.build_model`，重试来自 `app.agent.retry`，
工具落盘与路径沙箱来自 `app.agent.tools` / `app.agent.sandbox`。

对外只暴露 `ainvoke`，形状对齐模板里 `runners.ainvoke_stage_agent` 期望的
`{"messages": [...]}`，这样上游的 `extract_payload` / `parse_json_payload`（负责把模型最后
一条消息解析成阶段产物）可以原样复用。

**结构化输出走最后一条消息的 JSON 文本**，不另设 submit 工具：模板里
`_resolve_response_format` 在非 api.openai.com 的 base_url 上本来就会丢掉
`response_format`（比赛网关就是这种），模型直接回 JSON 正文才是实际生效的路径。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import RECURSION_LIMIT, build_graph
from app.agent.models import build_model, build_summary_model

logger = logging.getLogger(__name__)


class ArcStageAgent:
    """一个阶段调用的 agent。每次 `run()` 造一个实例，状态不跨调用累积。"""

    def __init__(
        self,
        *,
        name: str,
        system_prompt: str,
        tools: list[Any],
        workspace_root: str,
    ) -> None:
        self.name = name
        self.system_prompt = system_prompt
        self.tools = list(tools)
        self.workspace_root = workspace_root
        self._checkpointer = InMemorySaver()
        self.graph = build_graph(
            self._checkpointer,
            build_model(),
            build_summary_model(),
            tools=self.tools,
            system_prompt=system_prompt,
        )

    def _run_config(self, config: dict[str, Any] | None) -> dict[str, Any]:
        """把工作区与本次工具集塞进 `configurable`，沙箱与工具查找都从这里读。"""

        run_config = dict(config or {})
        configurable = dict(run_config.get("configurable") or {})
        configurable["workspace"] = self.workspace_root
        configurable["tools"] = self.tools
        run_config["configurable"] = configurable
        run_config.setdefault("recursion_limit", RECURSION_LIMIT)
        return run_config

    async def ainvoke(
        self,
        inputs: dict[str, Any],
        *,
        context: Any | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """跑一轮阶段任务，返回 `{"messages": [...]}`。

        `context` 只为兼容上游调用形状而存在（deepagents 用它传 `AgentRuntimeContext`），
        my-agent 的图不读它——阶段的元数据已经拼进 system prompt 与用户消息了。
        """

        del context
        state = await self.graph.ainvoke(
            {"messages": _normalize_messages(inputs.get("messages"))},
            config=self._run_config(config),
        )
        messages = list(state.get("messages") or [])
        logger.debug(
            "ARC 阶段 agent 结束 name=%s 消息数=%d 工具调用=%d",
            self.name,
            len(messages),
            sum(len(getattr(message, "tool_calls", None) or []) for message in messages),
        )
        return {"messages": messages}

    async def astream_events(
        self,
        inputs: dict[str, Any],
        *,
        context: Any | None = None,
        config: dict[str, Any] | None = None,
        version: str = "v2",
    ):
        """带事件流的同一次执行。

        **这个方法存在的唯一理由是让人看得见进度。** 没有它的时候，上游
        `runners.ainvoke_stage_agent` 会打印 "agent streaming is unavailable; falling
        back to ainvoke"，然后整段阶段调用变成一次阻塞调用——中途一个字都不打，
        工具批次要等全部结束才一次性吐出来。实测一轮 design 要 3.8 分钟、TDD 一轮
        要 6.6 分钟，那段时间日志完全静止，**分不清是在跑还是挂了**。

        产出的事件由上游 `runners._log_stream_event` 消费，映射成
        `tool-call>` / `tool-result>` / `model-final>` 三类实时日志。
        终态仍由同一个事件流里最后那个 agent state 取出，所以结果与 `ainvoke` 一致。
        """

        del context
        payload = {"messages": _normalize_messages(inputs.get("messages"))}
        run_config = self._run_config(config)
        async for event in self.graph.astream_events(payload, config=run_config, version=version):
            yield event


def _normalize_messages(raw: Any) -> list[BaseMessage]:
    """上游传的是 `[{"role": "user", "content": ...}]` 这种 dict 形状。"""

    if not isinstance(raw, list):
        return []
    normalized: list[BaseMessage] = []
    for item in raw:
        if isinstance(item, BaseMessage):
            normalized.append(item)
            continue
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip().lower()
        content = item.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        if role == "assistant":
            normalized.append(AIMessage(content=text))
        else:
            normalized.append(HumanMessage(content=text))
    return normalized


def final_text(messages: list[BaseMessage]) -> str:
    """取最后一条非 human / tool 消息的正文，也就是模型这一轮的最终回答。"""

    for message in reversed(messages):
        role = str(getattr(message, "type", "") or "").strip().lower()
        if role in {"human", "tool"}:
            continue
        content = getattr(message, "content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


__all__ = ["ArcStageAgent", "final_text"]
