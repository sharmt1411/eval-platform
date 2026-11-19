# -*- coding: utf-8 -*-
"""benchmark 命令组：常规评测、分歧筛选对比与动态抽样账本治理"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import click

from cli.client import CliError
from cli.config import EXIT_API_ERROR
from cli.utils import (
    check_command_allowed,
    emit_output,
    ensure_client,
    filter_side_by_side,
    load_payload,
    parse_json_arg,
    parse_str_list,
    quote_path_segment,
    read_text_file,
    wait_for_task,
)


@click.command("run")
@click.option("--dataset-path", default=None, help="评测集路径（如 data/datasets/demo_benchmark.jsonl）")
@click.option("--model-id", "model_ids", multiple=True, help="参评模型 ID（可多次传入或逗号分隔）")
@click.option("--judge-model-id", default=None, help="主观裁判模型 ID")
@click.option("--workers", type=int, default=None, help="并发线程数")
@click.option("--max-tokens", type=int, default=None, help="统一最大生成 Token 上限")
@click.option("--ignore-sample-max-tokens/--no-ignore-sample-max-tokens", default=None, help="是否忽略样本级 max_new_tokens 限制")
@click.option("--sampling-json", default=None, help="智能降频抽样配置 JSON")
@click.option("--payload", "payload_file", default=None, help="完整 BenchmarkRunRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 BenchmarkRunRequest JSON 字符串")
@click.option("--wait", is_flag=True, help="提交后轮询等待直至评测结束")
@click.option("--poll-interval", type=float, default=1.5, show_default=True, help="--wait 轮询间隔秒数")
@click.option("--wait-timeout", type=float, default=600.0, show_default=True, help="--wait 最大等待秒数")
@click.pass_context
def run_benchmark_cmd(
    ctx: click.Context,
    dataset_path: Optional[str],
    model_ids: Tuple[str, ...],
    judge_model_id: Optional[str],
    workers: Optional[int],
    max_tokens: Optional[int],
    ignore_sample_max_tokens: Optional[bool],
    sampling_json: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
    wait: bool,
    poll_interval: float,
    wait_timeout: float,
) -> None:
    """发起常规多模型横向对比评测任务"""
    check_command_allowed(ctx, "benchmark.run")
    overrides: Dict[str, Any] = {
        "dataset_path": dataset_path,
        "judge_model_id": judge_model_id,
        "workers": workers,
        "max_tokens": max_tokens,
        "ignore_sample_max_tokens": ignore_sample_max_tokens,
    }
    if model_ids:
        overrides["model_ids"] = parse_str_list(model_ids)
    if sampling_json is not None:
        overrides["sampling_config"] = parse_json_arg(sampling_json, "--sampling-json")

    body = load_payload(payload_file, payload_json, overrides)
    if not body.get("dataset_path"):
        raise CliError("MISSING_PARAM", "请提供 --dataset-path 或在 --payload 中包含 dataset_path", EXIT_API_ERROR)

    client = ensure_client(ctx)
    submit_res = client.request_json("POST", "/api/v1/benchmarks/run", json_body=body)
    if not wait or not isinstance(submit_res, dict) or not submit_res.get("task_id"):
        emit_output(ctx, submit_res)
        return

    status_data = wait_for_task(
        ctx,
        client,
        submit_res["task_id"],
        poll_interval=poll_interval,
        wait_timeout=wait_timeout,
        label="benchmark",
        fail_code="BENCHMARK_TASK_FAILED",
    )
    emit_output(ctx, status_data)


@click.command("tasks")
@click.option("--limit", type=int, default=50, show_default=True, help="最多返回任务数")
@click.pass_context
def list_tasks_cmd(ctx: click.Context, limit: int) -> None:
    """列出历史评测任务"""
    check_command_allowed(ctx, "benchmark.tasks")
    client = ensure_client(ctx)
    data = client.request_json("GET", "/api/v1/benchmarks/tasks", params={"limit": limit})
    emit_output(ctx, data)


@click.command("status")
@click.argument("task_id")
@click.pass_context
def task_status_cmd(ctx: click.Context, task_id: str) -> None:
    """查询指定评测任务状态与汇总指标"""
    check_command_allowed(ctx, "benchmark.status")
    client = ensure_client(ctx)
    data = client.request_json("GET", f"/api/v1/benchmarks/tasks/{quote_path_segment(task_id)}")
    emit_output(ctx, data)


@click.command("side-by-side")
@click.argument("task_id")
@click.option(
    "--filter",
    "filter_type",
    default="all",
    show_default=True,
    type=click.Choice(["all", "divergence", "disagree", "all_fail", "fail", "all_pass", "pass", "unknown", "api_error"], case_sensitive=False),
    help="按对比结论筛选样本（快速定位分歧/失败/API异常）",
)
@click.option("--category", default=None, help="按题目分类过滤")
@click.option("--difficulty", default=None, help="按题目难度过滤")
@click.option("--sample-id", default=None, help="精确查看单个 sample_id")
@click.option("--offset", type=int, default=0, show_default=True, help="分页偏移")
@click.option("--limit", type=int, default=None, help="最多返回样本条数")
@click.option("--summary-only", is_flag=True, help="只返回各模型状态与打分摘要，省略长回复正文")
@click.option("--raw", is_flag=True, help="原样输出后端返回的完整 side_by_side 字典")
@click.pass_context
def side_by_side_cmd(
    ctx: click.Context,
    task_id: str,
    filter_type: str,
    category: Optional[str],
    difficulty: Optional[str],
    sample_id: Optional[str],
    offset: int,
    limit: Optional[int],
    summary_only: bool,
    raw: bool,
) -> None:
    """查看评测任务 Side-by-Side 对比明细（支持本地分歧过滤与截断防刷爆上下文）"""
    check_command_allowed(ctx, "benchmark.side-by-side")
    client = ensure_client(ctx)
    data = client.request_json("GET", f"/api/v1/benchmarks/tasks/{quote_path_segment(task_id)}/side_by_side")
    if raw or not isinstance(data, dict):
        emit_output(ctx, data)
        return
    filtered = filter_side_by_side(
        data,
        filter_type=filter_type,
        category=category,
        difficulty=difficulty,
        sample_id=sample_id,
        offset=offset,
        limit=limit,
        summary_only=summary_only,
    )
    filtered["task_id"] = task_id
    emit_output(ctx, filtered)


@click.command("delete")
@click.argument("task_id")
@click.pass_context
def delete_task_cmd(ctx: click.Context, task_id: str) -> None:
    """删除单个评测任务及本地报告文件"""
    check_command_allowed(ctx, "benchmark.delete")
    client = ensure_client(ctx)
    data = client.request_json("DELETE", f"/api/v1/benchmarks/tasks/{quote_path_segment(task_id)}")
    emit_output(ctx, data)


@click.command("batch-delete")
@click.option("--task-id", "task_ids", multiple=True, help="待删除任务 ID（可多次传入或逗号分隔）")
@click.option("--payload", "payload_file", default=None, help="完整请求 JSON 文件")
@click.option("--payload-json", default=None, help="完整请求 JSON 字符串")
@click.pass_context
def batch_delete_tasks_cmd(
    ctx: click.Context,
    task_ids: Tuple[str, ...],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """批量删除评测任务"""
    check_command_allowed(ctx, "benchmark.batch-delete")
    overrides = {"task_ids": parse_str_list(task_ids)} if task_ids else None
    body = load_payload(payload_file, payload_json, overrides)
    if not body.get("task_ids"):
        raise CliError("MISSING_PARAM", "请提供 --task-id 或 --payload", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/benchmarks/tasks/batch-delete", json_body=body)
    emit_output(ctx, data)


@click.command("revoke-ledger")
@click.argument("task_id")
@click.pass_context
def revoke_ledger_cmd(ctx: click.Context, task_id: str) -> None:
    """撤销指定评测任务的抽样战绩流水"""
    check_command_allowed(ctx, "benchmark.revoke-ledger")
    client = ensure_client(ctx)
    data = client.request_json("POST", f"/api/v1/benchmarks/tasks/{quote_path_segment(task_id)}/revoke-ledger")
    emit_output(ctx, data)


@click.command("preview-sampling")
@click.option("--dataset-path", default=None, help="数据集路径")
@click.option("--enabled/--no-enabled", default=None, help="是否开启阶梯降频")
@click.option("--model-line", default=None, help="基准模型线标识")
@click.option("--version-tag", default=None, help="本次评测版本号")
@click.option("--benchmark-model-id", default=None, help="基准模型 ID")
@click.option("--pass-threshold-x", type=int, default=None, help="连续高分达标门槛 X")
@click.option("--interval-step-s", type=int, default=None, help="冷却间隔步长 S")
@click.option("--max-interval-n", type=int, default=None, help="最大冷却周期 N")
@click.option("--min-pass-score", type=float, default=None, help="判定通过最低分")
@click.option("--sampling-json", default=None, help="SamplingFilterConfig JSON")
@click.option("--payload", "payload_file", default=None, help="完整请求 JSON 文件")
@click.option("--payload-json", default=None, help="完整请求 JSON 字符串")
@click.pass_context
def preview_sampling_cmd(
    ctx: click.Context,
    dataset_path: Optional[str],
    enabled: Optional[bool],
    model_line: Optional[str],
    version_tag: Optional[str],
    benchmark_model_id: Optional[str],
    pass_threshold_x: Optional[int],
    interval_step_s: Optional[int],
    max_interval_n: Optional[int],
    min_pass_score: Optional[float],
    sampling_json: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """试算动态阶梯降频抽样分布与算力节省度"""
    check_command_allowed(ctx, "benchmark.preview-sampling")
    body = load_payload(payload_file, payload_json, {"dataset_path": dataset_path})
    sc: Dict[str, Any] = dict(body.get("sampling_config") or {})
    if sampling_json is not None:
        parsed = parse_json_arg(sampling_json, "--sampling-json")
        if not isinstance(parsed, dict):
            raise CliError("INVALID_PAYLOAD", "--sampling-json 必须是 JSON 对象", EXIT_API_ERROR)
        sc.update(parsed)
    for k, v in {
        "enabled": enabled,
        "model_line": model_line,
        "version_tag": version_tag,
        "benchmark_model_id": benchmark_model_id,
        "pass_threshold_x": pass_threshold_x,
        "interval_step_s": interval_step_s,
        "max_interval_n": max_interval_n,
        "min_pass_score": min_pass_score,
    }.items():
        if v is not None:
            sc[k] = v
    sc.setdefault("tracker_model_keys", [])
    body["sampling_config"] = sc
    if not body.get("dataset_path"):
        raise CliError("MISSING_PARAM", "请提供 --dataset-path", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/benchmarks/preview-sampling", json_body=body)
    emit_output(ctx, data)


@click.command("reset-tracker")
@click.option("--dataset-path", default=None, help="数据集路径")
@click.option("--model-line", default=None, help="仅重置指定模型线（留空则重置全部模型线）")
@click.option("--payload", "payload_file", default=None, help="完整请求 JSON 文件")
@click.option("--payload-json", default=None, help="完整请求 JSON 字符串")
@click.pass_context
def reset_tracker_cmd(
    ctx: click.Context,
    dataset_path: Optional[str],
    model_line: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """作废指定数据集历史抽样战绩流水"""
    check_command_allowed(ctx, "benchmark.reset-tracker")
    body = load_payload(
        payload_file,
        payload_json,
        {"dataset_path": dataset_path, "model_line": model_line},
    )
    if not body.get("dataset_path"):
        raise CliError("MISSING_PARAM", "请提供 --dataset-path", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/benchmarks/reset-tracker", json_body=body)
    emit_output(ctx, data)


@click.command("debug-judge")
@click.option("--dataset-path", default=None, help="样本所属数据集路径")
@click.option("--sample-id", default=None, help="指定样本 ID")
@click.option("--model-answer", default=None, help="模拟待测模型回答文本")
@click.option("--template-override", default=None, help="临时覆盖裁判 Prompt 模板")
@click.option("--template-file", default=None, help="从文件读取临时覆盖模板")
@click.option("--payload", "payload_file", default=None, help="完整请求 JSON 文件")
@click.option("--payload-json", default=None, help="完整请求 JSON 字符串")
@click.pass_context
def debug_judge_cmd(
    ctx: click.Context,
    dataset_path: Optional[str],
    sample_id: Optional[str],
    model_answer: Optional[str],
    template_override: Optional[str],
    template_file: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """基于真实评测样本组装常规裁判 Prompt 报文"""
    check_command_allowed(ctx, "benchmark.debug-judge")
    overrides: Dict[str, Any] = {
        "dataset_path": dataset_path,
        "sample_id": sample_id,
        "model_answer": model_answer,
        "template_override": read_text_file(template_file) if template_file else template_override,
    }
    body = load_payload(payload_file, payload_json, overrides)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/benchmarks/debug-judge-prompt", json_body=body)
    emit_output(ctx, data)


@click.command("test-judge")
@click.option("--model-id", default=None, help="已注册裁判模型 ID")
@click.option("--assembled-prompt", default=None, help="组装后的裁判 Prompt 文本（覆盖 payload 中的 assembled_messages）")
@click.option("--assembled-prompt-file", default=None, help="从文件读取组装后的裁判 Prompt（覆盖 payload 中的 assembled_messages）")
@click.option("--payload", "payload_file", default=None, help="完整请求 JSON 文件（可直接接 debug-judge 输出）")
@click.option("--payload-json", default=None, help="完整请求 JSON 字符串")
@click.pass_context
def test_judge_cmd(
    ctx: click.Context,
    model_id: Optional[str],
    assembled_prompt: Optional[str],
    assembled_prompt_file: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """调用已注册裁判模型在线实测常规裁判 Prompt"""
    check_command_allowed(ctx, "benchmark.test-judge")
    prompt_override = read_text_file(assembled_prompt_file) if assembled_prompt_file else assembled_prompt
    overrides: Dict[str, Any] = {
        "model_id": model_id,
        "assembled_prompt": prompt_override,
    }
    body = load_payload(payload_file, payload_json, overrides)
    if prompt_override is not None:
        body.pop("assembled_messages", None)
    if not body.get("model_id") or not body.get("assembled_prompt"):
        raise CliError("MISSING_PARAM", "请提供 --model-id 与 --assembled-prompt（或通过 --payload 传入）", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/benchmarks/test-judge-prompt", json_body=body)
    emit_output(ctx, data)


def register_commands(group: click.Group) -> None:
    group.add_command(run_benchmark_cmd)
    group.add_command(list_tasks_cmd)
    group.add_command(task_status_cmd)
    group.add_command(side_by_side_cmd)
    group.add_command(delete_task_cmd)
    group.add_command(batch_delete_tasks_cmd)
    group.add_command(revoke_ledger_cmd)
    group.add_command(preview_sampling_cmd)
    group.add_command(reset_tracker_cmd)
    group.add_command(debug_judge_cmd)
    group.add_command(test_judge_cmd)
