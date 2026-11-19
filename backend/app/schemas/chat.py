# -*- coding: utf-8 -*-
"""
统一对话与模型接入数据契约
"""
from typing import List, Dict, Any, Optional, Literal
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str
    thinking: Optional[str] = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {"role": self.role, "content": self.content}
        if self.thinking:
            d["thinking"] = self.thinking
        return d


class ModelConfig(BaseModel):
    temperature: float = 0.3
    top_p: float = 0.9
    max_tokens: int = 4096
    repetition_penalty: float = 1.05
    thinking_enabled: bool = False
    thinking_level: Literal["NONE", "LOW", "MEDIUM", "HIGH"] = "NONE"
    vertex_thinking_mode: Optional[
        Literal["auto", "gemini_native", "maas_reasoning_effort", "maas_chat_template", "maas_none", "none"]
    ] = None
    extra_body: Dict[str, Any] = Field(default_factory=dict)


class UnifiedChatRequest(BaseModel):
    model_id: str
    messages: List[ChatMessage]
    config: ModelConfig = Field(default_factory=ModelConfig)


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    thinking_tokens: int = 0
    answer_tokens: int = 0
    total_tokens: int = 0


class PerformanceMetrics(BaseModel):
    ttft_ms: float = 0.0
    total_latency_ms: float = 0.0
    tokens_per_second: float = 0.0


class UnifiedChatResponse(BaseModel):
    content: str
    thinking: str = ""
    raw_output: str = ""
    usage: TokenUsage = Field(default_factory=TokenUsage)
    metrics: PerformanceMetrics = Field(default_factory=PerformanceMetrics)
