# -*- coding: utf-8 -*-
"""
CLI 命令可见性与权限策略引擎
支持 YAML / JSON 配置文件、多 Profile 切换、组级别开关、风险分级、通配符 allow/deny（deny 优先）与高危确认。
"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from cli.catalog import COMMAND_CATALOG, CommandSpec, list_groups
from cli.client import CliError
from cli.config import EXIT_API_ERROR, is_policy_locked

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "policy.default.yaml"
ALL_RISKS = ("read", "run", "write", "destructive")
GOVERNANCE_ALWAYS_ALLOWED = {"system.capabilities", "system.policy"}


@dataclass
class PolicyProfile:
    name: str
    description: str = ""
    enabled_groups: Tuple[str, ...] = field(default_factory=lambda: tuple(list_groups()))
    allowed_risks: Tuple[str, ...] = ALL_RISKS
    allow: Tuple[str, ...] = ("*",)
    deny: Tuple[str, ...] = ()
    require_confirm_for_destructive: bool = False


@dataclass(frozen=True)
class PolicyDecision:
    command_id: str
    allowed: bool
    code: str
    reason: str
    requires_confirm: bool = False


def _as_str_tuple(raw: Any, default: Tuple[str, ...]) -> Tuple[str, ...]:
    if raw is None:
        return default
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple, set)):
        return tuple(str(x).strip() for x in raw if str(x).strip())
    raise CliError(
        code="INVALID_POLICY",
        message=f"策略字段必须为字符串列表，实际类型: {type(raw).__name__}",
        exit_code=EXIT_API_ERROR,
    )


def _parse_profile(name: str, data: Dict[str, Any]) -> PolicyProfile:
    for required_non_null in ("enabled_groups", "allowed_risks", "allow"):
        if required_non_null in data and data[required_non_null] is None:
            raise CliError(
                code="INVALID_POLICY",
                message=f"策略档案 '{name}' 的字段 '{required_non_null}' 不能为空(null)，请显式提供列表或移除该键",
                exit_code=EXIT_API_ERROR,
            )

    enabled_groups = _as_str_tuple(data.get("enabled_groups"), tuple(list_groups()))
    valid_groups = set(list_groups())
    bad_groups = [g for g in enabled_groups if g not in valid_groups]
    if bad_groups:
        raise CliError(
            code="INVALID_POLICY",
            message=f"策略档案 '{name}' 包含未知命令组 {bad_groups}，有效组名: {list(list_groups())}",
            exit_code=EXIT_API_ERROR,
        )

    allowed_risks = _as_str_tuple(data.get("allowed_risks"), ALL_RISKS)
    valid_risks = set(ALL_RISKS)
    bad_risks = [r for r in allowed_risks if r not in valid_risks]
    if bad_risks:
        raise CliError(
            code="INVALID_POLICY",
            message=f"策略档案 '{name}' 包含非法风险级别 {bad_risks}，有效级别: {list(ALL_RISKS)}",
            exit_code=EXIT_API_ERROR,
        )

    return PolicyProfile(
        name=name,
        description=str(data.get("description") or ""),
        enabled_groups=enabled_groups,
        allowed_risks=allowed_risks,
        allow=_as_str_tuple(data.get("allow"), ("*",)),
        deny=_as_str_tuple(data.get("deny"), ()),
        require_confirm_for_destructive=bool(data.get("require_confirm_for_destructive", False)),
    )


def discover_policy_file(explicit_path: Optional[str] = None) -> Path:
    if explicit_path:
        p = Path(explicit_path).expanduser().resolve()
        if not p.exists():
            raise CliError(
                code="POLICY_FILE_NOT_FOUND",
                message=f"指定的策略配置文件不存在: {explicit_path}",
                exit_code=EXIT_API_ERROR,
            )
        return p

    if is_policy_locked():
        return DEFAULT_POLICY_PATH

    cwd = Path.cwd()
    for fname in ("evalctl.policy.yaml", "evalctl.policy.yml", "evalctl.policy.json"):
        cand = cwd / fname
        if cand.exists():
            return cand

    return DEFAULT_POLICY_PATH


def _load_structured_file(path: Path) -> Dict[str, Any]:
    try:
        raw_text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            parsed = json.loads(raw_text)
        else:
            parsed = yaml.safe_load(raw_text)
    except (yaml.YAMLError, json.JSONDecodeError, OSError) as e:
        raise CliError(
            code="INVALID_POLICY",
            message=f"策略配置文件读取或语法解析失败 ({path}): {e}",
            exit_code=EXIT_API_ERROR,
        ) from e
    if not isinstance(parsed, dict):
        raise CliError(
            code="INVALID_POLICY",
            message=f"策略配置文件顶层必须是对象(Mapping): {path}",
            exit_code=EXIT_API_ERROR,
        )
    return parsed


class PolicyEngine:
    """负责加载策略配置并裁决每条命令是否可见、允许执行及是否需要 --yes 确认。"""

    def __init__(
        self,
        policy_path: Path,
        active_profile_name: str,
        profile: PolicyProfile,
        available_profiles: List[str],
        defaults: Optional[Dict[str, Any]] = None,
    ):
        self.policy_path = policy_path
        self.active_profile_name = active_profile_name
        self.profile = profile
        self.available_profiles = available_profiles
        self.defaults = defaults or {}

    @classmethod
    def load(
        cls,
        policy_file: Optional[str] = None,
        profile_override: Optional[str] = None,
    ) -> "PolicyEngine":
        path = discover_policy_file(policy_file)
        data = _load_structured_file(path)
        defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}

        if "profiles" in data:
            raw_profiles = data.get("profiles")
            if not isinstance(raw_profiles, dict):
                raise CliError(
                    code="INVALID_POLICY",
                    message=f"策略文件 profiles 必须为对象(Mapping)，不能为 {type(raw_profiles).__name__}: {path}",
                    exit_code=EXIT_API_ERROR,
                )
            profiles: Dict[str, PolicyProfile] = {}
            for pname, pcfg in raw_profiles.items():
                if isinstance(pcfg, dict):
                    profiles[str(pname)] = _parse_profile(str(pname), pcfg)
            if not profiles:
                raise CliError(
                    code="INVALID_POLICY",
                    message=f"策略文件 profiles 为空: {path}",
                    exit_code=EXIT_API_ERROR,
                )
            chosen = (
                profile_override.strip()
                if profile_override and profile_override.strip()
                else str(data.get("active_profile") or next(iter(profiles.keys())))
            )
            if chosen not in profiles:
                raise CliError(
                    code="INVALID_POLICY",
                    message=f"策略档案 '{chosen}' 不存在，可用档案: {', '.join(profiles.keys())}",
                    exit_code=EXIT_API_ERROR,
                )
            return cls(
                policy_path=path,
                active_profile_name=chosen,
                profile=profiles[chosen],
                available_profiles=list(profiles.keys()),
                defaults=defaults,
            )

        # 兼容极简扁平配置（无 profiles 块，直接顶层声明 allow/deny/enabled_groups/allowed_risks）
        if profile_override and profile_override.strip():
            raise CliError(
                code="INVALID_POLICY",
                message=f"策略文件 '{path}' 为扁平单档案配置（未定义 profiles），不支持切换为 '{profile_override.strip()}'",
                exit_code=EXIT_API_ERROR,
            )
        flat_name = str(data.get("active_profile") or "custom").strip()
        profile = _parse_profile(flat_name, data)
        return cls(
            policy_path=path,
            active_profile_name=flat_name,
            profile=profile,
            available_profiles=[flat_name],
            defaults=defaults,
        )

    def evaluate(self, spec: CommandSpec) -> PolicyDecision:
        cid = spec.command_id
        for pattern in self.profile.deny:
            if fnmatch.fnmatchcase(cid, pattern):
                return PolicyDecision(
                    command_id=cid,
                    allowed=False,
                    code="MATCHED_DENYLIST",
                    reason=f"命令 '{cid}' 命中黑名单规则 '{pattern}'（profile={self.active_profile_name}）",
                )

        if cid in GOVERNANCE_ALWAYS_ALLOWED:
            return PolicyDecision(
                command_id=cid,
                allowed=True,
                code="GOVERNANCE_ALLOWED",
                reason="自省命令默认放行",
                requires_confirm=False,
            )

        if spec.group not in self.profile.enabled_groups:
            return PolicyDecision(
                command_id=cid,
                allowed=False,
                code="GROUP_DISABLED",
                reason=f"命令组 '{spec.group}' 在策略档案 '{self.active_profile_name}' 中未启用",
            )

        if spec.risk not in self.profile.allowed_risks:
            return PolicyDecision(
                command_id=cid,
                allowed=False,
                code="RISK_NOT_ALLOWED",
                reason=f"命令 '{cid}' 风险级别为 '{spec.risk}'，当前档案 '{self.active_profile_name}' 仅允许 {list(self.profile.allowed_risks)}",
            )

        if not any(fnmatch.fnmatchcase(cid, pat) for pat in self.profile.allow):
            return PolicyDecision(
                command_id=cid,
                allowed=False,
                code="NOT_IN_ALLOWLIST",
                reason=f"命令 '{cid}' 未命中白名单规则 {list(self.profile.allow)}",
            )

        requires_confirm = bool(
            self.profile.require_confirm_for_destructive and spec.risk == "destructive"
        )
        return PolicyDecision(
            command_id=cid,
            allowed=True,
            code="ALLOWED",
            reason="策略允许执行",
            requires_confirm=requires_confirm,
        )

    def is_group_visible(self, group: str) -> bool:
        for spec in COMMAND_CATALOG:
            if spec.group == group and self.evaluate(spec).allowed:
                return True
        return False

    def allowed_commands(self) -> List[CommandSpec]:
        return [spec for spec in COMMAND_CATALOG if self.evaluate(spec).allowed]

    def denied_commands(self) -> List[Tuple[CommandSpec, PolicyDecision]]:
        res: List[Tuple[CommandSpec, PolicyDecision]] = []
        for spec in COMMAND_CATALOG:
            dec = self.evaluate(spec)
            if not dec.allowed:
                res.append((spec, dec))
        return res

    def to_summary_dict(self) -> Dict[str, Any]:
        return {
            "policy_file": str(self.policy_path),
            "active_profile": self.active_profile_name,
            "available_profiles": self.available_profiles,
            "policy_locked": is_policy_locked(),
            "description": self.profile.description,
            "enabled_groups": list(self.profile.enabled_groups),
            "allowed_risks": list(self.profile.allowed_risks),
            "allow": list(self.profile.allow),
            "deny": list(self.profile.deny),
            "require_confirm_for_destructive": self.profile.require_confirm_for_destructive,
        }
