# 仓库维护Agent

审批门控制的仓库维护 Agent：**在线 LLM（OpenAI 兼容 API）规划 + LangGraph 编排 + Tkinter GUI**。

## 核心能力

- **在线规划**：`ChatOpenAI` 调用 OpenAI 兼容端点（DeepSeek / 通义 / OpenAI 等），通过只读工具循环观察仓库后提交结构化变更计划；测试失败时自动修复（上限可配）
- **LangGraph 编排**：六阶段流水线 `inspect → plan → approve → execute → verify → repair` 用 `StateGraph` 表达，`graph.stream()` 提供逐阶段事件流
- **审批门**：所有写入需显式 `--approve`（CLI）或 GUI 中先「① 生成计划」再「② 审批并执行」
- **路径越界防护**：`../` 越权路径被拒绝
- **命令白名单**：测试命令需完全匹配白名单，`shell=False` 防注入
- **审计日志**：所有工具调用（允许和拒绝，含 LLM 的每次观察）均记录
- **Tkinter GUI**：桌面界面，实时显示阶段进度与审计日志
- **配置外置**：API 凭据与参数全部在 `.env`

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置在线 AI（复制模板并填入真实 key）
cp .env.example .env        # Windows: copy .env.example .env
# 编辑 .env：OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL

# 3a. 命令行（仅生成计划，不写入）
python cli.py "检查项目结构" --repo ./my-repo

# 3b. 命令行（审批并执行，配置测试白名单）
python cli.py "重构 utils 模块" --repo ./my-repo --approve \
  --allow-command "python -m pytest"

# 3c. GUI
python gui.py
```

CLI 输出 JSON 报告，包含 `status`、`plan`、`changed_files`、`trace`、`audit`。

## 配置（.env）

| 变量 | 说明 | 默认 |
|------|------|------|
| `OPENAI_BASE_URL` | OpenAI 兼容 API 地址 | `https://api.deepseek.com/v1` |
| `OPENAI_API_KEY` | API 密钥（必填） | — |
| `OPENAI_MODEL` | 模型名 | `deepseek-chat` |
| `OPENAI_TIMEOUT` | 单次 LLM 请求超时（秒） | `120` |
| `MAX_TOOL_ITERS` | 单轮规划内 LLM 最多工具调用轮数 | `6` |
| `MAX_REPAIRS` | 测试失败后自动修复上限 | `1` |

真实环境变量优先级高于 `.env` 文件。

## 架构

```
仓库维护Agent/
├── core.py          # 安全层：RepositoryTools（路径防护/审批/白名单/审计）+ 数据类
├── graph.py         # 编排层：LangGraph StateGraph（六阶段流水线）
├── llm_planner.py   # 智能层：在线规划/修复（工具循环 + 结构化输出）
├── config.py        # 配置层：.env 加载与校验
├── gui.py           # Tkinter 桌面界面
├── cli.py           # 命令行入口
├── .env.example     # 配置模板
└── requirements.txt
```

### 六阶段流水线（LangGraph）

```
entry ──plan为None?──> inspect → plan ──approved?──> execute → verify
   │                     ▲                          │        │
   └──plan已存在─────────┘                     repair  ←──失败且可修复
                                                          │
                                          awaiting_approval / completed / verification_failed
```

- **审批门 = 两次 stream**：`approved=False` 跑到 `awaiting_approval` 返回含计划的完整状态；批准后把该状态（带 plan）传回，入口路由跳过 inspect/plan 直接从 execute 执行
- **安全边界不变**：LLM 工具循环只暴露只读工具（`list_files`/`read_file`/`search_text`），写入一律经审批门由 `execute` 节点执行；命令执行受白名单 + `shell=False` + 30 秒超时约束
- **审计链完整**：LLM 的每次观察动作也记录在 `audit` 中

### GUI 使用

1. 填仓库路径（可浏览选择）与任务描述
2. 点「① 生成计划」：LLM 观察仓库并生成计划，展示在计划预览区（含验证命令）
3. 审阅后点「② 审批并执行」：实时滚动执行/验证/修复各阶段与审计日志
4. 「③ 导出报告」：将 `RunReport` 存为 JSON

后台线程跑图，UI 经队列轮询刷新，界面不冻结。

## 安全边界

| 层级 | 机制 |
|------|------|
| 路径解析 | `Path.resolve()` 防 `../` 越界 |
| 写入审批 | `--approve` / GUI 两按钮审批门控 |
| LLM 工具面 | 仅只读工具；写入不进工具列表 |
| 命令执行 | 白名单匹配 + `shell=False` + 30 秒超时；白名单为空时跳过验证（审计留痕） |
| 审计 | 所有操作（含拒绝与 LLM 观察）记录到 `audit` |
| 凭据 | API key 仅存 `.env`，`.env` 已被 `.gitignore` 排除 |
