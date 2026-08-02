# 任务Agent

面向本地仓库的**受控任务执行 Agent**：给定仓库和一句自然语言任务（修复 bug、补充测试、分析日志、重构代码），Agent 自主阅读代码、生成变更方案，经**人工审批**后落盘修改文件，并自动运行测试验证改动、失败时自修复重试。

可靠性由**确定性架构**而非模型自觉保证：变更方案与真实写入分离（审批门）、仓库路径隔离、命令白名单、全链路审计、有界自修复。

## 核心能力

- **在线规划**：`ChatOpenAI` 调用 OpenAI 兼容端点（DeepSeek / 通义 / OpenAI 等），通过只读工具循环观察仓库后提交结构化变更计划；测试失败时自动修复（上限可配）
- **LangGraph 编排**：六阶段流水线 `inspect → plan → approve → execute → verify → repair` 用 `StateGraph` 表达，`graph.stream()` 提供逐阶段事件流
- **审批门**：所有写入需显式 `--approve`（CLI）或 GUI 中先「① 生成计划」再「② 审批并执行」
- **路径越界防护**：`../` 越权路径被拒绝
- **命令白名单**：测试命令需完全匹配白名单，`shell=False` 防注入
- **审计日志**：所有工具调用（允许和拒绝，含 LLM 的每次观察）均记录
- **Tkinter GUI**：桌面界面，实时显示阶段进度与审计日志
- **配置外置**：API 凭据与参数全部在 `.env`

## 能力边界

**能做**：文本文件的新增/覆盖（代码重构、修 bug、补测试、写配置与文档）；静态分析（文件列表、全文搜索、代码评审）；配置白名单后的动态验证（运行测试命令判断是否合格）。

**不能做**：删除/重命名/移动文件（仅有 `write_file`，无删除类工具）；二进制文件（读写均为 UTF-8 文本，搜索自动跳过）；执行任意命令（仅白名单内测试命令，30 秒超时）；git 操作与外部网络访问（除 LLM API）。

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

所有在线 API 凭据与运行参数都通过仓库根目录的 `.env` 文件配置（也可用同名**系统环境变量**覆盖），由 `config.py::load_settings` 在启动时加载并校验，无需修改任何源码。

### 1. 创建 `.env`

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

编辑 `.env`，至少填入必填项（见下）。程序默认自动加载仓库根目录下的 `.env`；CLI 可用 `--env <path>` 指定其他路径；GUI 固定读取当前目录的 `.env`。

### 2. 必填变量

| 变量 | 说明 | 默认 |
|------|------|------|
| `OPENAI_BASE_URL` | OpenAI 兼容 API 地址（末尾多余的 `/` 会被去掉） | `https://api.deepseek.com/v1` |
| `OPENAI_API_KEY` | API 密钥（**必填**，缺失/为空时启动即报错） | — |
| `OPENAI_MODEL` | 模型名 | `deepseek-chat` |

示例（DeepSeek）：

```ini
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_API_KEY=sk-your-key-here
OPENAI_MODEL=deepseek-chat
```

> 兼容服务：DeepSeek / 通义千问 / OpenAI / 硅基流动 等，只需把 `OPENAI_BASE_URL` 换成对应端点、`OPENAI_MODEL` 换成对应模型名即可。

### 3. 可选调优参数

| 变量 | 说明 | 默认 |
|------|------|------|
| `OPENAI_TIMEOUT` | 单次 LLM 请求超时（秒） | `120` |
| `MAX_TOOL_ITERS` | 单轮规划内 LLM 最多调用工具的轮数 | `6` |
| `MAX_REPAIRS` | 测试失败后自动修复上限 | `1` |

数值型参数必须为整数，解析失败会抛 `ConfigError` 并中断启动。

### 4. 加载优先级与校验

- 优先级：**系统环境变量 > `.env` 文件 > 内置默认值**（`config.py::load_settings` 实际实现；不存在逐变量的命令行覆盖，CLI 的 `--env` 仅用于指定 `.env` 文件路径）。
- `OPENAI_API_KEY` 缺失或为空时启动即报错：CLI 打印 usage error 退出，GUI 弹窗提示「配置错误」。

### 5. 安全提示

- `.env` 已被 `.gitignore` 排除，**不要**提交到版本库，也不要截图/共享/录制（内含真实凭据）。
- 若 `.env` 中出现了真实格式的 key 且不确定是否仍在使用，请尽快轮换该凭据，本地只保留占位符。

## 架构

```
任务Agent/
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
4. 「③ 导出报告」：将 `RunReport` 存为 Markdown 报告

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
