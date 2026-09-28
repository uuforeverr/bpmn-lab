# BPMN Incremental Generation Lab

用于演示和实验“语义分段 + 增量 Graph-JSON + 多智能体审查”方法的前后端分离系统。

## 已实现能力

- LLM 前置语义消解、语义分段、增量生成、局部修订和语义审查。
- `semantic`、`fixed_length`、`single_segment` 三种分段策略。
- Task、开始/结束/中间事件，Exclusive、Parallel、Inclusive Gateway。
- 中间事件支持 `none`、`message`、`timer`；结束事件允许多个。
- Semantic Resolver 输出会进行句子完整性、唯一 ID、顺序和原文不可篡改校验；失败时把结构化问题反馈给模型重生成。
- 类型化节点工具 `add_task`、`add_event`、`add_gateway`，以及边编辑、局部修订工具和纯 Task/Event 串行宏 `add_linear_sequence`。
- Graph-JSON 副本执行、确定性校验、最多三次局部 repair、提交快照。
- 增量上下文从最早开放叶子所属片段回溯至当前片段，不单独注入 Gateway 列表。
- Graph-JSON 不使用 `pairId`；终局通过路径可达性检查每个 Parallel Split 是否具有所有分支共同可达的 Parallel Join。
- 每个已提交语义片段的 BPMN XML 和前端 BPMN 图展示。
- 独立 Node/Express BPMN 自动布局服务，为无 DI 的语义 XML 生成完整 BPMN DI。
- SQLite 保存运行状态、阶段事件、Graph 快照、调用用量与耗时。
- 单步执行、继续全部、长调用阶段轮询、语义分段展示、Graph-JSON/BPMN 下载。

## 配置模型

编辑 `backend/config.yaml`：

```yaml
llm:
  base_url: https://api.openai.com/v1
  api_key: ${OPENAI_API_KEY}
  model: your-model-name
  thinking: disabled
  thinking_parameter: thinking
  thinking_enabled_value: {type: enabled}
  thinking_disabled_value: {type: disabled}
  thinking_by_agent:
    semantic_resolver: enabled
    planner: disabled
    generator: disabled
    reviewer: disabled
    repair: disabled

layout:
  enabled: true
  base_url: http://127.0.0.1:3001
```

在 PowerShell 中设置密钥：

```powershell
$env:OPENAI_API_KEY='your-key'
```

系统只调用 OpenAI Chat Completions。切换 OpenAI 兼容服务时修改 `base_url`、`api_key` 和 `model` 即可；如果服务的兼容能力不同，还可通过 `temperature: null`、`max_tokens_parameter`、`json_response_format`、`parallel_tool_calls: null`、`tool_choice: null` 控制是否发送相应字段。`extra_body` 与 `extra_body_by_agent` 可传递其他供应商扩展参数。

所有 Agent 使用同一个模型，但可用 `thinking_by_agent` 为每个阶段独立启停思考模式；未单独配置的阶段继承 `thinking`。`thinking_parameter` 和两种 `thinking_*_value` 用于适配供应商协议：例如 DeepSeek 风格使用 `thinking: {type: enabled}`，布尔开关风格可改为 `thinking_parameter: enable_thinking`、值设为 `true/false`；不支持思考参数时将 `thinking_parameter` 设为 `null`。Prompt 位于 `backend/prompts/`，不会在前端展示。

终局结构校验会从所有开始事件遍历可达节点。每个没有后继的可达叶子都必须是 `eventType=end` 的 End Event；否则返回 `OPEN_NODE`，其中包含具体节点、来源句子以及连接 End Event 的修复提示。中间增量快照不执行该终局叶子规则。

## 启动自动布局服务

先在一个终端中启动布局服务：

```powershell
cd layout-service
pnpm install
pnpm start
```

健康检查：`http://127.0.0.1:3001/health`。该服务接收不含布局信息的 BPMN XML，并用 `bpmn-auto-layout` 补全 `BPMNDiagram`、节点坐标和连线路径。

## 启动后端

再打开第二个终端：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

API 文档：`http://127.0.0.1:8000/docs`

## 启动前端

最后打开第三个终端：

```powershell
cd frontend
pnpm install
pnpm dev
```

打开 `http://127.0.0.1:5173`。开发服务器会把 `/api` 代理到后端。

## 验证

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q

cd ..\frontend
pnpm build

cd ..\layout-service
pnpm test
```

使用 PMo 数据集做小批量端到端验证（从 `backend` 目录执行）：

```powershell
.\.venv\Scripts\python.exe -u scripts\evaluate_pmo.py `
  --dataset 'D:\path\to\pmo-dataset' `
  --samples 54 53 50 `
  --strategy semantic `
  --output results\pmo-small-batch.json
```

报告包含每个样例的完成状态、耗时、分段方案、LLM 调用与重试、局部修订次数、生成/标准元素计数、Review 警告和最终 Graph-JSON。Agent 返回空 JSON 正文或空工具调用时，流水线会按 `pipeline.max_agent_retries` 做有限重试；确定性编辑或终局校验问题仍进入局部 repair，不会静默跳过。

统计 PMo 55 个 ground-truth 流程的元素数和初始复杂度分档：

```powershell
.\analysis\summarize_pmo.ps1 -DatasetRoot 'D:\研究生\组会汇报\2026-7\pmo-dataset'
```

结果写入 `analysis/pmo-complexity.csv` 和 `analysis/pmo-complexity.md`。默认分数为 `tasks + 2 × gateways`，阈值按该数据集近似三等分：低 `≤22`、中 `23–32`、高 `≥33`。

未提供真实 API key 时，健康检查、创建实验、切句和本地核心测试可以运行；进入 LLM 阶段时会给出缺少模型配置的明确错误。

## 目录

```text
bpmn-lab/
  backend/
    app/           Graph、校验器、BPMN 转换、流水线与 API
    prompts/       各阶段 Prompt
    tests/         核心与 API 测试
    config.yaml    模型与流水线配置
  frontend/
    src/           React 实验工作台
  layout-service/  Node/Express BPMN 自动布局服务
  analysis/        PMo 数据集统计脚本与复杂度报告
```
