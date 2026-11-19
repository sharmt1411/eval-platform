# -*- coding: utf-8 -*-
"""
Harmony 双通道与微调专用适配器
支持:
1. 剥离 <|channel|>final / <|channel|>thought 等通道前缀残留
2. 隧道网络抖动自动指数退避重试
3. 统计端到端耗时与吞吐指标
"""
import re
import time
import requests
from typing import Optional, List, Dict, Any
from backend.app.adapters.base import BaseModelAdapter, split_thinking_and_content
from backend.app.schemas.chat import (
    UnifiedChatRequest,
    UnifiedChatResponse,
    TokenUsage,
    PerformanceMetrics,
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
    """被测模型 Harmony / OpenAI Chat 兼容端点专用适配器"""

    def __init__(
        self,
        model_name: str = "demo-target-model",
        api_url: str = "http://127.0.0.1:8000/v1/chat/completions",
        api_key: str = "EMPTY",
        timeout: int = 60,
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
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
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

        last_exception = None
        t_start = time.time()
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(
                    self.api_url, headers=headers, json=payload, timeout=self.timeout
                )
                resp.raise_for_status()
                total_latency = (time.time() - t_start) * 1000.0

                res_data = resp.json()
                raw_content = (
                    res_data["choices"][0]["message"].get("content") or ""
                ).strip()

                thinking_part, answer_part = split_thinking_and_content(raw_content)
                cleaned_answer = clean_harmony_prefix(answer_part)

                usage_data = res_data.get("usage", {})
                prompt_tokens = usage_data.get("prompt_tokens", len(str(raw_messages)) // 4)
                completion_tokens = usage_data.get("completion_tokens", len(raw_content) // 4)
                total_tokens = usage_data.get("total_tokens", prompt_tokens + completion_tokens)

                thinking_tokens = int(completion_tokens * (len(thinking_part) / max(len(raw_content), 1)))
                answer_tokens = max(completion_tokens - thinking_tokens, 0)

                tps = (completion_tokens / (total_latency / 1000.0)) if total_latency > 0 else 0.0

                return UnifiedChatResponse(
                    content=cleaned_answer,
                    thinking=thinking_part,
                    raw_output=raw_content,
                    usage=TokenUsage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        thinking_tokens=thinking_tokens,
                        answer_tokens=answer_tokens,
                        total_tokens=total_tokens,
                    ),
                    metrics=PerformanceMetrics(
                        ttft_ms=total_latency * 0.4,  # 非流式预估 TTFT
                        total_latency_ms=total_latency,
                        tokens_per_second=round(tps, 2),
                    ),
                )
            except requests.exceptions.RequestException as e:
                last_exception = e
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
                else:
                    raise RuntimeError(
                        f"HarmonyAdapter 调用失败 (已重试{self.max_retries}次) [{self.api_url}]: {str(last_exception)}"
                    )
        raise RuntimeError(f"HarmonyAdapter 未知错误: {str(last_exception)}")
