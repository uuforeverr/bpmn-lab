# BPMN Lab

面向自然语言流程描述的 BPMN 生成与评测实验项目。项目的核心研究问题是：当流程控制结构逐渐复杂时，语义分段、增量式构造和多智能体协作能否比一次性生成更稳定地恢复 Task、Gateway 和 Task 间控制流关系。

本文档面向第一次接手项目的开发者或 Agent。开始工作前请先阅读“当前实验口径”和“接手约束”，避免把历史脚本、诊断指标或理想化可视化数据误当成当前论文实验。

## 1. 一分钟了解项目

系统把流程描述转换为统一的 Graph-JSON v2，再生成 BPMN XML：

```text
流程描述
  -> 确定性切句
  -> Planner 语义分段
  -> Generator 按片段生成 Graph Patch
  -> 原子合并候选图
  -> 确定性结构校验
  -> Reviewer / Generator 语义讨论
  -> Arbitration（争议未解决时）
  -> Repair（结构问题的受控编辑）
  -> 提交片段快照
  -> 最终 Graph-JSON
  -> BPMN XML
  -> 自动布局 BPMN DI
```

系统由三个可独立启动的部分组成：

- `backend/`：FastAPI、生成流水线、Graph-JSON、校验、BPMN 转换、评测和实验脚本。
- `frontend/`：React + Vite 实验工作台，展示运行阶段、Graph 和 BPMN。
- `layout-service/`：Node/Express 自动布局服务，为 BPMN XML 补全 BPMN DI。

当前最新实验结果只有一个入口：

```text
backend/results/实验结果归档_20261023/
```

该目录自己的 `README.md` 是结果文件级说明；根目录本文档说明整个系统和数据处理链路。

## 2. 三种实验策略不要混淆

| 策略 | 实际行为 | 是否分段 | 是否经过完整框架 |
| --- | --- | --- | --- |
| `oneshot` | 使用英文 `generator_oneshot.md`，把 description 全文交给 Generator，只调用模型一次，直接输出完整 Graph-JSON | 否 | 否；不使用 Planner、Reviewer、Arbitration、Repair |
| `single_segment` | 把全文作为一个片段进入 Pipeline | 否 | 是；仍执行结构校验、语义审查、争议处理和修复 |
| `semantic` | Planner 按业务语义划分多个片段，Generator 逐段生成并合并 Graph Patch | 是 | 是 |

`fixed_length` 仍是交互系统支持的调试策略，但不是当前论文正式对比的核心策略。

前置实验使用 `oneshot`，用于观察一次性 Generator 的质量随复杂度变化。正式实验比较 `single_segment` 与 `semantic`。因此，`oneshot` 不能与“框架内单片段生成”混称为同一种方法。

## 3. 当前 Graph-JSON v2

Graph 的 Pydantic 定义在 `backend/app/domain.py`。持久化和模型交互均使用具体节点 `type`，不再使用旧的 `kind + eventType/gatewayType` 组合。

### 3.1 节点

- Task：`task`、`userTask`、`serviceTask`、`sendTask`、`receiveTask`、`businessRuleTask`、`manualTask`、`scriptTask`。
- Event：`startEvent`、`endEvent`、`intermediateCatchEvent`、`intermediateThrowEvent`。
- Gateway：`exclusiveGateway`、`parallelGateway`、`inclusiveGateway`。
- Gateway 的 `role` 只允许 `split` 或 `join`。当前模型不暴露 `mixed`。
- 同一控制位置若同时需要汇聚和分叉，应表示为同类型的 `join -> split` 两个 Gateway。
- Task 在最终图中要求恰好一条入边和一条出边；多路径先经过语义适当的 Join，多后继先经过语义适当的 Split。
- `sourceSentences` 保存元素的原文句子依据。

最小示例：

```json
{
  "schemaVersion": "2.0",
  "process": {"id": "process_main", "name": "Order process"},
  "nodes": [
    {"id": "start", "type": "startEvent", "name": "Order received"},
    {"id": "task_check", "type": "task", "name": "Check order"},
    {"id": "end", "type": "endEvent", "name": "Order checked"}
  ],
  "edges": [
    {"id": "flow_1", "source": "start", "target": "task_check"},
    {"id": "flow_2", "source": "task_check", "target": "end"}
  ]
}
```

### 3.2 Patch 与修复

增量 Generator 返回局部 Graph Patch，而不是每轮重写完整图。Patch 合并采用“先删除、后插入/更新”的语义。无效 JSON 或未通过输出格式校验时，系统使用完全相同的原 Prompt 和原数据重新调用。

Repair Agent 同样只操作当前累计图上的 Graph Patch，并按任务使用两份 Prompt：`repair_structure.md` 直接处理确定性结构校验与最终 Woflan 诊断，`repair_semantic.md` 独立审查 Reviewer findings 并修复接受项。不再使用 Repair Plan 或图编辑工具调用链。达到修复上限时保留最后一次修复产物和审计记录。

## 4. 生成流水线

主状态机在 `backend/app/pipeline.py`。关键阶段包括：

```text
INPUT_READY
SENTENCES_PREPARED
SEGMENTS_CREATED
CONTEXT_BUILT
SUBGRAPH_GENERATED
CANDIDATE_APPLIED
STRUCTURE_VALIDATED
SEMANTIC_REVIEWED
SEMANTIC_REPAIRING / SEMANTIC_REPAIR_DECIDED
ARBITRATING
STRUCTURE_REPAIRING
SEGMENT_COMMITTED
FINAL_VALIDATED
BPMN_GENERATED
COMPLETED
```

实现要点：

- 原文先由代码确定性切为带 ID 的句子，Planner 只能划分片段，不能篡改句子。
- Generator 接收当前片段、截至当前片段的完整描述前缀，以及完整已提交 Graph。
- Patch 先在副本上原子应用，候选图通过后才提交。
- Reviewer 只检查流程语义；确定性校验器负责结构错误。
- Repair Agent 可以接受或拒绝 Reviewer finding，但拒绝必须给出文本依据。
- 多轮讨论仍未解决的 finding 交给独立配置的 Arbitration 模型。
- 每次模型调用、重试、token、耗时、修复和审查历史写入运行状态与 SQLite。

## 5. 确定性结构校验与 Soundness

`backend/app/validation.py` 是生成时的确定性结构校验器。它检查：

- ID 合法性和唯一性；
- Edge 端点是否存在；
- Start/End Event 的入出边约束；
- 从 Start 的可达性和是否存在到 End 的路径；
- Task 是否形成隐式分叉、隐式汇聚或缺少连接；
- Split/Join Gateway 的角色和度数；
- Exclusive/Inclusive 的条件流、默认流组合；
- Parallel Split 不允许条件流或默认流；
- Parallel Split 的分支是否存在共同可达的 Parallel Join；
- 最终图是否存在非 End 的开放叶节点。

中间片段允许保留后续可扩展的开放出口，但不允许悬空引用、不可达节点或已经形成的非法控制结构。

`backend/app/process_quality.py::assess_soundness()` 是实验评测，不替代生成时的错误定位。它把 BPMN 转为 Petri 网并通过 PM4Py 判断 workflow-net soundness；转换层会对 Graph-JSON 支持的 Start、End 和 Intermediate Event 做控制流抽象。当前协议名为 `pm4py_workflow_net_soundness_with_event_abstraction_v4`。

项目不把 BPMN validity 作为论文最终指标，但生成过程中仍保留确定性结构校验。

## 6. Prompt 与模型配置

Prompt 必须同步维护中英文版本：

```text
backend/prompts/*.md       # 中文
backend/prompts/en/*.md    # 英文
```

当前 Prompt：

- `planner.md`：语义分段；
- `generator.md`：增量 Graph Patch；
- `generator_oneshot.md`：前置实验的一次完整生成；
- `repair_semantic.md`：Repair Agent 对 Reviewer finding 的判断与最小修订；
- `repair_structure.md`：Repair Agent 对确定性结构问题及终局 Woflan 诊断的直接修复；
- `reviewer.md`：语义一致性检查；
- `arbitrator.md`：未解决争议的裁决。

配置文件：

- `backend/config.yaml`：本机实际配置，不应提交密钥；
- `backend/config.example.yaml`：可提交的模板；
- `backend/.env`：本机环境变量，不应提交；
- `backend/.env.example`：环境变量模板。

当前本机配置使用主模型 `deepseek-flash`，Arbitration 使用 `deepseek-v4-pro`；Repair Agent 保持思考模式并具有更高输出上限。模型名取决于当前代理服务，复现实验时应以冻结配置和原始结果 manifest 为准。

设置密钥：

```powershell
$env:OPENAI_API_KEY='your-key'
```

`prompts.language` 控制整套 Prompt 的语言。正式 PMo 英文数据评测使用英文 Prompt；`compare_strategies.py --prompt-language auto` 会对英文数据自动选择 `en`。

## 7. 当前实验口径

### 7.1 数据选择与复杂度

正式 PMo-31 消融实验的完整样本 ID、复杂度分层和启动前检查见根目录 `PMo31实验数据说明.md`。不要将历史生成批次的全部 50 个流程误作为 PMo-31 正式数据。

正式重分析从 PMo 参考 BPMN 中选择参考 Task 数大于 10 的流程，共 31 个。Task 数只作为入选门槛，复杂度由参考 BPMN 的 Task 控制语境最大嵌套深度决定：

- Low：`taskMaxNestingDepth <= 1`，7 个流程；
- Medium：`taskMaxNestingDepth == 2`，12 个流程；
- High：`taskMaxNestingDepth >= 3`，12 个流程。

复杂度必须从参考 BPMN 计算，不能从生成结果或 PME 得分反推。实现位于 `backend/app/evaluation.py::calculate_task_nesting_complexity()`。

### 7.2 当前论文核心指标

1. **Completion**：每个流程、每种策略运行 3 次；只有 3 次全部到达完成状态时，流程级值才为 1，否则为 0。
2. **Soundness**：只有 3 次均完成且 3 个最终模型均 sound 时，流程级值才为 1，否则为 0。
3. **PMo Task Dice@0.7**：使用 `sentence-transformers/stsb-mpnet-base-v2` 余弦相似度和 0.7 阈值，按 PMo 的贪心语义匹配与频次感知 Dice 计算 Task 名称相似度。
4. **Gateway Dice**：比较六类 Gateway 数量，即 `exclusive/parallel/inclusive x split/join`，不依赖前后 Task 名称。
5. **Conditional Relation P/R/F1**：只在阈值下已经对齐的 Task 对之间比较严格先后、互斥、并发/包容与循环等控制流关系，用于观察“Task 已识别后，关系是否建模正确”。
6. **Global Relation P/R/F1**：在完整生成流程与参考流程上比较可分类的 Task 关系；未匹配 Task 所涉及的关系继续形成 FP 或 FN，因此同时反映 Task 覆盖和控制流关系恢复。
7. **端到端 token**：记录 input、output、reasoning、cached input 和 provider 返回的 total。cached input 是 input 的子集，reasoning 是 output 的子集，不得再次累加到 total。

Conditional 和 Global Relation 均保留 Task 对齐阈值 0.6、0.7、0.8 的敏感性结果，论文主展示使用既定阈值版本。

### 7.3 不再作为当前论文结果的指标

以下字段可能仍存在于兼容代码或历史 JSON 中，但不要加入当前论文主表或主图：

- Task Semantic Alignment Score（TSA）；
- Task Precision/Recall/F1@0.6/0.7/0.8；
- 旧 PM4Py Conformance Fitness、Precision、Behavioral F1；
- BPMN validity；
- 旧 PME Overall、GED/RGED 等探索性指标。

不要因为旧脚本仍能输出这些字段，就自动恢复为论文指标。

## 8. 实验数据与处理代码

### 8.1 最新归档

```text
backend/results/实验结果归档_20261023/
  README.md
  01_前置实验/
    数据/
    结果图/
  02_真实实验/
    数据/
    结果图/
  03_优化实验/
    数据/
    结果图/
```

含义如下：

- `01_前置实验`：50 个流程 x 3 次 `oneshot` 的原始 150 次调用，以及筛选 Task 数大于 10 后的 31 流程重分析。
- `02_真实实验`：31 个流程 x 2 种策略 x 3 次的正式观测数据。5 个 `batch*.json` 是原始批次；`depth31_metrics.json` 是当前统计和绘图的主输入。
- `03_优化实验`：在真实数据基础上生成的理想化可视化场景。其 manifest 明确标记 `SYNTHETIC_IDEALIZED_VISUALIZATION_ONLY_V1`、`notForEmpiricalReporting=true`，不能冒充模型真实调用结果。

### 8.2 当前离线处理主链路

从项目根目录执行：

```powershell
$env:PYTHONPATH=(Resolve-Path 'backend').Path

# 1. 从前置原始 JSON、正式 5 批 JSON 和 Soundness 重算文件，
#    生成逐次、逐流程、分层及配对差值数据。
backend\.venv\Scripts\python.exe backend\scripts\recompute_depth31_metrics.py

# 2. 基于真实流程级数据构造有明确标签的理想化可视化数据。
backend\.venv\Scripts\python.exe backend\scripts\build_idealized_visualization_dataset.py

# 3. 绘制前置实验和真实实验，并同步导出每张图的 source/summary CSV。
backend\.venv\Scripts\python.exe backend\scripts\plot_depth31_experiment.py

# 4. 仅绘制理想化场景预览图。
backend\.venv\Scripts\python.exe backend\scripts\plot_idealized_visualization_preview.py
```

以上四步都是离线处理，不调用大模型。

### 8.3 每个当前脚本负责什么

| 文件 | 输入 | 输出 / 用途 |
| --- | --- | --- |
| `backend/scripts/compare_strategies.py` | PMo 或合成数据集、策略、样本、重复次数 | 发起真实模型调用；原子写入运行 JSON，并可保存每次 BPMN/Graph-JSON；支持 `--resume` |
| `backend/scripts/evaluate_pmo.py` | 少量 PMo 样本 | 小批量 Pipeline 冒烟测试，不是当前正式统计入口 |
| `backend/scripts/recompute_depth31_metrics.py` | 前置 150 次原始 JSON、正式 5 批 JSON、Soundness v4 重算结果 | `depth31_metrics.json`、run/flow/depth/paired CSV 和 manifest；当前统计核心 |
| `backend/scripts/build_idealized_visualization_dataset.py` | 真实 `depth31_metrics.json` 和正式批次中的分母 | 带合成标签的优化场景 flow、paired、tier 数据 |
| `backend/scripts/plot_depth31_experiment.py` | 当前 `depth31_metrics.json` | 前置和真实实验图；同时输出精确作图源 CSV 与汇总 CSV |
| `backend/scripts/plot_idealized_visualization_preview.py` | 优化场景 manifest 和 paired CSV | 只生成理想化预览图，并校验合成标签 |
| `backend/scripts/recompute_soundness_v4.py` | 历史未归档目录结构中的正式批次 | Soundness v4 离线重算；当前脚本仍硬编码旧 `pmo_full_50_seed20261017` 路径，直接运行前必须改为归档路径 |
| `backend/scripts/diagnose_global_relation.py` | 历史重分析目录 | 诊断 Global Relation 低分与 Task 覆盖的关系；路径已过期，属于诊断脚本 |

`analyze_formal_experiment.py`、`summarize_formal_experiment.py`、`plot_formal_experiment.py` 是旧的 S/M/L/XL 统计链路，且硬编码了迁移前的结果目录。当前 low/medium/high 嵌套深度实验不要调用它们。

### 8.4 关键数据文件

- 前置原始调用：`01_前置实验/数据/oneshot_raw_150_runs.json`。
- 前置逐次指标：`01_前置实验/数据/oneshot_run_metrics.csv`。
- 前置逐流程均值：`01_前置实验/数据/oneshot_flow_means.csv`。
- 正式原始调用：`02_真实实验/数据/batch01.json` 至 `batch05.json`。
- 正式主统计：`02_真实实验/数据/depth31_metrics.json`。
- 正式逐次指标：`02_真实实验/数据/formal_run_metrics.csv`。
- 正式逐流程均值：`02_真实实验/数据/formal_flow_means.csv`。
- 正式逐流程差值：`02_真实实验/数据/formal_paired_differences.csv`，方向固定为 `semantic - single_segment`。
- 每张图的可追溯输入：同目录下对应的 `figure*_source.csv` 和 `figure*_summary.csv`。

Completion 和 Soundness 的流程级差值只能为 `-1`、`0`、`1`；如果看到 `0.33`、`0.67` 等值被用于流程级主比较，说明误用了运行级平均值。

## 9. 发起新的生成实验

安装评测依赖：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev,evaluation]'
cd ..
```

项目迁移过路径，若已有 `.venv` 出现解释器路径或中文路径异常，优先删除后重新创建虚拟环境，不要修改实验代码来迁就损坏的 venv。

示例：对三个样本、三种策略各执行一次。正式实验前先用少量样本确认模型、Prompt、布局服务和本地嵌入模型均正常。

```powershell
$env:PYTHONPATH=(Resolve-Path 'backend').Path
$env:OPENAI_API_KEY='your-key'

backend\.venv\Scripts\python.exe -u backend\scripts\compare_strategies.py `
  --dataset 'D:\path\to\pmo-dataset' `
  --samples 02 03 05 `
  --strategies oneshot single_segment semantic `
  --repetitions 1 `
  --seed 20261012 `
  --prompt-language auto `
  --embedding-model 'sentence-transformers/stsb-mpnet-base-v2' `
  --output 'backend\results\smoke-test.json' `
  --bpmn-output-dir 'backend\results\smoke-test-artifacts'
```

续跑：

```powershell
# 继续缺失的区组，不重复已经落盘的运行
... --resume

# 仅重试 API/网络等基础设施失败；普通模型失败不自动重跑
... --resume --retry-infrastructure-failures
```

运行 JSON 是 checkpoint。脚本会先保存单次 BPMN/Graph-JSON，再原子替换汇总 JSON，避免长批次中途失败导致全部数据丢失。

PMo 数据集需要包含：

```text
pmo-dataset/
  descriptions/*.txt
  pme/*.json
  bpmn/*.bpmn
```

仓库内合成集 `datasets/pmo_synthetic_v1` 只用于工具验证；当前正式实验没有使用合成流程。

## 10. 启动应用

### 10.1 自动布局服务

```powershell
cd layout-service
pnpm install
pnpm start
```

健康检查：`http://127.0.0.1:3001/health`。

### 10.2 后端

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- 健康检查：`http://127.0.0.1:8000/api/health`
- API 文档：`http://127.0.0.1:8000/docs`
- 运行 API：创建、查询、单步执行和继续执行均位于 `/api/runs`。

### 10.3 前端

```powershell
cd frontend
pnpm install
pnpm dev
```

打开 `http://127.0.0.1:5173`。Vite 会把 `/api` 代理到后端。

## 11. 测试

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q

cd ..\frontend
pnpm build

cd ..\layout-service
pnpm test
```

后端测试覆盖配置、Graph/BPMN、Pipeline、结构校验、布局、API、评测指标、Soundness 和当前 depth31 报告口径。修改指标公式、严格三次聚合规则或 Graph 结构约束时，必须同步补测试。

## 12. 代码导航

```text
bpmn-lab/
  backend/
    app/
      domain.py          Graph-JSON v2 数据模型
      pipeline.py        多阶段生成状态机
      validation.py      确定性结构校验
      editor.py          Graph 编辑与 Patch 应用
      tools.py           Repair 可调用编辑工具
      bpmn.py            Graph-JSON -> BPMN XML
      process_quality.py 当前 Task/Gateway/关系/Soundness 指标
      evaluation.py      数据集适配、PMo Dice、复杂度与聚合
      llm.py             OpenAI-compatible 模型调用与 token 记录
      store.py           SQLite 运行持久化
      main.py            FastAPI 路由
    prompts/             中文 Prompt
    prompts/en/          英文 Prompt
    scripts/             生成实验、离线统计、诊断和绘图
    tests/               后端测试
    results/             当前精简实验归档
  frontend/src/          React 实验工作台
  layout-service/        BPMN 自动布局服务
  datasets/              合成验证集，不是正式实验数据
  analysis/              数据集分析、论文写入和历史 QA 工具
  figures/               方法示意图
```

`analysis/` 中存在大量一次性论文编辑、渲染截图和历史诊断脚本。除非任务明确涉及论文写入或历史复核，不要把它当成正式实验执行入口。

## 13. 接手约束

1. 修改 Prompt 前先说明计划；获准后同时更新 `backend/prompts/` 和 `backend/prompts/en/`。
2. 不要把 `oneshot`、`single_segment` 和 `semantic` 混用或重命名。
3. 不要从生成模型或 PME 得分推断复杂度；复杂度来自参考 BPMN。
4. 不要把 TSA、Task P/R/F1 或旧 conformance 指标重新加入当前论文结果。
5. 不要把 `03_优化实验` 当作真实调用数据；读取 manifest 标签后再处理。
6. 不要覆盖原始批次 JSON。新的离线统计应写新文件，并在 manifest 中记录输入、算法版本和是否调用模型。
7. 严格 Completion/Soundness 以“3 次全部通过”为 1；运行级均值只用于审计。
8. Task 名称尽量复用原文动作动词和业务对象，只做必要语法规范化；不要用宽泛总结词替换具体动作。
9. Task 不允许多入或多出；Gateway 只用 `split/join`。多入多出控制位置拆成同类型 `join -> split`。
10. 所有实验修改都应保留模型配置、Prompt 版本、随机种子、样本清单、调用明细和 token 用量，以保证结果可追溯。

## 14. 当前已知注意事项

- 最新结果已迁移到中文归档目录，但部分旧脚本仍硬编码迁移前的 `pmo_full_50_seed20261017`、`depth31_reanalysis_20261020` 等路径，运行前必须先检查常量。
- manifest 中可能保留实验执行时的绝对数据集路径，这是审计信息，不表示该路径在其他机器上仍存在。
- `sentence-transformers/stsb-mpnet-base-v2` 是当前 PMo-compatible Task Dice 的冻结嵌入模型；更换模型只能作为额外诊断，不能与现有结果直接混合。
- 收到模型响应前发生的网络失败没有 provider token 数据；不得人为估算并填入 token。
- Git 工作区包含论文编辑、实验归档和其他未提交改动。修改前先看 `git status`，不要清理、回滚或覆盖与当前任务无关的文件。
