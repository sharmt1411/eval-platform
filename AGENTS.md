# AGENTS.md — 项目全局导览地图与最高行动准则

> **⚠️ 最高强制指令（MANDATORY OPERATIONAL MANDATE）**
> 
> 1. **先读后动**：任何 AI Agent 在本工作区执行任何分析、设计、编码、测试或数据操作前，**必须首先阅读本文件（`AGENTS.md`）与经验库（`lessons.md`）**，严格按照本文件规定的导航路径与架构约束执行。
> 2. **变更必更图（Sync-on-Change）**：本文件是整个项目的唯一权威导览地图（Single Source of Truth）。**任何操作只要涉及文件/目录增删、模块职责变更、数据/配置 Schema 修改，必须在操作完成的同时立即更新本文件的【文件树】与【资料地图】**；任何排障收获或踩坑教训，必须立即追加至 `lessons.md`。严禁出现代码已改而 `AGENTS.md` 滞后的情况！
> 3. **系统地图同步（Map-on-Change）**：用户不逐行读代码，靠 `docs/map/` 的系统地图掌握项目。**任何代码改动都必须按【第五节】同步更新系统地图并生成变更图；结构性改动必须先画改动方案图、经用户确认后再编码。** 分工：本文件第二、三节回答“东西在哪”，系统地图回答“怎么连、怎么流转、这次改了什么”。

---

## 一、 项目概况与当前所处阶段

- **项目名称**：通用大模型测试管理与数据飞轮平台（Eval & Data Flywheel Platform）
- **核心能力**：
  1. **统一模型接入层 (`backend/app/adapters/`)**：基于 Adapter 模式接入 OpenAI Completion/Chat 兼容模型（支持 vLLM/SGLang、DeepSeek/Qwen、Harmony、Vertex AI 及离线 Mock），对内统一为标准 `UnifiedChatRequest` / `UnifiedChatResponse`，自动映射思考开关（`thinking_enabled` / `thinking_level`）并剥离思考链与正文；底层统一配置 5 次指数退避重试。
  2. **模块一：常规数据集评测 (`backend/app/engine/benchmark_runner.py`)**：单数据集 × 多组（模型 + 参数配置）横向对比，覆盖工程性能（Latency/TTFT/TPS/思考Token占比）、客观规则算子（正反向正则/必含/违禁词/JSON合法性）、LLM 裁判算子及单样本 Side-by-Side 对比；严格将底层 API 异常与业务规则错误隔离统计，支持任务级联删除与样本级步进落盘。
  3. **模块二：微调模型验证与数据飞轮 (`backend/app/services/flywheel_service.py`)**：基于通用规则配置（Mermaid 流程图状态机 + 节点检查点 + 红线规则 + 客户人设），驱动 AI 模拟对话测试，并执行 **“对话记录生成 → 白盒质检 → 缺陷 AI 修复与二次质检 / 顽固样本丢弃 → 人工复核 → 导出标准 `messages` 格式新数据集”** 完整闭环。
- **当前阶段**：**前后端全栈全链路贯通阶段（已完成后端核心评测与飞轮引擎、RESTful API 及现代响应式 Web 工作台系统，实现 100% 契约闭环）**（已完成 `backend/` 下架构解耦、适配器层、规则引擎、评测执行器、飞轮服务、CLI 工具、FastAPI REST API 及 SQLite 持久化元数据库）。

---

## 二、 全局文件树（File Tree）

### 2.1 当前真实物理文件树（Current Physical Tree）

> 操作任何文件前，请以此树为准定位路径：

```text
eval-platform/
├── .gitignore                                     # [版本控制] Git 忽略规则配置
├── README.md                                      # [项目说明] 平台功能介绍、环境配置与启动指南
├── pyproject.toml                                 # [包配置] 注册统一命令行入口 evalctl 及 Python 包构建配置
├── AGENTS.md                                      # [核心导览] 全局操作宪章、真实文件树、资料地图与系统地图规则（变更必同步）
├── lessons.md                                     # [经验沉淀] 极简工程经验与避坑记录（每次踩坑或优化后必更新）
├── .agents/                                       # [Agent 本地发现技能]
│   └── skills/
│       └── eval-platform/
│           └── SKILL.md                           # 本机 Coding Agent 自动发现的 evalctl 技能文档
├── cli/                                           # [Agent CLI 客户端与权限策略治理层]
│   ├── __init__.py                                # CLI 包版本定义
│   ├── __main__.py                                # python -m cli 调度入口
│   ├── config.py                                  # 运行时配置、环境变量与统一退出码 (0/1/2/3/4)
│   ├── catalog.py                                 # 全量 8 组 59 条命令目录契约（覆盖全部 57 条后端 API）
│   ├── policy.py                                  # 命令可见性与权限策略引擎（Profile/通配符黑白名单/风险分级/二次确认）
│   ├── policy.default.yaml                        # 默认策略配置与 5 套预设 Profile (full/safe/readonly/eval-loop/flywheel-loop)
│   ├── client.py                                  # 平台薄客户端（默认 HTTP/SSE 直连后端，可选 --in-process ASGI 直调）
│   ├── utils.py                                   # Payload 合并、Side-by-Side 分歧过滤与结构化 JSON/table/--out 输出
│   ├── main.py                                    # 动态策略裁剪 Click 命令树与统一异常退出入口 (evalctl)
│   ├── SKILL.md                                   # CLI 随包附带 Agent 渐进式披露技能指南
│   └── commands/                                  # 8 大业务命令组实现
│       ├── __init__.py                            # 命令组注册汇总
│       ├── system.py                              # health / capabilities / policy (3)
│       ├── models.py                              # list / create / update / delete / test / chat (6)
│       ├── rules.py                               # list / get / save / default-templates / debug-prompt / test-prompt (6)
│       ├── prompts.py                             # list / get / save / reset / preview (5)
│       ├── datasets.py                            # list / samples / import / delete / filter-options / export-preview / export (7)
│       ├── benchmarks.py                          # run / tasks / status / side-by-side / delete / batch-delete / revoke-ledger / preview-sampling / reset-tracker / debug-judge / test-judge (11)
│       ├── simulation.py                          # run / stream / recent / evaluate / clear-all (5)
│       └── flywheel.py                            # samples / batch / judge / batch-judge / remediate / batch-remediate / update / batch-status / delete / batch-delete / slices-get / slices-update / export-gold / export-selected / export-benchmark / batch-export-benchmark (16)
├── docs/                                          # [统一设计与规范文档库]
│   ├── DESIGN_PLAN.md                             # [架构方案] 通用测试管理与数据飞轮平台完整设计方案说明书
│   ├── FRONTEND_DESIGN.md                         # [前端方案] Web 工作台信息架构、组件线框与交互规范说明书
│   ├── FLYWHEEL_FAQ_EVAL_LOOP_DESIGN.md           # [演进方案] 双轨并行质检(Mermaid∥FAQ)、累积上文切题与复测动态免训闭环设计方案
│   ├── FLYWHEEL_BENCHMARK_GENERATION_DESIGN.md    # [专项方案] 模拟对练质检/修复生成评测集与数据中心分类管理设计方案说明书
│   ├── BENCHMARK_SAMPLING_FILTER_DESIGN.md        # [专项方案] 常规评测集动态阶梯降频抽取与复测战绩治理设计方案说明书
│   └── map/                                       # [系统地图] 分层结构、时序、代码事实与变更图
│       ├── L1-overview.mmd                        # 系统架构全景图 (L1)
│       ├── L2-adapters.mmd                        # 统一模型接入底座内部图 (L2)
│       ├── L2-benchmark.mmd                       # 常规评测纵向切片 (L2)
│       ├── L2-cli.mmd                             # Agent CLI 与权限治理内部图 (L2)
│       ├── L2-dataset_center.mmd                  # 数据集中心纵向切片 (L2)
│       ├── L2-flywheel.mmd                        # 数据飞轮纵向切片 (L2)
│       ├── L2-judge_engine.mmd                    # 质检裁判底座内部图 (L2)
│       ├── L2-model_hub.mmd                       # 模型中心纵向切片 (L2)
│       ├── L2-prompt_center.mmd                   # 提示词管理中心纵向切片 (L2)
│       ├── L2-rule_center.mmd                     # 规则中心纵向切片 (L2)
│       ├── L2-simulation.mmd                      # 模拟对弈纵向切片 (L2)
│       ├── L3-benchmark_sampling_eval.mmd         # 常规评测动态降频抽样与矩阵执行时序图 (L3)
│       ├── L3-flywheel_rejudge_export.mmd         # 数据飞轮复核重检重修与切题导出时序图 (L3)
│       ├── L3-simulation_flywheel_loop.mmd        # AI模拟对弈与质检修复切题闭环时序图 (L3)
│       ├── build.py                               # 系统地图构建、静态/渲染校验与代码对齐门禁脚本
│       ├── fact_extractor.py                      # 后端 AST + 端到端事实提取与图-码对齐校验引擎
│       ├── ts_probe.mjs                           # 前端 TypeScript 7 Compiler API + TypeChecker 符号/事件探针
│       ├── changes/                               # [变更方案图目录] 每次结构性改动的变更图
│       │   ├── 2026-11-17-flywheel_service_encapsulation.mmd # 飞轮路由瘦身与服务层统一封装方案图
│       │   ├── 2026-11-17-simulation_flywheel_decouple.mmd   # 模拟对弈与数据飞轮职责解耦方案/变更图
│       │   ├── 2026-11-18-prompt_center_and_rule_prompt_debug.mmd # 规则中心轨迹/FAQ调试增强与提示词管理中心方案图
│       │   ├── 2026-11-18-vertex_adaptive_thinking_probe.mmd # Vertex思考协议自适应探测与持久化方案/变更图
│       │   └── 2026-11-19-agent_cli_platform.mmd  # 本机 Coding Agent CLI 与可配置命令治理方案/变更图
│       └── facts/                                 # [自动落盘事实库] 供 AI 与开发者查漏的持久化真相清单
│           ├── FACTS.md                           # 可读性事实清单（切片总表、依赖图、事件链、外网直连、盲区）
│           └── facts.json                         # 结构化机器事实（供 build.py 校验与下游读取）
├── backend/                                       # [后端服务与核心引擎]
│   ├── cli.py                                     # 兼容 CLI 调度入口（已统一转发至 cli.main:main）
│   ├── requirements.txt                           # 后端依赖清单 (fastapi, uvicorn, sqlalchemy, httpx 等)
│   ├── tests/                                     # [API 与引擎回归测试集]
│   │   ├── __init__.py                            # 测试包初始化
│   │   ├── test_agent_cli.py                      # Agent CLI 全量 59 命令、可配置策略裁剪与隔离端到端回归测试
│   │   ├── test_api.py                            # API 全链路与矩阵评测/任务删除/API异常隔离/飞轮对弈回归测试
│   │   ├── test_architecture.py                   # 架构守护断言测试(防胖路由/越级调引擎/聚合导入/外网直连/循环依赖)
│   │   ├── test_remediator_surgical.py            # 客服专属靶向微创修复与用户文本0篡改防御校验测试
│   │   ├── test_cache_friendly_simulation.py      # 模拟对弈静态System前缀保缓存与两极化人设矩阵测试
│   │   ├── test_topology.py                       # Mermaid有向图拓扑连通性与动力学规则测试
│   │   ├── test_benchmark_tokens.py               # 评测最大生成Token配置与样本级max_new_tokens忽略覆盖测试
│   │   ├── test_judge_two_step.py                 # 质检裁判两步解耦架构(专职轨迹抽取/槽位连通跳步/事实注入打分)测试
│   │   ├── test_faq_retrieval_and_inspection.py   # FAQ混合检索(BM25+CPU向量+RRF)/双轨并行质检/扣分汇合与三段式修复测试
│   │   ├── test_flywheel_rejudge_and_remediate.py # 飞轮按新标准重新质检(Re-Judge)与重新修复(Re-Remediate)全链路回归测试
│   │   ├── test_benchmark_slicer.py              # 测评切题引擎/原始序号防位移/外呼首轮补位/双向同步与批量导出测试
│   │   ├── test_benchmark_sampling.py            # 动态阶梯降频抽取/三池分流/基准模型战绩流转与只读摸底隔离测试
│   │   ├── test_dataset_filtered_export.py       # 评测集抽样与条件过滤导出(预览/下载/另存新评测集)回归测试
│   │   ├── test_rule_prompt_debug.py              # 规则中心轨迹/FAQ/质检/修复Prompt模板调试组装与在线实测回归测试
│   │   ├── test_prompt_center.py                  # 全平台提示词管理中心CRUD、引擎生效优先级与在线实测回归测试
│   │   └── test_vertex_adaptive_thinking.py       # Vertex模型思考协议自适应探测与MaaS持久化路由回归测试
│   └── app/
│       ├── main.py                                # FastAPI 服务入口 (挂载 /api/v1 路由, CORS, /health)
│       ├── config.py                              # 全局路径、数据库 URL 与环境配置
│       ├── schemas/                               # [Pydantic 接口契约]
│       │   ├── chat.py                            # ChatMessage, ModelConfig, UnifiedChatRequest/Response
│       │   ├── rule.py                            # UniversalRuleSchema, TopologySOPConfig, NodeCheckpoint
│       │   ├── benchmark.py                       # BenchmarkSample, RubricConfig, SampleEvalResult, Summary, SampleEvalTracker, SamplingFilterConfig
│       │   └── flywheel.py                        # InspectionReport, FlywheelRecord, FlywheelBenchmarkSlice, SimulationRunRequest
│       ├── adapters/                              # [统一模型接入层 - 默认5次重试]
│       │   ├── base.py                            # 抽象基类 BaseModelAdapter & split_thinking_and_content
│       │   ├── openai_adapter.py                  # 标准 OpenAI / vLLM / SGLang / DeepSeek 兼容适配器
│       │   ├── harmony_adapter.py                 # Harmony 双通道 (<|channel|>final) 专用适配器
│       │   ├── vertex_adapter.py                  # Google Vertex AI 适配器 (ADC免密, Windows代理感知, 无限读等待)
│       │   ├── mock_adapter.py                    # 离线自测 Mock 适配器 (零 API 依赖自测)
│       │   └── factory.py                         # 适配器工厂 AdapterFactory
│       ├── engine/                                # [核心评测与飞轮引擎 - 支持独立调用]
│       │   ├── topology.py                        # 通用 Mermaid 有向图拓扑连通性校验器 (UniversalTopologyValidator)
│       │   ├── simulator.py                       # 通用客户模拟对弈引擎 (UniversalSimulator)
│       │   ├── judge.py                           # 通用质检与 Rubric 裁判引擎 (UniversalJudgeEngine)
│       │   ├── faq_retriever.py                   # 通用 FAQ 混合检索引擎 (BM25词法+CPU/Remote向量+RRF倒数排名融合+内存缓存)
│       │   ├── remediator.py                      # 通用数据修复与 messages 格式清洗引擎 (UniversalDataRemediator)
│       │   ├── slicer.py                          # 核心测评切题引擎 (ProgressiveContextSlicer: 原始序号防位移/累积纠正/真实标答双源锚定)
│       │   ├── benchmark_sampler.py               # 评测集动态阶梯降频与 SQLite 账本治理引擎 (BenchmarkSamplingEngine v2.1: 题库只读/版本隔离账本/指纹防陈旧/三池分流/紧迫度配额/过滤导出)
│       │   ├── benchmark_runner.py                # 常规评测与矩阵聚合引擎 (支持API异常隔离、即时落盘与5次重试)
│       │   └── prompt_registry.py                 # 全平台提示词注册、持久化管理与激活模板解析底座 (Prompt Registry)
│       ├── services/                              # [业务编排服务]
│       │   ├── simulation_service.py              # 双 Agent 多轮实战对弈服务 (SimulationService)
│       │   └── flywheel_service.py                # 数据飞轮治理全流程服务 (FlywheelPipelineService, 支持断点续跑)
│       ├── models/                                # [SQLAlchemy 数据库实体]
│       │   └── base.py                            # RegisteredModel, RuleConfigRecord, DatasetRecord, TaskRecord, FlywheelSampleRecord, BenchmarkRunLedger, PromptTemplateRecord
│       └── api/                                   # [RESTful API 路由]
│           ├── deps.py                            # 路由公共依赖助手 (模型适配器加载与规则配置读取)
│           ├── models.py                            # 模型注册、删除、列表、连通性与多轮对话 API (/api/v1/models)
│           ├── prompts.py                         # 全平台提示词模板资产管理 API (/api/v1/prompts)
│           ├── rules.py                           # 场景规则配置增删改查与 7 类规则/对弈 Prompt 调试实测 API (/api/v1/rules)
│           ├── datasets.py                        # 数据集列表、外部导入、抽样过滤导出、删除与样本预览 API (/api/v1/datasets)
│           ├── benchmarks.py                      # 常规评测异步任务、任务删除/批量删除、即时落盘与 Side-by-Side API
│           ├── simulation.py                      # 模拟对弈流式/同步、历史会话回显/清空与自由对话评估 API (/api/v1/simulation)
│           └── flywheel.py                        # 数据飞轮样本治理、重检/重修、切题与数据集导出 API (/api/v1/flywheel)
├── frontend/                                      # [前端 Web 工作台 - Vite + React 18 + TS 5]
│   ├── package.json                               # 前端依赖清单 (AntD 5, Tailwind CSS, Mermaid, Monaco, ECharts)
│   ├── vite.config.ts                             # Vite 配置与 /api 反向代理至 9021
│   ├── tsconfig.json                              # TypeScript 编译配置
│   ├── tailwind.config.js                         # Tailwind 样式配置
│   ├── postcss.config.js                          # PostCSS 插件配置 (@tailwindcss/postcss)
│   └── src/
│       ├── main.tsx                               # React 入口挂载
│       ├── App.tsx                                # 页面路由与 AntD 主题全局配置
│       ├── index.css                              # Tailwind 样式与自定义滚动条/代码块
│       ├── types/                                 # [前端 TypeScript 契约 - 100% 对齐后端 Schemas]
│       │   └── index.ts                           # ChatMessage, ModelConfig, Rule, Benchmark, Flywheel
│       ├── api/                                   # [RESTful API 客户端 - Axios 封装]
│       │   └── index.ts                           # modelsApi, rulesApi, promptsApi, datasetsApi, benchmarksApi, simulationApi, flywheelApi
│       ├── utils/                                 # [前端通用工具函数]
│       │   └── promptDebugExport.ts               # Prompt 调试报文 JSON / Markdown 导出工具
│       ├── components/                            # [核心通用业务与可视化组件]
│       │   ├── Layout.tsx                         # 全局侧边导航栏与顶部状态条布局
│       │   ├── MermaidViewer.tsx                  # Mermaid 状态机流程图渲染与节点轨迹动画回放
│       │   ├── TurnDiffViewer.tsx                 # 轮次级安全比对视窗与行内微调编辑器 (内置 SFT 格式校验)
│       │   ├── MonacoDiffModal.tsx                # VS Code 内核 Monaco 源码双栏 Diff 模态框
│       │   ├── RequestDebugModal.tsx              # 质检与修复 Request Messages / Thinking API 调试查看与复制弹窗
│       │   └── MetricRadarChart.tsx               # ECharts 多模型性能、及格率与思考深度综合雷达图
│       └── pages/                                 # [七大核心业务工作台页面]
│           ├── ModelHub.tsx                       # 模型与参数中心 (/models) - 注册/克隆/编辑/连通性测试
│           ├── RuleCenter.tsx                     # 场景规则与 SOP 中心 (/rules) - 基础/Mermaid与轨迹调试/FAQ检测与知识库/质检红线/清洗/剧本
│           ├── BenchmarkList.tsx                  # 常规评测任务中心 (/benchmarks) - 评测记录批量管理(多选/批量删除/导出)/受控动态分页/状态统计/发起测试抽屉/一键克隆
│           ├── BenchmarkReport.tsx                # 评测深度对比报告 (/benchmarks/:taskId) - 独立全屏看板/宏观雷达图/微观Side-by-Side卡片流
│           ├── BenchmarkEval.tsx                  # 常规评测兼容导出层
│           ├── SimulationArena.tsx                # AI 模拟对弈台 (/simulation) - 三合一工作台: AI对弈/人工自由对练/历史批量管理
│           ├── FlywheelWorkbench.tsx              # 数据飞轮复核工作台 (/flywheel) - 批量管理(多选/批量流转/批量补考/导出/删除)/多状态过滤/双视图Diff
│           ├── DatasetCenter.tsx                  # 数据资产中心 (/datasets) - 外部评测集导入/样本抽检抽屉/一键评测流转
│           └── PromptCenter.tsx                   # 提示词管理中心 (/prompts) - 全平台 8 大内置提示词模板查阅、修改、默认对照与静态占位符预览
├── data/                                          # [统一本地数据资产存储目录 - 默认已 Git 忽略防泄漏]
│   ├── eval_platform.db                           # SQLite 平台元数据库 (本地运行生成)
│   ├── rules/                                     # 通用场景规则配置 JSON 文件库 (本地配置，不上云)
│   ├── datasets/                                  # 版本化评测集与飞轮产出的黄金 messages 数据集 (本地资产)
│   └── runs/                                      # 每次评测/对弈任务的独立输出报告与日志 (本地产物)
```

---

## 三、 全局资料与数据导览地图（Documentation & Data Map）

本表定义了工作区内所有核心文档、配置与数据资产的职责、具体格式、何时读取、何时更新。Agent 在执行对应领域的任务时必须按图索骥：

| 文件/资产路径                                                                                                                          | 定位与内容概述                                            | 文件格式与核心结构 (Schema)                                                                                                                                                                                       | 何时必须读取 (Read Trigger)                                                                                                                                                       | 何时必须更新 (Update Trigger)                                                         |
|:-------------------------------------------------------------------------------------------------------------------------------- |:-------------------------------------------------- |:-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |:--------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |:------------------------------------------------------------------------------- |
| [`AGENTS.md`](AGENTS.md)                                                                       | 全局最高操作规范、物理/规划文件树、资料导览地图、系统地图规则（第五节）               | Markdown（含文件树代码块与导览表格）                                                                                                                                                                                   | **每次会话开始、执行任何任务的第一步**                                                                                                                                                       | 新增/删除/移动任何文件或目录、修改模块职责、新增文档或改变数据格式时**立即同步更新**                                   |
| [`lessons.md`](lessons.md)                                                                     | 跨会话实战经验、踩坑原因与解决铁律速查表                               | Markdown（按模块分类的极简条目：`[标签] 现象 -> 根因 -> 铁律`）                                                                                                                                                               | **每次会话开始、编写网络调用/模型解析/质检修复代码前**                                                                                                                                              | 遇到 Bug 排障成功、发现模型接口特殊行为、或总结出新的架构约束时**立即追加**                                      |
| `docs/map/`                                                                                                                      | 系统地图：分层结构图、关键场景时序图、代码事实提取与每次改动的变更图（规则见第五节）         | Mermaid 文本（一图一个 `.mmd`）+ `facts/` 事实库 + `build.py` 生成的 `index.html`                                                                                                                                      | **每次改代码/画图前**：先跑 `python docs/map/build.py --facts` 并读 [`docs/map/facts/FACTS.md`](docs/map/facts/FACTS.md) + `L1/L2` 定位（不读 `index.html`） | **每次改代码后**：更新受影响的 `.mmd`、生成变更图并运行 `python docs/map/build.py`（自动刷新 `facts/` 与校验） |
| [`docs/map/facts/FACTS.md`](docs/map/facts/FACTS.md)                                           | 全栈 AST 代码事实真相清单（7大能力切片、事件→API→后端链、外网直连、盲区）         | Markdown（附 `facts.json` 机器快照）                                                                                                                                                                            | **改代码、画 L1/L2/L3、排查前后端链路前必读**，确保不漏线、不凭文件名猜                                                                                                                                  | 由 `python docs/map/build.py --facts` 或 `build.py` 自动覆写更新，**严禁手工修改**             |
| [`docs/DESIGN_PLAN.md`](docs/DESIGN_PLAN.md)                                                   | 通用大模型测试管理与数据飞轮平台架构设计说明书                            | Markdown（含 Mermaid 架构图、统一模型 Schema、通用规则 Schema、迁移表）                                                                                                                                                      | 设计前后端接口、重构模块、实现新评测或飞轮功能前                                                                                                                                                    | 用户提出架构变更、新增核心模块、调整统一接口字段或规则配置协议时同步更新                                            |
| [`docs/FRONTEND_DESIGN.md`](docs/FRONTEND_DESIGN.md)                                           | 前端 Web 工作台架构、七大核心模块线框与交互规范说明书                      | Markdown（含信息架构、组件选型、Side-by-Side 与 Diff 交互草图）                                                                                                                                                            | 开发前端页面、对接 API 路由、调整工作台交互与可视化组件时                                                                                                                                             | 调整前端技术栈、新增前端页面功能或修改页面交互流程时同步更新                                                  |
| [`docs/FLYWHEEL_FAQ_EVAL_LOOP_DESIGN.md`](docs/FLYWHEEL_FAQ_EVAL_LOOP_DESIGN.md)               | 双轨并行质检(Mermaid流程∥FAQ问答)、累积上文切题与复测动态免训闭环设计方案        | Markdown（含入口双开关并行架构图、FAQ提问抽取与问题改写契约、累积上文切题算法、复测免训5大指标）                                                                                                                                                   | 开发 FAQ 问答质检支线、实现飞轮多轮数据转评测切片、开发基于复测连续通过次数的动态免训过滤功能前                                                                                                                          | 调整双轨质检逻辑、修改 FAQ 检索与三段式修复规则或更新动态错题本过滤指标时同步更新                                     |
| [`docs/FLYWHEEL_BENCHMARK_GENERATION_DESIGN.md`](docs/FLYWHEEL_BENCHMARK_GENERATION_DESIGN.md) | 模拟对练质检/修复生成评测集与数据中心分类管理设计方案说明书                     | Markdown（含累积纠正上文切题算法推导、切片人工微调与双向同步、数据中心双Tab解耦方案）                                                                                                                                                         | 开发质检评测集生成、人工微调测评切片、数据集中心 Tab 改造前                                                                                                                                            | 评测切片契约变更或数据中心页面重构时同步更新                                                          |
| [`docs/BENCHMARK_SAMPLING_FILTER_DESIGN.md`](docs/BENCHMARK_SAMPLING_FILTER_DESIGN.md)         | 常规评测集动态阶梯降频抽取与复测战绩治理设计方案说明书 (v2.1)                 | Markdown（含 SQLite `benchmark_run_ledgers` 账本隔离、同版本快照去重、`is_holdout` 防泄漏、R_cooldown 动态加权等效通过率公式）                                                                                                          | 开发或调整评测集动态抽样过滤、账本战绩回放与双口径通过率报告时                                                                                                                                             | 抽样参数契约、降频公式、账本状态机或报告通过率折算口径变更时同步更新                                              |
| [`cli/`](cli/main.py)                                                                          | 面向本机 Coding Agent 的全能力 CLI (`evalctl`) 与可配置权限治理客户端 | Python Click 包 + [`cli/policy.default.yaml`](cli/policy.default.yaml) + [`cli/SKILL.md`](cli/SKILL.md)（支持 HTTP/SSE 与 `--in-process`） | 需要通过 CLI 操纵平台全部 7 大能力、切换策略 Profile 或排查命令行接入时                                                                                                                                | 新增/修改 CLI 子命令、策略配置结构或环境变量契约时同步更新                                                |
| [`backend/cli.py`](backend/cli.py)                                                             | 兼容旧版命令行调度入口                                        | Python 兼容转发脚本（统一委派至 `cli.main:main`）                                                                                                                                                                     | 通过 `python backend/cli.py` 兼容调用时                                                                                                                                            | 调整 CLI 入口路径或包结构时同步更新                                                            |
| [`backend/app/main.py`](backend/app/main.py)                                                   | FastAPI 应用程序主入口                                    | Python FastAPI app（含 `/health`, CORS, 挂载 `/api/v1`）                                                                                                                                                      | 调试 REST API 服务、排查中间件或启动参数时                                                                                                                                                  | 挂载新路由模块或修改全局异常处理时更新                                                             |
| [`backend/app/config.py`](backend/app/config.py)                                               | 后端全局环境变量与路径配置                                      | Pydantic BaseSettings（配置 SQLite、路径常量）                                                                                                                                                                    | 排查数据库连接或修改存储目录路径时                                                                                                                                                           | 增加全局环境变量配置项时更新                                                                  |
| [`backend/app/schemas/`](backend/app/schemas/)                                                 | Pydantic 数据模型契约库                                   | Python Pydantic Models (含 SampleEvalTracker, SamplingFilterConfig 等)                                                                                                                                     | 编写或调用任何模型适配器、引擎算子、API 接口前                                                                                                                                                   | 请求入参、出参字段或业务规则配置协议调整时更新                                                         |
| [`backend/app/adapters/`](backend/app/adapters/)                                               | 统一模型接入层与适配器实现                                      | Python 类（继承 `BaseModelAdapter`，默认 5 次重试）                                                                                                                                                                 | 接入新模型 Provider、调试思考开关或修改响应指标时                                                                                                                                               | 新增模型厂商协议适配器或修改思考剥离逻辑时更新                                                         |
| [`backend/app/engine/`](backend/app/engine/)                                                   | 评测执行、客户模拟、裁判质检、数据修复、切题与抽样治理核心引擎                    | Python 类（`UniversalTopologyValidator`, `BenchmarkRunner`, `ProgressiveContextSlicer`, `BenchmarkSamplingEngine` 等）                                                                                       | 优化质检逻辑、调整拓扑校验规则、增加评测算子或抽样算法时                                                                                                                                                | 质检打分算法、格式校验清洗、切题生成、抽样过滤或对比评测指标升级时更新                                             |
| `data/rules/*.json`                                                                                                              | 通用场景业务 SOP 规则配置模板（本地持久化，已 Git 忽略）                  | JSON Object（符合 `UniversalRuleSchema`）                                                                                                                                                                    | 运行外呼/客服场景评测、模拟对弈或测试飞轮闭环时                                                                                                                                                    | 业务流程图变更、调整节点红线或增加客户人设用例时更新                                                      |
| `data/datasets/*.jsonl`                                                                                                          | 常规基准测试集与飞轮产出的黄金数据集（本地持久化，已 Git 忽略）                 | JSONL（每行：`{"id", "category", "sub_category", "difficulty", "source", "max_new_tokens", "messages", "reference", "rubric", "regex_constraints"}` 或 `{"messages": [...]}`）                                 | 进行常规数据集多模型横向对比、验证客观正则约束与微调数据导出时                                                                                                                                             | 新增评测测试用例或飞轮导出新版数据集时更新                                                           |
| `data/eval_platform.db`                                                                                                          | SQLite 本地元数据库                                      | SQLite 数据库文件（管理模型池、任务、复核样本）                                                                                                                                                                              | 平台启动或数据初始化时自动读写                                                                                                                                                             | 平台运行时由 API 服务自动写入和更新                                                            |
| `data/runs/*.json`                                                                                                               | 任务运行结果与报告输出文件                                      | JSON / Markdown 报告（含详细 Side-by-Side 输出）                                                                                                                                                                  | 查阅某次评测任务的微观评测明细时                                                                                                                                                            | 每次运行 benchmark 评测或模拟对弈任务后自动生成                                                   |
| [frontend/src/types/index.ts](frontend/src/types/index.ts)                                     | 前端完整 TypeScript 契约库                                | TypeScript Interface (ChatMessage, UniversalRuleSchema 等)                                                                                                                                                | 页面研发、组件参数对接、类型检查时                                                                                                                                                           | 后端 Schema 调整或新增接口出入参时更新                                                         |
| [frontend/src/api/index.ts](frontend/src/api/index.ts)                                         | 前端 REST API 客户端 (Axios)                            | TypeScript API 封装 (modelsApi, rulesApi, benchmarksApi, simulationApi, flywheelApi 等)                                                                                                                     | 调试前后端接口通信或新增 API 路由时                                                                                                                                                        | 后端 API 端点增删改时更新                                                                 |
| [frontend/src/components/](frontend/src/components/)                                           | 状态机回放、多轮Diff编辑、雷达图通用组件                             | React TSX 组件 (MermaidViewer, TurnDiffViewer, MetricRadarChart 等)                                                                                                                                         | 优化图渲染、调整轮次对比交互或图表配置时                                                                                                                                                        | 组件功能扩展或视觉样式调优时更新                                                                |
| [frontend/src/pages/](frontend/src/pages/)                                                     | 七大工作台业务页面实现                                        | React TSX 页面 (ModelHub, RuleCenter, BenchmarkList, SimulationArena, PromptCenter 等)                                                                                                                      | 调整页面功能、表单交互或评测/飞轮看板时                                                                                                                                                        | 页面功能调整或布局变更时更新                                                                  |

---

## 四、 Agent 标准操作规程（SOP for Agents）

1. **当用户要求修改或新增功能时**：
   - 先查阅 [`docs/DESIGN_PLAN.md`](docs/DESIGN_PLAN.md) 与 [`docs/FLYWHEEL_FAQ_EVAL_LOOP_DESIGN.md`](docs/FLYWHEEL_FAQ_EVAL_LOOP_DESIGN.md) 确认该功能在总体架构中的模块归属与统一 Schema 约束；
   - 查阅 [`lessons.md`](lessons.md) 确认是否存在相关的历史踩坑记录（如超时设置、思考标签清洗、并发落盘锁等）；
   - 按【第五节】先读 `FACTS.md` 与系统地图定位；结构性改动先画 `changes/` 方案图并**等用户确认**后再编码；
   - 编码完成后，若增删改了文件/目录/数据格式，**同步更新本 `AGENTS.md` 的第二、三节**；
   - **每次修改完毕后 / 提交 Git 前**，按【第五节】先跑脚本生成 `FACTS.md` 并据此更新 `L2` 与变更图，最后运行 `python docs/map/build.py --strict-facts` 验收。
2. **当处理模型接入与推理调用时**：
   - 严禁在业务代码（如评测器、对弈器、质检器）中直接拼写特定厂商的 HTTP 请求；必须通过统一的模型适配器层调用（`backend/app/adapters/`），确保思考开关（`thinking_enabled` / `thinking_level`）、温度等参数通过统一配置传入，并在适配器出口处完成思考链（`thinking`）与正文（`content`）的干净剥离。
3. **当处理模拟对话生成新数据集（数据飞轮）时**：
   - 必须经过完整的四步链路：**原始对弈记录 → 白盒质检（拓扑连通性 + 规则红线 ∥ FAQ问答准确度） → 缺陷修复与二次验收（未达标样本进淘汰隔离库） → 格式严格清洗校验（`validate_and_sanitize_format`）后生成标准 `messages` 格式新数据集或累积纠正上文评测切片**。
   - 任何批量处理大数据集的任务，必须支持**基于样本唯一标识（如 `original_idx` 或 `id`）的断点续跑与逐条即时落盘（flush）**。
4. **当涉及长时运行服务时**：
   - 严格遵守全局规则：所有长期运行的服务（如 FastAPI 服务端、前端 Dev Server、Colab 隧道服务）必须提示由用户手动运行，或在明确获得用户批准后方可执行。

---

## 五、 系统地图与依赖事实（System Map）

> 用户通过 `docs/map/` 掌握系统全貌，AI 通过 `FACTS.md` 掌握真实依赖；代码与地图不一致时**以代码为准**。

### 5.1 核心文件

| 文件                                       | 作用                                                                     |
| ---------------------------------------- | ---------------------------------------------------------------------- |
| `docs/map/L1-overview.mmd`               | 全景图（入口、大业务能力、共用底座、外部系统）                                                |
| `docs/map/L2-<能力ID>.mmd`                 | 业务能力纵向切片（页面 → 路由 → 服务/引擎 → 底座）及底座内部图                                   |
| `docs/map/L3-<场景ID>.mmd`                 | 关键业务场景时序图（参与者须已存在于 L1/L2）                                              |
| `docs/map/changes/<YYYY-MM-DD>-<主题>.mmd` | 改动方案图 / 变更图（节点挂 `:::added` / `:::changed` / `:::removed`）              |
| `docs/map/facts/FACTS.md`                | **自动生成的代码事实底稿**（全栈切片、模块横向调用、事件→后端链、表读写、外网直连、盲区），严禁手改                   |
| `docs/map/build.py`                      | 一键脚本：提取事实、校验 `.mmd` 与图码对齐、生成含模块交互视图的 `index.html`（禁止读或手改 `index.html`） |

### 5.2 画图硬约束

- **格式与上限**：Mermaid UTF-8 文本，首行写 `%% title: 中文标题`，ID 仅用英文小写/数字/下划线；每张流程图 **≤12 节点、≤15 连线**，连线标签宽 **≤28**（汉字算 2）；时序图 **≤8 参与者、≤20 消息**。**超限时用 `subgraph` 合并次要节点或拆分子图，严禁为凑上限删掉 `FACTS.md` 中的真实依赖。**
- **节点写法**：文字用双引号包裹，两行：`第一行中文短名` + `第二行相对路径`（如 `backend/app/engine/judge.py:UniversalJudgeEngine`），**路径与拆分粒度（文件或 `文件:类/函数`）严格对齐 `FACTS.md`**；变更图着色节点加第三行说明（≤12 字），勿手写 `classDef`（由 `build.py` 注入）。
- **只画真实调用与数据流**：跳过 `schemas/`、`config.py`、`types/`、`api/index.ts`、`models/` 中间层；读写库直接连数据库节点并标表名；绕过 `adapters/` 的外网直连（如 `faq_retriever.py`）必须在 L1/L2 画全；拿不准的线画虚线标 `?`。
- **范围与下钻**：覆盖 `backend/app/`、`backend/cli.py`、`cli/`、`frontend/src/`、`data/`；L1 节点自动对应 `L2-<节点ID>.mmd`，其余跳转用 `%% link: 节点ID -> 目标.mmd`。在 L2 中通过 `%% link: 节点ID -> L2-<其他能力或共用底座>.mmd`（如 `L2-flywheel.mmd`、`L2-judge_engine.mmd`）显式委派或下钻的节点，图码校验器会在该边界止步，不再穿透展开对方内部实现。

### 5.3 标准操作流（每次改动 / 提交 Git）

1. **改前定位**：先看 `docs/map/facts/FACTS.md` 与 `L1`/`L2` 定位（不要一上来翻遍代码）；结构性改动先在 `changes/` 画方案图并运行 `build.py`，等用户确认后再写代码。
2. **编码红线**：保持代码可静态分析（补全参数与 `self.xxx` 的 Type Hints，前端纯类型用 `import type`，避免 `eval`/反射等动态调用）；严禁私自绕过 `adapters/` 直连外部服务；**内部跨层模块引用统一直接导入具体子模块**（如 `from backend.app.engine.judge import UniversalJudgeEngine`、`from backend.app.adapters.factory import AdapterFactory`），禁止在 `cli.py` / `api/` / `services/` / `engine/` 中走 `adapters`、`engine`、`services` 包入口聚合导入，对应 `__init__.py` 仅保留 PEP 562 `__getattr__` 惰性导出。
3. **改后生成事实（每次修改完毕后 / 提交 Git 前必做）**：`python docs/map/build.py --facts`
4. **分析 `FACTS.md` 并更新 `L2`**：读取刚生成的 `docs/map/facts/FACTS.md`，核对「业务能力纵向切片总表」「端到端链路」「外网直连」「盲区」的变化，据此更新受影响的 `L2-<能力ID>.mmd`（及 `L1`/`L3`），并在 `docs/map/changes/` 生成变更图（只改测试或文档时可不出变更图）。
5. **构建与校验继续**：平时预览运行 `python docs/map/build.py`（发现漂移只打警告、正常生成 `index.html`）；**提交前 / 交付验收必须运行 `python docs/map/build.py --strict-facts`**（严格阻断图-码漂移与全部校验），全部通过后方可交付或提交。
