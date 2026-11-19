# -*- coding: utf-8 -*-
"""
标准 OpenAI / vLLM / SGLang / DeepSeek 兼容适配器
"""
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


class OpenAIAdapter(BaseModelAdapter):
    """通用 OpenAI Chat 协议兼容适配器"""

    def __init__(
        self,
        model_name: str,
        api_url: str = "https://api.openai.com/v1/chat/completions",
        api_key: str = "",
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
                choice = res_data["choices"][0]
                msg = choice.get("message", {})

                raw_content = msg.get("content") or ""
                # 优先提取原生 reasoning_content (如 DeepSeek-R1 / OpenAI o系列)
                native_reasoning = msg.get("reasoning_content") or ""

                if native_reasoning:
                    thinking_part = native_reasoning.strip()
                    cleaned_answer = raw_content.strip()
                else:
                    thinking_part, cleaned_answer = split_thinking_and_content(raw_content)

                usage_data = res_data.get("usage", {})
                prompt_tokens = usage_data.get("prompt_tokens", len(str(raw_messages)) // 4)
                completion_tokens = usage_data.get("completion_tokens", len(raw_content) // 4)
                total_tokens = usage_data.get("total_tokens", prompt_tokens + completion_tokens)

                # 估算思考 Token 占比
                completion_details = usage_data.get("completion_tokens_details", {})
                thinking_tokens = completion_details.get("reasoning_tokens")
                if thinking_tokens is None:
                    if thinking_part:
                        thinking_tokens = int(
                            completion_tokens * (len(thinking_part) / max(len(raw_content) + len(thinking_part), 1))
                        )
                    else:
                        thinking_tokens = 0
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
                        ttft_ms=total_latency * 0.35,
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
                        f"OpenAIAdapter 调用失败 (已重试{self.max_retries}次) [{self.api_url}]: {str(last_exception)}"
                    )
        raise RuntimeError(f"OpenAIAdapter 未知错误: {str(last_exception)}")
