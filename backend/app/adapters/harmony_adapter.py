# -*- coding: utf-8 -*-
"""
Harmony 双通道与微调专用适配器
支持:
1. 剥离 <|channel|>final / <|channel|>thought 等通道前缀残留
2. 隧道网络抖动自动指数退避重试
3. 统计端到端耗时与吞吐指标
"""
import re
from typing import Any, Dict
from backend.app.adapters.base import BaseModelAdapter, execute_openai_chat_request
from backend.app.schemas.chat import (
    UnifiedChatRequest,
    UnifiedChatResponse,
)


def clean_harmony_prefix(text: str) -> str:
    """彻底剥除基座模型输出的 analysis/assistant/final/channel/thought 等前缀残留与叠加"""
    if not text:
        return ""
    if "<|channel|>final" in text:
        text = text.split("<|channel|>final")[-1]
    cleaned = re.sub(
        r"^(?:(?:<\|[^>]+\|>)|(?:analysis|assistant|final|thought|channel|\s))+\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


class HarmonyAdapter(BaseModelAdapter):
    """被测模型 Harmony / OpenAI Chat 兼容端点专用适配器（默认 SSE 流式保活）"""

    def __init__(
        self,
        model_name: str = "demo-target-model",
        api_url: str = "http://127.0.0.1:8000/v1/chat/completions",
        api_key: str = "EMPTY",
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
            "repetition_penalty": request.config.repetition_penalty,
        }
        if request.config.extra_body:
            payload.update(request.config.extra_body)

        return execute_openai_chat_request(
            adapter_label="HarmonyAdapter",
            api_url=self.api_url or "http://127.0.0.1:8000/v1/chat/completions",
            api_key=self.api_key,
            payload=payload,
            raw_messages=raw_messages,
            timeout=self.timeout,
            max_retries=self.max_retries,
            retry_delay=self.retry_delay,
            post_process_content=clean_harmony_prefix,
            fallback_ttft_ratio=0.4,
        )
