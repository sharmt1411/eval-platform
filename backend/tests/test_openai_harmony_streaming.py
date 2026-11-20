# -*- coding: utf-8 -*-
"""
OpenAIAdapter & HarmonyAdapter 默认 SSE 流式保活与兼容降级回归测试
"""
from unittest.mock import patch
import requests

from backend.app.adapters.openai_adapter import OpenAIAdapter
from backend.app.adapters.harmony_adapter import HarmonyAdapter
from backend.app.schemas.chat import ChatMessage, ModelConfig, UnifiedChatRequest


class _FakeStreamResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers=None,
        lines=None,
        json_data=None,
    ):
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/event-stream"}
        self._lines = lines or []
        self._json_data = json_data
        self.encoding = "ISO-8859-1"  # 模拟服务端未写 charset=utf-8 的默认情况

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_lines(self, decode_unicode: bool = False):
        for item in self._lines:
            if decode_unicode and isinstance(item, bytes):
                yield item.decode(self.encoding or "utf-8", errors="replace")
            else:
                yield item

    def json(self):
        return self._json_data


def test_openai_adapter_defaults_to_sse_streaming_with_chinese_utf8():
    adapter = OpenAIAdapter(model_name="qwen3-8b", api_url="https://demo.example/v1/chat/completions")
    assert adapter.timeout == 300

    captured = {}
    sse_lines = [
        b": keepalive",
        'data: {"choices":[{"delta":{"reasoning_content":"先梳理状态机"}}]}'.encode("utf-8"),
        'data: {"choices":[{"delta":{"content":"```mermaid\\nflowchart TD"}}]}'.encode("utf-8"),
        'data: {"choices":[{"delta":{"content":"\\n  A --> B\\n```"}}]}'.encode("utf-8"),
        'data: {"choices":[],"usage":{"prompt_tokens":120,"completion_tokens":45,"total_tokens":165,"completion_tokens_details":{"reasoning_tokens":15}}}'.encode("utf-8"),
        b"data: [DONE]",
    ]

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        captured["stream"] = stream
        return _FakeStreamResponse(lines=sse_lines)

    req = UnifiedChatRequest(
        model_id="qwen3-8b",
        messages=[ChatMessage(role="user", content="生成全景流程图")],
        config=ModelConfig(temperature=0.1, max_tokens=4096, thinking_enabled=True, thinking_level="HIGH"),
    )

    with patch("backend.app.adapters.base.requests.post", side_effect=fake_post):
        resp = adapter.chat(req)

    assert captured["stream"] is True
    assert captured["json"]["stream"] is True
    assert captured["json"]["stream_options"] == {"include_usage": True}
    assert captured["timeout"] == (30.0, 300.0)
    assert resp.thinking == "先梳理状态机"
    assert resp.content == "```mermaid\nflowchart TD\n  A --> B\n```"
    assert resp.usage.prompt_tokens == 120
    assert resp.usage.completion_tokens == 45
    assert resp.usage.thinking_tokens == 15
    assert resp.usage.answer_tokens == 30


def test_openai_adapter_strips_stream_options_on_400_without_wasting_retries():
    adapter = OpenAIAdapter(
        model_name="legacy-oai",
        api_url="https://legacy.example/v1/chat/completions",
        max_retries=2,
        retry_delay=0.0,
    )
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        calls.append(dict(json))
        if "stream_options" in json:
            return _FakeStreamResponse(status_code=400, headers={"content-type": "application/json"})
        return _FakeStreamResponse(
            lines=[
                'data: {"choices":[{"delta":{"content":"兼容成功"}}]}'.encode("utf-8"),
                b"data: [DONE]",
            ]
        )

    req = UnifiedChatRequest(
        model_id="legacy-oai",
        messages=[ChatMessage(role="user", content="hi")],
        config=ModelConfig(thinking_enabled=False),
    )

    with patch("backend.app.adapters.base.requests.post", side_effect=fake_post):
        resp = adapter.chat(req)

    assert len(calls) == 2
    assert "stream_options" in calls[0]
    assert "stream_options" not in calls[1]
    assert resp.content == "兼容成功"


def test_openai_adapter_supports_non_streaming_json_and_explicit_stream_false():
    adapter = OpenAIAdapter(model_name="json-only", api_url="https://json.example/v1/chat/completions")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        captured["stream"] = stream
        captured["json"] = json
        return _FakeStreamResponse(
            headers={"content-type": "application/json"},
            json_data={
                "choices": [{"message": {"content": "<think>推理</think>正式回复"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
            },
        )

    req = UnifiedChatRequest(
        model_id="json-only",
        messages=[ChatMessage(role="user", content="hi")],
        config=ModelConfig(extra_body={"stream": False}),
    )

    with patch("backend.app.adapters.base.requests.post", side_effect=fake_post):
        resp = adapter.chat(req)

    assert captured["stream"] is False
    assert captured["json"]["stream"] is False
    assert resp.thinking == "推理"
    assert resp.content == "正式回复"


def test_harmony_adapter_streaming_and_zero_timeout_unlimited():
    adapter = HarmonyAdapter(
        model_name="harmony-20b",
        api_url="https://tunnel.example/v1/chat/completions",
        timeout=0,
    )
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        captured["timeout"] = timeout
        return _FakeStreamResponse(
            lines=[
                'data: {"choices":[{"delta":{"content":"<|channel|>thought内部推理"}}]}'.encode("utf-8"),
                'data: {"choices":[{"delta":{"content":"<|channel|>final您好，这里是官方客服。"}}]}'.encode("utf-8"),
                b"data: [DONE]",
            ]
        )

    req = UnifiedChatRequest(
        model_id="harmony-20b",
        messages=[ChatMessage(role="user", content="你好")],
        config=ModelConfig(max_tokens=1024),
    )

    with patch("backend.app.adapters.base.requests.post", side_effect=fake_post):
        resp = adapter.chat(req)

    assert captured["timeout"] == (30.0, None)
    assert resp.thinking == "内部推理"
    assert resp.content == "您好，这里是官方客服。"


def test_openai_adapter_retries_on_read_timeout():
    adapter = OpenAIAdapter(
        model_name="slow-model",
        api_url="https://slow.example/v1/chat/completions",
        max_retries=2,
        retry_delay=0.0,
    )
    attempts = 0

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        nonlocal attempts
        attempts += 1
        raise requests.exceptions.ReadTimeout("Read timed out.")

    req = UnifiedChatRequest(
        model_id="slow-model",
        messages=[ChatMessage(role="user", content="hi")],
        config=ModelConfig(),
    )

    with patch("backend.app.adapters.base.requests.post", side_effect=fake_post):
        try:
            adapter.chat(req)
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "已重试2次" in str(exc)
    assert attempts == 2


def test_openai_adapter_retries_and_fails_on_http_200_error_payload():
    adapter = OpenAIAdapter(
        model_name="proxy-model",
        api_url="https://proxy.example/v1/chat/completions",
        max_retries=2,
        retry_delay=0.0,
    )
    attempts = 0

    def fake_sse_error_then_ok(url, headers=None, json=None, timeout=None, stream=False):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _FakeStreamResponse(
                lines=['data: {"error":{"code":429,"message":"upstream rate limit"}}'.encode("utf-8")]
            )
        return _FakeStreamResponse(
            lines=['data: {"choices":[{"delta":{"content":"恢复成功"}}]}'.encode("utf-8"), b"data: [DONE]"]
        )

    req = UnifiedChatRequest(
        model_id="proxy-model",
        messages=[ChatMessage(role="user", content="hi")],
        config=ModelConfig(),
    )
    with patch("backend.app.adapters.base.requests.post", side_effect=fake_sse_error_then_ok):
        resp = adapter.chat(req)
    assert attempts == 2
    assert resp.content == "恢复成功"

    # 非流式 HTTP 200 返回 {"error": ...} 且重试耗尽时应明确抛错，不静默返回空文本
    def fake_json_error(url, headers=None, json=None, timeout=None, stream=False):
        return _FakeStreamResponse(
            headers={"content-type": "application/json"},
            json_data={"error": {"code": 502, "message": "bad gateway"}},
        )

    with patch("backend.app.adapters.base.requests.post", side_effect=fake_json_error):
        try:
            adapter.chat(req)
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "bad gateway" in str(exc)
