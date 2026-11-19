# -*- coding: utf-8 -*-
"""rule 命令组：场景 SOP 规则管理与 7 类 Prompt 调试实测"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import click

from cli.client import CliError
from cli.config import EXIT_API_ERROR
from cli.utils import (
    check_command_allowed,
    emit_output,
    ensure_client,
    load_payload,
    quote_path_segment,
    read_json_input,
    read_text_file,
)

RULE_PROMPT_TYPES = click.Choice(
    ["trajectory_extract", "judge", "judge_plain", "faq_extract", "faq_judge", "remediate", "client_simulator"],
    case_sensitive=False,
)


@click.command("list")
@click.pass_context
def list_rules_cmd(ctx: click.Context) -> None:
    """列出已配置的场景规则"""
    check_command_allowed(ctx, "rule.list")
    client = ensure_client(ctx)
    data = client.request_json("GET", "/api/v1/rules/")
    emit_output(ctx, data)


@click.command("get")
@click.argument("rule_id")
@click.pass_context
def get_rule_cmd(ctx: click.Context, rule_id: str) -> None:
    """获取规则完整配置 JSON"""
    check_command_allowed(ctx, "rule.get")
    client = ensure_client(ctx)
    data = client.request_json("GET", f"/api/v1/rules/{quote_path_segment(rule_id)}")
    emit_output(ctx, data)


@click.command("save")
@click.option("--payload", "payload_file", default=None, help="UniversalRuleSchema JSON 文件（- 表示 stdin）")
@click.option("--payload-json", default=None, help="UniversalRuleSchema JSON 字符串")
@click.pass_context
def save_rule_cmd(
    ctx: click.Context,
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """新建或更新场景规则配置"""
    check_command_allowed(ctx, "rule.save")
    body = load_payload(payload_file, payload_json)
    if not body.get("rule_id"):
        raise CliError("MISSING_PARAM", "保存规则必须提供 --payload 或 --payload-json（含 rule_id）", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/rules/", json_body=body)
    emit_output(ctx, data)


@click.command("default-templates")
@click.pass_context
def default_templates_cmd(ctx: click.Context) -> None:
    """获取系统内置规则与对弈默认 Prompt 模板"""
    check_command_allowed(ctx, "rule.default-templates")
    client = ensure_client(ctx)
    data = client.request_json("GET", "/api/v1/rules/templates/defaults")
    emit_output(ctx, data)


@click.command("debug-prompt")
@click.option("--rule-id", default=None, help="目标规则 ID")
@click.option("--prompt-type", default=None, type=RULE_PROMPT_TYPES, help="调试的 Prompt 类型")
@click.option("--scenario-id", default=None, help="指定对弈/质检场景 ID")
@click.option("--template", default=None, help="临时覆盖模板文本")
@click.option("--template-file", default=None, help="从文件读取临时覆盖模板")
@click.option("--dialogue-file", default=None, help="自定义对话 JSON 数组文件")
@click.option("--defect", "defects", multiple=True, help="手动指定待修复缺陷文案（可多次传入，保留内部逗号）")
@click.option("--payload", "payload_file", default=None, help="完整 DebugPromptRequest JSON 文件")
@click.option("--payload-json", default=None, help="完整 DebugPromptRequest JSON 字符串")
@click.pass_context
def debug_prompt_cmd(
    ctx: click.Context,
    rule_id: Optional[str],
    prompt_type: Optional[str],
    scenario_id: Optional[str],
    template: Optional[str],
    template_file: Optional[str],
    dialogue_file: Optional[str],
    defects: Tuple[str, ...],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """动态拼装规则/对弈真实请求 Prompt 报文"""
    check_command_allowed(ctx, "rule.debug-prompt")
    overrides: Dict[str, Any] = {
        "rule_id": rule_id,
        "prompt_type": prompt_type.lower() if prompt_type else None,
        "scenario_id": scenario_id,
        "template": read_text_file(template_file) if template_file else template,
    }
    if dialogue_file:
        overrides["dialogue"] = read_json_input(dialogue_file)
    if defects:
        overrides["defects"] = [d.strip() for d in defects if d and d.strip()]
    body = load_payload(payload_file, payload_json, overrides)
    body.setdefault("prompt_type", "judge")
    if not body.get("rule_id"):
        raise CliError("MISSING_PARAM", "请提供 --rule-id 或在 --payload 中包含 rule_id", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/rules/debug-prompt", json_body=body)
    emit_output(ctx, data)


@click.command("test-prompt")
@click.option("--model-id", default=None, help="用于实测的已注册模型 ID")
@click.option("--prompt-type", default=None, type=RULE_PROMPT_TYPES, help="Prompt 类型（默认 judge）")
@click.option("--assembled-prompt", default=None, help="拼装后的完整 Prompt 文本（覆盖 payload 中的 assembled_messages）")
@click.option("--assembled-prompt-file", default=None, help="从文件读取拼装后的 Prompt 文本（覆盖 payload 中的 assembled_messages）")
@click.option("--payload", "payload_file", default=None, help="完整 TestPromptRequest JSON 文件（可直接接 debug-prompt 输出）")
@click.option("--payload-json", default=None, help="完整 TestPromptRequest JSON 字符串")
@click.pass_context
def test_prompt_cmd(
    ctx: click.Context,
    model_id: Optional[str],
    prompt_type: Optional[str],
    assembled_prompt: Optional[str],
    assembled_prompt_file: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """调用已注册模型在线实测组装后的规则/对弈 Prompt"""
    check_command_allowed(ctx, "rule.test-prompt")
    prompt_override = read_text_file(assembled_prompt_file) if assembled_prompt_file else assembled_prompt
    overrides: Dict[str, Any] = {
        "model_id": model_id,
        "prompt_type": prompt_type.lower() if prompt_type else None,
        "assembled_prompt": prompt_override,
    }
    body = load_payload(payload_file, payload_json, overrides)
    if prompt_override is not None:
        body.pop("assembled_messages", None)
    body.setdefault("prompt_type", "judge")
    if not body.get("model_id") or not body.get("assembled_prompt"):
        raise CliError("MISSING_PARAM", "请提供 --model-id 与 --assembled-prompt（或通过 --payload 传入）", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/rules/test-prompt", json_body=body)
    emit_output(ctx, data)


def register_commands(group: click.Group) -> None:
    group.add_command(list_rules_cmd)
    group.add_command(get_rule_cmd)
    group.add_command(save_rule_cmd)
    group.add_command(default_templates_cmd)
    group.add_command(debug_prompt_cmd)
    group.add_command(test_prompt_cmd)
