# -*- coding: utf-8 -*-
"""
CLI 运行时配置与统一退出码约定
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from cli.client import PlatformClient
    from cli.policy import PolicyEngine

EXIT_OK = 0
EXIT_API_ERROR = 1
EXIT_CONN_ERROR = 2
EXIT_POLICY_DENIED = 3
EXIT_CONFIRM_REQUIRED = 4


def parse_bool_env(val: Optional[str], default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def is_policy_locked() -> bool:
    return parse_bool_env(os.environ.get("EVALCTL_POLICY_LOCKED"), False)


def parse_timeout_env(val: Optional[str], default: Optional[float] = 600.0) -> Optional[float]:
    if val is None or not val.strip():
        return default
    raw = val.strip().lower()
    if raw in ("none", "inf", "infinite", "0"):
        return None
    try:
        num = float(raw)
        return None if num <= 0 else num
    except ValueError:
        return default


@dataclass
class CliRuntimeConfig:
    base_url: str = field(
        default_factory=lambda: os.environ.get("EVALCTL_BASE_URL", "http://127.0.0.1:9021").strip()
    )
    in_process: bool = field(
        default_factory=lambda: parse_bool_env(os.environ.get("EVALCTL_IN_PROCESS"), False)
    )
    policy_file: Optional[str] = field(
        default_factory=lambda: os.environ.get("EVALCTL_POLICY_FILE") or None
    )
    profile: Optional[str] = field(
        default_factory=lambda: os.environ.get("EVALCTL_PROFILE") or None
    )
    timeout: Optional[float] = field(
        default_factory=lambda: parse_timeout_env(os.environ.get("EVALCTL_TIMEOUT"), 600.0)
    )
    output_format: str = field(
        default_factory=lambda: (os.environ.get("EVALCTL_FORMAT") or "json").strip().lower()
    )
    out_file: Optional[str] = None
    yes: bool = False


@dataclass
class CliContext:
    config: CliRuntimeConfig
    policy: "PolicyEngine"
    client: Optional["PlatformClient"] = None
