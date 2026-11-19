# -*- coding: utf-8 -*-
"""
平台 CLI 主入口与动态策略裁剪器 (evalctl)
"""
from __future__ import annotations

import json
import os
import sys
from typing import List, Optional, Sequence, Tuple

import click

from cli import __version__
from cli.catalog import COMMAND_MAP, GROUP_DESCRIPTIONS
from cli.client import CliError
from cli.commands import GROUP_REGISTRARS
from cli.config import (
    CliContext,
    CliRuntimeConfig,
    EXIT_API_ERROR,
    EXIT_OK,
    EXIT_POLICY_DENIED,
    is_policy_locked,
    parse_bool_env,
    parse_timeout_env,
)
from cli.policy import PolicyEngine


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass


_GLOBAL_VALUE_OPTIONS = {"--config", "--profile", "--base-url", "--timeout", "--format", "--out"}
_GLOBAL_FLAG_OPTIONS = {"--in-process", "--http", "--yes", "-y", "--help", "-h", "--version"}


def _peek_policy_flags(raw_args: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
    """仅扫描第一个子命令之前的顶层全局选项，防止误扫子命令参数并忽略未注册短选项。"""
    cfg: Optional[str] = None
    prof: Optional[str] = None
    i = 0
    n = len(raw_args)
    while i < n:
        tok = raw_args[i]
        if tok == "--" or not tok.startswith("-"):
            break
        if tok.startswith("--config="):
            cfg = tok.split("=", 1)[1]
            i += 1
            continue
        if tok.startswith("--profile="):
            prof = tok.split("=", 1)[1]
            i += 1
            continue
        if tok in _GLOBAL_VALUE_OPTIONS:
            if i + 1 < n:
                if tok == "--config":
                    cfg = raw_args[i + 1]
                elif tok == "--profile":
                    prof = raw_args[i + 1]
                i += 2
                continue
            i += 1
            continue
        if tok in _GLOBAL_FLAG_OPTIONS or "=" in tok:
            i += 1
            continue
        i += 1
    return cfg, prof


def _enforce_policy_lock(cli_config_arg: Optional[str], cli_profile_arg: Optional[str]) -> None:
    if is_policy_locked() and (cli_config_arg is not None or cli_profile_arg is not None):
        raise CliError(
            code="POLICY_LOCKED",
            message="当前环境已锁定权限策略 (EVALCTL_POLICY_LOCKED=1)，禁止通过命令行 --config 或 --profile 覆盖",
            exit_code=EXIT_POLICY_DENIED,
        )


def resolve_policy_from_ctx(ctx: click.Context) -> PolicyEngine:
    root = ctx.find_root()
    if isinstance(root.obj, CliContext):
        return root.obj.policy
    cached = root.meta.get("evalctl_policy")
    if isinstance(cached, PolicyEngine):
        return cached

    raw_args = root.meta["raw_cli_args"] if "raw_cli_args" in root.meta else sys.argv[1:]
    peek_cfg, peek_prof = _peek_policy_flags(raw_args)
    cli_cfg = root.params.get("config_file") if root.params.get("config_file") is not None else peek_cfg
    cli_prof = root.params.get("profile") if root.params.get("profile") is not None else peek_prof
    _enforce_policy_lock(cli_cfg, cli_prof)

    policy_file = cli_cfg or os.environ.get("EVALCTL_POLICY_FILE") or None
    profile_name = cli_prof or os.environ.get("EVALCTL_PROFILE") or None
    engine = PolicyEngine.load(policy_file=policy_file, profile_override=profile_name)
    root.meta["evalctl_policy"] = engine
    return engine


def _make_denied_command(cmd_name: str, message: str, detail: dict) -> click.Command:
    @click.command(
        name=cmd_name,
        add_help_option=False,
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )
    def _denied_cmd() -> None:
        raise CliError(
            code="POLICY_DENIED",
            message=message,
            exit_code=EXIT_POLICY_DENIED,
            detail=detail,
        )

    return _denied_cmd


class PolicyAwareGroup(click.Group):
    """按当前策略动态隐藏或拦截子命令"""

    def __init__(self, *args, group_name: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.group_name = group_name

    def list_commands(self, ctx: click.Context) -> List[str]:
        all_cmds = super().list_commands(ctx)
        policy = resolve_policy_from_ctx(ctx)
        visible: List[str] = []
        for name in all_cmds:
            spec = COMMAND_MAP.get(f"{self.group_name}.{name}")
            if spec is None or policy.evaluate(spec).allowed:
                visible.append(name)
        return visible

    def get_command(self, ctx: click.Context, cmd_name: str) -> Optional[click.Command]:
        cmd = super().get_command(ctx, cmd_name)
        if cmd is None:
            return None
        spec = COMMAND_MAP.get(f"{self.group_name}.{cmd_name}")
        if spec is None:
            return cmd
        policy = resolve_policy_from_ctx(ctx)
        decision = policy.evaluate(spec)
        if decision.allowed:
            return cmd
        return _make_denied_command(
            cmd_name,
            decision.reason,
            {"command_id": spec.command_id, "reason_code": decision.code},
        )


class PolicyAwareRootGroup(click.Group):
    """顶层命令组：捕获原始参数支持 --help 阶段动态裁剪，并拦截禁用命令组"""

    def make_context(self, info_name, args, parent=None, **extra):
        arg_snapshot = list(args)
        ctx = super().make_context(info_name, args, parent=parent, **extra)
        ctx.meta["raw_cli_args"] = arg_snapshot
        return ctx

    def parse_args(self, ctx: click.Context, args: List[str]) -> List[str]:
        if "raw_cli_args" not in ctx.meta:
            ctx.meta["raw_cli_args"] = list(args)
        return super().parse_args(ctx, args)

    def list_commands(self, ctx: click.Context) -> List[str]:
        all_groups = super().list_commands(ctx)
        policy = resolve_policy_from_ctx(ctx)
        return [g for g in all_groups if policy.is_group_visible(g)]

    def get_command(self, ctx: click.Context, cmd_name: str) -> Optional[click.Command]:
        cmd = super().get_command(ctx, cmd_name)
        if cmd is None:
            return None
        policy = resolve_policy_from_ctx(ctx)
        if policy.is_group_visible(cmd_name):
            return cmd
        return _make_denied_command(
            cmd_name,
            f"命令组 '{cmd_name}' 在当前策略档案 '{policy.active_profile_name}' 中未启用",
            {"group": cmd_name, "profile": policy.active_profile_name, "reason_code": "GROUP_DISABLED"},
        )


def build_cli() -> click.Group:
    @click.group(
        cls=PolicyAwareRootGroup,
        context_settings={"help_option_names": ["-h", "--help"]},
    )
    @click.option("--config", "config_file", default=None, help="策略配置文件路径 (YAML/JSON)")
    @click.option("--profile", "profile", default=None, help="切换策略档案 (full/safe/readonly/eval-loop/flywheel-loop)")
    @click.option("--base-url", default=None, help="平台 FastAPI 服务基址 (默认 http://127.0.0.1:9021)")
    @click.option("--in-process/--http", default=None, help="使用进程内 ASGI 调用或 HTTP 连接运行中服务")
    @click.option("--timeout", default=None, help="HTTP 读取超时秒数（0 或 none 表示不限）")
    @click.option(
        "--format",
        "output_format",
        type=click.Choice(["json", "compact", "table"], case_sensitive=False),
        default=None,
        help="输出格式 (默认 json)",
    )
    @click.option("--out", "out_file", default=None, help="将完整 JSON 产物写入本地文件并在 stdout 返回摘要")
    @click.option("--yes", "-y", is_flag=True, help="确认执行高危破坏性命令")
    @click.version_option(version=__version__, prog_name="evalctl")
    @click.pass_context
    def cli_root(
        ctx: click.Context,
        config_file: Optional[str],
        profile: Optional[str],
        base_url: Optional[str],
        in_process: Optional[bool],
        timeout: Optional[str],
        output_format: Optional[str],
        out_file: Optional[str],
        yes: bool,
    ) -> None:
        """通用大模型测试管理与数据飞轮平台 CLI (支持策略裁剪与全能力 Agent 调度)"""
        _enforce_policy_lock(config_file, profile)
        policy = PolicyEngine.load(
            policy_file=config_file or os.environ.get("EVALCTL_POLICY_FILE") or None,
            profile_override=profile or os.environ.get("EVALCTL_PROFILE") or None,
        )
        ctx.meta["evalctl_policy"] = policy

        default_url = str(policy.defaults.get("base_url") or "http://127.0.0.1:9021")
        default_timeout = policy.defaults.get("timeout_seconds", 600.0)
        cfg = CliRuntimeConfig(
            base_url=(base_url or os.environ.get("EVALCTL_BASE_URL") or default_url).strip(),
            in_process=(
                in_process
                if in_process is not None
                else parse_bool_env(os.environ.get("EVALCTL_IN_PROCESS"), False)
            ),
            policy_file=str(policy.policy_path),
            profile=policy.active_profile_name,
            timeout=parse_timeout_env(
                timeout if timeout is not None else os.environ.get("EVALCTL_TIMEOUT"),
                float(default_timeout) if default_timeout is not None else 600.0,
            ),
            output_format=(
                output_format
                or os.environ.get("EVALCTL_FORMAT")
                or "json"
            ).strip().lower(),
            out_file=out_file,
            yes=yes,
        )
        cli_ctx = CliContext(config=cfg, policy=policy)
        ctx.obj = cli_ctx

        def _cleanup() -> None:
            if cli_ctx.client is not None:
                cli_ctx.client.close()

        ctx.call_on_close(_cleanup)

    for grp_name, registrar in GROUP_REGISTRARS.items():
        grp = PolicyAwareGroup(
            name=grp_name,
            group_name=grp_name,
            help=GROUP_DESCRIPTIONS.get(grp_name, ""),
        )
        registrar(grp)
        cli_root.add_command(grp)

    return cli_root


cli = build_cli()


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_utf8_stdio()
    args = list(argv) if argv is not None else sys.argv[1:]
    try:
        cli.main(args=args, prog_name="evalctl", standalone_mode=False)
        return EXIT_OK
    except click.exceptions.Exit as exc:
        return int(exc.exit_code or 0)
    except CliError as err:
        click.echo(json.dumps(err.to_dict(), ensure_ascii=False, indent=2), err=True)
        return err.exit_code
    except click.ClickException as exc:
        err = CliError(
            code="CLI_USAGE_ERROR",
            message=exc.format_message(),
            exit_code=EXIT_API_ERROR,
        )
        click.echo(json.dumps(err.to_dict(), ensure_ascii=False, indent=2), err=True)
        return err.exit_code
    except click.Abort:
        err = CliError(code="ABORTED", message="命令执行已中止", exit_code=EXIT_API_ERROR)
        click.echo(json.dumps(err.to_dict(), ensure_ascii=False, indent=2), err=True)
        return EXIT_API_ERROR
    except Exception as exc:
        err = CliError(code="INTERNAL_ERROR", message=str(exc), exit_code=EXIT_API_ERROR)
        click.echo(json.dumps(err.to_dict(), ensure_ascii=False, indent=2), err=True)
        return EXIT_API_ERROR


if __name__ == "__main__":
    sys.exit(main())
