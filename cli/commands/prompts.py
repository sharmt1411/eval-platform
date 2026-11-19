# -*- coding: utf-8 -*-
"""prompt 命令组：全平台 8 大内置提示词模板资产管理"""
from __future__ import annotations

from typing import Any, Dict, Optional

import click

from cli.client import CliError
from cli.config import EXIT_API_ERROR
from cli.utils import (
    check_command_allowed,
    emit_output,
    ensure_client,
    load_payload,
    parse_json_arg,
    quote_path_segment,
    read_text_file,
)


@click.command("list")
@click.option("--summary", is_flag=True, help="仅输出摘要字段，省略超长模板正文")
@click.pass_context
def list_prompts_cmd(ctx: click.Context, summary: bool) -> None:
    """列出全平台提示词模板"""
    check_command_allowed(ctx, "prompt.list")
    client = ensure_client(ctx)
    data = client.request_json("GET", "/api/v1/prompts/")
    if summary and isinstance(data, list):
        data = [
            {
                "prompt_key": it.get("prompt_key"),
                "name": it.get("name"),
                "category": it.get("category"),
                "is_customized": it.get("is_customized"),
                "missing_placeholders": it.get("missing_placeholders", []),
                "updated_at": it.get("updated_at"),
            }
            for it in data
        ]
    emit_output(ctx, data)


@click.command("get")
@click.argument("prompt_key")
@click.pass_context
def get_prompt_cmd(ctx: click.Context, prompt_key: str) -> None:
    """获取单个提示词模板详情"""
    check_command_allowed(ctx, "prompt.get")
    client = ensure_client(ctx)
    data = client.request_json("GET", f"/api/v1/prompts/{quote_path_segment(prompt_key)}")
    emit_output(ctx, data)


@click.command("save")
@click.argument("prompt_key")
@click.option("--content", default=None, help="新模板正文内容")
@click.option("--content-file", default=None, help="从本地文件读取模板内容")
@click.option("--payload", "payload_file", default=None, help="完整请求 JSON 文件")
@click.option("--payload-json", default=None, help="完整请求 JSON 字符串")
@click.pass_context
def save_prompt_cmd(
    ctx: click.Context,
    prompt_key: str,
    content: Optional[str],
    content_file: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """保存提示词模板自定义版本并即时生效"""
    check_command_allowed(ctx, "prompt.save")
    overrides: Dict[str, Any] = {
        "template_content": read_text_file(content_file) if content_file else content
    }
    body = load_payload(payload_file, payload_json, overrides)
    if not body.get("template_content"):
        raise CliError("MISSING_PARAM", "请提供 --content、--content-file 或 --payload", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("PUT", f"/api/v1/prompts/{quote_path_segment(prompt_key)}", json_body=body)
    emit_output(ctx, data)


@click.command("reset")
@click.argument("prompt_key")
@click.pass_context
def reset_prompt_cmd(ctx: click.Context, prompt_key: str) -> None:
    """恢复指定提示词为系统内置默认模板"""
    check_command_allowed(ctx, "prompt.reset")
    client = ensure_client(ctx)
    data = client.request_json("POST", f"/api/v1/prompts/{quote_path_segment(prompt_key)}/reset")
    emit_output(ctx, data)


@click.command("preview")
@click.argument("prompt_key")
@click.option("--template-override", default=None, help="临时覆盖模板文本")
@click.option("--template-file", default=None, help="从文件读取临时覆盖模板")
@click.option("--vars-json", default=None, help="自定义占位符示例变量 JSON 对象")
@click.option("--payload", "payload_file", default=None, help="完整请求 JSON 文件")
@click.option("--payload-json", default=None, help="完整请求 JSON 字符串")
@click.pass_context
def preview_prompt_cmd(
    ctx: click.Context,
    prompt_key: str,
    template_override: Optional[str],
    template_file: Optional[str],
    vars_json: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """纯静态占位符示例替换预览"""
    check_command_allowed(ctx, "prompt.preview")
    overrides: Dict[str, Any] = {
        "template_override": read_text_file(template_file) if template_file else template_override,
    }
    if vars_json is not None:
        overrides["custom_vars"] = parse_json_arg(vars_json, "--vars-json")
    body = load_payload(payload_file, payload_json, overrides)
    client = ensure_client(ctx)
    data = client.request_json("POST", f"/api/v1/prompts/{quote_path_segment(prompt_key)}/preview", json_body=body)
    emit_output(ctx, data)


def register_commands(group: click.Group) -> None:
    group.add_command(list_prompts_cmd)
    group.add_command(get_prompt_cmd)
    group.add_command(save_prompt_cmd)
    group.add_command(reset_prompt_cmd)
    group.add_command(preview_prompt_cmd)
