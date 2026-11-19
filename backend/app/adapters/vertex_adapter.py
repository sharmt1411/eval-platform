# -*- coding: utf-8 -*-
"""
Google Cloud Vertex AI (Gemini) 适配器
支持:
1. ADC (Application Default Credentials) 免密鉴权
2. Windows 注册表系统代理自动感知注入
3. 底层 HTTP read=None 无限读等待 (绝不私自掐断 Google 云端长思考)
4. 429 配额限制滑动窗口自适应退避 (35s / 62s)
5. 统一输出 UnifiedChatResponse (含 thinking 思考链与正文分离)
"""
import os
import re
import json
import time
import random
import logging
from typing import Optional, List, Dict, Any
from backend.app.adapters.base import BaseModelAdapter, split_thinking_and_content
from backend.app.schemas.chat import (
    ChatMessage,
    ModelConfig,
    UnifiedChatRequest,
    UnifiedChatResponse,
    TokenUsage,
    PerformanceMetrics,
)

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    genai = None
    types = None

logger = logging.getLogger("VertexGeminiAdapter")

DEFAULT_VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT", "your-gcp-project-id")
DEFAULT_VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "global")


class VertexGeminiAdapter(BaseModelAdapter):
    """Google Cloud Vertex AI 模型适配器"""

    def __init__(
        self,
        model_name: str = "gemini-3.8-flash",
        project: str = DEFAULT_VERTEX_PROJECT,
        location: str = DEFAULT_VERTEX_LOCATION,
        api_key: Optional[str] = None,
        vertex_thinking_mode: Optional[str] = None,
        max_retries: int = 20,
        base_delay: float = 3.0,
        max_delay: float = 65.0,
        **kwargs: Any,
    ):
        clean_key = (api_key or "").strip()
        if clean_key.upper() in ("EMPTY", "ADC", "NONE"):
            clean_key = ""

        super().__init__(
            model_name=model_name,
            api_url=f"vertexai://{project}/{location}/{model_name}",
            api_key=clean_key,
            timeout=120,
            max_retries=max_retries,
            retry_delay=base_delay,
            **kwargs,
        )
        self.project = project or DEFAULT_VERTEX_PROJECT
        self.location = location or DEFAULT_VERTEX_LOCATION
        self.thinking_mode = vertex_thinking_mode or "auto"
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.client = None
        self.proxy_url: Optional[str] = None
        self._credentials = None

        if GENAI_AVAILABLE:
            self._init_client()

    def _init_client(self):
        # 自动感知并注入 Windows 系统代理 (解决 Python httpx 不读取注册表代理导致的直连丢包)
        proxy_url = None
        if not os.environ.get("HTTP_PROXY") and not os.environ.get("HTTPS_PROXY"):
            try:
                import winreg
                k = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                )
                enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
                if enable:
                    server, _ = winreg.QueryValueEx(k, "ProxyServer")
                    if server:
                        proxy_url = (
                            f"http://{server}"
                            if not server.startswith("http")
                            else server
                        )
                        os.environ["HTTP_PROXY"] = proxy_url
                        os.environ["HTTPS_PROXY"] = proxy_url
            except Exception:
                pass
        else:
            proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.proxy_url = proxy_url

        import httpx

        custom_timeout = httpx.Timeout(
            connect=30.0,
            read=None,  # 彻底取消客户端自身读超时限制，无限等待 Google 推理回包
            write=60.0,
            pool=None,
        )
        transport = (
            httpx.HTTPTransport(proxy=proxy_url)
            if proxy_url
            else httpx.HTTPTransport()
        )
        custom_client = httpx.Client(transport=transport, timeout=custom_timeout)
        http_options = types.HttpOptions(httpx_client=custom_client)

        # 鉴权模式解析:
        # 1. 默认留空 -> 使用本机 Google Cloud ADC (Application Default Credentials) 免密鉴权
        # 2. 若填入服务账号 JSON 文件路径或 JSON 内容 -> 显式加载 Credentials
        # 3. 若填入显式 API Key 字符串 -> 使用 api_key 模式
        credentials = None
        if self.api_key:
            if self.api_key.endswith(".json") and os.path.exists(self.api_key):
                from google.oauth2 import service_account
                credentials = service_account.Credentials.from_service_account_file(
                    self.api_key,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
            elif self.api_key.startswith("{") and "service_account" in self.api_key:
                from google.oauth2 import service_account
                info = json.loads(self.api_key)
                credentials = service_account.Credentials.from_service_account_info(
                    info,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
        self._credentials = credentials

        if self.api_key and credentials is None:
            # 显式 API Key 模式 (注意 google-genai 中 api_key 与 project/location 互斥)
            self.client = genai.Client(
                vertexai=True,
                api_key=self.api_key,
                http_options=http_options,
            )
        else:
            self.client = genai.Client(
                vertexai=True,
                project=self.project,
                location=self.location,
                credentials=credentials,
                http_options=http_options,
            )

    def _get_bearer_token(self) -> str:
        """获取用于 Vertex OpenAI MaaS 端点的 Bearer Token（复用服务账号或本机 ADC 凭证）"""
        import google.auth
        import google.auth.transport.requests

        if self._credentials is not None:
            if not getattr(self._credentials, "valid", False) or not getattr(self._credentials, "token", None):
                self._credentials.refresh(google.auth.transport.requests.Request())
            return str(self._credentials.token or "")

        if self.api_key:
            return self.api_key

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        creds.refresh(google.auth.transport.requests.Request())
        self._credentials = creds
        return str(creds.token or "")

    def _execute_with_retry(self, func, max_attempts: Optional[int] = None):
        attempts = max_attempts or self.max_retries
        for attempt in range(1, attempts + 1):
            try:
                return func()
            except Exception as e:
                err_msg = str(e)
                err_code = getattr(e, "code", None)
                if err_code is None and getattr(e, "response", None) is not None:
                    err_code = getattr(e.response, "status_code", None)
                err_type_name = e.__class__.__name__

                is_connect_error = (
                    "ConnectTimeout" in err_type_name
                    or "ConnectError" in err_type_name
                    or "getaddrinfo failed" in err_msg
                    or "Failed to establish a new connection" in err_msg
                )

                is_rate_limit = (
                    err_code == 429
                    or "429" in err_msg
                    or "RESOURCE_EXHAUSTED" in err_msg
                    or "ResourceExhausted" in err_msg
                    or "quota" in err_msg.lower()
                    or "rate limit" in err_msg.lower()
                    or "too many requests" in err_msg.lower()
                )

                is_server_timeout = (
                    err_code in [500, 503, 504]
                    or "504" in err_msg
                    or "DEADLINE_EXCEEDED" in err_msg
                    or "Deadline expired" in err_msg
                    or "503" in err_msg
                    or "unavailable" in err_msg.lower()
                )

                if (
                    is_rate_limit or is_server_timeout or is_connect_error
                ) and attempt < attempts:
                    if is_connect_error:
                        reason = "底层连接/握手失败"
                        delay = 3.0 + random.uniform(0.5, 1.5)
                    elif is_rate_limit:
                        reason = "429 配额限制 (滑动窗口)"
                        delay = (
                            35.0 + random.uniform(1.0, 3.0)
                            if attempt == 1
                            else 62.0 + random.uniform(1.0, 4.0)
                        )
                    else:
                        reason = "Google 服务端超时 504"
                        delay = 5.0 + (attempt - 1) * 5.0 + random.uniform(1.0, 2.0)

                    match = re.search(
                        r"retry after ([0-9]+(?:\.[0-9]+)?)s?", err_msg, re.IGNORECASE
                    )
                    if match:
                        suggested_delay = float(match.group(1)) + 1.0
                        delay = max(delay, suggested_delay)

                    print(
                        f"\n⚠️ [Vertex AI: {self.model_name}] 触发 {reason}，等待 {delay:.1f}s 后第 {attempt}/{attempts} 次重试...",
                        flush=True,
                    )
                    time.sleep(delay)
                    continue
                raise

    @staticmethod
    def _wants_thinking(config: ModelConfig) -> bool:
        return bool(
            config.thinking_enabled
            or (
                config.thinking_level
                and config.thinking_level.upper() not in ("NONE", "OFF", "FALSE")
            )
        )

    def _call_gemini_native(
        self, request: UnifiedChatRequest, max_attempts: Optional[int] = None
    ) -> UnifiedChatResponse:
        if not GENAI_AVAILABLE or not self.client:
            raise RuntimeError("google-genai 未安装或初始化失败，无法调用 VertexGeminiAdapter")

        system_instruction = None
        gemini_contents = []

        for m in request.messages:
            if m.role == "system":
                if system_instruction is None:
                    system_instruction = m.content
                else:
                    system_instruction += "\n" + m.content
            else:
                gemini_role = "user" if m.role == "user" else "model"
                gemini_contents.append(
                    types.Content(
                        role=gemini_role, parts=[types.Part.from_text(text=m.content)]
                    )
                )

        # 规整化 contents: Gemini 严格要求必须以 user 开头且以 user 结尾，且不能有连续相同 role
        normalized_contents = []
        if gemini_contents and gemini_contents[0].role == "model":
            normalized_contents.append(
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text="你好，请进入角色对话。")],
                )
            )

        for c in gemini_contents:
            if normalized_contents and normalized_contents[-1].role == c.role:
                prev_text = "".join([p.text or "" for p in normalized_contents[-1].parts])
                curr_text = "".join([p.text or "" for p in c.parts])
                normalized_contents[-1] = types.Content(
                    role=c.role,
                    parts=[types.Part.from_text(text=f"{prev_text}\n{curr_text}")],
                )
            else:
                normalized_contents.append(c)

        if not normalized_contents:
            normalized_contents.append(
                types.Content(role="user", parts=[types.Part.from_text(text="你好")])
            )
        elif normalized_contents[-1].role == "model":
            normalized_contents.append(
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text="请继续按照要求进行回复。")],
                )
            )

        # 思考配置 (对齐原 ai_test_engine: 小写 level + include_thoughts=True)
        thinking_config = None
        if self._wants_thinking(request.config):
            level = (request.config.thinking_level or "LOW").upper()
            if level not in ("LOW", "MEDIUM", "HIGH"):
                level = "LOW"
            try:
                thinking_config = types.ThinkingConfig(
                    thinking_level=level.lower(),
                    include_thoughts=True,
                )
            except Exception:
                pass

        config_args = {
            "temperature": request.config.temperature,
            "top_p": request.config.top_p,
            "max_output_tokens": request.config.max_tokens,
        }
        if system_instruction:
            config_args["system_instruction"] = system_instruction
        if thinking_config:
            config_args["thinking_config"] = thinking_config

        gen_config = types.GenerateContentConfig(**config_args)

        t_start = time.time()

        def _call():
            return self.client.models.generate_content(
                model=self.model_name, contents=normalized_contents, config=gen_config
            )

        resp = self._execute_with_retry(_call, max_attempts=max_attempts)
        total_latency = (time.time() - t_start) * 1000.0

        full_content = ""
        full_thinking = ""
        if resp.candidates and len(resp.candidates) > 0:
            cand = resp.candidates[0]
            if cand.content and cand.content.parts:
                for part in cand.content.parts:
                    if getattr(part, "thought", False):
                        full_thinking += getattr(part, "text", "") or ""
                    else:
                        full_content += getattr(part, "text", "") or ""

        # 兼容正文中返回 <think>...</think> 的情况
        if not full_thinking and full_content:
            extracted_think, cleaned_text = split_thinking_and_content(full_content)
            if extracted_think:
                full_thinking = extracted_think
                full_content = cleaned_text

        # Usage
        usage_meta = getattr(resp, "usage_metadata", None)
        prompt_tokens = (
            (getattr(usage_meta, "prompt_token_count", 0) or 0) if usage_meta else 0
        )
        candidates_tokens = (
            (getattr(usage_meta, "candidates_token_count", 0) or 0) if usage_meta else 0
        )
        total_tokens = (
            (getattr(usage_meta, "total_token_count", 0) or 0)
            if usage_meta
            else (prompt_tokens + candidates_tokens)
        )

        thinking_tokens = (getattr(usage_meta, "thought_token_count", 0) or 0) if usage_meta else 0
        answer_tokens = max(candidates_tokens - thinking_tokens, 0)
        tps = (candidates_tokens / (total_latency / 1000.0)) if total_latency > 0 else 0.0

        return UnifiedChatResponse(
            content=full_content.strip(),
            thinking=full_thinking.strip(),
            raw_output=full_content,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=candidates_tokens,
                thinking_tokens=thinking_tokens,
                answer_tokens=answer_tokens,
                total_tokens=total_tokens,
            ),
            metrics=PerformanceMetrics(
                ttft_ms=total_latency * 0.3,
                total_latency_ms=total_latency,
                tokens_per_second=round(tps, 2),
            ),
        )

    def _call_maas_openai(
        self,
        request: UnifiedChatRequest,
        thinking_mode: str,
        max_attempts: Optional[int] = None,
    ) -> UnifiedChatResponse:
        """通过 Vertex AI OpenAI 兼容 MaaS 端点 (/endpoints/openapi/chat/completions) 发起推理"""
        import httpx

        host = (
            "aiplatform.googleapis.com"
            if self.location == "global"
            else f"{self.location}-aiplatform.googleapis.com"
        )
        url = f"https://{host}/v1/projects/{self.project}/locations/{self.location}/endpoints/openapi/chat/completions"

        payload: Dict[str, Any] = {
            "model": self.model_name,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": request.config.temperature,
            "top_p": request.config.top_p,
            "max_tokens": request.config.max_tokens,
        }

        wants_thinking = self._wants_thinking(request.config)
        if thinking_mode == "maas_reasoning_effort":
            if wants_thinking:
                level = (request.config.thinking_level or "LOW").lower()
                if level not in ("low", "medium", "high"):
                    level = "low"
                payload["reasoning_effort"] = level
        elif thinking_mode == "maas_chat_template":
            payload["chat_template_kwargs"] = {"thinking": wants_thinking}

        if request.config.extra_body:
            payload.update(request.config.extra_body)

        timeout = httpx.Timeout(connect=30.0, read=None, write=60.0, pool=None)
        t_start = time.time()

        def _call():
            token = self._get_bearer_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            with httpx.Client(proxy=self.proxy_url, timeout=timeout) as client:
                r = client.post(url, headers=headers, json=payload)
                r.raise_for_status()
                return r.json()

        res_data = self._execute_with_retry(_call, max_attempts=max_attempts)
        total_latency = (time.time() - t_start) * 1000.0

        choices = res_data.get("choices") or []
        msg = choices[0].get("message", {}) if choices else {}
        raw_content = msg.get("content") or ""
        native_reasoning = msg.get("reasoning_content") or ""

        if native_reasoning:
            thinking_part = native_reasoning.strip()
            cleaned_answer = raw_content.strip()
        else:
            thinking_part, cleaned_answer = split_thinking_and_content(raw_content)

        usage_data = res_data.get("usage") or {}
        prompt_tokens = usage_data.get("prompt_tokens", 0) or 0
        completion_tokens = usage_data.get("completion_tokens", 0) or 0
        total_tokens = usage_data.get("total_tokens", prompt_tokens + completion_tokens) or 0

        completion_details = usage_data.get("completion_tokens_details") or {}
        thinking_tokens = completion_details.get("reasoning_tokens")
        if thinking_tokens is None:
            if thinking_part and completion_tokens > 0:
                thinking_tokens = int(
                    completion_tokens
                    * (len(thinking_part) / max(len(raw_content) + len(thinking_part), 1))
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
                ttft_ms=total_latency * 0.3,
                total_latency_ms=total_latency,
                tokens_per_second=round(tps, 2),
            ),
        )

    def detect_thinking_mode(self) -> Dict[str, Any]:
        """
        自适应能力探测（不依赖模型名字符串判断）：
        依次测试 Gemini 原生协议 -> Vertex MaaS reasoning_effort -> Vertex MaaS chat_template_kwargs，
        以实际响应中是否真正产出思维链为依据，返回应存储的 vertex_thinking_mode。
        """
        probe_msgs = [
            ChatMessage(role="user", content="请简短推理并回答：17 * 23 等于多少？")
        ]
        probe_log: List[Dict[str, Any]] = []
        gemini_succeeded = False
        maas_succeeded = False

        # Probe A: Google Gemini 原生 ThinkingConfig
        if GENAI_AVAILABLE and self.client:
            try:
                req_a = UnifiedChatRequest(
                    model_id=self.model_name,
                    messages=probe_msgs,
                    config=ModelConfig(
                        temperature=0.1,
                        max_tokens=512,
                        thinking_enabled=True,
                        thinking_level="LOW",
                    ),
                )
                resp_a = self._call_gemini_native(req_a, max_attempts=2)
                gemini_succeeded = True
                has_thought = bool(resp_a.thinking or resp_a.usage.thinking_tokens > 0)
                probe_log.append({
                    "mode": "gemini_native",
                    "ok": True,
                    "has_thinking": has_thought,
                    "thinking_tokens": resp_a.usage.thinking_tokens,
                })
                if has_thought:
                    self.thinking_mode = "gemini_native"
                    return {
                        "thinking_mode": "gemini_native",
                        "sample_thinking": resp_a.thinking[:200],
                        "probe_log": probe_log,
                    }
            except Exception as e:
                probe_log.append({"mode": "gemini_native", "ok": False, "error": str(e)[:200]})

        # Probe B & C: Vertex AI OpenAI 兼容 MaaS 端点
        for maas_mode in ("maas_reasoning_effort", "maas_chat_template"):
            try:
                req_m = UnifiedChatRequest(
                    model_id=self.model_name,
                    messages=probe_msgs,
                    config=ModelConfig(
                        temperature=0.1,
                        max_tokens=512,
                        thinking_enabled=True,
                        thinking_level="HIGH",
                    ),
                )
                resp_m = self._call_maas_openai(req_m, maas_mode, max_attempts=2)
                maas_succeeded = True
                has_thought = bool(resp_m.thinking or resp_m.usage.thinking_tokens > 0)
                probe_log.append({
                    "mode": maas_mode,
                    "ok": True,
                    "has_thinking": has_thought,
                    "thinking_tokens": resp_m.usage.thinking_tokens,
                })
                if has_thought:
                    self.thinking_mode = maas_mode
                    return {
                        "thinking_mode": maas_mode,
                        "sample_thinking": resp_m.thinking[:200],
                        "probe_log": probe_log,
                    }
            except Exception as e:
                probe_log.append({"mode": maas_mode, "ok": False, "error": str(e)[:200]})

        if gemini_succeeded:
            self.thinking_mode = "none"
            return {"thinking_mode": "none", "sample_thinking": "", "probe_log": probe_log}

        if maas_succeeded:
            self.thinking_mode = "maas_none"
            return {"thinking_mode": "maas_none", "sample_thinking": "", "probe_log": probe_log}

        return {
            "thinking_mode": None,
            "error": "全部思考协议探测均失败",
            "probe_log": probe_log,
        }

    def chat(self, request: UnifiedChatRequest) -> UnifiedChatResponse:
        req_mode = request.config.vertex_thinking_mode
        eff_mode = (
            req_mode
            if req_mode and req_mode != "auto"
            else (self.thinking_mode if self.thinking_mode != "auto" else "gemini_native")
        )
        if eff_mode in ("maas_reasoning_effort", "maas_chat_template", "maas_none"):
            return self._call_maas_openai(request, eff_mode)
        return self._call_gemini_native(request)
