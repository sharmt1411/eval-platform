# -*- coding: utf-8 -*-
"""
平台薄 HTTP / ASGI 客户端
默认走 HTTP/SSE 直连本机 FastAPI 后端（与 Web 工作台共享 SQLite 写锁与后台任务）；
也支持 --in-process 模式直接经 FastAPI TestClient 调用应用（适合无常驻服务或测试隔离）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.parse import urlparse

import httpx

from cli.config import EXIT_API_ERROR, EXIT_CONN_ERROR


def _is_loopback_url(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").strip().lower()
    return host in ("localhost", "::1") or host.startswith("127.")


class CliError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        exit_code: int = EXIT_API_ERROR,
        status_code: Optional[int] = None,
        detail: Any = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.status_code = status_code
        self.detail = detail

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "exit_code": self.exit_code,
            },
        }
        if self.status_code is not None:
            out["error"]["status_code"] = self.status_code
        if self.detail is not None:
            out["error"]["detail"] = self.detail
        return out


def _extract_error_message(resp: httpx.Response) -> Tuple[str, Any]:
    try:
        payload = resp.json()
        if isinstance(payload, dict) and "detail" in payload:
            detail = payload["detail"]
            msg = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
            return msg, detail
        return json.dumps(payload, ensure_ascii=False), payload
    except Exception:
        text = resp.text.strip() or f"HTTP {resp.status_code}"
        return text, text


class PlatformClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:9021",
        in_process: bool = False,
        timeout: Optional[float] = 600.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.in_process = in_process
        self.timeout = timeout
        self._client: Any = None

    def __enter__(self) -> "PlatformClient":
        self._ensure_client()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self.in_process:
            from fastapi.testclient import TestClient
            from backend.app.main import app
            from backend.app.models import init_db

            init_db()
            self._client = TestClient(app)
            self._client.__enter__()
            return self._client

        timeout_cfg = httpx.Timeout(connect=15.0, read=self.timeout, write=60.0, pool=15.0)
        self._client = httpx.Client(
            base_url=self.base_url,
            follow_redirects=True,
            timeout=timeout_cfg,
            trust_env=not _is_loopback_url(self.base_url),
        )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                if self.in_process:
                    self._client.__exit__(None, None, None)
                else:
                    self._client.close()
            finally:
                self._client = None

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
    ) -> Any:
        client = self._ensure_client()
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = client.request(
                method=method,
                url=path,
                params=clean_params or None,
                json=json_body,
            )
        except httpx.ConnectError as e:
            raise CliError(
                code="CONNECTION_FAILED",
                message=f"无法连接平台后端服务 ({self.base_url})，请确认 FastAPI 服务已启动，或改用 --in-process 模式: {e}",
                exit_code=EXIT_CONN_ERROR,
            ) from e
        except httpx.TimeoutException as e:
            raise CliError(
                code="REQUEST_TIMEOUT",
                message=f"请求平台后端超时 ({method} {path}): {e}",
                exit_code=EXIT_CONN_ERROR,
            ) from e
        except httpx.HTTPError as e:
            raise CliError(
                code="HTTP_TRANSPORT_ERROR",
                message=f"HTTP 通信异常 ({method} {path}): {e}",
                exit_code=EXIT_CONN_ERROR,
            ) from e

        if resp.status_code >= 400:
            msg, detail = _extract_error_message(resp)
            raise CliError(
                code=f"HTTP_{resp.status_code}",
                message=msg,
                exit_code=EXIT_API_ERROR,
                status_code=resp.status_code,
                detail=detail,
            )

        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {"raw_text": resp.text}

    def stream_sse(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
    ) -> Iterator[Dict[str, Any]]:
        client = self._ensure_client()
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            with client.stream(
                method=method,
                url=path,
                params=clean_params or None,
                json=json_body,
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    msg, detail = _extract_error_message(resp)
                    raise CliError(
                        code=f"HTTP_{resp.status_code}",
                        message=msg,
                        exit_code=EXIT_API_ERROR,
                        status_code=resp.status_code,
                        detail=detail,
                    )
                for line in resp.iter_lines():
                    if not line:
                        continue
                    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
                    text = text.strip()
                    if not text.startswith("data:"):
                        continue
                    raw_json = text[5:].strip()
                    if not raw_json or raw_json == "[DONE]":
                        continue
                    try:
                        evt = json.loads(raw_json)
                        if isinstance(evt, dict):
                            yield evt
                    except json.JSONDecodeError:
                        yield {"event": "raw", "data": raw_json}
        except CliError:
            raise
        except httpx.ConnectError as e:
            raise CliError(
                code="CONNECTION_FAILED",
                message=f"无法连接平台后端服务 ({self.base_url})，请确认 FastAPI 服务已启动，或改用 --in-process 模式: {e}",
                exit_code=EXIT_CONN_ERROR,
            ) from e
        except httpx.TimeoutException as e:
            raise CliError(
                code="REQUEST_TIMEOUT",
                message=f"SSE 流式请求超时 ({method} {path}): {e}",
                exit_code=EXIT_CONN_ERROR,
            ) from e
        except httpx.HTTPError as e:
            raise CliError(
                code="HTTP_TRANSPORT_ERROR",
                message=f"SSE 流式通信异常 ({method} {path}): {e}",
                exit_code=EXIT_CONN_ERROR,
            ) from e
