# -*- coding: utf-8 -*-
"""
抽象模型适配器基类与通用工具函数
"""
import json
import re
import time
from abc import ABC, abstractmethod
from typing import Tuple, Dict, Any, Optional, Iterator, List, Callable, Union
import requests
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


def _build_requests_timeout(timeout: Optional[Union[int, float]]) -> Tuple[float, Optional[float]]:
    """将统一 timeout 秒数转换为 requests (connect, read) 元组，0/None 表示放开读等待。"""
    if timeout is None or float(timeout) <= 0:
        return (30.0, None)
    read_timeout = float(timeout)
    return (min(30.0, read_timeout), read_timeout)


def _format_upstream_error(err_obj: Any) -> str:
    """格式化 OpenAI 兼容网关在 HTTP 200 载荷内返回的 error 对象。"""
    if isinstance(err_obj, dict):
        msg = err_obj.get("message") or err_obj.get("detail") or err_obj.get("error")
        code = err_obj.get("code") or err_obj.get("type")
        if msg and code:
            return f"[{code}] {msg}"
        if msg:
            return str(msg)
    return str(err_obj)


def _consume_sse_chat_response(
    resp: Any,
    *,
    t_start: float,
    fallback_ttft_ratio: float,
) -> Tuple[str, str, Dict[str, Any], float, float]:
    """解析 OpenAI 兼容 SSE 流式响应；若服务端实际回传普通 JSON 则自动降级解析。"""
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    usage_data: Dict[str, Any] = {}
    first_token_time: Optional[float] = None
    non_sse_lines: List[str] = []
    saw_sse_data = False

    for raw_line in resp.iter_lines(decode_unicode=False):
        if raw_line is None:
            continue
        line = (
            raw_line.decode("utf-8", errors="replace").strip()
            if isinstance(raw_line, (bytes, bytearray))
            else str(raw_line).strip()
        )
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            saw_sse_data = True
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                raise requests.exceptions.RequestException(
                    f"服务端 SSE 返回错误: {_format_upstream_error(chunk['error'])}"
                )
            if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
                usage_data = chunk["usage"]
            choices = chunk.get("choices") or []
            if choices:
                choice0 = choices[0] or {}
                delta = choice0.get("delta") or choice0.get("message") or {}
                if isinstance(delta, dict):
                    c_piece = delta.get("content") or ""
                    r_piece = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if (c_piece or r_piece) and first_token_time is None:
                        first_token_time = time.time()
                    if c_piece:
                        content_parts.append(str(c_piece))
                    if r_piece:
                        reasoning_parts.append(str(r_piece))
        else:
            non_sse_lines.append(line)

    total_latency = (time.time() - t_start) * 1000.0
    if not saw_sse_data and non_sse_lines:
        try:
            res_data = json.loads("\n".join(non_sse_lines))
            if isinstance(res_data, dict):
                if res_data.get("error"):
                    raise requests.exceptions.RequestException(
                        f"服务端返回错误: {_format_upstream_error(res_data['error'])}"
                    )
                choice = (res_data.get("choices") or [{}])[0] or {}
                msg = choice.get("message") or {}
                raw_content = str(msg.get("content") or "")
                native_reasoning = str(msg.get("reasoning_content") or msg.get("reasoning") or "")
                usage = res_data.get("usage") or {}
                return raw_content, native_reasoning, usage, total_latency * fallback_ttft_ratio, total_latency
        except ValueError:
            pass

    raw_content = "".join(content_parts)
    native_reasoning = "".join(reasoning_parts)
    ttft_ms = (
        (first_token_time - t_start) * 1000.0
        if first_token_time is not None
        else total_latency * fallback_ttft_ratio
    )
    return raw_content, native_reasoning, usage_data, ttft_ms, total_latency


def execute_openai_chat_request(
    *,
    adapter_label: str,
    api_url: str,
    api_key: Optional[str],
    payload: Dict[str, Any],
    raw_messages: List[Dict[str, Any]],
    timeout: Union[int, float] = 300,
    max_retries: int = 5,
    retry_delay: float = 2.0,
    post_process_content: Optional[Callable[[str], str]] = None,
    fallback_ttft_ratio: float = 0.35,
) -> UnifiedChatResponse:
    """
    统一执行 OpenAI Chat 兼容推理请求：
    默认开启 stream=True（SSE 保活刷新单包读超时并精准统计 TTFT，兼容 Cloudflare 100s 网关与慢推理长输出），
    同时兼容不支持 stream_options 或直接返回 JSON 的服务端。
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or ''}",
    }
    req_payload = dict(payload)
    auto_added_stream_options = False
    if "stream" not in req_payload:
        req_payload["stream"] = True
        if "stream_options" not in req_payload:
            req_payload["stream_options"] = {"include_usage": True}
            auto_added_stream_options = True

    use_stream = bool(req_payload.get("stream"))
    req_timeout = _build_requests_timeout(timeout)

    last_exception: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            t_start = time.time()
            retry_without_stream_options = False
            with requests.post(
                api_url,
                headers=headers,
                json=req_payload,
                timeout=req_timeout,
                stream=use_stream,
            ) as resp:
                # 部分旧版兼容服务不识别 stream_options，遇 400/422 先关闭连接再剥离重发
                if resp.status_code in (400, 422) and auto_added_stream_options:
                    retry_without_stream_options = True
                else:
                    return _finalize_openai_http_response(
                        resp,
                        t_start=t_start,
                        use_stream=use_stream,
                        raw_messages=raw_messages,
                        post_process_content=post_process_content,
                        fallback_ttft_ratio=fallback_ttft_ratio,
                    )

            if retry_without_stream_options:
                req_payload.pop("stream_options", None)
                auto_added_stream_options = False
                with requests.post(
                    api_url,
                    headers=headers,
                    json=req_payload,
                    timeout=req_timeout,
                    stream=use_stream,
                ) as fallback_resp:
                    return _finalize_openai_http_response(
                        fallback_resp,
                        t_start=t_start,
                        use_stream=use_stream,
                        raw_messages=raw_messages,
                        post_process_content=post_process_content,
                        fallback_ttft_ratio=fallback_ttft_ratio,
                    )
        except requests.exceptions.RequestException as e:
            last_exception = e
            if attempt < max_retries:
                time.sleep(retry_delay * attempt)
            else:
                raise RuntimeError(
                    f"{adapter_label} 调用失败 (已重试{max_retries}次) [{api_url}]: {str(last_exception)}"
                )
    raise RuntimeError(f"{adapter_label} 未知错误: {str(last_exception)}")


def _finalize_openai_http_response(
    resp: Any,
    *,
    t_start: float,
    use_stream: bool,
    raw_messages: List[Dict[str, Any]],
    post_process_content: Optional[Callable[[str], str]],
    fallback_ttft_ratio: float,
) -> UnifiedChatResponse:
    resp.raise_for_status()
    resp.encoding = "utf-8"
    content_type = (resp.headers.get("content-type") or "").lower()

    if not use_stream or ("application/json" in content_type and "text/event-stream" not in content_type):
        total_latency = (time.time() - t_start) * 1000.0
        res_data = resp.json()
        if isinstance(res_data, dict) and res_data.get("error"):
            raise requests.exceptions.RequestException(
                f"服务端返回错误: {_format_upstream_error(res_data['error'])}"
            )
        choice = (res_data.get("choices") or [{}])[0] or {}
        msg = choice.get("message") or {}
        raw_content = str(msg.get("content") or "")
        native_reasoning = str(msg.get("reasoning_content") or msg.get("reasoning") or "")
        usage_data = res_data.get("usage") or {}
        ttft_ms = total_latency * fallback_ttft_ratio
    else:
        raw_content, native_reasoning, usage_data, ttft_ms, total_latency = _consume_sse_chat_response(
            resp,
            t_start=t_start,
            fallback_ttft_ratio=fallback_ttft_ratio,
        )

    if native_reasoning:
        thinking_part = native_reasoning.strip()
        answer_part = raw_content.strip()
    else:
        thinking_part, answer_part = split_thinking_and_content(raw_content)

    cleaned_answer = post_process_content(answer_part) if post_process_content else answer_part

    prompt_tokens = usage_data.get("prompt_tokens")
    if prompt_tokens is None:
        prompt_tokens = len(str(raw_messages)) // 4
    completion_tokens = usage_data.get("completion_tokens")
    if completion_tokens is None:
        est_len = len(raw_content) + len(native_reasoning)
        completion_tokens = max(1, est_len // 4) if est_len > 0 else 0
    total_tokens = usage_data.get("total_tokens", prompt_tokens + completion_tokens)

    completion_details = usage_data.get("completion_tokens_details") or {}
    thinking_tokens = completion_details.get("reasoning_tokens")
    if thinking_tokens is None:
        if thinking_part:
            denom = max(len(raw_content) + len(native_reasoning), 1) if native_reasoning else max(len(raw_content), 1)
            thinking_tokens = int(completion_tokens * (len(thinking_part) / denom))
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
            ttft_ms=round(ttft_ms, 2),
            total_latency_ms=round(total_latency, 2),
            tokens_per_second=round(tps, 2),
        ),
    )


class BaseModelAdapter(ABC):
    """模型接入层抽象基类"""

    def __init__(
        self,
        model_name: str,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 300,
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
