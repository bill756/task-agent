"""Offline smoke tests: graph paths with a FakePlanner + LLMPlanner tool loop with a mocked model."""
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))

from core import ChangePlan, RepositoryTools, WriteRequest
from graph import build_graph, initial_state
from llm_planner import LLMPlanner
from config import Settings


class FakePlanner:
    def __init__(self, repaired_plan=None):
        self.plans = 0
        self.repairs = 0
        self.repaired_plan = repaired_plan

    def plan(self, task, files):
        self.plans += 1
        return ChangePlan("fake plan", (WriteRequest("note.md", "hello"),))

    def repair(self, task, plan, test_output):
        self.repairs += 1
        return self.repaired_plan


def show(label, final):
    report = final["report"]
    print(f"{label}: status={final['status']} report.status={report.status} "
          f"changed={report.changed_files} exit={report.test_exit_code} "
          f"trace={[t['state'] for t in report.trace]}")


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    tools = RepositoryTools(root)
    planner = FakePlanner()
    g = build_graph(tools, planner, max_repairs=1)

    # case 1: approved=False -> awaiting_approval, no writes
    final = g.invoke(initial_state("task"))
    show("case1(no-approve)", final)
    assert final["status"] == "awaiting_approval"
    assert list(root.glob("*")) == [], "no file should be written"

    # case 2: pre-planned state enters at execute; no test_command -> completed
    state = initial_state("task", approved=True)
    state["plan"] = ChangePlan("pre", (WriteRequest("a.txt", "x"),))
    final = g.invoke(state)
    show("case2(no-test)", final)
    assert final["status"] == "completed" and (root / "a.txt").read_text() == "x"
    assert planner.plans == 1, "entry router must skip inspect/plan when plan exists"

    # case 3: verify failure, repair returns None -> verification_failed
    tools2 = RepositoryTools(root, allowed_commands=[("python", "-c", "import sys; sys.exit(1)")])
    planner2 = FakePlanner(repaired_plan=None)
    g2 = build_graph(tools2, planner2, max_repairs=1)
    state = initial_state("task", approved=True)
    state["plan"] = ChangePlan("p", (WriteRequest("a.txt", "x"),), test_command=("python", "-c", "import sys; sys.exit(1)"))
    final = g2.invoke(state)
    show("case3(fail-nofix)", final)
    assert final["status"] == "verification_failed" and planner2.repairs == 1

    # case 4: verify failure then repair to a passing plan -> completed
    tools3 = RepositoryTools(root, allowed_commands=[("python", "-c", "import sys; sys.exit(1)"), ("python", "-c", "pass")])
    planner3 = FakePlanner(
        repaired_plan=ChangePlan("fixed", (WriteRequest("a.txt", "y"),), test_command=("python", "-c", "pass"))
    )
    g3 = build_graph(tools3, planner3, max_repairs=1)
    state = initial_state("task", approved=True)
    state["plan"] = ChangePlan("p", (WriteRequest("a.txt", "x"),), test_command=("python", "-c", "import sys; sys.exit(1)"))
    final = g3.invoke(state)
    show("case4(repair-ok)", final)
    assert final["status"] == "completed" and planner3.repairs == 1

    # case 5: LLMPlanner tool loop with a mocked model
    settings = Settings(base_url="http://x", api_key="k", model="m")
    llm_tools = RepositoryTools(root)
    (root / "src.txt").write_text("hello world", encoding="utf-8")

    class FakeResp:
        def __init__(self, tool_calls=None, content=""):
            self.tool_calls = tool_calls or []
            self.content = content

    plan_payload = {
        "summary": "s",
        "writes": [{"path": "out.txt", "content": "hi"}],
        "test_command": None,
    }
    fake_model = MagicMock()
    fake_model.bind_tools.return_value = fake_model
    fake_model.invoke.side_effect = [
        FakeResp(tool_calls=[{"name": "list_files", "args": {"pattern": "*"}, "id": "1"}]),
        FakeResp(tool_calls=[{"name": "submit_plan", "args": {"plan": plan_payload}, "id": "2"}]),
    ]
    with patch("llm_planner.ChatOpenAI", return_value=fake_model):
        llm_planner = LLMPlanner(settings, llm_tools)
        plan = llm_planner.plan("task", ["src.txt"])
    assert plan.summary == "s" and plan.writes[0].path == "out.txt"
    audited = [e.tool for e in llm_tools.audit]
    print("case5(llm-tool-loop): ok; audit tools seen:", sorted(set(audited)))
    assert "list_files" in audited

    # case 6: NO_FIX marker -> repair returns None
    fake_model.invoke.side_effect = [FakeResp(content="NO_FIX")]
    with patch("llm_planner.ChatOpenAI", return_value=fake_model):
        llm_planner = LLMPlanner(settings, llm_tools)
        result = llm_planner.repair("task", plan, "boom")
    print("case6(no-fix):", result)
    assert result is None

    # case 7: JSON fallback parsing of a plain-text answer
    fake_model.invoke.side_effect = [FakeResp(content='```json\n{"summary":"js","writes":[{"path":"j.txt","content":"c"}]}\n```')]
    with patch("llm_planner.ChatOpenAI", return_value=fake_model):
        llm_planner = LLMPlanner(settings, llm_tools)
        plan = llm_planner.plan("task", [])
    print("case7(json-fallback):", plan.summary, plan.writes[0].path)
    assert plan.summary == "js"

    # case 8: repair succeeds but the fixed plan still fails, hitting max_repairs -> fail
    tools8 = RepositoryTools(
        root,
        allowed_commands=[
            ("python", "-c", "import sys; sys.exit(1)"),
            ("python", "-c", "import sys; sys.exit(2)"),
        ],
    )
    planner8 = FakePlanner(
        repaired_plan=ChangePlan(
            "still-bad", (WriteRequest("a.txt", "z"),),
            test_command=("python", "-c", "import sys; sys.exit(2)"),
        )
    )
    g8 = build_graph(tools8, planner8, max_repairs=1)
    state = initial_state("task", approved=True)
    state["plan"] = ChangePlan(
        "p", (WriteRequest("a.txt", "x"),),
        test_command=("python", "-c", "import sys; sys.exit(1)"),
    )
    final = g8.invoke(state)
    show("case8(repair-fail-limit)", final)
    assert final["status"] == "verification_failed" and planner8.repairs == 1
    verify_traces = [t for t in final["report"].trace if t["state"] == "verify"]
    assert len(verify_traces) == 2 and verify_traces[1]["repair"] == 1
    assert (root / "a.txt").read_text() == "z", "repaired write must land on disk"

    # case 9: two-phase contract - pass the awaiting state back with approved=True,
    # without resetting status; report status must still be derived from facts
    g9 = build_graph(tools, FakePlanner(), max_repairs=1)
    first = g9.invoke(initial_state("task", approved=False))
    assert first["status"] == "awaiting_approval"
    second_state = dict(first)
    second_state["approved"] = True
    final = g9.invoke(second_state)
    show("case9(two-phase)", final)
    assert final["status"] == "completed" and final["report"].status == "completed"

    # case 10: execute without approval must be blocked by the graph itself
    g10 = build_graph(tools, FakePlanner(), max_repairs=1)
    state = initial_state("task", approved=False)
    state["plan"] = ChangePlan("p", (WriteRequest("blocked.txt", "x"),))
    final = g10.invoke(state)
    print("case10(no-approve-exec):", final["status"], "write_happened=", (root / "blocked.txt").exists())
    assert final["status"] == "awaiting_approval"
    assert not (root / "blocked.txt").exists(), "unapproved plan must not be written"

    # case 11: empty allow-list -> test is skipped (audited), plan still completes
    tools11 = RepositoryTools(root)  # no allowed_commands
    g11 = build_graph(tools11, FakePlanner(), max_repairs=1)
    state = initial_state("task", approved=True)
    state["plan"] = ChangePlan(
        "p", (WriteRequest("skipped.txt", "x"),),
        test_command=("python", "-c", "pass"),
    )
    final = g11.invoke(state)
    show("case11(empty-allowlist)", final)
    assert final["status"] == "completed"
    assert any("empty allow-list" in e.detail for e in final["report"].audit if e.tool == "run_test")

print("\nALL_OK")
