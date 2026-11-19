---
name: eval-platform-cli
description: 通过统一命令行工具 evalctl（或 python -m cli / python backend/cli.py）操纵通用大模型测试管理与数据飞轮平台：模型注册连通、规则SOP与Prompt调试实测、数据集导入过滤导出、常规评测对比与分歧分析、双Agent模拟对弈（含SSE流式）以及数据飞轮质检/微创修复/切题导出全闭环。
---

# Eval Platform Agent CLI 技能指南 (`evalctl`)

本指南面向本机 Coding Agent，采用**渐进式披露**方式按需探索并调用平台全部 8 大能力模块（59 条命令）。

## 一、快速入口、语法约束与路径语义

### 1. 标准入口与传输模式
- 全文示例统一使用全局命令 `evalctl`（若当前环境未安装包，可在平台源码根目录用 `python -m cli` 替代）。
- **默认 HTTP 模式**：连接运行中的 FastAPI 后端 `:9021`（可通过 `--base-url` 或环境变量 `EVALCTL_BASE_URL` 指定），与 Web 工作台共享数据库与产物。
- **进程内模式 `--in-process`**：本地未启动 `:9021` 服务时，加全局参数 `--in-process` 通过 ASGI 直调后端（`evalctl --in-process system health`）。

### 2. 全局参数语法硬约束（必看）
```text
evalctl [全局参数] <group> <command> [子命令参数]
```
- **所有全局参数必须置于子命令之前**，包括：`--out`、`--format`、`--timeout`、`--profile`、`--config`、`--base-url`、`--in-process`、`--yes`。放在子命令之后会导致 Click 解析报错。
- 正确示例：`evalctl --out data/runs/sim.json --timeout 0 simulation stream ...`

### 3. 跨项目调用的路径语义
在外部业务仓库（如训练/微调项目目录）中运行 `evalctl` 时，请严格区分两类路径：
- **客户端当前路径**（相对当前终端工作目录）：`dataset import --file`、`--payload`、`--out`、`--save-jsonl`、`--dialogue-file`、`--messages-file`、`--template-file`、`--assembled-prompt-file`。
- **平台服务端路径**（相对平台仓库根目录或服务端绝对路径）：`benchmark run --dataset-path`、`flywheel batch --input-file-path`、`dataset import --source-file-path`。
- **外部项目标准导入做法**：先用 `evalctl dataset import --dataset-id <id> --dataset-type benchmark --file <本地路径>` 导入，平台会自动落盘为 `data/datasets/<id>.jsonl` 并返回 `file_path`；随后再使用该平台路径运行 `benchmark run --dataset-path ...`。

---

## 二、渐进式发现与权限策略（不要死记硬背参数）

1. **先探明当前放行边界**：
   ```bash
   evalctl --format compact system capabilities
   ```
   若执行命令返回退出码 `3`（`POLICY_DENIED`），再查具体拦截原因：
   ```bash
   evalctl system policy --check <cmd_id>
   ```
2. **按需查看子命令参数**（已被策略禁用的命令不会出现在 `--help` 中）：
   ```bash
   evalctl benchmark --help
   evalctl benchmark side-by-side --help
   ```
3. **Profile 边界速览**：
   - 内置 5 套 Profile：`full`（默认全开，59 条）、`safe`（禁用删除与重置）、`readonly`（仅只读查询与组装预览）、`eval-loop`（评测调优闭环，仅放行 31 条命令）、`flywheel-loop`（对弈修复闭环）。
   - ⚠️ **Profile 常见踩坑**：`eval-loop` **不包含** `model.create` / `model.update`（及 `simulation` / `flywheel`）。若需要注册模型或修改远程推理地址，必须使用 `full` 或 `safe`，否则会报 `POLICY_DENIED`（退出码 `3`）。
   - 可通过全局 `--profile <name>` 切换；开启破坏性操作二次确认时补加全局 `--yes`。

---

## 三、高频端到端闭环工作流

### 闭环 0：动态接入远程推理地址（Colab / Kaggle / 隧道评测必跑）
```bash
# 1. 更新已有模型或注册新模型
#    注意：openai / harmony 协议的 --api-url 必须填完整端点（含 /v1/chat/completions）
evalctl model update <model_id> --api-url https://<tunnel-host>/v1/chat/completions
# 或注册新模型：
evalctl model create --id qwen3-sft --name "Qwen3-SFT" --provider-type openai \
  --api-url https://<tunnel-host>/v1/chat/completions --model-name qwen3-8b

# 2. 连通性探测 + 单轮对话验真
evalctl model test <model_id>
evalctl model chat <model_id> --message "你好，请简要自我介绍"
```

### 闭环 1：外部导入 → 常规评测对比 → 分歧诊断
```bash
# 1. 紧凑查看模型，导入外部评测集并核对资产列表
evalctl --format table model list --brief
evalctl dataset import --dataset-id my_eval --dataset-type benchmark --file ./eval.jsonl
evalctl --format table dataset list

# 2. 发起多模型横向对比评测并等待完成
#    注：单卡/内网穿透串行服务（如 GPU_THREAD_LOCK）务必设 --workers 1 并调大 --wait-timeout；
#    全局 --timeout 0 防读取超时；flywheel_gold 纯对话集无 rubric/reference，传 --judge-model-id 不会触发主观打分
evalctl --timeout 0 benchmark run \
  --dataset-path data/datasets/my_eval.jsonl \
  --model-id qwen3-sft --model-id mock-a \
  --judge-model-id vertex-gemini-flash-judge \
  --workers 1 --wait --wait-timeout 3600

# 3. 先看分歧摘要，再把明细落盘做深度分析
evalctl benchmark side-by-side <task_id> --filter divergence --summary-only
evalctl --out data/runs/debug_sbs.json benchmark side-by-side <task_id> --filter fail
```

### 闭环 2：规则 / 提示词模板调优与在线实测
```bash
# 1. 紧凑列出提示词摘要或规则列表
evalctl --format table prompt list --summary
evalctl --format table rule list

# 2. 基于真实规则动态拼装调试 Prompt 并落盘，再直接用该报文实测
evalctl --out data/runs/debug_req.json rule debug-prompt --rule-id demo_after_sales --prompt-type judge
evalctl rule test-prompt --model-id vertex-gemini-flash-judge --payload data/runs/debug_req.json

# 3. 常规评测裁判 Prompt 调试同理
evalctl --out data/runs/bench_judge.json benchmark debug-judge --dataset-path data/datasets/demo_benchmark.jsonl
evalctl benchmark test-judge --model-id vertex-gemini-flash-judge --payload data/runs/bench_judge.json
```

### 闭环 3：双 Agent 模拟对弈 → 飞轮质检/修复 → 切题/导出 SFT 与评测集
```bash
# 1. 流式运行对弈（进度打到 stderr；汇总 JSON 约 200KB+，务必加全局 --out 落盘）
evalctl --timeout 0 --out data/runs/sim_result.json simulation stream \
  --rule-id demo_after_sales \
  --target-model-id harmony-demo-target-model \
  --client-model-id vertex-gemini-flash-client \
  --judge-model-id vertex-gemini-flash-judge \
  --remediate-model-id vertex-gemini-flash-judge \
  --scenario-id case_01_standard_after_sales

# 2. 查看飞轮样本摘要（--brief + --format table 防爆上下文）
evalctl --format table flywheel samples --rule-id demo_after_sales --brief --limit 20

# 3. 按新规则重检（只检不修）或微创修复，再检查切题切片
evalctl --timeout 0 flywheel judge <sample_id> --rule-id demo_after_sales --judge-model-id vertex-gemini-flash-judge
evalctl --timeout 0 flywheel remediate <sample_id> --remediate-model-id vertex-gemini-flash-judge
evalctl flywheel slices-get <sample_id>

# 4. 导出黄金 SFT 训练集（与留出评测集天然防泄漏隔离）或导出为 Benchmark 评测集
evalctl flywheel export-gold --rule-id demo_after_sales --version-tag v1.1
evalctl flywheel export-benchmark <sample_id> --dataset-id fw_eval_v1 --name "飞轮回归评测集"

# 5. 批量飞轮治理（--input-file-path 为服务端路径；单卡/隧道服务加 --workers 1 与较大 --wait-timeout）
evalctl --timeout 0 flywheel batch --rule-id demo_after_sales --input-file-path data/runs/raw.jsonl \
  --judge-model-id vertex-gemini-flash-judge --workers 1 --wait --wait-timeout 3600
```

### 闭环 4：数据集智能抽样试算与条件过滤导出
```bash
# 1. 查看历史基准模型线与分类分布
evalctl dataset filter-options demo_benchmark

# 2. 试算并另存为聚焦薄弱错题的新评测集
evalctl dataset export-preview demo_benchmark --export-scope failed_only --model-line gpt-oss-20b
evalctl dataset export demo_benchmark \
  --export-scope failed_only --model-line gpt-oss-20b \
  --export-target save_dataset --target-dataset-id demo_failed_focus
```

---

## 四、防爆上下文、超时/并发参数与退出码

### 1. 大载荷保护与紧凑格式（防爆上下文）
- **必用全局 `--out <path>` 或 `--brief` 的超大载荷命令**：
  - `simulation recent`（全量拉取 `--limit 3` 可达 ~292 KB；列表检索优先加 `--brief`，需看完整对话时配 `--out` 并收紧 `--limit`）
  - `simulation stream`（汇总输出约 ~219 KB，务必配 `--out`）
  - `benchmark tasks`（全量拉取 `--limit 10` 约 ~34 KB；列表检索优先加 `--brief`，需看完整配置时配 `--limit` 与 `--out`）
  - `benchmark side-by-side`、`flywheel samples`、`rule get`（优先搭配 `--summary-only` / `--brief` 或 `--out`）
- **紧凑视图推荐**：在只读列表命令（如 `model list --brief`、`benchmark tasks --brief`、`simulation recent --brief`、`flywheel samples --brief`、`dataset list`、`system capabilities`、`prompt list --summary`）上使用全局 `--format table`（或 `--format compact`），大幅节省上下文 Token。

### 2. 慢推理 / 隧道防超时与裁判边界
- **HTTP 读取超时 `--timeout`**：默认 `600` 秒；对 `simulation stream`、`flywheel judge/remediate` 或慢推理隧道请求，传入全局 `--timeout 0`（不限）或 `--timeout 300`/`1200`。
- **异步等待 `--wait-timeout` 与串行并发 `--workers 1`**：`benchmark run --wait` 和 `flywheel batch --wait` 默认 `--wait-timeout 600` 秒，超过 30 题容易超时，应按需调大（如 `--wait-timeout 3600`）；面对单卡或带 `GPU_THREAD_LOCK` 的内网穿透串行服务，必须配合 `--workers 1`。
- **数据集类型与裁判模式边界**：`benchmark run --judge-model-id` 仅在样本包含 `reference` 或非空 `rubric` 时触发 LLM 主观打分；`flywheel_gold` 类型（纯对话 `messages`，无 `rubric`/`reference`）即使传入 `--judge-model-id` 也**不会**触发主观裁判。

### 3. 协议约定与退出码
- **输出流分离**：成功时 `stdout` 仅输出合法 JSON（或 `--format table`）；轮询进度、SSE 逐轮日志与错误 JSON 均输出到 `stderr`。
- **复杂载荷传递**：写/运行类命令支持 `--payload <file.json|->`（`-` 从 stdin 读取）与 `--payload-json '<json>'`，再叠加具体 CLI 选项覆盖。

| 退出码 | 含义 | 常见处理建议 |
| :---: | :--- | :--- |
| `0` | 成功 | 解析 stdout JSON 结果 |
| `1` | 参数错误或后端 HTTP 4xx/5xx 业务异常 | 查看 stderr JSON `error.message`（如全局参数放错位置、缺必填项） |
| `2` | 后端连接失败或请求超时 | 检查 `:9021` 服务是否启动；长耗时请求加全局 `--timeout 0` 或调大 `--wait-timeout` |
| `3` | 命令被当前权限策略拦截 (`POLICY_DENIED`) | 运行 `evalctl system policy --check <cmd_id>`；如修改模型配置需切回 `full` 或 `safe` |
| `4` | 破坏性命令缺少确认 (`CONFIRMATION_REQUIRED`) | 确认无误后在全局参数补加 `--yes` |
