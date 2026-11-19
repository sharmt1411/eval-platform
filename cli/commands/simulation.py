# -*- coding: utf-8 -*-
"""simulation 命令组：双 Agent 模拟对弈（同步/SSE流式）、历史查询清空与自由对话质检"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from cli.client import CliError
from cli.config import CliContext, EXIT_API_ERROR
from cli.utils import (
    check_command_allowed,
    emit_output,
    ensure_client,
    load_payload,
    parse_str_list,
    read_json_input,
)


def _build_simulation_payload(
    *,
    rule_id: Optional[str],
    target_model_id: Optional[str],
    client_model_id: Optional[str],
    judge_model_id: Optional[str],
    remediate_model_id: Optional[str],
    scenario_ids: Tuple[str, ...],
    max_turns: Optional[int],
    auto_remediate: Optional[bool],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {
        "rule_id": rule_id,
        "target_model_id": target_model_id,
        "client_model_id": client_model_id,
        "judge_model_id": judge_model_id,
        "remediate_model_id": remediate_model_id,
        "max_turns": max_turns,
        "auto_remediate": auto_remediate,
    }
    if scenario_ids:
        overrides["scenario_ids"] = parse_str_list(scenario_ids)
    body = load_payload(payload_file, payload_json, overrides)
    body.setdefault("scenario_ids", ["all"])
    missing = [
        k
        for k in ("rule_id", "target_model_id", "client_model_id", "judge_model_id")
        if not body.get(k)
    ]
    if missing:
        raise CliError("MISSING_PARAM", f"缺少必填参数: {', '.join(missing)}", EXIT_API_ERROR)
    return body


@click.command("run")
@click.option("--rule-id", default=None, help="场景规则 ID")
@click.option("--target-model-id", default=None, help="被测机器人模型 ID")
@click.option("--client-model-id", default=None, help="模拟客户模型 ID")
@click.option("--judge-model-id", default=None, help="质检裁判模型 ID")
@click.option("--remediate-model-id", default=None, help="修复模型 ID（不传则仅初检不修）")
@click.option("--scenario-id", "scenario_ids", multiple=True, help="剧本 ID（默认 all，可多次传入或逗号分隔）")
@click.option("--max-turns", type=int, default=None, help="最大对弈轮数（默认 60）")
@click.option("--auto-remediate/--no-auto-remediate", default=None, help="不达标是否自动修复与二次验收")
@click.option("--payload", "payload_file", default=None, help="完整 SimulationRunRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 SimulationRunRequest JSON 字符串")
@click.pass_context
def run_sim_cmd(
    ctx: click.Context,
    rule_id: Optional[str],
    target_model_id: Optional[str],
    client_model_id: Optional[str],
    judge_model_id: Optional[str],
    remediate_model_id: Optional[str],
    scenario_ids: Tuple[str, ...],
    max_turns: Optional[int],
    auto_remediate: Optional[bool],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """同步执行双 Agent 模拟对弈并入库"""
    check_command_allowed(ctx, "simulation.run")
    body = _build_simulation_payload(
        rule_id=rule_id,
        target_model_id=target_model_id,
        client_model_id=client_model_id,
        judge_model_id=judge_model_id,
        remediate_model_id=remediate_model_id,
        scenario_ids=scenario_ids,
        max_turns=max_turns,
        auto_remediate=auto_remediate,
        payload_file=payload_file,
        payload_json=payload_json,
    )
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/simulation/simulate", json_body=body)
    emit_output(ctx, data)


@click.command("stream")
@click.option("--rule-id", default=None, help="场景规则 ID")
@click.option("--target-model-id", default=None, help="被测机器人模型 ID")
@click.option("--client-model-id", default=None, help="模拟客户模型 ID")
@click.option("--judge-model-id", default=None, help="质检裁判模型 ID")
@click.option("--remediate-model-id", default=None, help="修复模型 ID")
@click.option("--scenario-id", "scenario_ids", multiple=True, help="剧本 ID（默认 all）")
@click.option("--max-turns", type=int, default=None, help="最大对弈轮数")
@click.option("--auto-remediate/--no-auto-remediate", default=None, help="不达标是否自动修复")
@click.option("--ndjson", is_flag=True, help="逐事件按行输出 NDJSON 到 stdout；默认仅输出最终汇总 JSON")
@click.option("--payload", "payload_file", default=None, help="完整 SimulationRunRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 SimulationRunRequest JSON 字符串")
@click.pass_context
def stream_sim_cmd(
    ctx: click.Context,
    rule_id: Optional[str],
    target_model_id: Optional[str],
    client_model_id: Optional[str],
    judge_model_id: Optional[str],
    remediate_model_id: Optional[str],
    scenario_ids: Tuple[str, ...],
    max_turns: Optional[int],
    auto_remediate: Optional[bool],
    ndjson: bool,
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """流式消费 SSE 双 Agent 模拟对弈"""
    check_command_allowed(ctx, "simulation.stream")
    body = _build_simulation_payload(
        rule_id=rule_id,
        target_model_id=target_model_id,
        client_model_id=client_model_id,
        judge_model_id=judge_model_id,
        remediate_model_id=remediate_model_id,
        scenario_ids=scenario_ids,
        max_turns=max_turns,
        auto_remediate=auto_remediate,
        payload_file=payload_file,
        payload_json=payload_json,
    )
    client = ensure_client(ctx)
    cli_ctx: CliContext = ctx.obj
    out_file = cli_ctx.config.out_file
    event_count = 0
    final_payload: Optional[Dict[str, Any]] = None
    last_error: Optional[str] = None
    scenario_results: List[Dict[str, Any]] = []

    ndjson_fh = None
    saved_ndjson_path: Optional[Path] = None
    if ndjson and out_file:
        saved_ndjson_path = Path(out_file).expanduser()
        saved_ndjson_path.parent.mkdir(parents=True, exist_ok=True)
        ndjson_fh = saved_ndjson_path.open("w", encoding="utf-8")

    try:
        for evt in client.stream_sse("POST", "/api/v1/simulation/simulate/stream", json_body=body):
            event_count += 1
            ev_name = evt.get("event")
            ev_data = evt.get("data") or {}
            if ndjson:
                line = json.dumps(evt, ensure_ascii=False)
                if ndjson_fh is not None:
                    ndjson_fh.write(line + "\n")
                    ndjson_fh.flush()
                else:
                    click.echo(line)
            else:
                if ev_name == "scenario_start":
                    click.echo(
                        f"[simulation] 开始场景: {ev_data.get('scenario_id')} ({ev_data.get('scenario_index', 0) + 1}/{ev_data.get('total_scenarios')})",
                        err=True,
                    )
                elif ev_name == "turn":
                    msg = ev_data.get("message") or {}
                    click.echo(f"  turn #{ev_data.get('turn_index')} [{msg.get('role')}]: {str(msg.get('content', ''))[:60]}", err=True)
                elif ev_name == "judging":
                    click.echo(f"  judging {ev_data.get('scenario_id')} ({ev_data.get('turn_count')} msgs)...", err=True)
                elif ev_name == "remediating":
                    click.echo(f"  remediating {ev_data.get('scenario_id')}...", err=True)

            if ev_name == "scenario_done" and isinstance(ev_data, dict):
                scenario_results.append(ev_data)
            elif ev_name in ("finish", "all_done") and isinstance(ev_data, dict):
                final_payload = ev_data
            elif ev_name == "error":
                last_error = str(ev_data.get("message") if isinstance(ev_data, dict) else ev_data)
    finally:
        if ndjson_fh is not None:
            ndjson_fh.close()

    if last_error and final_payload is None:
        detail = {"written_to": str(saved_ndjson_path)} if saved_ndjson_path else None
        raise CliError("SIMULATION_STREAM_ERROR", last_error, EXIT_API_ERROR, detail=detail)

    if ndjson:
        if saved_ndjson_path is not None:
            fmt = cli_ctx.config.output_format
            summary = {
                "ok": True,
                "written_to": str(saved_ndjson_path),
                "format": "ndjson",
                "event_count": event_count,
                "task_id": (final_payload or {}).get("task_id"),
            }
            click.echo(json.dumps(summary, ensure_ascii=False, indent=None if fmt == "compact" else 2))
        return

    out = final_payload or {
        "event_count": event_count,
        "results": scenario_results,
    }
    if isinstance(out, dict):
        out.setdefault("event_count", event_count)
    emit_output(ctx, out)


@click.command("recent")
@click.option("--rule-id", default=None, help="按规则 ID 过滤")
@click.option("--status", default=None, help="按样本状态过滤")
@click.option("--limit", type=int, default=50, show_default=True, help="返回最大条数")
@click.pass_context
def recent_sim_cmd(
    ctx: click.Context,
    rule_id: Optional[str],
    status: Optional[str],
    limit: int,
) -> None:
    """查询最近模拟对弈历史会话"""
    check_command_allowed(ctx, "simulation.recent")
    client = ensure_client(ctx)
    data = client.request_json(
        "GET",
        "/api/v1/simulation/battles/recent",
        params={"rule_id": rule_id, "status": status, "limit": limit},
    )
    emit_output(ctx, data)


@click.command("evaluate")
@click.option("--rule-id", default=None, help="场景规则 ID")
@click.option("--scenario-id", default=None, help="场景 ID（默认 manual_playground）")
@click.option("--judge-model-id", default=None, help="质检裁判模型 ID")
@click.option("--dialogue-file", default=None, help="对话消息数组 JSON 文件")
@click.option("--save-as-sample/--no-save-as-sample", default=None, help="是否入库飞轮样本池")
@click.option("--payload", "payload_file", default=None, help="完整 EvaluateDialoguePayload JSON 文件")
@click.option("--payload-json", default=None, help="完整 EvaluateDialoguePayload JSON 字符串")
@click.pass_context
def evaluate_dialogue_cmd(
    ctx: click.Context,
    rule_id: Optional[str],
    scenario_id: Optional[str],
    judge_model_id: Optional[str],
    dialogue_file: Optional[str],
    save_as_sample: Optional[bool],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """对一段对话执行白盒质检并可选入库"""
    check_command_allowed(ctx, "simulation.evaluate")
    overrides: Dict[str, Any] = {
        "rule_id": rule_id,
        "scenario_id": scenario_id,
        "judge_model_id": judge_model_id,
        "save_as_sample": save_as_sample,
    }
    if dialogue_file:
        overrides["dialogue"] = read_json_input(dialogue_file)
    body = load_payload(payload_file, payload_json, overrides)
    if not body.get("rule_id") or not body.get("judge_model_id") or not body.get("dialogue"):
        raise CliError("MISSING_PARAM", "请提供 --rule-id、--judge-model-id 与 --dialogue-file（或通过 --payload 传入）", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/simulation/evaluate-dialogue", json_body=body)
    emit_output(ctx, data)


@click.command("clear-all")
@click.option("--rule-id", default=None, help="仅清空指定规则下的历史记录（不传则全部清空）")
@click.pass_context
def clear_all_cmd(ctx: click.Context, rule_id: Optional[str]) -> None:
    """清空模拟对弈与飞轮样本历史记录"""
    check_command_allowed(ctx, "simulation.clear-all")
    client = ensure_client(ctx)
    data = client.request_json(
        "DELETE",
        "/api/v1/simulation/battles/all",
        params={"rule_id": rule_id},
    )
    emit_output(ctx, data)


def register_commands(group: click.Group) -> None:
    group.add_command(run_sim_cmd)
    group.add_command(stream_sim_cmd)
    group.add_command(recent_sim_cmd)
    group.add_command(evaluate_dialogue_cmd)
    group.add_command(clear_all_cmd)
