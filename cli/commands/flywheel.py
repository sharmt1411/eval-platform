# -*- coding: utf-8 -*-
"""flywheel 命令组：样本查询、单条/批量重检与微创修复、人工复核、切题同步与数据集导出"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import click

from cli.client import CliError
from cli.config import EXIT_API_ERROR
from cli.utils import (
    check_command_allowed,
    emit_output,
    ensure_client,
    load_payload,
    parse_str_list,
    quote_path_segment,
    read_json_input,
    wait_for_task,
)

FLYWHEEL_STATUS_CHOICES = click.Choice(
    ["PENDING", "GOLD_PASS", "REPAIRED_PASS", "DISCARDED", "MANUAL_APPROVED"],
    case_sensitive=False,
)


def _merge_dedup_config(
    body: Dict[str, Any],
    *,
    enable_dedup: Optional[bool],
    cluster_quota: Optional[int],
    sim_threshold: Optional[float],
    target_count: Optional[int],
) -> None:
    """将 CLI 去重参数仅在显式传参时并入 body.dedup_config，防止覆盖 --payload 配置。"""
    if (
        enable_dedup is None
        and cluster_quota is None
        and sim_threshold is None
        and target_count is None
    ):
        return
    cfg: Dict[str, Any] = dict(body.get("dedup_config") or {})
    if enable_dedup is not None:
        cfg["enabled"] = enable_dedup
    else:
        cfg.setdefault("enabled", True)
    if cluster_quota is not None:
        cfg["cluster_quota"] = cluster_quota
    if sim_threshold is not None:
        cfg["answer_sim_threshold"] = sim_threshold
    if target_count is not None:
        cfg["target_total_count"] = target_count
    body["dedup_config"] = cfg


def _brief_flywheel_sample(item: Dict[str, Any]) -> Dict[str, Any]:
    init_eval = item.get("initial_eval") or {}
    ver_eval = item.get("verified_eval") or {}
    raw_msgs = item.get("raw_messages") or []
    remed_msgs = item.get("remediated_messages")
    return {
        "id": item.get("id"),
        "task_id": item.get("task_id"),
        "rule_id": item.get("rule_id"),
        "scenario_id": item.get("scenario_id"),
        "status": item.get("status"),
        "raw_verdict": item.get("raw_verdict"),
        "remediated_verdict": item.get("remediated_verdict"),
        "manual_decision": item.get("manual_decision"),
        "active_version": item.get("active_version"),
        "score": item.get("score"),
        "raw_turns": len(raw_msgs) if isinstance(raw_msgs, list) else 0,
        "remediated_turns": len(remed_msgs) if isinstance(remed_msgs, list) else None,
        "initial_verdict": init_eval.get("verdict") if isinstance(init_eval, dict) else None,
        "initial_defects": init_eval.get("detected_defects") if isinstance(init_eval, dict) else None,
        "verified_verdict": ver_eval.get("verdict") if isinstance(ver_eval, dict) else None,
        "exported_to_benchmark": item.get("exported_to_benchmark"),
        "exported_training_datasets": item.get("exported_training_datasets") or [],
        "exported_benchmark_datasets": item.get("exported_benchmark_datasets") or [],
        "reviewer_comment": item.get("reviewer_comment"),
        "updated_at": item.get("updated_at"),
    }


@click.command("samples")
@click.option("--task-id", default=None, help="按对弈/任务 ID 过滤")
@click.option("--status", default=None, help="按治理状态过滤（PENDING/GOLD_PASS/REPAIRED_PASS/DISCARDED/MANUAL_APPROVED）")
@click.option("--rule-id", default=None, help="按规则 ID 过滤")
@click.option("--limit", type=int, default=100, show_default=True, help="返回最大样本数")
@click.option("--brief", is_flag=True, help="返回紧凑摘要，省略大体积对话正文与调试请求")
@click.pass_context
def list_samples_cmd(
    ctx: click.Context,
    task_id: Optional[str],
    status: Optional[str],
    rule_id: Optional[str],
    limit: int,
    brief: bool,
) -> None:
    """查询飞轮复核样本池"""
    check_command_allowed(ctx, "flywheel.samples")
    client = ensure_client(ctx)
    data = client.request_json(
        "GET",
        "/api/v1/flywheel/samples",
        params={"task_id": task_id, "status": status, "rule_id": rule_id, "limit": limit},
    )
    if brief and isinstance(data, list):
        data = [_brief_flywheel_sample(item) for item in data if isinstance(item, dict)]
    emit_output(ctx, data)


@click.command("batch")
@click.option("--rule-id", default=None, help="规则 ID")
@click.option("--input-file-path", default=None, help="待治理原始对话 JSONL 文件路径")
@click.option("--judge-model-id", default=None, help="质检裁判模型 ID")
@click.option("--remediate-model-id", default=None, help="修复模型 ID")
@click.option("--gold-output-path", default=None, help="黄金 SFT 输出路径")
@click.option("--discard-output-path", default=None, help="淘汰样本输出路径")
@click.option("--workers", type=int, default=None, help="并发线程数")
@click.option("--enable-dedup/--no-dedup", default=None, help="是否对导出黄金集启用去重防稀释")
@click.option("--cluster-quota", type=int, default=None, help="单簇保留上限 K")
@click.option("--sim-threshold", type=float, default=None, help="同复拆簇余弦相似度阈值 θ")
@click.option("--target-count", type=int, default=None, help="可选目标总条数，未满时差异最大化补足")
@click.option("--payload", "payload_file", default=None, help="完整 BatchFlywheelRunRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 BatchFlywheelRunRequest JSON 字符串")
@click.option("--wait", is_flag=True, help="提交后轮询等待直至批量飞轮治理完成")
@click.option("--poll-interval", type=float, default=1.5, show_default=True, help="--wait 轮询间隔秒数")
@click.option("--wait-timeout", type=float, default=600.0, show_default=True, help="--wait 最大等待秒数")
@click.pass_context
def batch_flywheel_cmd(
    ctx: click.Context,
    rule_id: Optional[str],
    input_file_path: Optional[str],
    judge_model_id: Optional[str],
    remediate_model_id: Optional[str],
    gold_output_path: Optional[str],
    discard_output_path: Optional[str],
    workers: Optional[int],
    enable_dedup: Optional[bool],
    cluster_quota: Optional[int],
    sim_threshold: Optional[float],
    target_count: Optional[int],
    payload_file: Optional[str],
    payload_json: Optional[str],
    wait: bool,
    poll_interval: float,
    wait_timeout: float,
) -> None:
    """提交存量数据集批量飞轮治理异步任务"""
    check_command_allowed(ctx, "flywheel.batch")
    body = load_payload(
        payload_file,
        payload_json,
        {
            "rule_id": rule_id,
            "input_file_path": input_file_path,
            "judge_model_id": judge_model_id,
            "remediate_model_id": remediate_model_id,
            "gold_output_path": gold_output_path,
            "discard_output_path": discard_output_path,
            "workers": workers,
        },
    )
    _merge_dedup_config(
        body,
        enable_dedup=enable_dedup,
        cluster_quota=cluster_quota,
        sim_threshold=sim_threshold,
        target_count=target_count,
    )
    for req_field in ("rule_id", "input_file_path", "judge_model_id"):
        if not body.get(req_field):
            raise CliError("MISSING_PARAM", f"缺少必填字段: {req_field}", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/flywheel/batch", json_body=body)
    if not wait or not isinstance(data, dict) or not data.get("task_id"):
        emit_output(ctx, data)
        return

    status_data = wait_for_task(
        ctx,
        client,
        data["task_id"],
        poll_interval=poll_interval,
        wait_timeout=wait_timeout,
        label="flywheel",
        fail_code="FLYWHEEL_BATCH_FAILED",
    )
    emit_output(ctx, status_data)


@click.command("judge")
@click.argument("sample_id")
@click.option("--rule-id", default=None, help="质检规则 ID")
@click.option("--judge-model-id", default=None, help="质检裁判模型 ID")
@click.option("--target-data", default=None, type=click.Choice(["raw", "remediated"], case_sensitive=False), help="质检目标 (raw=原会话, remediated=修复后语料)")
@click.option("--payload", "payload_file", default=None, help="完整 SingleJudgeRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 SingleJudgeRequest JSON 字符串")
@click.pass_context
def judge_sample_cmd(
    ctx: click.Context,
    sample_id: str,
    rule_id: Optional[str],
    judge_model_id: Optional[str],
    target_data: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """对单条飞轮样本按指定规则重新白盒质检（只检不修）"""
    check_command_allowed(ctx, "flywheel.judge")
    body = load_payload(
        payload_file,
        payload_json,
        {"rule_id": rule_id, "judge_model_id": judge_model_id, "target_data": target_data},
    )
    if not body.get("rule_id") or not body.get("judge_model_id"):
        raise CliError("MISSING_PARAM", "请提供 --rule-id 与 --judge-model-id", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json(
        "POST",
        f"/api/v1/flywheel/samples/{quote_path_segment(sample_id)}/judge",
        json_body=body,
    )
    emit_output(ctx, data)


@click.command("batch-judge")
@click.option("--sample-id", "sample_ids", multiple=True, help="样本 ID（可多次传入或逗号分隔）")
@click.option("--rule-id", default=None, help="质检规则 ID")
@click.option("--judge-model-id", default=None, help="质检裁判模型 ID")
@click.option("--payload", "payload_file", default=None, help="完整 BatchJudgeRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 BatchJudgeRequest JSON 字符串")
@click.pass_context
def batch_judge_cmd(
    ctx: click.Context,
    sample_ids: Tuple[str, ...],
    rule_id: Optional[str],
    judge_model_id: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """批量对选定样本执行白盒质检"""
    check_command_allowed(ctx, "flywheel.batch-judge")
    overrides: Dict[str, Any] = {"rule_id": rule_id, "judge_model_id": judge_model_id}
    if sample_ids:
        overrides["sample_ids"] = parse_str_list(sample_ids)
    body = load_payload(payload_file, payload_json, overrides)
    if not body.get("sample_ids") or not body.get("rule_id") or not body.get("judge_model_id"):
        raise CliError("MISSING_PARAM", "请提供 --sample-id、--rule-id 与 --judge-model-id", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/flywheel/samples/batch-judge", json_body=body)
    emit_output(ctx, data)


@click.command("remediate")
@click.argument("sample_id")
@click.option("--rule-id", default=None, help="规则 ID（默认继承样本原规则）")
@click.option("--judge-model-id", default=None, help="裁判模型 ID（开启初检/复检时使用）")
@click.option("--remediate-model-id", default=None, help="修复模型 ID")
@click.option("--run-initial-judge/--no-run-initial-judge", default=None, help="修复前是否重跑初检")
@click.option("--skip-if-gold-pass/--no-skip-if-gold-pass", default=None, help="初检达标时是否跳过修复")
@click.option("--run-verified-judge/--no-run-verified-judge", default=None, help="修复后是否继续二次质检验收")
@click.option("--payload", "payload_file", default=None, help="完整 SingleRemediateRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 SingleRemediateRequest JSON 字符串")
@click.pass_context
def remediate_sample_cmd(
    ctx: click.Context,
    sample_id: str,
    rule_id: Optional[str],
    judge_model_id: Optional[str],
    remediate_model_id: Optional[str],
    run_initial_judge: Optional[bool],
    skip_if_gold_pass: Optional[bool],
    run_verified_judge: Optional[bool],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """对单条样本触发靶向微创修复"""
    check_command_allowed(ctx, "flywheel.remediate")
    body = load_payload(
        payload_file,
        payload_json,
        {
            "rule_id": rule_id,
            "judge_model_id": judge_model_id,
            "remediate_model_id": remediate_model_id,
            "run_initial_judge": run_initial_judge,
            "skip_if_gold_pass": skip_if_gold_pass,
            "run_verified_judge": run_verified_judge,
        },
    )
    client = ensure_client(ctx)
    data = client.request_json(
        "POST",
        f"/api/v1/flywheel/samples/{quote_path_segment(sample_id)}/remediate",
        json_body=body,
    )
    emit_output(ctx, data)


@click.command("batch-remediate")
@click.option("--sample-id", "sample_ids", multiple=True, help="样本 ID（可多次传入或逗号分隔）")
@click.option("--rule-id", default=None, help="统一指定规则 ID")
@click.option("--judge-model-id", default=None, help="裁判模型 ID")
@click.option("--remediate-model-id", default=None, help="修复模型 ID")
@click.option("--run-initial-judge/--no-run-initial-judge", default=None, help="修复前是否重跑初检")
@click.option("--skip-if-gold-pass/--no-skip-if-gold-pass", default=None, help="初检达标时是否跳过修复")
@click.option("--run-verified-judge/--no-run-verified-judge", default=None, help="修复后是否执行二次验收")
@click.option("--payload", "payload_file", default=None, help="完整 BatchRemediateRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 BatchRemediateRequest JSON 字符串")
@click.pass_context
def batch_remediate_cmd(
    ctx: click.Context,
    sample_ids: Tuple[str, ...],
    rule_id: Optional[str],
    judge_model_id: Optional[str],
    remediate_model_id: Optional[str],
    run_initial_judge: Optional[bool],
    skip_if_gold_pass: Optional[bool],
    run_verified_judge: Optional[bool],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """批量对选定样本触发定点修复"""
    check_command_allowed(ctx, "flywheel.batch-remediate")
    overrides: Dict[str, Any] = {
        "rule_id": rule_id,
        "judge_model_id": judge_model_id,
        "remediate_model_id": remediate_model_id,
        "run_initial_judge": run_initial_judge,
        "skip_if_gold_pass": skip_if_gold_pass,
        "run_verified_judge": run_verified_judge,
    }
    if sample_ids:
        overrides["sample_ids"] = parse_str_list(sample_ids)
    body = load_payload(payload_file, payload_json, overrides)
    if not body.get("sample_ids"):
        raise CliError("MISSING_PARAM", "请提供 --sample-id 或 --payload", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/flywheel/samples/batch-remediate", json_body=body)
    emit_output(ctx, data)


@click.command("update")
@click.argument("sample_id")
@click.option("--status", default=None, type=FLYWHEEL_STATUS_CHOICES, help="复核状态 (GOLD_PASS/REPAIRED_PASS/DISCARDED/MANUAL_APPROVED/PENDING)")
@click.option("--active-version", default=None, type=click.Choice(["raw", "remediated"], case_sensitive=False), help="指定生效对话版本 (raw/remediated)")
@click.option("--reviewer-comment", default=None, help="人工评审备注")
@click.option("--messages-file", default=None, help="人工修订后的 remediated_messages JSON 文件")
@click.option("--payload", "payload_file", default=None, help="完整 SampleReviewUpdateRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 SampleReviewUpdateRequest JSON 字符串")
@click.pass_context
def update_sample_cmd(
    ctx: click.Context,
    sample_id: str,
    status: Optional[str],
    active_version: Optional[str],
    reviewer_comment: Optional[str],
    messages_file: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """人工复核更新单条样本状态、生效版本、语料或备注"""
    check_command_allowed(ctx, "flywheel.update")
    overrides: Dict[str, Any] = {
        "status": status.upper() if status else None,
        "active_version": active_version.lower() if active_version else None,
        "reviewer_comment": reviewer_comment,
    }
    if messages_file:
        overrides["remediated_messages"] = read_json_input(messages_file)
    body = load_payload(payload_file, payload_json, overrides)
    client = ensure_client(ctx)
    data = client.request_json(
        "PUT",
        f"/api/v1/flywheel/samples/{quote_path_segment(sample_id)}",
        json_body=body,
    )
    emit_output(ctx, data)


@click.command("batch-status")
@click.option("--sample-id", "sample_ids", multiple=True, help="样本 ID（可多次传入或逗号分隔）")
@click.option("--status", default=None, type=FLYWHEEL_STATUS_CHOICES, help="目标状态")
@click.option("--active-version", default=None, type=click.Choice(["raw", "remediated"], case_sensitive=False), help="统一指定生效对话版本 (raw/remediated)")
@click.option("--reviewer-comment", default=None, help="批量评审备注")
@click.option("--payload", "payload_file", default=None, help="完整 BatchUpdateStatusRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 BatchUpdateStatusRequest JSON 字符串")
@click.pass_context
def batch_status_cmd(
    ctx: click.Context,
    sample_ids: Tuple[str, ...],
    status: Optional[str],
    active_version: Optional[str],
    reviewer_comment: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """批量更新样本复核状态"""
    check_command_allowed(ctx, "flywheel.batch-status")
    overrides: Dict[str, Any] = {
        "status": status.upper() if status else None,
        "active_version": active_version.lower() if active_version else None,
        "reviewer_comment": reviewer_comment,
    }
    if sample_ids:
        overrides["sample_ids"] = parse_str_list(sample_ids)
    body = load_payload(payload_file, payload_json, overrides)
    if not body.get("sample_ids") or not body.get("status"):
        raise CliError("MISSING_PARAM", "请提供 --sample-id 与 --status", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/flywheel/samples/batch-status", json_body=body)
    emit_output(ctx, data)


@click.command("delete")
@click.argument("sample_id")
@click.pass_context
def delete_sample_cmd(ctx: click.Context, sample_id: str) -> None:
    """删除单条飞轮样本"""
    check_command_allowed(ctx, "flywheel.delete")
    client = ensure_client(ctx)
    data = client.request_json("DELETE", f"/api/v1/flywheel/samples/{quote_path_segment(sample_id)}")
    emit_output(ctx, data)


@click.command("batch-delete")
@click.option("--sample-id", "sample_ids", multiple=True, help="待删除样本 ID")
@click.option("--payload", "payload_file", default=None, help="完整 BatchDeleteSamplesRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 BatchDeleteSamplesRequest JSON 字符串")
@click.pass_context
def batch_delete_samples_cmd(
    ctx: click.Context,
    sample_ids: Tuple[str, ...],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """批量删除飞轮样本"""
    check_command_allowed(ctx, "flywheel.batch-delete")
    overrides = {"sample_ids": parse_str_list(sample_ids)} if sample_ids else None
    body = load_payload(payload_file, payload_json, overrides)
    if not body.get("sample_ids"):
        raise CliError("MISSING_PARAM", "请提供 --sample-id 或 --payload", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/flywheel/samples/batch-delete", json_body=body)
    emit_output(ctx, data)


@click.command("slices-get")
@click.argument("sample_id")
@click.pass_context
def get_slices_cmd(ctx: click.Context, sample_id: str) -> None:
    """读取或按需生成单条样本的渐进切题评测切片"""
    check_command_allowed(ctx, "flywheel.slices-get")
    client = ensure_client(ctx)
    data = client.request_json("GET", f"/api/v1/flywheel/samples/{quote_path_segment(sample_id)}/slices")
    emit_output(ctx, data)


@click.command("slices-update")
@click.argument("sample_id")
@click.option("--slices-file", default=None, help="微调后的切片数组 JSON 文件")
@click.option("--sync-to-dialogue/--no-sync-to-dialogue", default=None, help="是否同步回写修复后对话")
@click.option("--payload", "payload_file", default=None, help="完整 UpdateBenchmarkSlicesRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 UpdateBenchmarkSlicesRequest JSON 字符串")
@click.pass_context
def update_slices_cmd(
    ctx: click.Context,
    sample_id: str,
    slices_file: Optional[str],
    sync_to_dialogue: Optional[bool],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """保存人工微调切片并双向同步至多轮对话"""
    check_command_allowed(ctx, "flywheel.slices-update")
    overrides: Dict[str, Any] = {"sync_to_dialogue": sync_to_dialogue}
    if slices_file:
        overrides["slices"] = read_json_input(slices_file)
    body = load_payload(payload_file, payload_json, overrides)
    if "slices" not in body:
        raise CliError("MISSING_PARAM", "请提供 --slices-file 或 --payload", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json(
        "PUT",
        f"/api/v1/flywheel/samples/{quote_path_segment(sample_id)}/slices",
        json_body=body,
    )
    emit_output(ctx, data)


@click.command("export-gold")
@click.option("--rule-id", required=True, help="规则 ID")
@click.option("--version-tag", default="v1.0", show_default=True, help="导出版本标签")
@click.option("--include-manual-approved/--no-include-manual-approved", default=True, show_default=True, help="是否包含人工批准样本")
@click.option("--inject-thinking/--no-inject-thinking", default=False, show_default=True, help="是否在 assistant 中保留 thinking")
@click.option("--exclude-benchmark-exported/--no-exclude-benchmark-exported", default=True, show_default=True, help="是否排除已导出进评测集的同源对话")
@click.option("--mode", default=None, type=click.Choice(["create", "append"], case_sensitive=False), help="覆盖创建或追加合并")
@click.option("--target-dataset-id", default=None, help="自定义目标数据集 ID")
@click.option("--enable-dedup/--no-dedup", default=None, help="是否启用去重与防稀释")
@click.option("--cluster-quota", type=int, default=None, help="单簇保留上限 K")
@click.option("--sim-threshold", type=float, default=None, help="同复拆簇余弦相似度阈值 θ")
@click.option("--target-count", type=int, default=None, help="可选目标总条数，未满时差异最大化补足")
@click.pass_context
def export_gold_cmd(
    ctx: click.Context,
    rule_id: str,
    version_tag: str,
    include_manual_approved: bool,
    inject_thinking: bool,
    exclude_benchmark_exported: bool,
    mode: Optional[str],
    target_dataset_id: Optional[str],
    enable_dedup: Optional[bool],
    cluster_quota: Optional[int],
    sim_threshold: Optional[float],
    target_count: Optional[int],
) -> None:
    """按规则导出达标黄金微调 SFT 数据集"""
    check_command_allowed(ctx, "flywheel.export-gold")
    query_params: Dict[str, Any] = {
        "rule_id": rule_id,
        "version_tag": version_tag,
        "include_manual_approved": include_manual_approved,
        "inject_thinking": inject_thinking,
        "exclude_benchmark_exported": exclude_benchmark_exported,
    }
    body: Dict[str, Any] = {}
    if mode is not None:
        query_params["mode"] = mode
        body["mode"] = mode
    if target_dataset_id is not None:
        query_params["target_dataset_id"] = target_dataset_id
        body["target_dataset_id"] = target_dataset_id
    _merge_dedup_config(
        body,
        enable_dedup=enable_dedup,
        cluster_quota=cluster_quota,
        sim_threshold=sim_threshold,
        target_count=target_count,
    )
    client = ensure_client(ctx)
    data = client.request_json(
        "POST",
        "/api/v1/flywheel/export",
        params=query_params,
        json_body=body if body else None,
    )
    emit_output(ctx, data)


@click.command("export-selected")
@click.option("--sample-id", "sample_ids", multiple=True, help="待导出样本 ID")
@click.option("--version-tag", default=None, help="导出版本标签")
@click.option("--inject-thinking/--no-inject-thinking", default=None, help="是否注入 thinking 字段")
@click.option("--exclude-benchmark-exported/--no-exclude-benchmark-exported", default=None, help="是否排除已导出进评测集的同源对话")
@click.option("--mode", default=None, type=click.Choice(["create", "append"], case_sensitive=False), help="覆盖创建或追加合并")
@click.option("--target-dataset-id", default=None, help="自定义目标数据集 ID")
@click.option("--enable-dedup/--no-dedup", default=None, help="是否启用去重与防稀释")
@click.option("--cluster-quota", type=int, default=None, help="单簇保留上限 K")
@click.option("--sim-threshold", type=float, default=None, help="同复拆簇余弦相似度阈值 θ")
@click.option("--target-count", type=int, default=None, help="可选目标总条数，未满时差异最大化补足")
@click.option("--payload", "payload_file", default=None, help="完整 BatchExportSelectedRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 BatchExportSelectedRequest JSON 字符串")
@click.pass_context
def export_selected_cmd(
    ctx: click.Context,
    sample_ids: Tuple[str, ...],
    version_tag: Optional[str],
    inject_thinking: Optional[bool],
    exclude_benchmark_exported: Optional[bool],
    mode: Optional[str],
    target_dataset_id: Optional[str],
    enable_dedup: Optional[bool],
    cluster_quota: Optional[int],
    sim_threshold: Optional[float],
    target_count: Optional[int],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """将手动选定样本导出为标准 messages 数据集"""
    check_command_allowed(ctx, "flywheel.export-selected")
    overrides: Dict[str, Any] = {
        "version_tag": version_tag,
        "inject_thinking": inject_thinking,
        "exclude_benchmark_exported": exclude_benchmark_exported,
        "mode": mode,
        "target_dataset_id": target_dataset_id,
    }
    if sample_ids:
        overrides["sample_ids"] = parse_str_list(sample_ids)
    body = load_payload(payload_file, payload_json, overrides)
    _merge_dedup_config(
        body,
        enable_dedup=enable_dedup,
        cluster_quota=cluster_quota,
        sim_threshold=sim_threshold,
        target_count=target_count,
    )
    if not body.get("sample_ids"):
        raise CliError("MISSING_PARAM", "请提供 --sample-id 或 --payload", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/flywheel/samples/export-selected", json_body=body)
    emit_output(ctx, data)


@click.command("export-benchmark")
@click.argument("sample_id")
@click.option("--dataset-id", default=None, help="目标评测集 ID")
@click.option("--name", default=None, help="目标评测集名称")
@click.option("--slice-id", "slice_ids", multiple=True, help="仅导出指定 slice_id（不传则导出全部切片）")
@click.option("--mode", default=None, type=click.Choice(["create", "append"], case_sensitive=False), help="新建覆盖或追加")
@click.option("--reserve-for-training/--no-reserve-for-training", default=None, help="预留后续进训练集：主动标为普通题 (is_holdout=false)")
@click.option("--enable-dedup/--no-dedup", default=None, help="是否启用去重与防稀释")
@click.option("--cluster-quota", type=int, default=None, help="单簇保留上限 K")
@click.option("--sim-threshold", type=float, default=None, help="同答拆簇余弦相似度阈值 θ")
@click.option("--target-count", type=int, default=None, help="可选目标总题数，未满时差异最大化补足")
@click.option("--payload", "payload_file", default=None, help="完整 ExportBenchmarkDatasetRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 ExportBenchmarkDatasetRequest JSON 字符串")
@click.pass_context
def export_benchmark_cmd(
    ctx: click.Context,
    sample_id: str,
    dataset_id: Optional[str],
    name: Optional[str],
    slice_ids: Tuple[str, ...],
    mode: Optional[str],
    reserve_for_training: Optional[bool],
    enable_dedup: Optional[bool],
    cluster_quota: Optional[int],
    sim_threshold: Optional[float],
    target_count: Optional[int],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """将单条样本切片导出并注册为评测集（按训练集留底如实标留出题或普通题）"""
    check_command_allowed(ctx, "flywheel.export-benchmark")
    overrides: Dict[str, Any] = {
        "dataset_id": dataset_id,
        "name": name,
        "mode": mode,
        "reserve_for_training": reserve_for_training,
    }
    if slice_ids:
        overrides["slice_ids"] = parse_str_list(slice_ids)
    body = load_payload(payload_file, payload_json, overrides)
    _merge_dedup_config(
        body,
        enable_dedup=enable_dedup,
        cluster_quota=cluster_quota,
        sim_threshold=sim_threshold,
        target_count=target_count,
    )
    if not body.get("dataset_id") or not body.get("name"):
        raise CliError("MISSING_PARAM", "请提供 --dataset-id 与 --name", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json(
        "POST",
        f"/api/v1/flywheel/samples/{quote_path_segment(sample_id)}/export-benchmark",
        json_body=body,
    )
    emit_output(ctx, data)


@click.command("batch-export-benchmark")
@click.option("--sample-id", "sample_ids", multiple=True, help="样本 ID（可多次传入或逗号分隔）")
@click.option("--dataset-id", default=None, help="目标评测集 ID")
@click.option("--name", default=None, help="目标评测集名称")
@click.option("--mode", default=None, type=click.Choice(["create", "append"], case_sensitive=False))
@click.option("--strategy", default=None, type=click.Choice(["all_defects", "primary_defect_only"], case_sensitive=False))
@click.option("--reserve-for-training/--no-reserve-for-training", default=None, help="预留后续进训练集：主动标为普通题 (is_holdout=false)")
@click.option("--enable-dedup/--no-dedup", default=None, help="是否启用去重与防稀释")
@click.option("--cluster-quota", type=int, default=None, help="单簇保留上限 K")
@click.option("--sim-threshold", type=float, default=None, help="同答拆簇余弦相似度阈值 θ")
@click.option("--target-count", type=int, default=None, help="可选目标总题数，未满时差异最大化补足")
@click.option("--payload", "payload_file", default=None, help="完整 BatchExportBenchmarkRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 BatchExportBenchmarkRequest JSON 字符串")
@click.pass_context
def batch_export_benchmark_cmd(
    ctx: click.Context,
    sample_ids: Tuple[str, ...],
    dataset_id: Optional[str],
    name: Optional[str],
    mode: Optional[str],
    strategy: Optional[str],
    reserve_for_training: Optional[bool],
    enable_dedup: Optional[bool],
    cluster_quota: Optional[int],
    sim_threshold: Optional[float],
    target_count: Optional[int],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """批量将多个样本切片合并导出注册为 Benchmark 评测集"""
    check_command_allowed(ctx, "flywheel.batch-export-benchmark")
    overrides: Dict[str, Any] = {
        "dataset_id": dataset_id,
        "name": name,
        "mode": mode,
        "strategy": strategy,
        "reserve_for_training": reserve_for_training,
    }
    if sample_ids:
        overrides["sample_ids"] = parse_str_list(sample_ids)
    body = load_payload(payload_file, payload_json, overrides)
    _merge_dedup_config(
        body,
        enable_dedup=enable_dedup,
        cluster_quota=cluster_quota,
        sim_threshold=sim_threshold,
        target_count=target_count,
    )
    if not body.get("sample_ids") or not body.get("dataset_id") or not body.get("name"):
        raise CliError("MISSING_PARAM", "请提供 --sample-id、--dataset-id 与 --name", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/flywheel/batch-export-benchmark", json_body=body)
    emit_output(ctx, data)


def _brief_inspect_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """裁剪规则质检报告的逐样本明细，保留汇总指标与核心扣分标签以防爆上下文。"""
    samples = report.get("samples") or []
    compact_samples = []
    for s in samples:
        if not isinstance(s, dict):
            continue
        compact_samples.append({
            "sample_id": s.get("sample_id"),
            "scenario_id": s.get("scenario_id"),
            "turn_count": s.get("turn_count"),
            "score": s.get("score"),
            "verdict": s.get("verdict"),
            "topology_valid": s.get("topology_valid"),
            "deduction_items": [
                d.get("item") for d in (s.get("deductions") or []) if isinstance(d, dict) and d.get("item")
            ],
            "defect_count": len(s.get("detected_defects") or []),
        })
    copied = dict(report)
    copied["samples"] = compact_samples
    return copied


@click.command("inspect-dataset")
@click.option("--dataset-id", default=None, help="已登记数据集 ID（与 --dataset-path 二选一）")
@click.option("--dataset-path", default=None, help="服务端对话数据集路径（与 --dataset-id 二选一）")
@click.option("--rule-id", default=None, help="质检依据的场景规则 ID")
@click.option("--judge-model-id", default=None, help="质检裁判模型 ID")
@click.option("--workers", type=int, default=None, help="并发质检线程数")
@click.option("--limit", type=int, default=None, help="仅质检前 N 条样本")
@click.option("--save-to-flywheel/--no-save-to-flywheel", default=None, help="是否同步写入飞轮复核样本池")
@click.option("--payload", "payload_file", default=None, help="完整 DatasetInspectRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 DatasetInspectRequest JSON 字符串")
@click.option("--wait", is_flag=True, help="提交后轮询等待直至批量质检完成")
@click.option("--poll-interval", type=float, default=1.5, show_default=True, help="--wait 轮询间隔秒数")
@click.option("--wait-timeout", type=float, default=600.0, show_default=True, help="--wait 最大等待秒数")
@click.option("--brief", is_flag=True, help="搭配 --wait 使用：返回汇总看板与紧凑样本摘要")
@click.pass_context
def inspect_dataset_cmd(
    ctx: click.Context,
    dataset_id: Optional[str],
    dataset_path: Optional[str],
    rule_id: Optional[str],
    judge_model_id: Optional[str],
    workers: Optional[int],
    limit: Optional[int],
    save_to_flywheel: Optional[bool],
    payload_file: Optional[str],
    payload_json: Optional[str],
    wait: bool,
    poll_interval: float,
    wait_timeout: float,
    brief: bool,
) -> None:
    """对纯对话数据集执行批量规则质检打分（只检不修，不触发 AI 修复）"""
    check_command_allowed(ctx, "flywheel.inspect-dataset")
    body = load_payload(
        payload_file,
        payload_json,
        {
            "dataset_id": dataset_id,
            "dataset_path": dataset_path,
            "rule_id": rule_id,
            "judge_model_id": judge_model_id,
            "workers": workers,
            "limit": limit,
            "save_to_flywheel": save_to_flywheel,
        },
    )
    if not body.get("dataset_id") and not body.get("dataset_path"):
        raise CliError("MISSING_PARAM", "请提供 --dataset-id 或 --dataset-path", EXIT_API_ERROR)
    if not body.get("rule_id") or not body.get("judge_model_id"):
        raise CliError("MISSING_PARAM", "请提供 --rule-id 与 --judge-model-id", EXIT_API_ERROR)

    client = ensure_client(ctx)
    submit_res = client.request_json("POST", "/api/v1/flywheel/inspect-dataset", json_body=body)
    if not wait or not isinstance(submit_res, dict) or not submit_res.get("task_id"):
        emit_output(ctx, submit_res)
        return

    report_data = wait_for_task(
        ctx,
        client,
        submit_res["task_id"],
        poll_interval=poll_interval,
        wait_timeout=wait_timeout,
        label="flywheel_inspect",
        fail_code="FLYWHEEL_INSPECT_FAILED",
        status_endpoint_template="/api/v1/flywheel/inspect-tasks/{task_id}",
    )
    if brief and isinstance(report_data, dict):
        report_data = _brief_inspect_report(report_data)
    emit_output(ctx, report_data)


@click.command("inspect-report")
@click.argument("task_id")
@click.option("--only-fail", is_flag=True, help="仅展示未通过 (FAIL/ERROR) 的样本明细")
@click.option("--brief", is_flag=True, help="返回紧凑样本摘要，省略长文本评语与轨迹")
@click.option("--limit", type=int, default=None, help="限制返回明细样本条数")
@click.pass_context
def inspect_report_cmd(
    ctx: click.Context,
    task_id: str,
    only_fail: bool,
    brief: bool,
    limit: Optional[int],
) -> None:
    """查询数据集批量规则质检打分报告"""
    check_command_allowed(ctx, "flywheel.inspect-report")
    client = ensure_client(ctx)
    data = client.request_json(
        "GET",
        f"/api/v1/flywheel/inspect-tasks/{quote_path_segment(task_id)}",
    )
    if isinstance(data, dict):
        samples = data.get("samples") or []
        if only_fail:
            samples = [s for s in samples if isinstance(s, dict) and s.get("verdict") != "PASS"]
        if limit is not None and limit >= 0:
            samples = samples[:limit]
        data = dict(data)
        data["samples"] = samples
        if brief:
            data = _brief_inspect_report(data)
    emit_output(ctx, data)


def register_commands(group: click.Group) -> None:
    group.add_command(list_samples_cmd)
    group.add_command(batch_flywheel_cmd)
    group.add_command(inspect_dataset_cmd)
    group.add_command(inspect_report_cmd)
    group.add_command(judge_sample_cmd)
    group.add_command(batch_judge_cmd)
    group.add_command(remediate_sample_cmd)
    group.add_command(batch_remediate_cmd)
    group.add_command(update_sample_cmd)
    group.add_command(batch_status_cmd)
    group.add_command(delete_sample_cmd)
    group.add_command(batch_delete_samples_cmd)
    group.add_command(get_slices_cmd)
    group.add_command(update_slices_cmd)
    group.add_command(export_gold_cmd)
    group.add_command(export_selected_cmd)
    group.add_command(export_benchmark_cmd)
    group.add_command(batch_export_benchmark_cmd)
