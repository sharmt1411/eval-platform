---
name: eval-platform-cli
description: 通过统一命令行工具 evalctl（或 python -m cli / python backend/cli.py）操纵通用大模型测试管理与数据飞轮平台：模型注册连通、规则SOP与Prompt调试实测、数据集导入过滤导出、常规评测对比与分歧分析、双Agent模拟对弈（含SSE流式）以及数据飞轮质检/微创修复/切题导出全闭环。
---

# Eval Platform Agent CLI 技能指南 (`evalctl`)

本指南面向本机 Coding Agent，采用**渐进式披露**方式按需探索并调用平台全部 8 大能力模块（59 条命令）。

## 一、快速入口与运行模式

以下三种入口完全等价（如尚未执行包安装，优先用 `python -m cli`）：

```bash
evalctl system health
python -m cli system health
python backend/cli.py system health
```

### 两种传输模式

1. **默认 HTTP 模式**（推荐，当平台 FastAPI 后端 `:9021` 正在运行时使用）：
   - 与 Web 工作台共享 SQLite 元数据库、写锁、异步任务与报告文件，Agent 跑完任务后用户在浏览器立即可见。
   - 可通过 `--base-url http://127.0.0.1:9021` 或环境变量 `EVALCTL_BASE_URL` 指定服务端地址。
2. **进程内模式 `--in-process`**（当本地未启动 FastAPI 常驻服务时使用）：
   - 通过 ASGI 进程内直调后端应用，适合单次离线批处理或自动化脚本验证：
   ```bash
   python -m cli --in-process system health
   ```

---

## 二、渐进式发现与权限策略（不要死记硬背参数）

1. **第一步先查当前放行能力清单**：
   ```bash
   python -m cli --format compact system capabilities
   python -m cli system policy
   ```
2. **第二步按需看具体子命令参数**（已被策略禁用的命令不会出现在 `--help` 中）：
   ```bash
   python -m cli benchmark --help
   python -m cli benchmark side-by-side --help
   ```
3. **切换策略档案与自定义配置文件**：
   - 内置 5 套 Profile：`full`（默认全开）、`safe`（禁用删除与重置）、`readonly`（仅只读查询与组装预览）、`eval-loop`（评测调优闭环）、`flywheel-loop`（对弈修复与任务进度查询闭环）。
   - 通过全局参数 `--profile <name>` / `--config <yaml_or_json>` 或环境变量 `EVALCTL_PROFILE` / `EVALCTL_POLICY_FILE` 切换（扁平配置不支持 `--profile`）。
   - 若宿主机设置了 `EVALCTL_POLICY_LOCKED=1`，命令行将禁止再通过 `--config` / `--profile` 覆盖环境策略（返回退出码 `3`）。
   - 若策略开启 `require_confirm_for_destructive: true`，执行删除/重置类命令需追加全局参数 `--yes`。

---

## 三、四大高频端到端闭环工作流

### 闭环 1：发起常规评测并精准诊断分歧（防爆上下文）
```bash
# 1. 查看模型与数据集
python -m cli model list --brief
python -m cli dataset list

# 2. 发起多模型横向对比评测并等待完成
python -m cli benchmark run \
  --dataset-path data/datasets/demo_benchmark.jsonl \
  --model-id mock-a --model-id mock-b \
  --judge-model-id vertex-gemini-flash-judge \
  --wait

# 3. 先看分歧摘要，再把明细落盘做深度分析
python -m cli benchmark side-by-side <task_id> --filter divergence --summary-only
python -m cli --out data/runs/debug_sbs.json benchmark side-by-side <task_id> --filter fail
```

### 闭环 2：规则 / 提示词模板调优与在线实测
```bash
# 1. 列出提示词摘要或规则详情
python -m cli prompt list --summary
python -m cli rule list

# 2. 基于真实规则动态拼装调试 Prompt 并保存报文，再直接用该报文实测
python -m cli --out data/runs/debug_req.json rule debug-prompt --rule-id demo_after_sales --prompt-type judge
python -m cli rule test-prompt --model-id vertex-gemini-flash-judge --payload data/runs/debug_req.json

# 3. 常规评测裁判 Prompt 调试同理
python -m cli --out data/runs/bench_judge.json benchmark debug-judge --dataset-path data/datasets/demo_benchmark.jsonl
python -m cli benchmark test-judge --model-id vertex-gemini-flash-judge --payload data/runs/bench_judge.json
```

### 闭环 3：双 Agent 模拟对弈 → 飞轮质检/修复 → 切题/导出 SFT 与评测集
```bash
# 1. 流式运行对弈（进度打到 stderr，最终汇总 JSON 输出到 stdout）
python -m cli simulation stream \
  --rule-id demo_after_sales \
  --target-model-id harmony-demo-target-model \
  --client-model-id vertex-gemini-flash-client \
  --judge-model-id vertex-gemini-flash-judge \
  --remediate-model-id vertex-gemini-flash-judge \
  --scenario-id case_01_standard_after_sales

# 2. 查看飞轮样本摘要（--brief 避免大体积对话撑爆上下文）
python -m cli flywheel samples --rule-id demo_after_sales --brief --limit 20

# 3. 对样本按新规则重检（只检不修）或微创修复，再检查切题切片
python -m cli flywheel judge <sample_id> --rule-id demo_after_sales --judge-model-id vertex-gemini-flash-judge
python -m cli flywheel remediate <sample_id> --remediate-model-id vertex-gemini-flash-judge
python -m cli flywheel slices-get <sample_id>

# 4. 导出黄金 SFT 训练集（注：与纯评测留出集天然防泄漏隔离）或导出为 Benchmark 评测集
python -m cli flywheel export-gold --rule-id demo_after_sales --version-tag v1.1
python -m cli flywheel export-benchmark <sample_id> --dataset-id fw_eval_v1 --name "飞轮回归评测集"

# 5. 批量飞轮治理也支持 --wait 轮询等待完成（或在后台完成后用 benchmark status 查询）
python -m cli flywheel batch --rule-id demo_after_sales --input-file-path data/runs/raw.jsonl --judge-model-id vertex-gemini-flash-judge --wait
```

### 闭环 4：数据集智能抽样试算与条件过滤导出
```bash
# 1. 查看历史基准模型线与分类分布
python -m cli dataset filter-options demo_benchmark

# 2. 试算并另存为聚焦薄弱错题的新评测集
python -m cli dataset export-preview demo_benchmark --export-scope failed_only --model-line gpt-oss-20b
python -m cli dataset export demo_benchmark \
  --export-scope failed_only --model-line gpt-oss-20b \
  --export-target save_dataset --target-dataset-id demo_failed_focus
```

---

## 四、协议约定、防爆上下文技巧与退出码

- **输出流分离**：成功时 `stdout` 仅输出合法 JSON（或 `--format table`）；轮询进度、SSE 逐轮实时对话与错误结构化 JSON 均输出到 `stderr`。
- **复杂请求传递**：所有写/运行类命令均支持 `--payload <file.json|->`（`-` 从 stdin 读取）与 `--payload-json '<json>'`，再叠加具体 CLI 选项覆盖。
- **大载荷保护**：拉取 `side-by-side`、`flywheel samples`、`rule get` 等大体积数据时，优先搭配 `--brief` / `--summary` / `--summary-only` / `--limit` 或全局 `--out <path>` 先落盘后用脚本统计。

| 退出码 | 含义 | 常见处理建议 |
| :---: | :--- | :--- |
| `0` | 成功 | 解析 stdout JSON 结果 |
| `1` | 参数错误或后端 HTTP 4xx/5xx 业务异常 | 查看 stderr JSON `error.message` 修正入参 |
| `2` | 后端连接失败或请求超时 | 检查 `:9021` 服务是否启动，或改加 `--in-process` |
| `3` | 命令被当前权限策略拦截 (`POLICY_DENIED`) | 运行 `system policy --check <cmd_id>` 确认档案权限 |
| `4` | 破坏性命令缺少确认 (`CONFIRMATION_REQUIRED`) | 确认无误后在全局参数补加 `--yes` |
