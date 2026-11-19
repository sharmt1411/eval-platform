# -*- coding: utf-8 -*-
"""model 命令组：模型注册、参数更新、删除、连通性探测与自由对话"""
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
    read_json_input,
)


@click.command("list")
@click.option("--brief", is_flag=True, help="仅保留摘要字段，省略完整 config 详情")
@click.pass_context
def list_models_cmd(ctx: click.Context, brief: bool) -> None:
    """列出已注册的模型"""
    check_command_allowed(ctx, "model.list")
    client = ensure_client(ctx)
    data = client.request_json("GET", "/api/v1/models/")
    if brief and isinstance(data, list):
        data = [
            {
                "id": m.get("id"),
                "name": m.get("name"),
                "provider_type": m.get("provider_type"),
                "model_name": m.get("model_name"),
                "thinking_enabled": (m.get("config") or {}).get("thinking_enabled"),
                "thinking_level": (m.get("config") or {}).get("thinking_level"),
            }
            for m in data
        ]
    emit_output(ctx, data)


@click.command("create")
@click.option("--id", "model_id", default=None, help="模型唯一 ID")
@click.option("--name", default=None, help="模型展示名称")
@click.option("--provider-type", default=None, help="接入协议类型 (openai/harmony/vertex/mock)")
@click.option("--api-url", default=None, help="API 地址或 vertexai://project/location")
@click.option("--api-key", default=None, help="API Key")
@click.option("--model-name", default=None, help="底层模型标识名")
@click.option("--config-json", default=None, help="ModelConfig JSON 字符串")
@click.option("--payload", "payload_file", default=None, help="完整请求 JSON 文件（- 表示 stdin）")
@click.option("--payload-json", default=None, help="完整请求 JSON 字符串")
@click.pass_context
def create_model_cmd(
    ctx: click.Context,
    model_id: Optional[str],
    name: Optional[str],
    provider_type: Optional[str],
    api_url: Optional[str],
    api_key: Optional[str],
    model_name: Optional[str],
    config_json: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """注册新模型"""
    check_command_allowed(ctx, "model.create")
    overrides: Dict[str, Any] = {
        "id": model_id,
        "name": name,
        "provider_type": provider_type,
        "api_url": api_url,
        "api_key": api_key,
        "model_name": model_name,
    }
    if config_json is not None:
        overrides["config"] = parse_json_arg(config_json, "--config-json")
    body = load_payload(payload_file, payload_json, overrides)
    if not body.get("id") or not body.get("name"):
        raise CliError("MISSING_PARAM", "注册模型必须提供 id 与 name（通过参数或 --payload）", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", "/api/v1/models/", json_body=body)
    emit_output(ctx, data)


@click.command("update")
@click.argument("model_id")
@click.option("--name", default=None, help="模型展示名称")
@click.option("--provider-type", default=None, help="接入协议类型")
@click.option("--api-url", default=None, help="API 地址")
@click.option("--api-key", default=None, help="API Key")
@click.option("--model-name", default=None, help="底层模型标识名")
@click.option("--config-json", default=None, help="ModelConfig JSON 字符串")
@click.option("--payload", "payload_file", default=None, help="完整请求 JSON 文件")
@click.option("--payload-json", default=None, help="完整请求 JSON 字符串")
@click.pass_context
def update_model_cmd(
    ctx: click.Context,
    model_id: str,
    name: Optional[str],
    provider_type: Optional[str],
    api_url: Optional[str],
    api_key: Optional[str],
    model_name: Optional[str],
    config_json: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """更新指定模型配置"""
    check_command_allowed(ctx, "model.update")
    overrides: Dict[str, Any] = {
        "name": name,
        "provider_type": provider_type,
        "api_url": api_url,
        "api_key": api_key,
        "model_name": model_name,
    }
    if config_json is not None:
        overrides["config"] = parse_json_arg(config_json, "--config-json")
    body = load_payload(payload_file, payload_json, overrides)
    client = ensure_client(ctx)
    data = client.request_json("PUT", f"/api/v1/models/{quote_path_segment(model_id)}", json_body=body)
    emit_output(ctx, data)


@click.command("delete")
@click.argument("model_id")
@click.pass_context
def delete_model_cmd(ctx: click.Context, model_id: str) -> None:
    """删除已注册模型"""
    check_command_allowed(ctx, "model.delete")
    client = ensure_client(ctx)
    data = client.request_json("DELETE", f"/api/v1/models/{quote_path_segment(model_id)}")
    emit_output(ctx, data)


@click.command("test")
@click.argument("model_id")
@click.pass_context
def test_model_cmd(ctx: click.Context, model_id: str) -> None:
    """测试模型连通性与 Vertex 思考协议自动探测"""
    check_command_allowed(ctx, "model.test")
    client = ensure_client(ctx)
    data = client.request_json("POST", f"/api/v1/models/{quote_path_segment(model_id)}/test")
    emit_output(ctx, data)


@click.command("chat")
@click.argument("model_id")
@click.option("--message", default=None, help="快捷发送单条 user 消息")
@click.option("--messages-file", default=None, help="messages JSON 数组文件")
@click.option("--messages-json", default=None, help="messages JSON 数组字符串")
@click.option("--system-prompt", default=None, help="可选系统提示词")
@click.option("--config-json", default=None, help="临时覆盖 ModelConfig JSON")
@click.option("--payload", "payload_file", default=None, help="完整请求 JSON 文件")
@click.option("--payload-json", default=None, help="完整请求 JSON 字符串")
@click.pass_context
def chat_model_cmd(
    ctx: click.Context,
    model_id: str,
    message: Optional[str],
    messages_file: Optional[str],
    messages_json: Optional[str],
    system_prompt: Optional[str],
    config_json: Optional[str],
    payload_file: Optional[str],
    payload_json: Optional[str],
) -> None:
    """与指定模型进行单轮或多轮自由对话"""
    check_command_allowed(ctx, "model.chat")
    overrides: Dict[str, Any] = {"system_prompt": system_prompt}
    if messages_file is not None:
        overrides["messages"] = read_json_input(messages_file)
    elif messages_json is not None:
        overrides["messages"] = parse_json_arg(messages_json, "--messages-json")
    elif message is not None:
        overrides["messages"] = [{"role": "user", "content": message}]
    if config_json is not None:
        overrides["config"] = parse_json_arg(config_json, "--config-json")

    body = load_payload(payload_file, payload_json, overrides)
    if not body.get("messages"):
        raise CliError("MISSING_PARAM", "请提供 --message、--messages-file 或 --payload", EXIT_API_ERROR)
    client = ensure_client(ctx)
    data = client.request_json("POST", f"/api/v1/models/{quote_path_segment(model_id)}/chat", json_body=body)
    emit_output(ctx, data)


def register_commands(group: click.Group) -> None:
    group.add_command(list_models_cmd)
    group.add_command(create_model_cmd)
    group.add_command(update_model_cmd)
    group.add_command(delete_model_cmd)
    group.add_command(test_model_cmd)
    group.add_command(chat_model_cmd)
