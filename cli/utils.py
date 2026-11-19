# -*- coding: utf-8 -*-
"""
CLI 公共辅助工具：参数载荷合并、列表解析、确认拦截、Side-by-Side 分类过滤与统一输出
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

import click

from cli.catalog import get_command_spec
from cli.client import CliError, PlatformClient
from cli.config import (
    CliContext,
    EXIT_API_ERROR,
    EXIT_CONFIRM_REQUIRED,
    EXIT_CONN_ERROR,
    EXIT_POLICY_DENIED,
)


def quote_path_segment(value: Any) -> str:
    return quote(str(value), safe="")


def read_text_file(path_str: str) -> str:
    p = Path(path_str)
    if not p.exists():
        raise CliError("FILE_NOT_FOUND", f"找不到文件: {path_str}", EXIT_API_ERROR)
    return p.read_text(encoding="utf-8")


def ensure_client(ctx: click.Context) -> PlatformClient:
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.client is None:
        cli_ctx.client = PlatformClient(
            base_url=cli_ctx.config.base_url,
            in_process=cli_ctx.config.in_process,
            timeout=cli_ctx.config.timeout,
        )
    return cli_ctx.client


def check_command_allowed(ctx: click.Context, command_id: str) -> None:
    cli_ctx: CliContext = ctx.obj
    spec = get_command_spec(command_id)
    decision = cli_ctx.policy.evaluate(spec)
    if not decision.allowed:
        raise CliError(
            code="POLICY_DENIED",
            message=decision.reason,
            exit_code=EXIT_POLICY_DENIED,
            detail={"command_id": command_id, "reason_code": decision.code},
        )
    if decision.requires_confirm and not cli_ctx.config.yes:
        raise CliError(
            code="CONFIRMATION_REQUIRED",
            message=f"命令 '{command_id}' 属于高危破坏性操作，请追加全局参数 --yes 确认执行。",
            exit_code=EXIT_CONFIRM_REQUIRED,
            detail={"command_id": command_id, "risk": spec.risk},
        )


def parse_str_list(values: Sequence[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in values:
        for part in str(item).split(","):
            val = part.strip()
            if val and val not in seen:
                seen.add(val)
                result.append(val)
    return result


def parse_json_arg(raw: str, label: str = "JSON") -> Any:
    try:
        return json.loads(raw)
    except Exception as e:
        raise CliError(
            code="INVALID_JSON",
            message=f"解析 {label} 失败: {e}",
            exit_code=EXIT_API_ERROR,
        ) from e


def read_json_input(source: str) -> Any:
    if source == "-":
        raw = sys.stdin.read()
        return parse_json_arg(raw, "stdin JSON")
    p = Path(source)
    if not p.exists():
        raise CliError(
            code="FILE_NOT_FOUND",
            message=f"找不到指定文件: {source}",
            exit_code=EXIT_API_ERROR,
        )
    return parse_json_arg(p.read_text(encoding="utf-8"), f"文件 {source}")


def load_payload(
    payload_file: Optional[str] = None,
    payload_json: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    if payload_file:
        data = read_json_input(payload_file)
        if not isinstance(data, dict):
            raise CliError(
                code="INVALID_PAYLOAD",
                message="--payload 文件顶层必须是 JSON 对象",
                exit_code=EXIT_API_ERROR,
            )
        merged.update(data)
    if payload_json:
        data = parse_json_arg(payload_json, "--payload-json")
        if not isinstance(data, dict):
            raise CliError(
                code="INVALID_PAYLOAD",
                message="--payload-json 顶层必须是 JSON 对象",
                exit_code=EXIT_API_ERROR,
            )
        merged.update(data)
    if overrides:
        for k, v in overrides.items():
            if v is not None:
                merged[k] = v
    return merged


def write_json_file(path_str: str, data: Any) -> Path:
    out_path = Path(path_str).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def _format_table_lines(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "(无记录)"
    cols: List[str] = []
    for r in rows:
        for k in r.keys():
            if k not in cols and len(cols) < 8:
                cols.append(k)
    widths = {c: len(c) for c in cols}
    str_rows: List[Dict[str, str]] = []
    for r in rows:
        sr: Dict[str, str] = {}
        for c in cols:
            v = r.get(c, "")
            if isinstance(v, (dict, list)):
                txt = json.dumps(v, ensure_ascii=False)
            else:
                txt = "" if v is None else str(v)
            if len(txt) > 48:
                txt = txt[:45] + "..."
            sr[c] = txt
            widths[c] = max(widths[c], len(txt))
        str_rows.append(sr)

    header = " | ".join(c.ljust(widths[c]) for c in cols)
    sep = "-+-".join("-" * widths[c] for c in cols)
    lines = [header, sep]
    for sr in str_rows:
        lines.append(" | ".join(sr[c].ljust(widths[c]) for c in cols))
    return "\n".join(lines)


def emit_output(ctx: click.Context, data: Any, summary_when_saved: Optional[Dict[str, Any]] = None) -> None:
    cli_ctx: CliContext = ctx.obj
    out_file = cli_ctx.config.out_file
    fmt = cli_ctx.config.output_format

    if out_file:
        saved = write_json_file(out_file, data)
        envelope: Dict[str, Any] = {
            "ok": True,
            "written_to": str(saved),
        }
        if isinstance(data, list):
            envelope["count"] = len(data)
        elif isinstance(data, dict):
            envelope["keys"] = list(data.keys())[:20]
        if summary_when_saved:
            envelope["summary"] = summary_when_saved
        click.echo(json.dumps(envelope, ensure_ascii=False, indent=None if fmt == "compact" else 2))
        return

    if fmt == "compact":
        click.echo(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    elif fmt == "table":
        if isinstance(data, list) and all(isinstance(x, dict) for x in data):
            click.echo(_format_table_lines(data))
        elif isinstance(data, dict) and isinstance(data.get("commands"), list):
            click.echo(_format_table_lines(data["commands"]))
        elif isinstance(data, dict):
            click.echo(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            click.echo(str(data))
    else:
        click.echo(json.dumps(data, ensure_ascii=False, indent=2))


def wait_for_task(
    ctx: click.Context,
    client: PlatformClient,
    task_id: str,
    *,
    poll_interval: float = 1.5,
    wait_timeout: float = 600.0,
    label: str = "task",
    fail_code: str = "TASK_FAILED",
) -> Dict[str, Any]:
    """统一轮询后台异步任务状态；任务失败时先按需落盘再抛出非零错误。"""
    deadline = time.monotonic() + wait_timeout
    last_progress = -1.0
    task_seg = quote_path_segment(task_id)
    while True:
        status_data = client.request_json("GET", f"/api/v1/benchmarks/tasks/{task_seg}")
        st = status_data.get("status")
        prog = float(status_data.get("progress") or 0.0)
        if prog != last_progress:
            click.echo(f"[{label}:{task_id}] status={st} progress={prog:.1%}", err=True)
            last_progress = prog
        if st == "COMPLETED":
            return status_data
        if st in ("FAILED", "ERROR"):
            cli_ctx: CliContext = ctx.obj
            detail_payload: Any = status_data
            if cli_ctx.config.out_file:
                saved = write_json_file(cli_ctx.config.out_file, status_data)
                detail_payload = {"written_to": str(saved), "task": status_data}
            err_msg = status_data.get("error_message") or f"{label} 任务 '{task_id}' 执行失败 (status={st})"
            raise CliError(
                code=fail_code,
                message=str(err_msg),
                exit_code=EXIT_API_ERROR,
                detail=detail_payload,
            )
        if time.monotonic() >= deadline:
            raise CliError(
                code="WAIT_TIMEOUT",
                message=f"等待 {label} 任务 '{task_id}' 超时（已等待 {wait_timeout}s），任务仍在后台执行",
                exit_code=EXIT_CONN_ERROR,
                detail={"task_id": task_id, "last_status": status_data},
            )
        time.sleep(max(0.05, poll_interval))


def classify_side_by_side_sample(sample_entry: Dict[str, Any]) -> Tuple[str, bool]:
    """严格对齐 BenchmarkReport.tsx 的样本状态分类与 API 异常标志。"""
    models_map = sample_entry.get("results") or sample_entry.get("models") or {}
    if not isinstance(models_map, dict) or not models_map:
        return "UNKNOWN", False

    pass_count = 0
    error_count = 0
    total = len(models_map)
    for r in models_map.values():
        if not isinstance(r, dict):
            continue
        content = r.get("content") or ""
        reply = r.get("reply") or ""
        is_err = (
            r.get("status") == "API_ERROR"
            or (isinstance(content, str) and content.startswith("[API调用异常"))
            or (isinstance(reply, str) and reply.startswith("[API调用异常"))
        )
        if is_err:
            error_count += 1
        elif r.get("status") == "PASS" or (r.get("status") is None and bool(r.get("objective_passed"))):
            pass_count += 1

    has_api_error = error_count > 0
    if error_count == total:
        return "API_ERROR", has_api_error
    if pass_count == total:
        return "ALL_PASS", has_api_error
    if pass_count == 0:
        return "ALL_FAIL", has_api_error
    return "DIVERGENCE", has_api_error


def filter_side_by_side(
    sbs_map: Dict[str, Any],
    *,
    filter_type: str = "all",
    category: Optional[str] = None,
    difficulty: Optional[str] = None,
    sample_id: Optional[str] = None,
    offset: int = 0,
    limit: Optional[int] = None,
    summary_only: bool = False,
) -> Dict[str, Any]:
    counts = {
        "ALL_PASS": 0,
        "ALL_FAIL": 0,
        "DIVERGENCE": 0,
        "UNKNOWN": 0,
        "API_ERROR_SAMPLES": 0,
    }
    norm_filter = (filter_type or "all").strip().lower()
    matched: List[Tuple[str, Dict[str, Any], str, bool]] = []

    for sid, entry in sbs_map.items():
        if not isinstance(entry, dict):
            continue
        klass, has_api_err = classify_side_by_side_sample(entry)
        if klass in counts:
            counts[klass] += 1
        if has_api_err:
            counts["API_ERROR_SAMPLES"] += 1

        if sample_id and sid != sample_id:
            continue
        if category and entry.get("category") != category:
            continue
        if difficulty and entry.get("difficulty") != difficulty:
            continue
        if norm_filter in ("divergence", "disagree") and klass != "DIVERGENCE":
            continue
        if norm_filter in ("all_fail", "fail") and klass != "ALL_FAIL":
            continue
        if norm_filter in ("all_pass", "pass") and klass != "ALL_PASS":
            continue
        if norm_filter == "unknown" and klass != "UNKNOWN":
            continue
        if norm_filter == "api_error" and not has_api_err:
            continue

        matched.append((sid, entry, klass, has_api_err))

    sliced = matched[offset:] if limit is None else matched[offset : offset + limit]
    out_samples: Dict[str, Any] = {}
    for sid, entry, klass, has_api_err in sliced:
        if summary_only:
            models_map = entry.get("results") or entry.get("models") or {}
            model_brief = {
                mk: {
                    "status": mv.get("status"),
                    "objective_passed": mv.get("objective_passed"),
                    "judge_score": mv.get("judge_score"),
                    "judge_verdict": mv.get("judge_verdict"),
                }
                for mk, mv in models_map.items()
                if isinstance(mv, dict)
            }
            out_samples[sid] = {
                "sample_id": sid,
                "category": entry.get("category"),
                "difficulty": entry.get("difficulty"),
                "classification": klass,
                "has_api_error": has_api_err,
                "models": model_brief,
            }
        else:
            out_samples[sid] = entry

    return {
        "total_samples": len(sbs_map),
        "matched_count": len(matched),
        "returned_count": len(out_samples),
        "classification_counts": counts,
        "samples": out_samples,
    }
