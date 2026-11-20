# -*- coding: utf-8 -*-
"""
全量命令目录契约 (59 条后端 API + 2 条本地能力治理命令 = 61 条)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Literal, Tuple

RiskLevel = Literal["read", "run", "write", "destructive"]


@dataclass(frozen=True)
class CommandSpec:
    command_id: str
    group: str
    name: str
    risk: RiskLevel
    method: str
    endpoint: str
    summary: str
    supports_payload: bool = False
    is_streaming: bool = False
    is_async_task: bool = False


COMMAND_CATALOG: Tuple[CommandSpec, ...] = (
    # --- system (3) ---
    CommandSpec("system.health", "system", "health", "read", "GET", "/health", "检查平台后端健康状态"),
    CommandSpec("system.capabilities", "system", "capabilities", "read", "LOCAL", "local://capabilities", "列出当前策略允许的命令能力清单"),
    CommandSpec("system.policy", "system", "policy", "read", "LOCAL", "local://policy", "查看当前权限策略或预检某命令是否放行"),

    # --- model (6) ---
    CommandSpec("model.list", "model", "list", "read", "GET", "/api/v1/models/", "列出已注册模型"),
    CommandSpec("model.create", "model", "create", "write", "POST", "/api/v1/models/", "注册新模型", supports_payload=True),
    CommandSpec("model.update", "model", "update", "write", "PUT", "/api/v1/models/{model_id}", "更新模型配置", supports_payload=True),
    CommandSpec("model.delete", "model", "delete", "destructive", "DELETE", "/api/v1/models/{model_id}", "删除已注册模型"),
    CommandSpec("model.test", "model", "test", "run", "POST", "/api/v1/models/{model_id}/test", "测试模型连通性与思考探测"),
    CommandSpec("model.chat", "model", "chat", "run", "POST", "/api/v1/models/{model_id}/chat", "与指定模型进行多轮对话", supports_payload=True),

    # --- rule (6) ---
    CommandSpec("rule.list", "rule", "list", "read", "GET", "/api/v1/rules/", "列出场景规则配置"),
    CommandSpec("rule.get", "rule", "get", "read", "GET", "/api/v1/rules/{rule_id}", "获取规则完整 JSON 配置"),
    CommandSpec("rule.save", "rule", "save", "write", "POST", "/api/v1/rules/", "新建或更新规则配置", supports_payload=True),
    CommandSpec("rule.default-templates", "rule", "default-templates", "read", "GET", "/api/v1/rules/templates/defaults", "获取规则默认内置 Prompt 模板"),
    CommandSpec("rule.debug-prompt", "rule", "debug-prompt", "read", "POST", "/api/v1/rules/debug-prompt", "组装规则或对弈调试 Prompt 报文", supports_payload=True),
    CommandSpec("rule.test-prompt", "rule", "test-prompt", "run", "POST", "/api/v1/rules/test-prompt", "调用模型在线实测组装后的规则 Prompt", supports_payload=True),

    # --- prompt (5) ---
    CommandSpec("prompt.list", "prompt", "list", "read", "GET", "/api/v1/prompts/", "列出全平台提示词模板"),
    CommandSpec("prompt.get", "prompt", "get", "read", "GET", "/api/v1/prompts/{prompt_key}", "查看单个提示词模板详情"),
    CommandSpec("prompt.save", "prompt", "save", "write", "PUT", "/api/v1/prompts/{prompt_key}", "保存提示词模板自定义内容", supports_payload=True),
    CommandSpec("prompt.reset", "prompt", "reset", "write", "POST", "/api/v1/prompts/{prompt_key}/reset", "恢复提示词为系统内置默认模板"),
    CommandSpec("prompt.preview", "prompt", "preview", "read", "POST", "/api/v1/prompts/{prompt_key}/preview", "静态占位符替换预览提示词", supports_payload=True),

    # --- dataset (7) ---
    CommandSpec("dataset.list", "dataset", "list", "read", "GET", "/api/v1/datasets/", "列出本地数据资产目录"),
    CommandSpec("dataset.samples", "dataset", "samples", "read", "GET", "/api/v1/datasets/{dataset_id}/samples", "抽检数据集前 N 条样本"),
    CommandSpec("dataset.import", "dataset", "import", "write", "POST", "/api/v1/datasets/import", "导入评测集或微调语料", supports_payload=True),
    CommandSpec("dataset.delete", "dataset", "delete", "destructive", "DELETE", "/api/v1/datasets/{dataset_id}", "删除数据集资产及本地文件"),
    CommandSpec("dataset.filter-options", "dataset", "filter-options", "read", "GET", "/api/v1/datasets/{dataset_id}/filter-options", "获取评测集可用过滤维度与模型线"),
    CommandSpec("dataset.export-preview", "dataset", "export-preview", "read", "POST", "/api/v1/datasets/{dataset_id}/export/preview", "试算过滤导出题量与分布", supports_payload=True),
    CommandSpec("dataset.export", "dataset", "export", "write", "POST", "/api/v1/datasets/{dataset_id}/export", "按抽样及条件过滤导出评测集", supports_payload=True),

    # --- benchmark (11) ---
    CommandSpec("benchmark.run", "benchmark", "run", "run", "POST", "/api/v1/benchmarks/run", "发起常规评测对比任务（可选 --wait 阻塞等待）", supports_payload=True, is_async_task=True),
    CommandSpec("benchmark.tasks", "benchmark", "tasks", "read", "GET", "/api/v1/benchmarks/tasks", "列出历史评测任务"),
    CommandSpec("benchmark.status", "benchmark", "status", "read", "GET", "/api/v1/benchmarks/tasks/{task_id}", "查询评测任务状态与汇总指标"),
    CommandSpec("benchmark.side-by-side", "benchmark", "side-by-side", "read", "GET", "/api/v1/benchmarks/tasks/{task_id}/side_by_side", "查看评测 Side-by-Side 样本对比明细（支持本地分歧过滤）"),
    CommandSpec("benchmark.delete", "benchmark", "delete", "destructive", "DELETE", "/api/v1/benchmarks/tasks/{task_id}", "删除单个评测任务及报告文件"),
    CommandSpec("benchmark.batch-delete", "benchmark", "batch-delete", "destructive", "POST", "/api/v1/benchmarks/tasks/batch-delete", "批量删除评测任务", supports_payload=True),
    CommandSpec("benchmark.revoke-ledger", "benchmark", "revoke-ledger", "write", "POST", "/api/v1/benchmarks/tasks/{task_id}/revoke-ledger", "撤销指定评测任务的抽样战绩流水"),
    CommandSpec("benchmark.preview-sampling", "benchmark", "preview-sampling", "read", "POST", "/api/v1/benchmarks/preview-sampling", "试算动态阶梯降频抽样分布", supports_payload=True),
    CommandSpec("benchmark.reset-tracker", "benchmark", "reset-tracker", "destructive", "POST", "/api/v1/benchmarks/reset-tracker", "重置数据集历史抽样战绩流水", supports_payload=True),
    CommandSpec("benchmark.debug-judge", "benchmark", "debug-judge", "read", "POST", "/api/v1/benchmarks/debug-judge-prompt", "组装常规评测裁判调试 Prompt", supports_payload=True),
    CommandSpec("benchmark.test-judge", "benchmark", "test-judge", "run", "POST", "/api/v1/benchmarks/test-judge-prompt", "在线实测常规评测裁判 Prompt", supports_payload=True),

    # --- simulation (5) ---
    CommandSpec("simulation.run", "simulation", "run", "run", "POST", "/api/v1/simulation/simulate", "同步执行双 Agent 模拟对弈与质检修复", supports_payload=True),
    CommandSpec("simulation.stream", "simulation", "stream", "run", "POST", "/api/v1/simulation/simulate/stream", "流式消费 SSE 双 Agent 模拟对弈", supports_payload=True, is_streaming=True),
    CommandSpec("simulation.recent", "simulation", "recent", "read", "GET", "/api/v1/simulation/battles/recent", "查询近期模拟对弈历史记录"),
    CommandSpec("simulation.evaluate", "simulation", "evaluate", "run", "POST", "/api/v1/simulation/evaluate-dialogue", "对一段人工对话执行白盒质检与可选入库", supports_payload=True),
    CommandSpec("simulation.clear-all", "simulation", "clear-all", "destructive", "DELETE", "/api/v1/simulation/battles/all", "清空全部或指定规则下的历史对弈样本"),

    # --- flywheel (18) ---
    CommandSpec("flywheel.samples", "flywheel", "samples", "read", "GET", "/api/v1/flywheel/samples", "查询飞轮样本与质检修复状态"),
    CommandSpec("flywheel.batch", "flywheel", "batch", "run", "POST", "/api/v1/flywheel/batch", "提交存量语料批量飞轮治理异步任务", supports_payload=True, is_async_task=True),
    CommandSpec("flywheel.inspect-dataset", "flywheel", "inspect-dataset", "run", "POST", "/api/v1/flywheel/inspect-dataset", "对纯对话数据集批量规则质检打分（只检不修，可选 --wait）", supports_payload=True, is_async_task=True),
    CommandSpec("flywheel.inspect-report", "flywheel", "inspect-report", "read", "GET", "/api/v1/flywheel/inspect-tasks/{task_id}", "查看批量规则质检打分任务状态、汇总与明细报告"),
    CommandSpec("flywheel.judge", "flywheel", "judge", "run", "POST", "/api/v1/flywheel/samples/{sample_id}/judge", "对单个样本按指定规则重新白盒质检（只检不修）", supports_payload=True),
    CommandSpec("flywheel.batch-judge", "flywheel", "batch-judge", "run", "POST", "/api/v1/flywheel/samples/batch-judge", "批量按规则重新质检样本", supports_payload=True),
    CommandSpec("flywheel.remediate", "flywheel", "remediate", "run", "POST", "/api/v1/flywheel/samples/{sample_id}/remediate", "对单个样本触发靶向微创修复", supports_payload=True),
    CommandSpec("flywheel.batch-remediate", "flywheel", "batch-remediate", "run", "POST", "/api/v1/flywheel/samples/batch-remediate", "批量对样本触发定点修复", supports_payload=True),
    CommandSpec("flywheel.update", "flywheel", "update", "write", "PUT", "/api/v1/flywheel/samples/{sample_id}", "人工复核更新样本状态、对话或备注", supports_payload=True),
    CommandSpec("flywheel.batch-status", "flywheel", "batch-status", "write", "POST", "/api/v1/flywheel/samples/batch-status", "批量更新样本复核状态", supports_payload=True),
    CommandSpec("flywheel.delete", "flywheel", "delete", "destructive", "DELETE", "/api/v1/flywheel/samples/{sample_id}", "删除单条飞轮样本"),
    CommandSpec("flywheel.batch-delete", "flywheel", "batch-delete", "destructive", "POST", "/api/v1/flywheel/samples/batch-delete", "批量删除飞轮样本", supports_payload=True),
    CommandSpec("flywheel.slices-get", "flywheel", "slices-get", "read", "GET", "/api/v1/flywheel/samples/{sample_id}/slices", "获取或按需生成样本渐进切题评测切片"),
    CommandSpec("flywheel.slices-update", "flywheel", "slices-update", "write", "PUT", "/api/v1/flywheel/samples/{sample_id}/slices", "保存人工微调的评测切片并双向同步对话", supports_payload=True),
    CommandSpec("flywheel.export-gold", "flywheel", "export-gold", "write", "POST", "/api/v1/flywheel/export", "按规则导出达标黄金微调 SFT 数据集"),
    CommandSpec("flywheel.export-selected", "flywheel", "export-selected", "write", "POST", "/api/v1/flywheel/samples/export-selected", "导出手动勾选样本为标准 messages 数据集", supports_payload=True),
    CommandSpec("flywheel.export-benchmark", "flywheel", "export-benchmark", "write", "POST", "/api/v1/flywheel/samples/{sample_id}/export-benchmark", "将单样本切片导出注册为留出 Benchmark 评测集", supports_payload=True),
    CommandSpec("flywheel.batch-export-benchmark", "flywheel", "batch-export-benchmark", "write", "POST", "/api/v1/flywheel/batch-export-benchmark", "批量将多个样本切片合并导出为 Benchmark 评测集", supports_payload=True),
)

COMMAND_MAP: Dict[str, CommandSpec] = {spec.command_id: spec for spec in COMMAND_CATALOG}

GROUP_DESCRIPTIONS: Dict[str, str] = {
    "system": "平台健康检查、可用能力探测与策略自省",
    "model": "模型注册、参数更新、连通性测试与自由对话",
    "rule": "场景 SOP 规则管理与 7 类 Prompt 调试实测",
    "prompt": "全平台 8 大内置提示词模板管理与静态预览",
    "dataset": "评测集与训练集资产导入、抽检与过滤导出",
    "benchmark": "常规评测执行、分歧筛选对比与动态抽样账本治理",
    "simulation": "双 Agent 模拟对弈（同步/SSE流式）与自由对话质检",
    "flywheel": "飞轮样本治理、重检/重修、渐进切题与数据集导出",
}


def get_command_spec(command_id: str) -> CommandSpec:
    return COMMAND_MAP[command_id]


def list_groups() -> List[str]:
    seen: List[str] = []
    for spec in COMMAND_CATALOG:
        if spec.group not in seen:
            seen.append(spec.group)
    return seen
