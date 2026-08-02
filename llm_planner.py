"""Online LLM planner: OpenAI-compatible chat model with a read-only tool loop.

The model may call read-only repository tools (list_files / read_file / search_text)
to inspect the repo, then submits its plan via the `submit_plan` tool, whose argument
is a structured `ChangePlanSchema` -- so the plan always arrives as validated JSON.

Writes are NOT exposed to the model: they stay behind the approval gate and are
performed by the graph's execute node.
"""

from __future__ import annotations

import json
import re

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from config import Settings
from core import ChangePlan, RepositoryTools, WriteRequest

NO_FIX_MARKER = "NO_FIX"

SYSTEM_PLAN_PROMPT = """你是一个仓库维护 Agent 的规划器。用户会给你维护任务和仓库文件清单。
你可以调用只读工具（list_files / read_file / search_text）观察仓库后再决定改动。
最后必须通过 submit_plan 工具提交变更计划，参数为结构化 JSON：
- summary: 计划摘要
- writes: 要写入的文件列表，每项 {path, content}，content 必须是该文件的完整新内容
- test_command: 可选的验证命令 argv 列表（如 ["python", "-m", "pytest"]）；不需要验证时省略

安全约束：
- path 必须是相对仓库根的相对路径，禁止 ../ 越界
- 只规划必要的改动，不要重写无关文件
"""

SYSTEM_REPAIR_PROMPT = """你是仓库维护 Agent 的修复器。测试失败，需要你根据测试输出修正计划。
可以调用只读工具观察仓库。修正后通过 submit_plan 提交新计划；
如果问题无法修复，直接回复 NO_FIX。"""


class WriteSchema(BaseModel):
    path: str
    content: str


class ChangePlanSchema(BaseModel):
    summary: str
    writes: list[WriteSchema]
    test_command: list[str] | None = None


class PlanParseError(RuntimeError):
    pass


class ToolLoopLimitError(RuntimeError):
    pass


def _as_change_plan(schema: ChangePlanSchema) -> ChangePlan:
    return ChangePlan(
        summary=schema.summary,
        writes=tuple(WriteRequest(w.path, w.content) for w in schema.writes),
        test_command=tuple(schema.test_command) if schema.test_command else None,
    )


def _parse_plan_content(content: str) -> ChangePlan | None:
    """Tolerant fallback: strip markdown fences, parse JSON into ChangePlanSchema."""
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        schema = ChangePlanSchema.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValueError):
        return None
    return _as_change_plan(schema)


class LLMPlanner:
    """Implements the planning/repair node logic on top of an OpenAI-compatible API."""

    def __init__(self, settings: Settings, tools: RepositoryTools, temperature: float = 0.2) -> None:
        self.settings = settings
        self.tools = tools
        self.model = ChatOpenAI(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
            temperature=temperature,
            timeout=settings.timeout,
        )
        self._tools_by_name = self._build_tools()
        self._model_with_tools = self.model.bind_tools(list(self._tools_by_name.values()))

    def _build_tools(self) -> dict[str, object]:
        tools = self.tools

        @tool
        def list_files(pattern: str = "*") -> str:
            """列出仓库内的文件（相对路径，每行一个），可用 fnmatch 通配符过滤。"""
            files = tools.list_files(pattern)
            return "\n".join(files) if files else "(仓库为空)"

        @tool
        def read_file(relative_path: str) -> str:
            """读取指定文件完整内容（UTF-8）。路径相对仓库根。"""
            return tools.read_file(relative_path)

        @tool
        def search_text(query: str) -> str:
            """在仓库所有文本文件中不区分大小写搜索子串，返回 路径:行号:内容。"""
            matches = tools.search_text(query)
            return "\n".join(
                f"{m['path']}:{m['line']}:{m['text']}" for m in matches
            ) or "(no matches)"

        @tool
        def submit_plan(plan: ChangePlanSchema) -> str:
            """提交最终变更计划（summary/writes/test_command）。完成观察后调用本工具。"""
            return "plan accepted"

        return {t.name: t for t in (list_files, read_file, search_text, submit_plan)}

    def _run_loop(self, messages: list, task: str, allow_no_fix: bool) -> ChangePlan | None:
        retried_parse = False
        for _ in range(self.settings.max_tool_iters):
            response = self._model_with_tools.invoke(messages)
            messages.append(response)
            if not getattr(response, "tool_calls", None):
                content = str(response.content or "").strip()
                if allow_no_fix and content == NO_FIX_MARKER:
                    return None
                plan = _parse_plan_content(content)
                if plan is not None:
                    return plan
                if not retried_parse:
                    retried_parse = True
                    messages.append(
                        SystemMessage(
                            content="你的回复无法解析为变更计划。请通过 submit_plan 工具提交计划，"
                            "或直接输出符合 ChangePlanSchema 的 JSON。"
                        )
                    )
                    continue
                raise PlanParseError(
                    f"LLM 未通过 submit_plan 提交计划，且回复无法解析为计划：{content[:200]!r}"
                )
            for call in response.tool_calls:
                name = call.get("name", "")
                args = call.get("args", {})
                if name == "submit_plan":
                    schema = ChangePlanSchema.model_validate(args["plan"])
                    return _as_change_plan(schema)
                fn = self._tools_by_name.get(name)
                if fn is None:
                    result = f"unknown tool: {name}"
                else:
                    try:
                        result = fn.invoke(args)
                    except Exception as exc:  # tool errors are fed back to the model
                        result = f"tool error: {exc}"
                messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
        raise ToolLoopLimitError(
            f"LLM 在 {self.settings.max_tool_iters} 轮内未提交计划，已终止工具循环"
        )

    def plan(self, task: str, files: list[str]) -> ChangePlan:
        file_list = "\n".join(files) if files else "(仓库为空)"
        messages = [
            SystemMessage(content=SYSTEM_PLAN_PROMPT),
            HumanMessage(
                content=f"维护任务：{task}\n\n仓库文件清单：\n{file_list}\n\n"
                "请观察仓库后通过 submit_plan 提交变更计划。"
            ),
        ]
        plan = self._run_loop(messages, task, allow_no_fix=False)
        assert plan is not None
        return plan

    def repair(self, task: str, plan: ChangePlan, test_output: str) -> ChangePlan | None:
        writes_summary = "\n".join(f"- {w.path}" for w in plan.writes)
        messages = [
            SystemMessage(content=SYSTEM_REPAIR_PROMPT),
            HumanMessage(
                content=f"维护任务：{task}\n"
                f"当前计划：{plan.summary}\n计划写入文件：\n{writes_summary}\n"
                f"验证命令：{' '.join(plan.test_command) if plan.test_command else '(无)'}\n\n"
                f"测试输出：\n{test_output}\n\n"
                "修正后通过 submit_plan 提交新计划；无法修复则回复 NO_FIX。"
            ),
        ]
        return self._run_loop(messages, task, allow_no_fix=True)
