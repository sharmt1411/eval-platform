# -*- coding: utf-8 -*-
"""
统一模型适配器工厂
"""
import re
from typing import Dict, Any, Optional
from backend.app.adapters.base import BaseModelAdapter
from backend.app.adapters.openai_adapter import OpenAIAdapter
from backend.app.adapters.harmony_adapter import HarmonyAdapter
from backend.app.adapters.vertex_adapter import (
    VertexGeminiAdapter,
    DEFAULT_VERTEX_PROJECT,
    DEFAULT_VERTEX_LOCATION,
)
from backend.app.adapters.mock_adapter import MockAdapter


class AdapterFactory:
    """模型适配器工厂"""

    @staticmethod
    def create(
        provider_type: str,
        model_name: str,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> BaseModelAdapter:
        vertex_thinking_mode = kwargs.get("vertex_thinking_mode")
        # 剔除推理时专用的 ModelConfig 字段，仅透传连接层与底层重试参数
        filtered_kwargs = {
            k: v for k, v in kwargs.items()
            if k not in (
                "temperature", "top_p", "max_tokens", "repetition_penalty",
                "thinking_enabled", "thinking_level", "vertex_thinking_mode"
            )
        }

        provider = provider_type.lower()
        if provider == "openai":
            return OpenAIAdapter(
                model_name=model_name,
                api_url=api_url or "https://api.openai.com/v1/chat/completions",
                api_key=api_key or "",
                **filtered_kwargs,
            )
        elif provider == "harmony":
            return HarmonyAdapter(
                model_name=model_name,
                api_url=api_url or "http://127.0.0.1:8000/v1/chat/completions",
                api_key=api_key or "EMPTY",
                **filtered_kwargs,
            )
        elif provider in ("vertex", "gemini"):
            project = filtered_kwargs.pop("project", None)
            location = filtered_kwargs.pop("location", None)

            # 支持从 api_url 解析 (如 vertexai://project-id/global 或 project-id/global)
            if api_url and not project:
                clean_url = re.sub(r"^vertexai://", "", api_url.strip()).strip("/")
                parts = [p.strip() for p in clean_url.split("/") if p.strip()]
                if len(parts) >= 2:
                    project, location = parts[0], parts[1]
                elif len(parts) == 1:
                    project = parts[0]

            return VertexGeminiAdapter(
                model_name=model_name,
                project=project or DEFAULT_VERTEX_PROJECT,
                location=location or DEFAULT_VERTEX_LOCATION,
                api_key=api_key,
                vertex_thinking_mode=vertex_thinking_mode,
                **filtered_kwargs,
            )
        elif provider == "mock":
            return MockAdapter(model_name=model_name, **filtered_kwargs)
        else:
            raise ValueError(f"不支持的 Provider 类型: {provider_type}，可选: openai, harmony, vertex, mock")
