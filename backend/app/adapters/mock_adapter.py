# -*- coding: utf-8 -*-
"""
本地离线 Mock 适配器 (零 API 依赖自测与单元测试)
"""
import time
from typing import Optional, List, Dict, Any, Callable
from backend.app.adapters.base import BaseModelAdapter
from backend.app.schemas.chat import (
    UnifiedChatRequest,
    UnifiedChatResponse,
    TokenUsage,
    PerformanceMetrics,
)


class MockAdapter(BaseModelAdapter):
    """离线 Mock 模型适配器"""

    def __init__(
        self,
        model_name: str = "mock-model",
        response_generator: Optional[Callable[[UnifiedChatRequest], str]] = None,
        **kwargs: Any,
    ):
        super().__init__(model_name=model_name, **kwargs)
        self.response_generator = response_generator

    def chat(self, request: UnifiedChatRequest) -> UnifiedChatResponse:
        t0 = time.time()
        time.sleep(0.01)  # 模拟微小耗时

        if self.response_generator:
            content = self.response_generator(request)
        else:
            last_msg = request.messages[-1].content if request.messages else ""
            content = f"【Mock回复】已收到您的消息: {last_msg[:30]}"

        thinking = "正在结合业务规则进行逻辑推演..." if request.config.thinking_enabled else ""
        latency_ms = (time.time() - t0) * 1000.0

        p_tokens = sum(len(m.content) // 2 for m in request.messages)
        c_tokens = (len(content) + len(thinking)) // 2
        t_tokens = len(thinking) // 2 if thinking else 0

        return UnifiedChatResponse(
            content=content,
            thinking=thinking,
            raw_output=f"<think>{thinking}</think>\n{content}" if thinking else content,
            usage=TokenUsage(
                prompt_tokens=p_tokens,
                completion_tokens=c_tokens,
                thinking_tokens=t_tokens,
                answer_tokens=c_tokens - t_tokens,
                total_tokens=p_tokens + c_tokens,
            ),
            metrics=PerformanceMetrics(
                ttft_ms=5.0,
                total_latency_ms=latency_ms,
                tokens_per_second=100.0,
            ),
        )
