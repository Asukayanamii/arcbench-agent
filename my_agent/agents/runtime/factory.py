"""阶段 agent 的组装：ARC 的提示词 + 受纪律约束的工具 + my-agent 的图。

替代模板里的 `deepagents.create_deep_agent` 那一层。与上游的差别，逐条说明：

- **文件工具**：用 `agents.runtime.workspace_tools` 提供的 `read_file` / `write_file` /
  `edit_file` / `glob` / `grep` / `ls`，实现在 my-agent 的工具与沙箱之上。名字与参数 schema
  与 deepagents 对齐，所以 ARC 的提示词与技能无需改动。
- **结构化输出**：不再走 `response_format`（比赛网关会把它丢掉，见 `arc_agent` 的说明），
  改为在系统提示词末尾附一份 JSON 形状要求，产物仍是模型最后一条消息里的 JSON。
- **权限**：deepagents 用 `FilesystemPermission` 列表表达；这里由 my-agent 的沙箱
  （工作区信任边界 + 硬阻断）叠加 `workspace_tools` 的 ARC 禁区实现，取更严的一侧。
- **技能**：保持渐进披露——提示词里给 `/skills/<名字>/SKILL.md` 的路径，模型按需
  `read_file` 正文；`read_file` 对 `/skills/` 只读放行，其余一律锁在工作区内。
- **子 agent / memory / execute**：不提供。ARC 的提示词本来就要求关掉 `execute`，
  模板也把 general-purpose 子 agent 关掉了。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

from agents.runtime.arc_agent import ArcStageAgent
from agents.runtime.extra_tools import build_extra_tools, render_extra_tools
from agents.runtime.stage_policy import StageDiscipline, guard_tools, render_stage_limits
from agents.runtime.workspace_tools import FILE_TOOLS
from app.agent.tools.todo import TODO_TOOL_NAMES


def build_stage_agent(
    *,
    name: str,
    stage: str,
    model: str | object | None = None,
    system_prompt: str,
    response_format: object | None = None,
    workspace_root: str,
    writable_roots: list[str] | None = None,
    skills: list[str] | None = None,
    permitted_skill_names: list[str] | None = None,
    memory: list[str] | None = None,
    tools: list[object] | None = None,
) -> ArcStageAgent:
    """造一个受 ARC 阶段纪律约束的 my-agent 阶段 agent。

    `model` / `writable_roots` / `memory` 只为与上游签名保持兼容：模型由 my-agent 的
    `build_model()` 从环境变量构造，写入范围由沙箱的工作区边界决定，memory 不使用。
    """

    del model, writable_roots, memory

    discipline = StageDiscipline(stage=stage)
    stage_tools = _coerce_tools(tools or [])
    extra_tools = build_extra_tools(stage, read_tools=FILE_TOOLS, workspace_root=workspace_root)
    injected_extra = [tool for tool in extra_tools if tool.name in TODO_TOOL_NAMES]
    plain_extra = [tool for tool in extra_tools if tool.name not in TODO_TOOL_NAMES]
    all_tools = [
        *guard_tools([*FILE_TOOLS, *stage_tools, *plain_extra], discipline),
        *injected_extra,
    ]

    return ArcStageAgent(
        name=name,
        system_prompt=_with_response_shape(
            system_prompt,
            response_format=response_format,
            skills=skills,
            permitted_skill_names=permitted_skill_names,
            stage=stage,
        ),
        tools=all_tools,
        workspace_root=workspace_root,
    )


def _coerce_tools(tools: list[object]) -> list[BaseTool]:
    """普通 async 函数（`run_tests` / `run_build` / 追溯工具）也要能当工具用。"""

    coerced: list[BaseTool] = []
    for tool in tools:
        if isinstance(tool, BaseTool):
            coerced.append(tool)
            continue
        if callable(tool):
            coerced.append(StructuredTool.from_function(coroutine=tool))
            continue
        raise TypeError(f"无法把 {tool!r} 当工具使用。")
    return coerced


def _with_response_shape(
    system_prompt: str,
    *,
    response_format: object | None,
    skills: list[str] | None,
    permitted_skill_names: list[str] | None,
    stage: str,
) -> str:
    """按需给系统提示词补上"最后一条消息必须是 JSON"的形状说明与阶段硬约束。"""

    blocks = [system_prompt]

    # 本阶段会被拦下的硬约束。拦截逻辑在 stage_policy，文案也从那里来，改上限不会两边分叉。
    limits = render_stage_limits(stage)
    if limits:
        blocks.append(limits)
    extra_limits = render_extra_tools(stage)
    if extra_limits:
        blocks.append(extra_limits)

    if response_format is not None and isinstance(response_format, type) and issubclass(
        response_format, BaseModel
    ):
        lines = [
            "### Machine-Readable Response",
            "- Your final assistant message must be exactly one JSON object and nothing else.",
            "- Do not wrap it in Markdown fences, prose, or a label.",
            "- Required JSON shape:",
            f"```json\n{json.dumps(_schema_hint(response_format), ensure_ascii=False, indent=2)}\n```",
            "- Structured output is not enforced by the provider here, so the JSON text itself is the artifact.",
        ]
        blocks.append("\n".join(lines))

    listed = [name for name in (permitted_skill_names or []) if str(name).strip()]
    if listed:
        blocks.append(
            "\n".join(
                [
                    "### Available Skill Files",
                    *[f"- `/skills/{name}/SKILL.md`" for name in listed],
                ]
            )
        )

    return "\n\n".join(block for block in blocks if block and block.strip())


def _schema_hint(model: type[BaseModel]) -> dict[str, Any]:
    """把 pydantic 模型压成一份带字段说明的骨架，给模型当填写模板。"""

    hint: dict[str, Any] = {}
    for field_name, field in model.model_fields.items():
        description = (field.description or "").strip()
        annotation = field.annotation
        if field_name == "interfaces":
            hint[field_name] = [{"<interface object>": description or "interface contract"}]
            continue
        if field_name == "tests":
            hint[field_name] = [{"<test object>": description or "test manifest item"}]
            continue
        if annotation in (str, str | None):
            hint[field_name] = description or "string"
            continue
        if annotation in (list[str], list[str] | None):
            hint[field_name] = [description or "string"]
            continue
        hint[field_name] = description or getattr(annotation, "__name__", str(annotation))
    return hint


__all__ = ["build_stage_agent"]
