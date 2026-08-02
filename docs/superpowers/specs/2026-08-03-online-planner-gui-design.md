# 在线 AI Planner + GUI 设计文档

- 日期：2026-08-03
- 状态：已获用户批准（架构总览 / LLM 集成 / GUI 与清理 三节逐一确认）
- 关联需求：
  1. 彻底摆脱离线模型，使用在线完整 AI 服务，参数放 `.env`
  2. 新增 GUI

## 1. 背景与目标

现有项目为纯标准库"审批门控制的确定性仓库维护 Agent"：`RepositoryTools` 提供
MCP 风格工具与安全边界，`RepositoryAgent` 以显式状态机编排六阶段流水线，
`DeterministicPlanner` 为离线占位规划器。

本次改造：

- 用在线 LLM（OpenAI 兼容 API）彻底替代 `DeterministicPlanner`，凭据与模型配置
  从 `.env` 读取。
- 用 LangGraph `StateGraph` 重写编排层（原 `RepositoryAgent` 状态机）。
- 新增 Tkinter 桌面 GUI，实时展示各阶段进度并保留审批门交互。
- 安全层 `RepositoryTools` 原样保留，作为图的工具节点与写入执行节点。

## 2. 关键决策（用户已确认）

| 决策点 | 结论 |
|--------|------|
| 在线 AI 服务 | OpenAI 兼容 API（`base_url` + `api_key` + `model`，兼容 DeepSeek/通义等国内可达端点） |
| GUI 技术栈 | Tkinter 桌面窗口（标准库） |
| 集成方案 | 方案 C：LangGraph/LangChain 框架重写编排层 |
| 安全边界 | `RepositoryTools` 确定性代码原样保留，只重写编排层 |
| 离线代码 | 删除 `RepositoryAgent` 与 `DeterministicPlanner`，不保留兼容层 |
| 审批门实现 | 两次 `graph.stream` 调用 + 入口路由（`plan` 存在时从 execute 入场），不用 checkpoint |

## 3. 架构总览

### 3.1 文件结构

```
仓库维护Agent/
├── __init__.py        # 出口更新：RepositoryTools + build_graph
├── core.py            # 保留 RepositoryTools/ChangePlan/WriteRequest/RunReport/PolicyError/AuditEvent；删除 RepositoryAgent/DeterministicPlanner
├── graph.py           # 新增：LangGraph StateGraph 编排
├── llm_planner.py     # 新增：在线规划/修复（ChatOpenAI + 工具循环）
├── config.py          # 新增：.env 加载与校验
├── gui.py             # 新增：Tkinter 界面
├── cli.py             # 改造：驱动 graph，语义不变
├── .env.example       # 新增：配置模板
├── requirements.txt   # 更新：langgraph, langchain-openai, python-dotenv, pydantic
└── README.md          # 更新
```

### 3.2 LangGraph 状态图（graph.py）

```python
class AgentState(TypedDict):
    task: str
    files: list[str]
    plan: ChangePlan | None     # None ⇒ 走 inspect→plan；非 None ⇒ 从 execute 入场
    approved: bool
    repair_count: int
    changed: list[str]
    exit_code: int | None
    trace: list[dict]
    audit: list[AuditEvent]
    status: str
```

节点与边：

- 入口条件路由：`plan is None` → `inspect`；否则 → `execute`
- `inspect`：调 `tools.list_files`，写 `files` 与 trace
- `plan`：LLM 工具循环生成 `ChangePlan`（见 §4），写 trace
- approve 条件边：`approved=False` → 终止，`status="awaiting_approval"`；否则 `execute`
- `execute`：逐条 `write_file(approved=True)`，更新 `changed` 与 trace
- `verify`：有 `test_command` 则 `tools.run_test`（白名单强制）；无则 `exit_code=None`
- repair 条件边：通过或无测试 → report；失败且 `repair_count < max_repairs` → `repair`；否则终止 `verification_failed`
- `repair`：LLM 生成修复计划，`repair_count += 1`，回 `execute`
- report 节点：组装 `RunReport`

`graph.stream(state)` 逐步产出节点名与状态增量 —— GUI 实时进度与 CLI 阶段打印的数据源。

### 3.3 审批门（两次 stream）

1. 第一次：`approved=False` → 图在 approve 处终止，返回含完整 `plan` 的
   `awaiting_approval` 状态。调用方（GUI/CLI）展示计划。
2. 第二次：把上次状态原样传回并置 `approved=True` → 入口路由跳过
   inspect/plan，从 `execute` 继续，直到 report。

无需 checkpoint；逻辑显式、可复现。

## 4. 在线 LLM 集成

### 4.1 .env 配置（config.py，python-dotenv 加载，环境变量优先）

```ini
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_API_KEY=sk-xxxxxxxx
OPENAI_MODEL=deepseek-chat
OPENAI_TIMEOUT=120
MAX_TOOL_ITERS=6
MAX_REPAIRS=1
```

缺 `OPENAI_API_KEY` 时启动即报错（清晰提示），不延迟到运行中途。

### 4.2 规划节点（plan）：轻量工具循环

不引入 AgentExecutor，手动循环保证行为可控：

```
messages = [system_prompt, user(task + 文件清单)]
for _ in range(MAX_TOOL_ITERS):
    resp = llm.bind_tools([list_files, read_file, search_text]).invoke(messages)
    if resp.tool_calls:
        执行对应 RepositoryTools 只读工具 → tool message 追加 → continue
    else:
        with_structured_output(ChangePlanSchema) 解析 → ChangePlan
```

- `ChangePlanSchema`（pydantic）：`summary` / `writes: list[WriteSchema]` /
  `test_command: list[str] | None`，与现有 `ChangePlan` 一一对应。
- **只暴露只读工具**给 LLM 工具循环；`write_file` 不进工具列表，写入仍由
  execute 节点经审批门统一执行。
- 工具调用照常走 `RepositoryTools._record()`，审计链完整。

### 4.3 修复节点（repair）

结构化输出，输入 `task + 原计划 + 测试输出`，返回修复后 `ChangePlan`；
无法修复返回 `None` → 图终止为 `verification_failed`。

### 4.4 容错

- LLM 输出解析失败重试一次。
- `with_structured_output` 走 tool-calling（DeepSeek/OpenAI 均支持）；
  不支持时降级为纯 JSON 提示 + 剥离 markdown 围栏后解析。

## 5. GUI（gui.py，Tkinter）

### 5.1 布局

```
[仓库路径 Entry] [浏览…]
[任务描述 Text（3行）]
计划预览区（Treeview）：summary + 待写文件 + test_command
[① 生成计划] [② 审批并执行] [③ 导出报告]
状态栏：当前阶段（inspect/plan/execute/…）
运行日志区（Text）：实时追加 trace + audit
```

### 5.2 线程模型

- `graph.stream()` 放后台 `threading.Thread`，避免 UI 冻结（LLM 可能数十秒）。
- 工作线程把 `(node_name, state)` 推入 `queue.Queue`；UI 用
  `root.after(100, poll_queue)` 轮询刷新状态栏与日志区。

### 5.3 审批交互

1. 「① 生成计划」→ 第一次 stream（`approved=False`）→ 展示计划预览，
   启用「② 审批并执行」。
2. 「② 审批并执行」→ 传回上次 state（plan 就绪）`approved=True` →
   日志区实时滚动 execute/verify/repair → 展示最终 `RunReport`。
3. 「③ 导出报告」→ `RunReport` 存 JSON 文件。

## 6. 遗留代码处理

- `core.py`：删除 `RepositoryAgent`、`DeterministicPlanner`、`Planner` Protocol
  （框架接管编排，协议不再需要）。保留 `RepositoryTools`、`ChangePlan`、
  `WriteRequest`、`RunReport`、`PolicyError`、`AuditEvent`。
- `RunReport` 继续作为 CLI/GUI 最终报告载体，由 report 节点组装。
- `cli.py`：改为调用图；无 `--approve` 时输出 `awaiting_approval` 报告，
  语义与现状一致。
- `__init__.py`：导出 `RepositoryTools` + `build_graph`。
- 不保留 `RepositoryAgent`/`DeterministicPlanner` 兼容别名（彻底重写）。
- README 同步更新（架构、.env 配置、GUI 用法）。

## 7. 错误处理

| 场景 | 行为 |
|------|------|
| 缺少 `OPENAI_API_KEY` | 启动即报错退出 |
| base_url 不可达 / 超时 | 节点抛异常，GUI 显示错误，CLI 非零退出 |
| LLM 输出非 JSON | 重试一次，仍失败则终止并标记失败 |
| 测试失败且无法修复 | `status=verification_failed`，保留 `changed_files` 与审计 |
| 路径越界 / 未审批写入 / 非白名单命令 | `PolicyError`，行为与现状一致 |

## 8. 测试

- 单元：`config.py` 加载与缺 key 报错；`llm_planner` 用 mock 的
  `ChatOpenAI` 验证工具循环与 JSON 解析（含降级路径）。
- 图级：`build_graph` 的 approved=False 短路、入口路由（带/不带 plan）、
  repair 循环上限。
- 手工：GUI 两按钮流程 + 后台线程不冻结 UI。

## 9. 实现偏差记录（2026-08-03，实现阶段，经 review 驱动）

- **审批门图内强制**：入口路由对"plan 已存在但 `approved=False`"的状态直接导向 awaiting；`execute_node` 增加 `approved` 防御检查。不再依赖调用方约定。
- **报告状态推导**：`report_node` 按 trace 中是否执行过 execute 推导最终状态，第二次 stream 无需调用方重置 `status`。
- **GUI 新增「验证命令白名单」输入框**（每行一条 argv）；`RepositoryTools.run_test` 在白名单为空时跳过验证并在审计留痕（CLI/GUI 均可不配白名单直接运行）。
- **LLM 解析失败重试一次**（追加 SystemMessage 提示），仍失败才抛 `PlanParseError`。
- 测试以 `smoke_test.py` 承载（11 个离线用例：图路径 + mock LLM 工具循环 + 配置校验）。

