"""LangGraph orchestration: the six-stage pipeline expressed as a StateGraph.

Approval gate is implemented with two stream calls:
  1. run with approved=False -> graph stops at `awaiting` (plan is in state).
  2. pass the returned state back with approved=True -> the entry router sees a
     non-None plan and enters directly at `execute`.
This keeps the gate explicit and needs no checkpoint persistence.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from core import ChangePlan, PolicyError, RepositoryTools, RunReport
from llm_planner import LLMPlanner


class AgentState(TypedDict):
    task: str
    files: list[str]
    plan: ChangePlan | None
    approved: bool
    repair_count: int
    changed: list[str]
    exit_code: int | None
    test_output: str
    trace: list[dict[str, Any]]
    status: str
    report: RunReport | None


def initial_state(task: str, approved: bool = False) -> AgentState:
    return {
        "task": task,
        "files": [],
        "plan": None,
        "approved": approved,
        "repair_count": 0,
        "changed": [],
        "exit_code": None,
        "test_output": "",
        "trace": [],
        "status": "running",
        "report": None,
    }


def build_graph(
    tools: RepositoryTools,
    planner: LLMPlanner,
    max_repairs: int = 1,
) -> CompiledStateGraph:
    builder = StateGraph(AgentState)

    def route_entry(state: AgentState) -> str:
        if state["plan"] is None:
            return "inspect"
        return "execute" if state["approved"] else "awaiting"

    def inspect_node(state: AgentState) -> dict[str, Any]:
        files = tools.list_files("*")
        trace = state["trace"] + [{"state": "inspect", "file_count": len(files)}]
        return {"files": files, "trace": trace}

    def plan_node(state: AgentState) -> dict[str, Any]:
        plan = planner.plan(state["task"], state["files"])
        trace = state["trace"] + [
            {"state": "plan", "summary": plan.summary, "writes": [w.path for w in plan.writes]}
        ]
        return {"plan": plan, "trace": trace}

    def route_after_plan(state: AgentState) -> str:
        return "execute" if state["approved"] else "awaiting"

    def awaiting_node(state: AgentState) -> dict[str, Any]:
        return {"status": "awaiting_approval"}

    def execute_node(state: AgentState) -> dict[str, Any]:
        if not state["approved"]:
            raise PolicyError("execute requires explicit approval")
        changed = list(state["changed"])
        for request in state["plan"].writes:  # type: ignore[union-attr]
            tools.write_file(request.path, request.content, approved=True)
            if request.path not in changed:
                changed.append(request.path)
        trace = state["trace"] + [{"state": "execute", "changed_files": changed}]
        return {"changed": changed, "trace": trace}

    def verify_node(state: AgentState) -> dict[str, Any]:
        plan = state["plan"]
        if plan is None or plan.test_command is None:
            return {"exit_code": None, "test_output": "", "trace": state["trace"]}
        result = tools.run_test(plan.test_command)
        trace = state["trace"] + [
            {"state": "verify", "exit_code": result.returncode, "repair": state["repair_count"]}
        ]
        return {
            "exit_code": result.returncode,
            "test_output": result.stdout + result.stderr,
            "trace": trace,
        }

    def route_after_verify(state: AgentState) -> str:
        exit_code = state["exit_code"]
        if exit_code is None or exit_code == 0:
            return "report"
        if state["repair_count"] >= max_repairs:
            return "fail"
        return "repair"

    def repair_node(state: AgentState) -> dict[str, Any]:
        repaired = planner.repair(state["task"], state["plan"], state["test_output"])
        if repaired is None:
            trace = state["trace"] + [
                {"state": "repair", "attempt": state["repair_count"] + 1, "result": "no_fix"}
            ]
            return {"status": "verification_failed", "trace": trace}
        trace = state["trace"] + [
            {
                "state": "repair",
                "attempt": state["repair_count"] + 1,
                "summary": repaired.summary,
            }
        ]
        return {"plan": repaired, "repair_count": state["repair_count"] + 1, "trace": trace}

    def route_after_repair(state: AgentState) -> str:
        return "fail" if state["status"] == "verification_failed" else "execute"

    def fail_node(state: AgentState) -> dict[str, Any]:
        return {"status": "verification_failed"}

    def report_node(state: AgentState) -> dict[str, Any]:
        executed = any(t["state"] == "execute" for t in state["trace"])
        if state["status"] == "verification_failed":
            status = "verification_failed"
        elif executed:
            # The run reached execute: derive the final status from facts.
            status = "completed" if state["exit_code"] in (None, 0) else "verification_failed"
        else:
            # Stopped at the approval gate (plan produced, nothing executed).
            status = "awaiting_approval"
        report = RunReport(
            status=status,
            plan=state["plan"],
            changed_files=state["changed"],
            test_exit_code=state["exit_code"],
            trace=state["trace"],
            audit=list(tools.audit),
        )
        return {"status": status, "report": report}

    builder.add_node("inspect", inspect_node)
    builder.add_node("plan", plan_node)
    builder.add_node("awaiting", awaiting_node)
    builder.add_node("execute", execute_node)
    builder.add_node("verify", verify_node)
    builder.add_node("repair", repair_node)
    builder.add_node("fail", fail_node)
    builder.add_node("report", report_node)

    builder.add_conditional_edges(
        START, route_entry, {"inspect": "inspect", "execute": "execute", "awaiting": "awaiting"}
    )
    builder.add_edge("inspect", "plan")
    builder.add_conditional_edges(
        "plan", route_after_plan, {"execute": "execute", "awaiting": "awaiting"}
    )
    builder.add_edge("awaiting", "report")
    builder.add_edge("execute", "verify")
    builder.add_conditional_edges(
        "verify", route_after_verify, {"report": "report", "fail": "fail", "repair": "repair"}
    )
    builder.add_conditional_edges(
        "repair", route_after_repair, {"execute": "execute", "fail": "fail"}
    )
    builder.add_edge("fail", "report")
    builder.add_edge("report", END)
    return builder.compile()
