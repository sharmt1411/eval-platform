# 通用大模型测试管理与数据飞轮平台（Eval & Data Flywheel Platform）

前后端分离、配置驱动的通用大模型评测与对话机器人数据飞轮平台，集成了**统一模型接入底座**、**常规数据集横向对比评测**、**双 Agent 模拟对弈**、**Mermaid 拓扑 + FAQ 双轨白盒质检**、**声明式微创数据修复**、**渐进式切题与复测动态降频**以及 **Agent CLI (`evalctl`)** 能力。

---

## 核心能力

1. **统一模型接入层 (`backend/app/adapters/`)**
   - 支持 OpenAI / vLLM / SGLang / DeepSeek 兼容接口、Harmony 双通道格式、Google Vertex AI（ADC 免密鉴权 + 思考协议自适应探测）以及离线 `MockAdapter`。
   - 对内统一为 `UnifiedChatRequest` / `UnifiedChatResponse`，自动分离 `thinking` 思考链与正文 `content`，支持多配置克隆对比。
2. **常规数据集评测与动态降频抽样 (`backend/app/engine/benchmark_runner.py`, `benchmark_sampler.py`)**
   - 单数据集 × 多模型/多推理配置矩阵并发评测，统计 Latency、TTFT、TPS 与思考 Token 占比。
   - 支持正向必配/反向禁含正则约束 (`regex_constraints`) 与 LLM-as-a-Judge 主观 Rubric 评分，严格隔离底层 API 异常 (`API_ERROR`)。
   - 基于 SQLite 账本 (`benchmark_run_ledgers`) 实现确定性阶梯降频抽样、版本隔离与双口径通过率统计。
3. **规则中心、提示词中心与模拟对弈 (`backend/app/engine/simulator.py`, `topology.py`, `prompt_registry.py`)**
   - 通用规则配置 (`UniversalRuleSchema`)：支持 Mermaid 状态机流程图、节点检查点、全局红线、FAQ 知识库与两极化客户人设剧本。
   - 支持 SSE 实时流式双 Agent 模拟对弈与人工自由对话调试；支持全平台内置提示词模板管理与在线组装实测。
4. **数据飞轮全链路闭环 (`backend/app/services/flywheel_service.py`)**
   - **双轨白盒质检 (`judge.py`, `faq_retriever.py`)**：Mermaid 轨迹抽取与图论拓扑校验 ∥ BM25 + 向量 RRF 混合检索 FAQ 核查。
   - **声明式微创修复 (`remediator.py`)**：成对剪枝 (`prune`) + 仅改坐席话术替换 (`replace`)，强制冻结客户原话 (`enforce_user_immutability`)。
   - **渐进式切题与防泄漏导出 (`slicer.py`)**：基于不可变原始轮次序号生成累积纠正上文切片，支持双向回写与 SFT 训练集 / 留出评测集隔离导出。
5. **Agent CLI (`cli/` / `evalctl`)**
   - 覆盖平台 8 大命令组、59 条子命令，支持 HTTP/SSE 直连与 `--in-process` ASGI 直调，内置权限 Profile 与风险分级策略 (`cli/policy.default.yaml`)。

---

## 目录结构

```text
eval-platform/
├── backend/                # FastAPI 服务、适配器层、核心评测与飞轮引擎、pytest 回归测试
├── cli/                    # evalctl 命令行工具与权限策略引擎
├── frontend/               # React + TypeScript + Vite + Ant Design + ECharts/Mermaid/Monaco 工作台
├── docs/                   # 架构设计文档与系统地图 (docs/map/)
├── data/                   # 本地数据资产目录（规则、数据集、运行产物、SQLite 数据库，已默认 Git 忽略）
├── AGENTS.md               # 导览地图与工程规范
├── lessons.md              # 工程经验记录
└── pyproject.toml          # CLI 与包构建配置
```

---

## 快速开始

### 1. 环境准备

- **Python**: `>= 3.10`
- **Node.js**: `>= 18`（前端构建与开发服务器）

安装后端与 CLI 依赖：

```bash
pip install -r backend/requirements.txt
pip install -e .
```

### 2. 环境变量配置（按需）

| 变量名 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `SERVER_HOST` | 后端监听地址 | `127.0.0.1` |
| `SERVER_PORT` | 后端监听端口 | `9021` |
| `DATABASE_URL` | 数据库连接串 | `sqlite:///.../data/eval_platform.db` |
| `VERTEX_PROJECT` | Google Cloud Vertex AI Project ID（使用 Vertex 时填写） | `your-gcp-project-id` |
| `VERTEX_LOCATION` | Vertex AI 区域 | `global` |

> 使用 Vertex AI 模型时，请先在本机完成 ADC 鉴权（`gcloud auth application-default login`）并设置 `VERTEX_PROJECT` 环境变量。

### 3. 启动后端服务

```bash
uvicorn backend.app.main:app --host 127.0.0.1 --port 9021 --reload
```

### 4. 启动前端工作台

```bash
cd frontend
npm install
npm run dev
```

前端默认通过 Vite 代理 `/api` 至 `127.0.0.1:9021`。

### 5. 使用 CLI (`evalctl`)

```bash
# 查看平台健康状态与可用命令
evalctl system health
python -m cli --help

# 查看权限策略 Profile
python -m cli system policy
```

### 6. 运行自动化测试与系统地图校验

```bash
# 运行全部后端与 CLI 回归测试
python -m pytest -v

# 校验前后端系统地图与代码事实对齐
python docs/map/build.py --strict-facts

# 前端类型检查与构建
cd frontend && npm run build
```

---

## 数据安全说明

本地 `data/rules/*.json`、`data/datasets/*.jsonl`、`data/runs/*.json`、`data/runs/*.md` 以及 `*.db` / `.env` 已配置在 `.gitignore` 中，避免将私有业务规则、SOP 流程图、测试数据或本地数据库提交至代码仓库。
