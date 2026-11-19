# -*- coding: utf-8 -*-
"""
抽象模型适配器基类与通用工具函数
"""
import re
import time
from abc import ABC, abstractmethod
from typing import Tuple, Dict, Any, Optional, Iterator
from backend.app.schemas.chat import (
    ModelConfig,
    UnifiedChatRequest,
    UnifiedChatResponse,
    TokenUsage,
    PerformanceMetrics,
)


def split_thinking_and_content(raw_text: str) -> Tuple[str, str]:
    """
    通用思考链与正文分离函数
    支持:
    1. <think>...</think> 标签提取
    2. [thinking]...[/thinking] 标签提取
    3. Harmony <|channel|>thought...<|channel|>final 格式切分
    """
    if not raw_text:
        return "", ""

    # 1. 优先检测 Harmony 通道
    if "<|channel|>final" in raw_text:
        parts = raw_text.split("<|channel|>final")
        thinking_part = parts[0].replace("<|channel|>thought", "").strip()
        final_part = parts[1].strip()
        return thinking_part, final_part

    # 2. 检测 <think>...</think> 标签
    think_match = re.search(r"<think>(.*?)</think>", raw_text, flags=re.DOTALL)
    if think_match:
        thinking_part = think_match.group(1).strip()
        final_part = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
        return thinking_part, final_part

    # 3. 检测 [thinking] 标签
    bracket_match = re.search(r"\[thinking\](.*?)\[/thinking\]", raw_text, flags=re.DOTALL)
    if bracket_match:
        thinking_part = bracket_match.group(1).strip()
        final_part = re.sub(r"\[thinking\].*?\[/thinking\]", "", raw_text, flags=re.DOTALL).strip()
        return thinking_part, final_part

    return "", raw_text.strip()


def build_downgraded_retry_config(
    config: ModelConfig,
    resp: UnifiedChatResponse,
    scene_label: str = "模型推理",
) -> Tuple[ModelConfig, Optional[str]]:
    """
    统一构建非 JSON / 空正文纠偏重试参数:
    1. 若 temperature < 0.6，温和抬高至 max(t + 0.25, 0.35)（上限 0.6）；
    2. 若首次思考链已消耗额度导致正文为空，统一降级思考深度 (HIGH -> MEDIUM -> LOW -> NONE)，
       并返回中文降级提示 notice 供落库与前端通知用户；未触发思考降级时返回 None。
    """
    orig_temp = 0.1 if config.temperature is None else float(config.temperature)
    next_temp = (
        min(0.6, max(orig_temp + 0.25, 0.35))
        if orig_temp < 0.6
        else orig_temp
    )
    updates: Dict[str, Any] = {"temperature": next_temp}

    first_empty = not (resp.content or "").strip()
    has_thinking = bool((getattr(resp, "thinking", "") or "").strip())
    downgrade_notice: Optional[str] = None

    if first_empty and has_thinking:
        cur_lvl = (config.thinking_level or "HIGH").upper()
        cur_max = int(config.max_tokens or 0)
        boosted_max = max(cur_max, 16384)
        updates["max_tokens"] = boosted_max
        if cur_lvl == "HIGH":
            updates["thinking_level"] = "LOW"
            downgrade_notice = (
                f"【思考自动降级】{scene_label}首次因 HIGH 思考耗尽 {cur_max} 输出额度导致正文为空，"
                f"已自动将思考深度降级为 LOW 并放大 max_tokens 至 {boosted_max} 重试。"
            )
        elif cur_lvl == "MEDIUM":
            updates["thinking_level"] = "LOW"
            downgrade_notice = (
                f"【思考自动降级】{scene_label}首次因 MEDIUM 思考耗尽 {cur_max} 输出额度导致正文为空，"
                f"已自动将思考深度降级为 LOW 并放大 max_tokens 至 {boosted_max} 重试。"
            )
        elif config.thinking_enabled:
            updates["thinking_enabled"] = False
            updates["thinking_level"] = "NONE"
            downgrade_notice = (
                f"【思考自动降级】{scene_label}首次因思考耗尽 {cur_max} 输出额度导致正文为空，"
                f"已自动关闭思考模式 (NONE) 并放大 max_tokens 至 {boosted_max} 重试。"
            )
        else:
            downgrade_notice = (
                f"【思考自动降级】{scene_label}首次输出思考链后正文为空，"
                f"已自动放大 max_tokens 至 {boosted_max} 触发纠偏重采样。"
            )

    return config.model_copy(update=updates), downgrade_notice


class BaseModelAdapter(ABC):
    """模型接入层抽象基类"""

    def __init__(
        self,
        model_name: str,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 60,
        max_retries: int = 5,
        retry_delay: float = 2.0,
        **kwargs: Any,
    ):
        self.model_name = model_name
        self.api_url = api_url
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.extra_kwargs = kwargs

    @abstractmethod
    def chat(self, request: UnifiedChatRequest) -> UnifiedChatResponse:
        """执行同步对话推理，返回包含思考与性能指标的标准化响应"""
        pass

    def stream_chat(self, request: UnifiedChatRequest) -> Iterator[str]:
        """流式生成（默认非流式降级回退）"""
        response = self.chat(request)
        yield response.content
