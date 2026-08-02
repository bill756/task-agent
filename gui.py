"""Tkinter GUI for the repository Task Agent.

Threading model: graph.stream() runs in a background thread and pushes
(node_name, updates) events into a queue.Queue; the UI polls the queue with
root.after() -- Tkinter widgets are only ever touched from the main thread.
"""

from __future__ import annotations

import queue
import shlex
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from config import ConfigError, Settings, load_settings
from core import RepositoryTools
from graph import AgentState, build_graph, initial_state
from llm_planner import LLMPlanner

LOG_LIMIT = 2000


def render_report_markdown(report) -> str:
    """Render a RunReport as human-readable Markdown."""
    lines: list[str] = ["# 任务报告", ""]
    lines.append(f"- **状态**: `{report.status}`")
    if report.test_exit_code is None:
        lines.append("- **测试退出码**: 未运行")
    else:
        lines.append(f"- **测试退出码**: `{report.test_exit_code}`")
    lines += ["", "## 变更计划", ""]
    lines.append(f"- **摘要**: {report.plan.summary}")
    lines.append("- **待写文件**:")
    for write in report.plan.writes:
        lines.append(f"  - `{write.path}`")
    if report.plan.test_command:
        lines.append(f"- **验证命令**: `{' '.join(report.plan.test_command)}`")
    else:
        lines.append("- **验证命令**: （无）")
    lines += ["", "## 实际变更文件", ""]
    if report.changed_files:
        lines.extend(f"- `{path}`" for path in report.changed_files)
    else:
        lines.append("（无）")
    lines += ["", "## 执行轨迹", ""]
    if report.trace:
        for item in report.trace:
            details = " ".join(f"{k}={v}" for k, v in item.items() if k != "state")
            lines.append(f"- **{item.get('state')}** {details}")
    else:
        lines.append("（无）")
    lines += ["", "## 审计日志", ""]
    if report.audit:
        for event in report.audit:
            lines.append(f"- `{event.tool}` allowed={event.allowed}: {event.detail}")
    else:
        lines.append("（无）")
    return "\n".join(lines) + "\n"


class MaintenanceGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("任务 Agent")
        root.geometry("920x700")

        self.settings: Settings | None = None
        self.tools: RepositoryTools | None = None
        self.graph = None
        self.state: AgentState | None = None
        self.last_report = None
        self._repo_loaded: str | None = None
        self._allowed_loaded: list[tuple[str, ...]] | None = None
        self.busy = False
        self.events: "queue.Queue[tuple]" = queue.Queue()

        self._build_widgets()
        root.after(100, self._poll_events)

    # ---------- widgets ----------

    def _build_widgets(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="仓库路径:").pack(side="left")
        self.repo_var = tk.StringVar(value=str(Path.cwd()))
        ttk.Entry(top, textvariable=self.repo_var, width=55).pack(side="left", padx=4)
        ttk.Button(top, text="浏览…", command=self._browse).pack(side="left")

        task_frame = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        task_frame.pack(fill="x")
        ttk.Label(task_frame, text="任务描述:").pack(anchor="w")
        self.task_text = tk.Text(task_frame, height=3)
        self.task_text.pack(fill="x")

        allow_frame = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        allow_frame.pack(fill="x")
        ttk.Label(allow_frame, text="验证命令白名单（每行一条 argv，留空则跳过验证）:").pack(anchor="w")
        self.allow_text = tk.Text(allow_frame, height=2)
        self.allow_text.pack(fill="x")

        plan_frame = ttk.LabelFrame(self.root, text="变更计划预览", padding=4)
        plan_frame.pack(fill="both", expand=True, padx=8)
        self.plan_tree = ttk.Treeview(
            plan_frame, columns=("path", "test"), show="tree", height=6
        )
        self.plan_tree.heading("#0", text="摘要")
        self.plan_tree.heading("path", text="待写文件")
        self.plan_tree.heading("test", text="验证命令")
        self.plan_tree.column("#0", width=340)
        self.plan_tree.column("path", width=320)
        self.plan_tree.column("test", width=200)
        self.plan_tree.pack(fill="both", expand=True)

        btns = ttk.Frame(self.root, padding=8)
        btns.pack(fill="x")
        self.btn_plan = ttk.Button(btns, text="① 生成计划", command=self._make_plan)
        self.btn_plan.pack(side="left", padx=4)
        self.btn_execute = ttk.Button(
            btns, text="② 审批并执行", command=self._execute, state="disabled"
        )
        self.btn_execute.pack(side="left", padx=4)
        self.btn_export = ttk.Button(
            btns, text="③ 导出报告", command=self._export, state="disabled"
        )
        self.btn_export.pack(side="left", padx=4)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(btns, textvariable=self.status_var).pack(side="right")

        log_frame = ttk.LabelFrame(self.root, text="运行日志", padding=4)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_frame, height=10, state="disabled")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ---------- engine ----------

    def _parse_allowed_commands(self) -> list[tuple[str, ...]]:
        allowed: list[tuple[str, ...]] = []
        for number, line in enumerate(self.allow_text.get("1.0", "end").splitlines(), 1):
            if not line.strip():
                continue
            try:
                allowed.append(tuple(shlex.split(line)))
            except ValueError as exc:
                raise ValueError(f"第 {number} 行解析失败：{exc}") from exc
        return allowed

    def _ensure_engine(self) -> bool:
        """Build tools/planner/graph for the current repo path + allow-list."""
        repo = self.repo_var.get().strip()
        if not repo:
            messagebox.showerror("错误", "请先填写仓库路径")
            return False
        try:
            allowed = self._parse_allowed_commands()
        except ValueError as exc:
            messagebox.showerror("白名单格式错误", str(exc))
            return False
        if (
            self.graph is not None
            and self._repo_loaded == repo
            and self._allowed_loaded == allowed
        ):
            return True
        try:
            settings = load_settings()
        except ConfigError as exc:
            messagebox.showerror("配置错误", str(exc))
            return False
        self.settings = settings
        self.tools = RepositoryTools(Path(repo), allowed_commands=allowed)
        self.graph = build_graph(
            self.tools, LLMPlanner(settings, self.tools), max_repairs=settings.max_repairs
        )
        self._repo_loaded = repo
        self._allowed_loaded = allowed
        return True

    def _task_text(self) -> str:
        task = self.task_text.get("1.0", "end").strip()
        if not task:
            messagebox.showerror("错误", "请填写任务描述")
        return task

    # ---------- actions ----------

    def _browse(self) -> None:
        path = filedialog.askdirectory(initialdir=self.repo_var.get())
        if path:
            self.repo_var.set(path)

    def _make_plan(self) -> None:
        if self.busy or not self._ensure_engine():
            return
        task = self._task_text()
        if not task:
            return
        self._set_busy(True)
        state = initial_state(task, approved=False)
        threading.Thread(
            target=self._run_stream, args=(state,), daemon=True
        ).start()

    def _execute(self) -> None:
        if self.busy or self.state is None or self.state["status"] != "awaiting_approval":
            return
        self._set_busy(True)
        state = dict(self.state)
        state["approved"] = True
        threading.Thread(
            target=self._run_stream, args=(state,), daemon=True
        ).start()

    def _export(self) -> None:
        if self.last_report is None:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".md", filetypes=[("Markdown", "*.md")], initialfile="report.md"
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(render_report_markdown(self.last_report))
        self._append_log(f"[导出] 报告已保存到 {path}")

    # ---------- background thread ----------

    def _run_stream(self, state: AgentState) -> None:
        try:
            assert self.graph is not None
            for chunk in self.graph.stream(state, config={"recursion_limit": 100}):
                for node_name, updates in chunk.items():
                    state.update(updates)
                    self.events.put(("progress", node_name, updates))
            self.events.put(("done", state))
        except Exception as exc:  # noqa: BLE001 -- surface any failure to the UI
            self.events.put(("error", exc))

    # ---------- UI event polling (main thread only) ----------

    def _poll_events(self) -> None:
        try:
            while True:
                kind, *payload = self.events.get_nowait()
                if kind == "progress":
                    self._on_progress(payload[0], payload[1])
                elif kind == "done":
                    self._on_done(payload[0])
                elif kind == "error":
                    self._on_error(payload[0])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _on_progress(self, node_name: str, updates: dict) -> None:
        self.status_var.set(f"阶段: {node_name}")
        self._append_log(f"[{node_name}] {self._format_updates(node_name, updates)}")
        if node_name == "plan":
            plan = updates.get("plan")
            if plan is not None:
                self._fill_plan_tree(plan)

    def _on_done(self, state: AgentState) -> None:
        self.state = state
        self.last_report = state["report"]
        self._set_busy(False)
        if state["status"] == "awaiting_approval":
            self.status_var.set("计划已生成，等待审批")
            self.btn_execute.config(state="normal")
            self._append_log("[完成] 计划已生成（未审批）。审阅后点击『② 审批并执行』。")
            return
        report = state["report"]
        if report is None:
            self.status_var.set("状态异常：无报告")
            return
        self.status_var.set(f"完成: {report.status}")
        self.btn_execute.config(state="disabled")
        self.btn_export.config(state="normal")
        self._append_log(
            f"[完成] status={report.status}; changed_files={report.changed_files}; "
            f"test_exit_code={report.test_exit_code}"
        )
        audit_lines = "\n".join(
            f"  - {event.tool}: allowed={event.allowed} detail={event.detail}"
            for event in report.audit
        )
        self._append_log(f"[审计] {audit_lines}")

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        self.status_var.set("运行出错")
        self._append_log(f"[错误] {exc}")
        messagebox.showerror("运行出错", str(exc))

    # ---------- helpers ----------

    def _format_updates(self, node_name: str, updates: dict) -> str:
        if node_name == "inspect":
            return f"file_count={updates.get('files', []) and len(updates['files'])}"
        if node_name == "plan":
            plan = updates.get("plan")
            return f"summary={getattr(plan, 'summary', '?')}" if plan is not None else "no plan"
        if node_name == "execute":
            return f"changed_files={updates.get('changed', [])}"
        if node_name == "verify":
            return f"exit_code={updates.get('exit_code')}"
        if node_name == "repair":
            if updates.get("status") == "verification_failed":
                return "no_fix，进入失败路径"
            return f"attempt={updates.get('repair_count')}, summary={getattr(updates.get('plan'), 'summary', '?')}"
        return str(updates)

    def _fill_plan_tree(self, plan) -> None:
        self.plan_tree.delete(*self.plan_tree.get_children())
        summary = getattr(plan, "summary", "")
        test_command = " ".join(plan.test_command) if plan.test_command else "(无)"
        self.plan_tree.insert("", "end", text=summary, values=("", test_command))
        for write in plan.writes:
            self.plan_tree.insert("", "end", text="", values=(write.path, ""))

    def _append_log(self, line: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", line + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > LOG_LIMIT:
            self.log_text.delete("1.0", f"{LOG_LIMIT}.0")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.btn_plan.config(state=state)
        self.btn_execute.config(state="disabled" if busy else "normal")


def main() -> None:
    root = tk.Tk()
    MaintenanceGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
