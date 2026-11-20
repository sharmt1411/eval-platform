# -*- coding: utf-8 -*-
"""
标准 OpenAI / vLLM / SGLang / DeepSeek 兼容适配器
"""
from typing import Optional, List, Dict, Any
from backend.app.adapters.base import BaseModelAdapter, execute_openai_chat_request
from backend.app.schemas.chat import (
    UnifiedChatRequest,
    UnifiedChatResponse,
)


class OpenAIAdapter(BaseModelAdapter):
    """通用 OpenAI Chat 协议兼容适配器（默认 SSE 流式保活，支持非流式 JSON 降级）"""

    def __init__(
        self,
        model_name: str,
        api_url: str = "https://api.openai.com/v1/chat/completions",
        api_key: str = "",
        timeout: int = 300,
        max_retries: int = 5,
        retry_delay: float = 2.0,
        **kwargs: Any,
    ):
        super().__init__(
            model_name=model_name,
            api_url=api_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
            retry_delay=retry_delay,
            **kwargs,
        )

    def chat(self, request: UnifiedChatRequest) -> UnifiedChatResponse:
        raw_messages = [
            {"role": m.role, "content": m.content} for m in request.messages
        ]

        payload: Dict[str, Any] = {
            "model": self.model_name,
            "messages": raw_messages,
            "temperature": request.config.temperature,
            "top_p": request.config.top_p,
            "max_tokens": request.config.max_tokens,
        }

        # 思考开关参数映射
        if request.config.thinking_enabled:
            level = request.config.thinking_level.lower()
            if level in ("low", "medium", "high"):
                payload["reasoning_effort"] = level
            # 兼容 vLLM/SGLang 模板开关
            payload["chat_template_kwargs"] = {"enable_thinking": True}
        else:
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        if request.config.extra_body:
            payload.update(request.config.extra_body)

        return execute_openai_chat_request(
            adapter_label="OpenAIAdapter",
            api_url=self.api_url or "https://api.openai.com/v1/chat/completions",
            api_key=self.api_key,
            payload=payload,
            raw_messages=raw_messages,
            timeout=self.timeout,
            max_retries=self.max_retries,
            retry_delay=self.retry_delay,
            fallback_ttft_ratio=0.35,
        )
