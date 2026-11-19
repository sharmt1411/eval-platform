# -*- coding: utf-8 -*-
"""
Vertex 模型思考协议自适应探测与持久化路由回归测试:
1. 验证 detect_thinking_mode() 纯粹依据响应产出的思考能力（不依赖模型名关键字）选择协议:
   gemini_native -> maas_reasoning_effort -> maas_chat_template -> none
2. 验证 VertexGeminiAdapter.chat() 在 MaaS 模式下完整透传 system 角色、设置 reasoning_effort/chat_template_kwargs，
   并提取 reasoning_content 与 reasoning_tokens；且当调用方构造全新 ModelConfig(vertex_thinking_mode=None) 时，
   依然继承适配器实例存储的 self.thinking_mode（保障 Judge/Remediator 等调用链路无需二次查库）。
3. 验证 POST / PUT / test 接口自动探测并持久化 vertex_thinking_mode 至 SQLite，且 deps 加载适配器时透传生效。
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from fastapi.testclient import TestClient

from backend.app.adapters.factory import AdapterFactory
from backend.app.adapters.vertex_adapter import VertexGeminiAdapter
from backend.app.api.deps import get_adapter_and_config_by_model_id
from backend.app.main import app
from backend.app.models.base import SessionLocal, init_db
from backend.app.schemas.chat import (
    ChatMessage,
    ModelConfig,
    PerformanceMetrics,
    TokenUsage,
    UnifiedChatRequest,
    UnifiedChatResponse,
)

init_db()
client = TestClient(app)


def _make_resp(content: str, thinking: str = "", thinking_tokens: int = 0) -> UnifiedChatResponse:
    return UnifiedChatResponse(
        content=content,
        thinking=thinking,
        raw_output=content,
        usage=TokenUsage(
            prompt_tokens=10,
            completion_tokens=20 + thinking_tokens,
            thinking_tokens=thinking_tokens,
            answer_tokens=20,
            total_tokens=30 + thinking_tokens,
        ),
        metrics=PerformanceMetrics(ttft_ms=10.0, total_latency_ms=30.0, tokens_per_second=100.0),
    )


def _build_bare_vertex_adapter(model_name: str = "opaque-custom-model-v1", mode: str = "auto") -> VertexGeminiAdapter:
    with patch("backend.app.adapters.vertex_adapter.GENAI_AVAILABLE", False):
        adapter = VertexGeminiAdapter(
            model_name=model_name,
            project="test-proj",
            location="global",
            vertex_thinking_mode=mode,
        )
    adapter.client = MagicMock()
    return adapter


def test_detect_thinking_mode_selects_gemini_native_by_capability():
    adapter = _build_bare_vertex_adapter(model_name="opaque-model-xyz")
    with (
        patch("backend.app.adapters.vertex_adapter.GENAI_AVAILABLE", True),
        patch.object(
            adapter,
            "_call_gemini_native",
            return_value=_make_resp("391", thinking="17 * 23 = 391", thinking_tokens=12),
        ),
        patch.object(adapter, "_call_maas_openai") as mock_maas,
    ):
        res = adapter.detect_thinking_mode()
    assert res["thinking_mode"] == "gemini_native"
    assert adapter.thinking_mode == "gemini_native"
    mock_maas.assert_not_called()


def test_detect_thinking_mode_falls_back_to_maas_reasoning_effort():
    adapter = _build_bare_vertex_adapter(model_name="opaque-model-xyz")

    def fake_maas(req, mode, max_attempts=None):
        if mode == "maas_reasoning_effort":
            return _make_resp("391", thinking="先拆成 17*20 + 17*3", thinking_tokens=18)
        return _make_resp("391", thinking="")

    with (
        patch("backend.app.adapters.vertex_adapter.GENAI_AVAILABLE", True),
        patch.object(adapter, "_call_gemini_native", return_value=_make_resp("391", thinking="")),
        patch.object(adapter, "_call_maas_openai", side_effect=fake_maas),
    ):
        res = adapter.detect_thinking_mode()
    assert res["thinking_mode"] == "maas_reasoning_effort"
    assert adapter.thinking_mode == "maas_reasoning_effort"


def test_detect_thinking_mode_falls_back_to_maas_chat_template():
    adapter = _build_bare_vertex_adapter(model_name="opaque-model-xyz")

    def fake_maas(req, mode, max_attempts=None):
        if mode == "maas_reasoning_effort":
            return _make_resp("391", thinking="")
        if mode == "maas_chat_template":
            return _make_resp("391", thinking="chat_template thinking trace", thinking_tokens=9)
        return _make_resp("391", thinking="")

    with (
        patch("backend.app.adapters.vertex_adapter.GENAI_AVAILABLE", True),
        patch.object(adapter, "_call_gemini_native", side_effect=RuntimeError("not gemini")),
        patch.object(adapter, "_call_maas_openai", side_effect=fake_maas),
    ):
        res = adapter.detect_thinking_mode()
    assert res["thinking_mode"] == "maas_chat_template"
    assert adapter.thinking_mode == "maas_chat_template"


def test_detect_thinking_mode_returns_none_when_no_thinking_supported():
    adapter = _build_bare_vertex_adapter(model_name="opaque-model-xyz")
    with (
        patch("backend.app.adapters.vertex_adapter.GENAI_AVAILABLE", True),
        patch.object(adapter, "_call_gemini_native", return_value=_make_resp("391", thinking="")),
        patch.object(adapter, "_call_maas_openai", return_value=_make_resp("391", thinking="")),
    ):
        res = adapter.detect_thinking_mode()
    assert res["thinking_mode"] == "none"
    assert adapter.thinking_mode == "none"


def test_detect_thinking_mode_maas_none_routes_to_maas_openai():
    adapter = _build_bare_vertex_adapter(model_name="opaque-maas-no-think")
    with (
        patch("backend.app.adapters.vertex_adapter.GENAI_AVAILABLE", True),
        patch.object(adapter, "_call_gemini_native", side_effect=RuntimeError("not a gemini native model")),
        patch.object(adapter, "_call_maas_openai", return_value=_make_resp("391", thinking="")),
    ):
        res = adapter.detect_thinking_mode()
    assert res["thinking_mode"] == "maas_none"
    assert adapter.thinking_mode == "maas_none"

    req = UnifiedChatRequest(
        model_id="opaque-maas-no-think",
        messages=[ChatMessage(role="user", content="hi")],
        config=ModelConfig(temperature=0.2, max_tokens=256),
    )
    with (
        patch.object(adapter, "_call_maas_openai", return_value=_make_resp("hello", thinking="")) as mock_maas,
        patch.object(adapter, "_call_gemini_native") as mock_native,
    ):
        resp = adapter.chat(req)
    mock_maas.assert_called_once()
    mock_native.assert_not_called()
    assert resp.content == "hello"


def test_maas_chat_routing_preserves_system_and_extracts_reasoning():
    with patch("backend.app.adapters.vertex_adapter.GENAI_AVAILABLE", False):
        adapter = AdapterFactory.create(
            provider_type="vertex",
            model_name="opaque-maas-model",
            api_url="vertexai://demo-proj/global",
            vertex_thinking_mode="maas_reasoning_effort",
        )
    assert isinstance(adapter, VertexGeminiAdapter)
    assert adapter.thinking_mode == "maas_reasoning_effort"

    captured = {}

    class DummyResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"overall_pass": true}',
                            "reasoning_content": "逐步核对SOP与红线完毕",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 55,
                    "total_tokens": 175,
                    "completion_tokens_details": {"reasoning_tokens": 40},
                },
            }

    class DummyHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return DummyResp()

    req = UnifiedChatRequest(
        model_id="opaque-maas-model",
        messages=[
            ChatMessage(role="system", content="你是质检专家"),
            ChatMessage(role="user", content="请质检以下对话"),
        ],
        # 模拟 judge.py / remediator.py 构造全新 ModelConfig 且未填 vertex_thinking_mode 的场景
        config=ModelConfig(
            temperature=0.1,
            max_tokens=2048,
            thinking_enabled=True,
            thinking_level="HIGH",
        ),
    )

    with (
        patch.object(adapter, "_get_bearer_token", return_value="fake-adc-token"),
        patch("httpx.Client", DummyHttpxClient),
    ):
        resp = adapter.chat(req)

    assert captured["url"] == (
        "https://aiplatform.googleapis.com/v1/projects/demo-proj/locations/global/endpoints/openapi/chat/completions"
    )
    assert captured["json"]["reasoning_effort"] == "high"
    assert captured["json"]["messages"] == [
        {"role": "system", "content": "你是质检专家"},
        {"role": "user", "content": "请质检以下对话"},
    ]
    assert resp.thinking == "逐步核对SOP与红线完毕"
    assert resp.content == '{"overall_pass": true}'
    assert resp.usage.thinking_tokens == 40
    assert resp.usage.answer_tokens == 15


def test_api_register_update_test_persists_vertex_thinking_mode():
    model_id = "test-vertex-adaptive-probe"
    client.delete(f"/api/v1/models/{model_id}")

    try:
        with (
            patch("backend.app.adapters.vertex_adapter.GENAI_AVAILABLE", False),
            patch.object(
                VertexGeminiAdapter,
                "detect_thinking_mode",
                return_value={"thinking_mode": "maas_reasoning_effort", "sample_thinking": "ok"},
            ),
        ):
            r_create = client.post(
                "/api/v1/models/",
                json={
                    "id": model_id,
                    "name": "自适应探测测试模型",
                    "provider_type": "vertex",
                    "api_url": "vertexai://demo-proj/global",
                    "model_name": "opaque-probe-target",
                    "config": {
                        "temperature": 0.2,
                        "max_tokens": 2048,
                        "thinking_enabled": True,
                        "thinking_level": "HIGH",
                    },
                },
            )
        assert r_create.status_code == 200, r_create.text
        assert r_create.json()["vertex_thinking_mode"] == "maas_reasoning_effort"

        models = client.get("/api/v1/models/").json()
        target = next((m for m in models if m["id"] == model_id), None)
        assert target is not None
        assert target["config"]["vertex_thinking_mode"] == "maas_reasoning_effort"

        # 验证 deps 加载出的 Adapter 实例已继承持久化模式
        with patch("backend.app.adapters.vertex_adapter.GENAI_AVAILABLE", False):
            db = SessionLocal()
            try:
                adapter, loaded_cfg = get_adapter_and_config_by_model_id(db, model_id)
            finally:
                db.close()
        assert isinstance(adapter, VertexGeminiAdapter)
        assert adapter.thinking_mode == "maas_reasoning_effort"
        assert loaded_cfg.vertex_thinking_mode == "maas_reasoning_effort"

        # 仅修改温度（不改 model_name/api_url），应保留原 vertex_thinking_mode 而非丢失
        with patch("backend.app.adapters.vertex_adapter.GENAI_AVAILABLE", False):
            r_update_keep = client.put(
                f"/api/v1/models/{model_id}",
                json={
                    "config": {
                        "temperature": 0.4,
                        "max_tokens": 2048,
                        "thinking_enabled": True,
                        "thinking_level": "HIGH",
                    }
                },
            )
        assert r_update_keep.status_code == 200, r_update_keep.text
        assert r_update_keep.json()["vertex_thinking_mode"] == "maas_reasoning_effort"

        # 更换 model_name 时应重新探测并更新持久化协议
        with (
            patch("backend.app.adapters.vertex_adapter.GENAI_AVAILABLE", False),
            patch.object(
                VertexGeminiAdapter,
                "detect_thinking_mode",
                return_value={"thinking_mode": "gemini_native", "sample_thinking": "native"},
            ),
        ):
            r_update_reprobe = client.put(
                f"/api/v1/models/{model_id}",
                json={"model_name": "another-opaque-model"},
            )
        assert r_update_reprobe.status_code == 200, r_update_reprobe.text
        assert r_update_reprobe.json()["vertex_thinking_mode"] == "gemini_native"

        # 点击连通性测试应重新探测更新模式并返回
        with (
            patch("backend.app.adapters.vertex_adapter.GENAI_AVAILABLE", False),
            patch.object(
                VertexGeminiAdapter,
                "detect_thinking_mode",
                return_value={"thinking_mode": "maas_chat_template", "sample_thinking": "ct"},
            ),
            patch.object(
                VertexGeminiAdapter,
                "chat",
                return_value=_make_resp("你好，连通性正常", thinking="思考探测正常", thinking_tokens=10),
            ),
        ):
            r_test = client.post(f"/api/v1/models/{model_id}/test")
        assert r_test.status_code == 200, r_test.text
        test_body = r_test.json()
        assert test_body["status"] == "SUCCESS"
        assert test_body["vertex_thinking_mode"] == "maas_chat_template"
        assert test_body["thinking"] == "思考探测正常"

        models_after = client.get("/api/v1/models/").json()
        target_after = next((m for m in models_after if m["id"] == model_id), None)
        assert target_after["config"]["vertex_thinking_mode"] == "maas_chat_template"

        # 更换 model_name 且重探测全失败时，不得残留旧模型的 vertex_thinking_mode
        with (
            patch("backend.app.adapters.vertex_adapter.GENAI_AVAILABLE", False),
            patch.object(
                VertexGeminiAdapter,
                "detect_thinking_mode",
                return_value={"thinking_mode": None, "error": "probe failed"},
            ),
        ):
            r_update_fail = client.put(
                f"/api/v1/models/{model_id}",
                json={"model_name": "broken-new-model"},
            )
        assert r_update_fail.status_code == 200, r_update_fail.text
        assert r_update_fail.json()["vertex_thinking_mode"] is None

        models_cleared = client.get("/api/v1/models/").json()
        target_cleared = next((m for m in models_cleared if m["id"] == model_id), None)
        assert target_cleared["config"].get("vertex_thinking_mode") is None
    finally:
        client.delete(f"/api/v1/models/{model_id}")
