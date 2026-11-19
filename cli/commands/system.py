# -*- coding: utf-8 -*-
"""system 命令组：健康检查、可用能力探测与策略自省"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import click

from cli.catalog import COMMAND_CATALOG, COMMAND_MAP, GROUP_DESCRIPTIONS
from cli.client import CliError
from cli.config import CliContext, EXIT_API_ERROR
from cli.utils import check_command_allowed, emit_output, ensure_client


@click.command("health")
@click.pass_context
def health_cmd(ctx: click.Context) -> None:
    """检查平台后端服务健康状态"""
    check_command_allowed(ctx, "system.health")
    client = ensure_client(ctx)
    data = client.request_json("GET", "/health")
    emit_output(ctx, data)


@click.command("capabilities")
@click.option("--group", "group_filter", default=None, help="仅查看指定命令组")
@click.option("--include-denied", is_flag=True, help="同时列出当前策略禁用的命令及原因")
@click.pass_context
def capabilities_cmd(
    ctx: click.Context,
    group_filter: Optional[str],
    include_denied: bool,
) -> None:
    """动态列出当前权限策略放行的所有命令"""
    check_command_allowed(ctx, "system.capabilities")
    cli_ctx: CliContext = ctx.obj
    policy = cli_ctx.policy

    allowed_items: List[Dict[str, Any]] = []
    denied_items: List[Dict[str, Any]] = []
    for spec in COMMAND_CATALOG:
        if group_filter and spec.group != group_filter:
            continue
        dec = policy.evaluate(spec)
        item = {
            "command_id": spec.command_id,
            "cli_command": f"{spec.group} {spec.name}",
            "group": spec.group,
            "name": spec.name,
            "risk": spec.risk,
            "method": spec.method,
            "endpoint": spec.endpoint,
            "supports_payload": spec.supports_payload,
            "is_streaming": spec.is_streaming,
            "is_async_task": spec.is_async_task,
            "requires_confirm": dec.requires_confirm,
            "summary": spec.summary,
        }
        if dec.allowed:
            allowed_items.append(item)
        elif include_denied:
            item["denied_code"] = dec.code
            item["denied_reason"] = dec.reason
            denied_items.append(item)

    payload: Dict[str, Any] = {
        "active_profile": policy.active_profile_name,
        "policy_file": str(policy.policy_path),
        "allowed_count": len(allowed_items),
        "groups": {
            g: GROUP_DESCRIPTIONS.get(g, "")
            for g in sorted({i["group"] for i in allowed_items})
        },
        "commands": allowed_items,
    }
    if include_denied:
        payload["denied_count"] = len(denied_items)
        payload["denied_commands"] = denied_items
    emit_output(ctx, payload)


@click.command("policy")
@click.option("--check", "check_cmd_id", default=None, help="预检指定 command_id（如 model.delete）是否允许执行")
@click.pass_context
def policy_cmd(ctx: click.Context, check_cmd_id: Optional[str]) -> None:
    """查看当前生效策略配置或预检命令权限"""
    check_command_allowed(ctx, "system.policy")
    cli_ctx: CliContext = ctx.obj
    policy = cli_ctx.policy

    if check_cmd_id:
        spec = COMMAND_MAP.get(check_cmd_id)
        if not spec:
            raise CliError(
                code="UNKNOWN_COMMAND",
                message=f"未知 command_id: {check_cmd_id}",
                exit_code=EXIT_API_ERROR,
            )
        dec = policy.evaluate(spec)
        emit_output(
            ctx,
            {
                "active_profile": policy.active_profile_name,
                "command_id": spec.command_id,
                "risk": spec.risk,
                "allowed": dec.allowed,
                "code": dec.code,
                "reason": dec.reason,
                "requires_confirm": dec.requires_confirm,
            },
        )
        return

    summary = policy.to_summary_dict()
    summary["allowed_command_count"] = len(policy.allowed_commands())
    summary["denied_command_ids"] = [s.command_id for s, _ in policy.denied_commands()]
    emit_output(ctx, summary)


def register_commands(group: click.Group) -> None:
    group.add_command(health_cmd)
    group.add_command(capabilities_cmd)
    group.add_command(policy_cmd)
