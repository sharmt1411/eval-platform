# -*- coding: utf-8 -*-
"""dataset 命令组：数据集列表、抽检、导入、删除与抽样条件过滤导出"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import click

from cli.client import CliError
from cli.config import EXIT_API_ERROR
from cli.utils import (
    check_command_allowed,
    emit_output,
    ensure_client,
    load_payload,
    parse_json_arg,
    parse_str_list,
    quote_path_segment,
)


def _build_export_filter_payload(
    *,
    export_scope: Optional[str],
    model_line: Optional[str],
    sampling_enabled: Optional[bool],
    categories: Tuple[str, ...],
    difficulties: Tuple[str, ...],
    holdout_filter: Optional[str],
    keyword: Optional[str],
    sampling_json: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
    extra_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    body = load_payload(payload_file, payload_json, extra_overrides)
    has_sampling_input = any(
        x is not None for x in (sampling_json, sampling_enabled, model_line)
    ) or ("sampling_config" in body and body["sampling_config"] is not None)

    if has_sampling_input:
        sampling_cfg: Dict[str, Any] = dict(body.get("sampling_config") or {})
        if sampling_json is not None:
            parsed = parse_json_arg(sampling_json, "--sampling-json")
            if not isinstance(parsed, dict):
                raise CliError("INVALID_PAYLOAD", "--sampling-json 必须是 JSON 对象", EXIT_API_ERROR)
            sampling_cfg.update(parsed)
        if sampling_enabled is not None:
            sampling_cfg["enabled"] = sampling_enabled
        else:
            sampling_cfg.setdefault("enabled", True)
        if model_line is not None:
            sampling_cfg["model_line"] = model_line
        body["sampling_config"] = sampling_cfg

    if export_scope is not None:
        body["export_scope"] = export_scope
    if categories:
        body["categories"] = parse_str_list(categories)
    else:
        body.setdefault("categories", [])
    if difficulties:
        body["difficulties"] = parse_str_list(difficulties)
    else:
        body.setdefault("difficulties", [])
    if holdout_filter is not None:
        body["holdout_filter"] = holdout_filter
    if keyword is not None:
        body["keyword"] = keyword
    return body


@click.command("list")
@click.pass_context
def list_datasets_cmd(ctx: click.Context) -> None:
    """列出本地数据资产目录"""
    check_command_allowed(ctx, "dataset.list")
    client = ensure_client(ctx)
    data = client.request_json("GET", "/api/v1/datasets/")
    emit_output(ctx, data)


@click.command("samples")
@click.argument("dataset_id")
@click.option("--limit", type=int, default=20, show_default=True, help="返回前 N 条样本")
@click.pass_context
def dataset_samples_cmd(ctx: click.Context, dataset_id: str, limit: int) -> None:
    """抽检数据集样本"""
    check_command_allowed(ctx, "dataset.samples")
    client = ensure_client(ctx)
    data = client.request_json(
        "GET",
        f"/api/v1/datasets/{quote_path_segment(dataset_id)}/samples",
        params={"limit": limit},
    )
    emit_output(ctx, data)


@click.command("import")
@click.option("--dataset-id", default=None, help="目标数据集唯一 ID（不含后缀）")
@click.option(
    "--dataset-type",
    default=None,
    type=click.Choice(["benchmark", "flywheel_gold"], case_sensitive=False),
    help="数据集分类",
)
@click.option(
    "--mode",
    default=None,
    type=click.Choice(["overwrite", "append"], case_sensitive=False),
    help="导入模式",
)
@click.option("--mark-as-holdout/--no-mark-as-holdout", default=None, help="是否标记为纯评测留出题")
@click.option("--file", "local_file", default=None, help="读取本地 JSON/JSONL 文件内容导入")
@click.option("--source-file-path", default=None, help="服务端本地外部文件路径")
@click.option("--payload", "payload_file", default=None, help="完整导入请求 JSON 文件")
@click.option("--payload-json", default=None, help="完整导入请求 JSON 字符串")
@click.pass_context
def import_dataset_cmd(
    ctx: click.Context,
    dataset_id: Optional[str],
    dataset_type: Optional[str],
    mode: Optional[str],
    mark_as_holdout: Optional[bool],
    local_file: Optional[str],
    source_file_path: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """导入外部评测集或微调语料"""
    check_command_allowed(ctx, "dataset.import")
    overrides: Dict[str, Any] = {
        "dataset_id": dataset_id,
        "dataset_type": dataset_type,
        "mode": mode,
        "mark_as_holdout": mark_as_holdout,
        "source_file_path": source_file_path,
    }
    if local_file:
        p = Path(local_file)
        if not p.exists():
            raise CliError("FILE_NOT_FOUND", f"找不到本地导入文件: {local_file}", EXIT_API_ERROR)
        overrides["content"] = p.read_text(encoding="utf-8")

    body = load_payload(payload_file, payload_json, overrides)
    if not body.get("dataset_id"):
        raise CliError("MISSING_PARAM", "导入数据集必须提供 --dataset-id", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/datasets/import", json_body=body)
    emit_output(ctx, data)


@click.command("delete")
@click.argument("dataset_id")
@click.pass_context
def delete_dataset_cmd(ctx: click.Context, dataset_id: str) -> None:
    """删除数据集资产及本地文件"""
    check_command_allowed(ctx, "dataset.delete")
    client = ensure_client(ctx)
    data = client.request_json("DELETE", f"/api/v1/datasets/{quote_path_segment(dataset_id)}")
    emit_output(ctx, data)


@click.command("filter-options")
@click.argument("dataset_id")
@click.pass_context
def filter_options_cmd(ctx: click.Context, dataset_id: str) -> None:
    """获取评测集基础过滤维度与历史基准模型线选项"""
    check_command_allowed(ctx, "dataset.filter-options")
    client = ensure_client(ctx)
    data = client.request_json("GET", f"/api/v1/datasets/{quote_path_segment(dataset_id)}/filter-options")
    emit_output(ctx, data)


@click.command("export-preview")
@click.argument("dataset_id")
@click.option("--export-scope", default=None, type=click.Choice(["sampled", "active_only", "cooldown_all", "skipped", "failed_only", "all"]))
@click.option("--model-line", default=None, help="基准模型线标识")
@click.option("--sampling-enabled/--no-sampling-enabled", default=None, help="是否结合智能抽样池过滤")
@click.option("--category", "categories", multiple=True, help="按分类过滤（可多次传入或逗号分隔）")
@click.option("--difficulty", "difficulties", multiple=True, help="按难度过滤（可多次传入或逗号分隔）")
@click.option("--holdout-filter", default=None, type=click.Choice(["all", "holdout_only", "non_holdout_only"]))
@click.option("--keyword", default=None, help="关键词搜索过滤")
@click.option("--sampling-json", default=None, help="完整 SamplingFilterConfig JSON")
@click.option("--payload", "payload_file", default=None, help="完整请求 JSON 文件")
@click.option("--payload-json", default=None, help="完整请求 JSON 字符串")
@click.pass_context
def export_preview_cmd(
    ctx: click.Context,
    dataset_id: str,
    export_scope: Optional[str],
    model_line: Optional[str],
    sampling_enabled: Optional[bool],
    categories: Tuple[str, ...],
    difficulties: Tuple[str, ...],
    holdout_filter: Optional[str],
    keyword: Optional[str],
    sampling_json: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """试算按条件过滤导出的题量与分布"""
    check_command_allowed(ctx, "dataset.export-preview")
    body = _build_export_filter_payload(
        export_scope=export_scope,
        model_line=model_line,
        sampling_enabled=sampling_enabled,
        categories=categories,
        difficulties=difficulties,
        holdout_filter=holdout_filter,
        keyword=keyword,
        sampling_json=sampling_json,
        payload_file=payload_file,
        payload_json=payload_json,
    )
    client = ensure_client(ctx)
    data = client.request_json(
        "POST",
        f"/api/v1/datasets/{quote_path_segment(dataset_id)}/export/preview",
        json_body=body,
    )
    emit_output(ctx, data)


@click.command("export")
@click.argument("dataset_id")
@click.option("--export-scope", default=None, type=click.Choice(["sampled", "active_only", "cooldown_all", "skipped", "failed_only", "all"]))
@click.option("--model-line", default=None, help="基准模型线标识")
@click.option("--sampling-enabled/--no-sampling-enabled", default=None, help="是否结合智能抽样池过滤")
@click.option("--category", "categories", multiple=True, help="按分类过滤")
@click.option("--difficulty", "difficulties", multiple=True, help="按难度过滤")
@click.option("--holdout-filter", default=None, type=click.Choice(["all", "holdout_only", "non_holdout_only"]))
@click.option("--keyword", default=None, help="关键词搜索过滤")
@click.option("--include-tracker/--no-include-tracker", default=None, help="是否包含 eval_tracker 字段")
@click.option("--export-target", default=None, type=click.Choice(["download", "save_dataset"]))
@click.option("--target-dataset-id", default=None, help="另存为平台新评测集的 ID")
@click.option("--target-dataset-name", default=None, help="另存为平台新评测集名称")
@click.option("--save-mode", default=None, type=click.Choice(["overwrite", "append"]))
@click.option("--save-jsonl", default=None, help="当 export-target=download 时将 jsonl_content 直接写入本地文件")
@click.option("--sampling-json", default=None, help="完整 SamplingFilterConfig JSON")
@click.option("--payload", "payload_file", default=None, help="完整请求 JSON 文件")
@click.option("--payload-json", default=None, help="完整请求 JSON 字符串")
@click.pass_context
def export_dataset_cmd(
    ctx: click.Context,
    dataset_id: str,
    export_scope: Optional[str],
    model_line: Optional[str],
    sampling_enabled: Optional[bool],
    categories: Tuple[str, ...],
    difficulties: Tuple[str, ...],
    holdout_filter: Optional[str],
    keyword: Optional[str],
    include_tracker: Optional[bool],
    export_target: Optional[str],
    target_dataset_id: Optional[str],
    target_dataset_name: Optional[str],
    save_mode: Optional[str],
    save_jsonl: Optional[str],
    sampling_json: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """按抽样与过滤条件导出评测集（下载或另存为新数据集）"""
    check_command_allowed(ctx, "dataset.export")
    extra: Dict[str, Any] = {
        "include_tracker": include_tracker,
        "export_target": export_target,
        "target_dataset_id": target_dataset_id,
        "target_dataset_name": target_dataset_name,
        "save_mode": save_mode,
    }
    body = _build_export_filter_payload(
        export_scope=export_scope,
        model_line=model_line,
        sampling_enabled=sampling_enabled,
        categories=categories,
        difficulties=difficulties,
        holdout_filter=holdout_filter,
        keyword=keyword,
        sampling_json=sampling_json,
        payload_file=payload_file,
        payload_json=payload_json,
        extra_overrides=extra,
    )
    client = ensure_client(ctx)
    data = client.request_json(
        "POST",
        f"/api/v1/datasets/{quote_path_segment(dataset_id)}/export",
        json_body=body,
    )
    if save_jsonl and isinstance(data, dict) and "jsonl_content" in data:
        out_p = Path(save_jsonl).expanduser()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(data["jsonl_content"], encoding="utf-8")
        data = {k: v for k, v in data.items() if k != "jsonl_content"}
        data["saved_jsonl_path"] = str(out_p)
    emit_output(ctx, data)


def register_commands(group: click.Group) -> None:
    group.add_command(list_datasets_cmd)
    group.add_command(dataset_samples_cmd)
    group.add_command(import_dataset_cmd)
    group.add_command(delete_dataset_cmd)
    group.add_command(filter_options_cmd)
    group.add_command(export_preview_cmd)
    group.add_command(export_dataset_cmd)
