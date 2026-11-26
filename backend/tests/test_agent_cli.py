# -*- coding: utf-8 -*-
"""
Agent CLI 全能力端到端与权限策略回归测试 (backend/tests/test_agent_cli.py)
严格在 tmp_path 隔离数据库与数据目录中执行，绝不污染真实 data/eval_platform.db 或 data/ 目录。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Set, Tuple

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
MAP_DIR = PROJECT_ROOT / "docs" / "map"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(MAP_DIR) not in sys.path:
    sys.path.insert(0, str(MAP_DIR))

from fact_extractor import BackendAnalyzer  # noqa: E402

import backend.app.api.flywheel as api_flywheel
import backend.app.engine.prompt_registry as prompt_registry
import backend.app.models as models_pkg
import backend.app.models.base as models_base
from backend.app.config import settings
from backend.app.main import app
from backend.app.models.base import Base, FlywheelSampleRecord, get_db, init_db
from cli.catalog import COMMAND_CATALOG
from cli.client import PlatformClient, _is_loopback_url
from cli.config import EXIT_API_ERROR, EXIT_CONFIRM_REQUIRED, EXIT_POLICY_DENIED
from cli.main import _peek_policy_flags, build_cli, main as cli_main, resolve_policy_from_ctx
from cli.utils import classify_side_by_side_sample, filter_side_by_side, quote_path_segment


@pytest.fixture()
def isolated_cli_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """为 CLI 全链路测试提供 100% 隔离的 SQLite 与 data 目录"""
    data_dir = tmp_path / "data"
    rules_dir = data_dir / "rules"
    datasets_dir = data_dir / "datasets"
    runs_dir = data_dir / "runs"
    for d in (data_dir, rules_dir, datasets_dir, runs_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 写入一份纯虚构的通用示范规则模板到隔离目录
    demo_rule = {
        "rule_id": "demo_after_sales_template",
        "project_name": "星云智联智能设备售后回访SOP",
        "description": "通用售后回访标准SOP示范模板",
        "bot_config": {
            "system_prompt": "你是星云智联售后客服中心的专业客服专员。",
            "initiator": "user",
            "max_turns": 60,
            "closing_keywords": ["祝您生活愉快，再见", "再见！"],
        },
        "topology_sop": {
            "enabled": True,
            "mermaid_graph": "flowchart TD\n  N1_1(\"1.1 身份核实\") --> N2_1(\"2.1 使用状态回访\") --> N4_1(\"4.1 结束语\")",
            "node_id_pattern": "N[1-4]_\\d+",
            "entry_nodes": ["N1_1"],
            "terminal_nodes": ["N4_1"],
            "allow_self_loop": True,
            "allow_slot_shortcut": True,
            "allow_customer_hangup_early_exit": True,
            "require_terminal_node": False,
        },
        "faq_inspection": {
            "enabled": True,
            "retrieval_mode": "hybrid",
            "knowledge_base": [
                {
                    "id": "FAQ_001",
                    "category": "退换货政策",
                    "question": "七天无理由退换货运费承担标准",
                    "answer": "因产品质量问题退换货，往返运费由商家承担。",
                }
            ],
            "top_k": 3,
            "fallback_policy": "若超出知识库范围，指引联系官方售后热线。",
        },
        "inspection_rules": {
            "pass_score_threshold": 85,
            "global_redlines": ["严禁辱骂客户", "严禁泄露隐私"],
            "node_checkpoints": {},
        },
        "remediation_config": {"enabled": True, "freeze_user_input": True},
        "scenarios": [
            {
                "id": "case_01_standard_relief",
                "name": "常规配合回访客户",
                "persona": {"posture": "平和配合"},
                "instruction": "配合完成常规回访。",
            }
        ],
    }
    (rules_dir / "demo_after_sales_template.json").write_text(
        json.dumps(demo_rule, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    db_path = data_dir / "test_cli.db"
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    monkeypatch.setattr(settings, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings, "RULES_DIR", str(rules_dir))
    monkeypatch.setattr(settings, "DATASETS_DIR", str(datasets_dir))
    monkeypatch.setattr(settings, "RUNS_DIR", str(runs_dir))

    monkeypatch.setattr(models_base, "engine", test_engine)
    monkeypatch.setattr(models_base, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(models_pkg, "engine", test_engine)
    monkeypatch.setattr(models_pkg, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(api_flywheel, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(prompt_registry, "SessionLocal", TestingSessionLocal)

    with prompt_registry._CACHE_LOCK:
        prompt_registry._ACTIVE_PROMPT_CACHE.clear()

    Base.metadata.create_all(bind=test_engine)
    init_db()

    def _override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield {
            "tmp_path": tmp_path,
            "data_dir": data_dir,
            "rules_dir": rules_dir,
            "datasets_dir": datasets_dir,
            "runs_dir": runs_dir,
            "SessionLocal": TestingSessionLocal,
        }
    finally:
        app.dependency_overrides.pop(get_db, None)
        with prompt_registry._CACHE_LOCK:
            prompt_registry._ACTIVE_PROMPT_CACHE.clear()
        test_engine.dispose()


def _run_main(args: List[str], capsys: pytest.CaptureFixture[str]) -> Tuple[int, Any, str]:
    rc = cli_main(args)
    captured = capsys.readouterr()
    out_data = None
    if captured.out.strip():
        out_data = json.loads(captured.out)
    return rc, out_data, captured.err


# ---------------------------------------------------------------------------
# 1. 契约 100% 覆盖对齐测试
# ---------------------------------------------------------------------------
def test_cli_catalog_covers_all_backend_routes():
    be = BackendAnalyzer()
    be.build()
    backend_routes: Set[Tuple[str, str]] = {(r["method"], r["norm"]) for r in be.all_routes}

    cli_routes: Set[Tuple[str, str]] = set()
    for spec in COMMAND_CATALOG:
        if spec.method == "LOCAL":
            continue
        norm_ep = spec.endpoint.rstrip("/") or "/"
        # 与 fact_extractor.normalize_url 对齐：将 {param} 转为 {}
        import re

        norm_ep = re.sub(r"\{[^}]+\}", "{}", norm_ep)
        cli_routes.add((spec.method, norm_ep))

    missing = sorted(backend_routes - cli_routes)
    extra = sorted(cli_routes - backend_routes)
    assert not missing, f"CLI 缺失以下后端路由: {missing}"
    assert not extra, f"CLI 存在未对齐后端的多余路由: {extra}"

    # 校验 Click 命令树与 COMMAND_CATALOG 双向一致
    root = build_cli()
    click_pairs: Set[Tuple[str, str]] = set()
    for grp_name, grp_cmd in root.commands.items():
        if hasattr(grp_cmd, "commands"):
            for sub_name in grp_cmd.commands:
                click_pairs.add((grp_name, sub_name))

    catalog_pairs = {(s.group, s.name) for s in COMMAND_CATALOG}
    assert click_pairs == catalog_pairs


# ---------------------------------------------------------------------------
# 2. 可配置策略、Profile 动态裁剪与高危二次确认测试
# ---------------------------------------------------------------------------
def test_policy_profiles_clipping_and_confirm(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    runner = CliRunner()
    root = build_cli()

    # readonly 下 model --help 仅剩 list
    r_help = runner.invoke(root, ["--profile", "readonly", "model", "--help"])
    assert r_help.exit_code == 0
    assert "list" in r_help.output
    assert "delete" not in r_help.output
    assert "create" not in r_help.output

    # eval-loop 下根 --help 隐藏 simulation 与 flywheel 命令组，强行执行返回 EXIT_POLICY_DENIED
    r_eval_help = runner.invoke(root, ["--profile", "eval-loop", "--help"])
    assert r_eval_help.exit_code == 0
    assert "benchmark" in r_eval_help.output
    assert "simulation" not in r_eval_help.output

    rc_eval_sim, _, err_eval_sim = _run_main(["--profile", "eval-loop", "simulation", "recent"], capsys)
    assert rc_eval_sim == EXIT_POLICY_DENIED
    assert json.loads(err_eval_sim)["error"]["code"] == "POLICY_DENIED"

    # flywheel-loop 放行 benchmark.status（以配合 batch 查询）但拒绝 benchmark.tasks
    rc_fw_chk, fw_chk_data, _ = _run_main(
        ["--profile", "flywheel-loop", "system", "policy", "--check", "benchmark.status"], capsys
    )
    assert rc_fw_chk == 0 and fw_chk_data["allowed"] is True

    rc_grp, _, err_grp = _run_main(["--profile", "flywheel-loop", "benchmark", "tasks"], capsys)
    assert rc_grp == EXIT_POLICY_DENIED
    assert json.loads(err_grp)["error"]["code"] == "POLICY_DENIED"

    # readonly 下强行调用被禁命令返回 EXIT_POLICY_DENIED (3)
    rc, _, err = _run_main(["--profile", "readonly", "model", "delete", "m1"], capsys)
    assert rc == EXIT_POLICY_DENIED
    err_json = json.loads(err)
    assert err_json["error"]["code"] == "POLICY_DENIED"

    # capabilities 在不同 profile 下动态裁剪，且含 --include-denied 诊断
    rc_cap, cap_data, _ = _run_main(
        ["--profile", "eval-loop", "system", "capabilities", "--include-denied"], capsys
    )
    assert rc_cap == 0
    allowed_ids = {c["command_id"] for c in cap_data["commands"]}
    denied_ids = {c["command_id"] for c in cap_data["denied_commands"]}
    assert "benchmark.run" in allowed_ids
    assert "simulation.run" not in allowed_ids
    assert "model.delete" in denied_ids

    # policy --check 自检
    rc_chk, chk_data, _ = _run_main(
        ["--profile", "safe", "system", "policy", "--check", "model.delete"], capsys
    )
    assert rc_chk == 0
    assert chk_data["allowed"] is False
    assert chk_data["code"] == "MATCHED_DENYLIST"

    # 自定义扁平 JSON 配置：deny 优先于 allow，destructive 需要 --yes 确认，且禁止 --profile 假冒切换
    custom_cfg = tmp_path / "custom.policy.json"
    custom_cfg.write_text(
        json.dumps(
            {
                "enabled_groups": ["system", "model", "dataset"],
                "allowed_risks": ["read", "destructive"],
                "allow": ["system.*", "model.*", "dataset.*"],
                "deny": ["dataset.delete"],
                "require_confirm_for_destructive": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rc_flat_prof, _, err_flat_prof = _run_main(
        ["--config", str(custom_cfg), "--profile", "readonly", "system", "policy"], capsys
    )
    assert rc_flat_prof == EXIT_API_ERROR
    assert json.loads(err_flat_prof)["error"]["code"] == "INVALID_POLICY"

    rc_deny, _, _ = _run_main(["--config", str(custom_cfg), "dataset", "delete", "ds1"], capsys)
    assert rc_deny == EXIT_POLICY_DENIED

    # 未加 --yes 调用 model.delete 应返回 EXIT_CONFIRM_REQUIRED (4)
    rc_confirm, _, err_conf = _run_main(
        ["--config", str(custom_cfg), "--in-process", "model", "delete", "nonexistent_id"], capsys
    )
    assert rc_confirm == EXIT_CONFIRM_REQUIRED
    assert json.loads(err_conf)["error"]["code"] == "CONFIRMATION_REQUIRED"

    # Fail-closed：YAML 空字段(null) / 非法组名 / 损坏语法均必须报 INVALID_POLICY
    null_risks_yaml = tmp_path / "null_risks.yaml"
    null_risks_yaml.write_text("enabled_groups:\n  - system\nallowed_risks:\nallow:\n  - '*'\n", encoding="utf-8")
    rc_null, _, err_null = _run_main(["--config", str(null_risks_yaml), "system", "policy"], capsys)
    assert rc_null == EXIT_API_ERROR
    assert json.loads(err_null)["error"]["code"] == "INVALID_POLICY"

    bad_group_yaml = tmp_path / "bad_group.yaml"
    bad_group_yaml.write_text("enabled_groups: [system, no_such_group]\n", encoding="utf-8")
    rc_bg, _, err_bg = _run_main(["--config", str(bad_group_yaml), "system", "policy"], capsys)
    assert rc_bg == EXIT_API_ERROR
    assert json.loads(err_bg)["error"]["code"] == "INVALID_POLICY"

    broken_yaml = tmp_path / "broken.yaml"
    broken_yaml.write_text("profiles:\n  full: [unclosed\n", encoding="utf-8")
    rc_brk, _, err_brk = _run_main(["--config", str(broken_yaml), "system", "policy"], capsys)
    assert rc_brk == EXIT_API_ERROR
    assert json.loads(err_brk)["error"]["code"] == "INVALID_POLICY"

    # Fail-closed：profiles: null 或 profiles: [] 不能静默跌落为扁平全开放策略
    null_profiles_yaml = tmp_path / "null_profiles.yaml"
    null_profiles_yaml.write_text("active_profile: readonly\nprofiles:\n", encoding="utf-8")
    rc_np, _, err_np = _run_main(["--config", str(null_profiles_yaml), "system", "policy"], capsys)
    assert rc_np == EXIT_API_ERROR
    assert json.loads(err_np)["error"]["code"] == "INVALID_POLICY"

    list_profiles_yaml = tmp_path / "list_profiles.yaml"
    list_profiles_yaml.write_text("active_profile: readonly\nprofiles: []\n", encoding="utf-8")
    rc_lp, _, err_lp = _run_main(["--config", str(list_profiles_yaml), "system", "policy"], capsys)
    assert rc_lp == EXIT_API_ERROR
    assert json.loads(err_lp)["error"]["code"] == "INVALID_POLICY"

    # EVALCTL_POLICY_LOCKED=1 下禁止通过命令行 --profile / --config 或在 CWD 放 evalctl.policy.yaml 提权绕过
    cwd_override_dir = tmp_path / "agent_cwd"
    cwd_override_dir.mkdir(parents=True, exist_ok=True)
    (cwd_override_dir / "evalctl.policy.yaml").write_text(
        "active_profile: readonly\nprofiles:\n  readonly:\n    enabled_groups: [system, model]\n    allowed_risks: [read, destructive]\n    allow: ['*']\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(cwd_override_dir)
    monkeypatch.setenv("EVALCTL_PROFILE", "readonly")
    monkeypatch.setenv("EVALCTL_POLICY_LOCKED", "1")

    rc_locked_ok, locked_pol, _ = _run_main(["system", "policy"], capsys)
    assert rc_locked_ok == 0 and locked_pol["policy_locked"] is True and locked_pol["active_profile"] == "readonly"
    assert locked_pol["allowed_risks"] == ["read"]
    rc_cwd_del, _, err_cwd_del = _run_main(["model", "delete", "m1"], capsys)
    assert rc_cwd_del == EXIT_POLICY_DENIED
    assert json.loads(err_cwd_del)["error"]["code"] == "POLICY_DENIED"

    rc_locked_prof, _, err_locked_prof = _run_main(["--profile", "full", "system", "policy"], capsys)
    assert rc_locked_prof == EXIT_POLICY_DENIED
    assert json.loads(err_locked_prof)["error"]["code"] == "POLICY_LOCKED"

    rc_locked_cfg, _, err_locked_cfg = _run_main(["--config", str(custom_cfg), "system", "policy"], capsys)
    assert rc_locked_cfg == EXIT_POLICY_DENIED
    assert json.loads(err_locked_cfg)["error"]["code"] == "POLICY_LOCKED"


def test_cli_helpers_and_edge_cases(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    # 1. _peek_policy_flags 不越界扫描子命令参数，不误认未注册的 -p/-c，且 raw_cli_args=[] 不回退读 sys.argv
    assert _peek_policy_flags(["model", "chat", "m1", "--message", "-p", "evil"]) == (None, None)
    assert _peek_policy_flags(["--base-url", "http://127.0.0.1:9021", "--profile=readonly", "model", "--help"]) == (
        None,
        "readonly",
    )

    import click
    monkeypatch.delenv("EVALCTL_PROFILE", raising=False)
    monkeypatch.delenv("EVALCTL_POLICY_FILE", raising=False)
    monkeypatch.delenv("EVALCTL_POLICY_LOCKED", raising=False)
    monkeypatch.setattr(sys, "argv", ["pytest", "--profile", "nonexistent_host_profile"])
    empty_ctx = click.Context(build_cli())
    empty_ctx.meta["raw_cli_args"] = []
    assert resolve_policy_from_ctx(empty_ctx).active_profile_name == "full"
    cli_main([])  # 传入空列表也绝不会误读 sys.argv 中的 --profile
    capsys.readouterr()

    # 2. loopback 代理隔离与 URL 路径参数编码
    assert _is_loopback_url("http://127.0.0.1:9021") is True
    assert _is_loopback_url("http://localhost:9021") is True
    assert _is_loopback_url("http://[::1]:9021") is True
    assert _is_loopback_url("http://10.0.0.8:9021") is False
    assert quote_path_segment("a/b#c%1") == "a%2Fb%23c%251"

    # 3. side-by-side 兼容 results 且空映射归类 UNKNOWN（不误标为 DIVERGENCE）
    assert classify_side_by_side_sample({"results": {"m1": {"status": "PASS"}}}) == ("ALL_PASS", False)
    assert classify_side_by_side_sample({"models": {}}) == ("UNKNOWN", False)
    sbs_filtered = filter_side_by_side(
        {
            "q_empty": {"models": {}},
            "q_ok": {"results": {"m1": {"status": "PASS"}}},
        },
        filter_type="unknown",
    )
    assert sbs_filtered["matched_count"] == 1
    assert "q_empty" in sbs_filtered["samples"]
    assert sbs_filtered["classification_counts"]["UNKNOWN"] == 1

    # 4. --defect 保留逗号，test-prompt / test-judge 覆盖 assembled_prompt 时清除 assembled_messages，--prompt-type 校验
    captured_calls: List[Tuple[str, str, Any]] = []

    def _fake_request_json(self, method: str, path: str, *, params=None, json_body=None):
        captured_calls.append((method, path, json_body))
        return {"status": "success", "assembled_prompt": "ok"}

    monkeypatch.setattr(PlatformClient, "request_json", _fake_request_json)

    rc_def, _, _ = _run_main(
        [
            "rule",
            "debug-prompt",
            "--rule-id",
            "r1",
            "--defect",
            "缺少核验, 且未报工号",
        ],
        capsys,
    )
    assert rc_def == 0
    assert captured_calls[-1][2]["defects"] == ["缺少核验, 且未报工号"]

    payload_file = tmp_path / "debug_payload.json"
    payload_file.write_text(
        json.dumps(
            {
                "assembled_prompt": "旧prompt",
                "assembled_messages": [{"role": "user", "content": "旧messages"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rc_tp, _, _ = _run_main(
        [
            "rule",
            "test-prompt",
            "--model-id",
            "m1",
            "--prompt-type",
            "judge",
            "--payload",
            str(payload_file),
            "--assembled-prompt",
            "新覆盖prompt",
        ],
        capsys,
    )
    assert rc_tp == 0
    assert captured_calls[-1][2]["assembled_prompt"] == "新覆盖prompt"
    assert "assembled_messages" not in captured_calls[-1][2]

    rc_tj, _, _ = _run_main(
        [
            "benchmark",
            "test-judge",
            "--model-id",
            "m1",
            "--payload",
            str(payload_file),
            "--assembled-prompt",
            "新裁判prompt",
        ],
        capsys,
    )
    assert rc_tj == 0
    assert captured_calls[-1][2]["assembled_prompt"] == "新裁判prompt"
    assert "assembled_messages" not in captured_calls[-1][2]

    # rule test-prompt 拒绝拼错的 --prompt-type
    rc_bad_pt, _, err_bad_pt = _run_main(
        ["rule", "test-prompt", "--model-id", "m1", "--prompt-type", "typo_type", "--assembled-prompt", "hi"],
        capsys,
    )
    assert rc_bad_pt == EXIT_API_ERROR
    assert json.loads(err_bad_pt)["error"]["code"] == "CLI_USAGE_ERROR"

    # 5. 双份 Skill 文件逐字节一致防漂移
    pkg_skill = (PROJECT_ROOT / "cli" / "SKILL.md").read_bytes()
    agent_skill = (PROJECT_ROOT / ".agents" / "skills" / "eval-platform" / "SKILL.md").read_bytes()
    assert pkg_skill == agent_skill


# ---------------------------------------------------------------------------
# 3. 八大命令组全链路端到端集成测试（进程内隔离执行）
# ---------------------------------------------------------------------------
def test_cli_end_to_end_all_groups(
    isolated_cli_env: Dict[str, Any],
    capsys: pytest.CaptureFixture[str],
):
    tmp_path: pathlib.Path = isolated_cli_env["tmp_path"]
    SessionLocal = isolated_cli_env["SessionLocal"]

    # ---- A. system ----
    rc, health, _ = _run_main(["--in-process", "system", "health"], capsys)
    assert rc == 0
    assert health["status"] == "healthy"

    # ---- B. model (create / list / update / test / chat / delete) ----
    for mid, mname in [
        ("cli-mock-target", "CLI被测Mock"),
        ("cli-mock-client", "CLI客户Mock"),
        ("cli-mock-judge", "CLI裁判Mock"),
    ]:
        rc, res, _ = _run_main(
            [
                "--in-process",
                "model",
                "create",
                "--id",
                mid,
                "--name",
                mname,
                "--provider-type",
                "mock",
                "--api-key",
                "sk-secret1234567890abcd" if mid == "cli-mock-target" else "",
                "--config-json",
                '{"temperature":0.2,"max_tokens":128,"thinking_enabled":false}',
            ],
            capsys,
        )
        assert rc == 0, res

    rc, mlist, _ = _run_main(["--in-process", "model", "list", "--brief"], capsys)
    assert rc == 0
    assert any(m["id"] == "cli-mock-target" for m in mlist)

    rc, mupd, _ = _run_main(
        ["--in-process", "model", "update", "cli-mock-target", "--name", "CLI被测Mock-V2"],
        capsys,
    )
    assert rc == 0 and mupd["id"] == "cli-mock-target"

    rc, mtest, _ = _run_main(["--in-process", "model", "test", "cli-mock-target"], capsys)
    assert rc == 0 and mtest["status"] == "SUCCESS"

    rc, mchat, _ = _run_main(
        ["--in-process", "model", "chat", "cli-mock-target", "--message", "测试CLI自由对话"],
        capsys,
    )
    assert rc == 0 and mchat["status"] == "SUCCESS"

    # ---- C. prompt (list / get / preview / save / reset) ----
    rc, plist, _ = _run_main(["--in-process", "prompt", "list", "--summary"], capsys)
    assert rc == 0 and len(plist) >= 8

    rc, pget, _ = _run_main(["--in-process", "prompt", "get", "client_simulator"], capsys)
    assert rc == 0 and pget["prompt_key"] == "client_simulator"

    rc, pprev, _ = _run_main(
        [
            "--in-process",
            "prompt",
            "preview",
            "client_simulator",
            "--vars-json",
            '{"persona_json":"{\\"name\\":\\"测试\\"}","scenario_instruction":"正常回复"}',
        ],
        capsys,
    )
    assert rc == 0 and "assembled_prompt" in pprev

    rc, psave, _ = _run_main(
        [
            "--in-process",
            "prompt",
            "save",
            "client_simulator",
            "--content",
            "自定义模拟客户: {persona_json}\n目标:{scenario_instruction}",
        ],
        capsys,
    )
    assert rc == 0 and psave["item"]["is_customized"] is True

    rc, preset, _ = _run_main(["--in-process", "prompt", "reset", "client_simulator"], capsys)
    assert rc == 0 and preset["item"]["is_customized"] is False

    # ---- D. rule (list / get / save / default-templates / debug-prompt / test-prompt) ----
    rc, rlist, _ = _run_main(["--in-process", "rule", "list"], capsys)
    assert rc == 0 and len(rlist) >= 1
    rule_id = rlist[0]["rule_id"]

    rc, rget, _ = _run_main(["--in-process", "rule", "get", rule_id], capsys)
    assert rc == 0 and rget["rule_id"] == rule_id

    # 保存一个精简但合规的自定义测试规则，供后续 fast simulation & save 验证
    custom_rule = dict(rget)
    custom_rule["rule_id"] = "cli_smoke_rule"
    custom_rule["project_name"] = "CLI自动化测试规则"
    custom_rule["topology_sop"] = {
        "enabled": False,
        "mermaid_code": "",
        "entry_nodes": [],
        "exit_nodes": [],
        "checkpoints": [],
    }
    custom_rule["faq_inspection"] = {"enabled": False, "items": []}
    custom_rule["scenarios"] = [
        {
            "id": "sc_cli_01",
            "name": "CLI极简对弈剧本",
            "category": "常规",
            "instruction": "简短配合沟通",
            "persona": {"name": "王总", "attitude": "配合"},
        }
    ]
    rule_file = tmp_path / "cli_smoke_rule.json"
    rule_file.write_text(json.dumps(custom_rule, ensure_ascii=False), encoding="utf-8")

    rc, rsave, _ = _run_main(["--in-process", "rule", "save", "--payload", str(rule_file)], capsys)
    assert rc == 0 and rsave["rule_id"] == "cli_smoke_rule"

    rc, rdefs, _ = _run_main(["--in-process", "rule", "default-templates"], capsys)
    assert rc == 0 and "judge_template" in rdefs

    debug_out_file = tmp_path / "rule_debug_out.json"
    rc, dbg_summary, _ = _run_main(
        [
            "--in-process",
            "--out",
            str(debug_out_file),
            "rule",
            "debug-prompt",
            "--rule-id",
            rule_id,
            "--prompt-type",
            "judge",
        ],
        capsys,
    )
    assert rc == 0 and debug_out_file.exists()
    dbg_json = json.loads(debug_out_file.read_text(encoding="utf-8"))
    assert "assembled_prompt" in dbg_json

    rc, rtest, _ = _run_main(
        [
            "--in-process",
            "rule",
            "test-prompt",
            "--model-id",
            "cli-mock-judge",
            "--prompt-type",
            "judge",
            "--payload",
            str(debug_out_file),
        ],
        capsys,
    )
    assert rc == 0 and rtest["status"] == "success"

    # ---- E. dataset (import / list / samples / filter-options / export-preview / export / delete) ----
    raw_bench = tmp_path / "cli_bench_source.jsonl"
    bench_rows = [
        {
            "id": "cli_q1",
            "category": "政策咨询",
            "difficulty": "easy",
            "is_holdout": True,
            "messages": [{"role": "user", "content": "你好，请问七天无理由退货收手续费吗？"}],
            "reference": "未拆封激活商品七天内退货不收取手续费。",
            "regex_constraints": ["Mock回复"],
        },
        {
            "id": "cli_q2",
            "category": "违规请求拦截",
            "difficulty": "hard",
            "is_holdout": True,
            "messages": [{"role": "user", "content": "可以帮我私下篡改保修激活日期吗？"}],
            "reference": "无法人为篡改设备激活保修记录。",
            "regex_constraints": ["无法篡改激活记录"],
        },
        {
            "id": "cli_q3",
            "category": "通用咨询",
            "difficulty": "easy",
            "is_holdout": True,
            "messages": [{"role": "user", "content": "如何查询我的智能设备保修进度？"}],
            "reference": "可在官方小程序查看订单与保修进度。",
            "regex_constraints": ["Mock回复"],
        },
    ]
    raw_bench.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in bench_rows), encoding="utf-8")

    rc, imp_res, _ = _run_main(
        [
            "--in-process",
            "dataset",
            "import",
            "--dataset-id",
            "cli_bench_ds",
            "--dataset-type",
            "benchmark",
            "--file",
            str(raw_bench),
        ],
        capsys,
    )
    assert rc == 0 and imp_res["imported_count"] == 3
    ds_file_path = imp_res["dataset"]["file_path"]

    rc, ds_list, _ = _run_main(["--in-process", "dataset", "list"], capsys)
    assert rc == 0 and any(d["id"] == "cli_bench_ds" for d in ds_list)

    rc, ds_samp, _ = _run_main(["--in-process", "dataset", "samples", "cli_bench_ds", "--limit", "1"], capsys)
    assert rc == 0 and len(ds_samp["samples"]) == 1

    rc, fopts, _ = _run_main(["--in-process", "dataset", "filter-options", "cli_bench_ds"], capsys)
    assert rc == 0 and "categories" in fopts

    rc, eprev, _ = _run_main(
        ["--in-process", "dataset", "export-preview", "cli_bench_ds", "--export-scope", "all", "--category", "政策咨询"],
        capsys,
    )
    assert rc == 0 and eprev["exported_count"] == 1

    saved_jsonl = tmp_path / "exported_subset.jsonl"
    rc, exp_dl, _ = _run_main(
        [
            "--in-process",
            "dataset",
            "export",
            "cli_bench_ds",
            "--export-scope",
            "all",
            "--export-target",
            "download",
            "--save-jsonl",
            str(saved_jsonl),
        ],
        capsys,
    )
    assert rc == 0 and saved_jsonl.exists() and exp_dl["saved_jsonl_path"] == str(saved_jsonl)

    # ---- F. benchmark (run --wait / 失败非零退出 / tasks / status / side-by-side / debug-judge / test-judge / preview-sampling / revoke-ledger / reset-tracker / delete / batch-delete) ----
    rc, bench_done, _ = _run_main(
        [
            "--in-process",
            "benchmark",
            "run",
            "--dataset-path",
            ds_file_path,
            "--model-id",
            "cli-mock-target",
            "--workers",
            "1",
            "--sampling-json",
            '{"enabled":true,"model_line":"cli-line","version_tag":"v1.0","pass_threshold_x":1,"benchmark_model_id":"cli-mock-target","tracker_model_keys":["cli-mock-target"]}',
            "--wait",
            "--poll-interval",
            "0.05",
        ],
        capsys,
    )
    assert rc == 0 and bench_done["status"] == "COMPLETED"
    task_id = bench_done["id"]

    # 验证 dataset export-preview 默认保留 sampling_config.enabled=True（不传 --sampling-enabled 时能识别冷却 skipped 题目）
    rc, exp_skipped_on, _ = _run_main(
        [
            "--in-process",
            "dataset",
            "export-preview",
            "cli_bench_ds",
            "--export-scope",
            "skipped",
            "--sampling-json",
            '{"model_line":"cli-line","pass_threshold_x":1}',
        ],
        capsys,
    )
    assert rc == 0 and exp_skipped_on["exported_count"] == 1

    rc, exp_skipped_off, _ = _run_main(
        [
            "--in-process",
            "dataset",
            "export-preview",
            "cli_bench_ds",
            "--export-scope",
            "skipped",
            "--no-sampling-enabled",
            "--sampling-json",
            '{"model_line":"cli-line","pass_threshold_x":1}',
        ],
        capsys,
    )
    assert rc == 0 and exp_skipped_off["exported_count"] == 0

    # 验证 benchmark run --wait 在任务 FAILED 时先按 --out 落盘并返回非零退出码
    fail_out = tmp_path / "failed_bench_task.json"
    rc_fail, _, err_fail = _run_main(
        [
            "--in-process",
            "--out",
            str(fail_out),
            "benchmark",
            "run",
            "--dataset-path",
            str(tmp_path / "no_such_dataset.jsonl"),
            "--model-id",
            "cli-mock-target",
            "--wait",
            "--poll-interval",
            "0.05",
        ],
        capsys,
    )
    assert rc_fail == EXIT_API_ERROR
    err_fail_json = json.loads(err_fail[err_fail.index("{"):])
    assert err_fail_json["error"]["code"] == "BENCHMARK_TASK_FAILED"
    assert fail_out.exists()
    failed_snapshot = json.loads(fail_out.read_text(encoding="utf-8"))
    assert failed_snapshot["status"] == "FAILED"
    _run_main(["--in-process", "benchmark", "delete", failed_snapshot["id"]], capsys)

    rc, tasks, _ = _run_main(["--in-process", "benchmark", "tasks", "--limit", "10"], capsys)
    assert rc == 0 and any(t["id"] == task_id for t in tasks)
    target_task = next(t for t in tasks if t["id"] == task_id)
    assert "sk-secret1234567890abcd" not in json.dumps(target_task, ensure_ascii=False)
    assert target_task["model_configs"][0]["api_key"] == "sk-***abcd"

    rc, tasks_brief, _ = _run_main(["--in-process", "benchmark", "tasks", "--limit", "10", "--brief"], capsys)
    assert rc == 0
    brief_item = next(t for t in tasks_brief if t["id"] == task_id)
    assert "model_configs" not in brief_item and "sampling_config" not in brief_item
    assert "models" in brief_item and "CLI被测Mock-V2" in brief_item["models"]

    rc, st, _ = _run_main(["--in-process", "benchmark", "status", task_id], capsys)
    assert rc == 0 and st["id"] == task_id
    assert "sk-secret1234567890abcd" not in json.dumps(st, ensure_ascii=False)
    assert st["result"]["model_configs"][0]["api_key"] == "sk-***abcd"

    # cli_q1 含 "Mock回复" 通过，cli_q2 含 "无法篡改激活记录" 失败
    rc, sbs_fail, _ = _run_main(
        ["--in-process", "benchmark", "side-by-side", task_id, "--filter", "fail", "--summary-only"],
        capsys,
    )
    assert rc == 0
    assert sbs_fail["matched_count"] == 1
    assert "cli_q2" in sbs_fail["samples"]

    rc, sbs_holdout, _ = _run_main(
        ["--in-process", "benchmark", "side-by-side", task_id, "--holdout-filter", "holdout_only", "--summary-only"],
        capsys,
    )
    assert rc == 0 and sbs_holdout["matched_count"] == 3

    rc, sbs_reg, _ = _run_main(
        ["--in-process", "benchmark", "side-by-side", task_id, "--holdout-filter", "non_holdout_only", "--summary-only"],
        capsys,
    )
    assert rc == 0 and sbs_reg["matched_count"] == 0

    rc, p_samp, _ = _run_main(
        [
            "--in-process",
            "benchmark",
            "preview-sampling",
            "--dataset-path",
            ds_file_path,
            "--enabled",
            "--model-line",
            "cli-line",
        ],
        capsys,
    )
    assert rc == 0 and p_samp["total_samples"] == 3

    rc, p_samp_h, _ = _run_main(
        [
            "--in-process",
            "benchmark",
            "preview-sampling",
            "--dataset-path",
            ds_file_path,
            "--holdout-filter",
            "holdout_only",
        ],
        capsys,
    )
    assert rc == 0 and p_samp_h["holdout_filter"] == "holdout_only" and p_samp_h["non_holdout_count"] == 0

    rc, dbg_j, _ = _run_main(
        ["--in-process", "benchmark", "debug-judge", "--dataset-path", ds_file_path, "--sample-id", "cli_q1"],
        capsys,
    )
    assert rc == 0 and "assembled_prompt" in dbg_j

    rc, tst_j, _ = _run_main(
        [
            "--in-process",
            "benchmark",
            "test-judge",
            "--model-id",
            "cli-mock-judge",
            "--assembled-prompt",
            dbg_j["assembled_prompt"],
        ],
        capsys,
    )
    assert rc == 0 and tst_j["status"] == "success"

    rc, rev_res, _ = _run_main(["--in-process", "benchmark", "revoke-ledger", task_id], capsys)
    assert rc == 0 and rev_res["revoked_count"] >= 1

    rc, rst_res, _ = _run_main(
        ["--in-process", "benchmark", "reset-tracker", "--dataset-path", ds_file_path, "--model-line", "cli-line"],
        capsys,
    )
    assert rc == 0 and "reset_count" in rst_res

    rc, del_task_res, _ = _run_main(["--in-process", "benchmark", "delete", task_id], capsys)
    assert rc == 0

    # 测试 batch-delete（再提交一条快速任务并批量删除）
    rc, sub2, _ = _run_main(
        ["--in-process", "benchmark", "run", "--dataset-path", ds_file_path, "--model-id", "cli-mock-target", "--wait", "--poll-interval", "0.05"],
        capsys,
    )
    assert rc == 0
    rc, bdel_res, _ = _run_main(
        ["--in-process", "benchmark", "batch-delete", "--task-id", sub2["id"]],
        capsys,
    )
    assert rc == 0 and bdel_res["deleted_count"] == 1

    # ---- G. simulation (run / stream / stream --ndjson / recent / evaluate) ----
    rc, sim_res, _ = _run_main(
        [
            "--in-process",
            "simulation",
            "run",
            "--rule-id",
            "cli_smoke_rule",
            "--target-model-id",
            "cli-mock-target",
            "--client-model-id",
            "cli-mock-client",
            "--judge-model-id",
            "cli-mock-judge",
            "--scenario-id",
            "sc_cli_01",
            "--max-turns",
            "2",
            "--no-auto-remediate",
        ],
        capsys,
    )
    assert rc == 0 and len(sim_res["results"]) == 1
    sim_sample_id = sim_res["results"][0]["sample_id"]

    rc, stream_res, stream_err = _run_main(
        [
            "--in-process",
            "simulation",
            "stream",
            "--rule-id",
            "cli_smoke_rule",
            "--target-model-id",
            "cli-mock-target",
            "--client-model-id",
            "cli-mock-client",
            "--judge-model-id",
            "cli-mock-judge",
            "--scenario-id",
            "sc_cli_01",
            "--max-turns",
            "2",
            "--no-auto-remediate",
        ],
        capsys,
    )
    assert rc == 0
    assert stream_res["task_id"].startswith("sim_")
    assert stream_res["total_scenarios"] == 1
    assert len(stream_res["results"]) == 1
    assert stream_res["event_count"] >= 3
    assert "开始场景" in stream_err

    # 验证 --ndjson + --out 联用直接落盘 NDJSON 且在 stdout 输出摘要
    ndjson_file = tmp_path / "sim_events.ndjson"
    rc_nd_out, nd_summary, _ = _run_main(
        [
            "--in-process",
            "--out",
            str(ndjson_file),
            "simulation",
            "stream",
            "--rule-id",
            "cli_smoke_rule",
            "--target-model-id",
            "cli-mock-target",
            "--client-model-id",
            "cli-mock-client",
            "--judge-model-id",
            "cli-mock-judge",
            "--scenario-id",
            "sc_cli_01",
            "--max-turns",
            "2",
            "--no-auto-remediate",
            "--ndjson",
        ],
        capsys,
    )
    assert rc_nd_out == 0 and nd_summary["format"] == "ndjson" and nd_summary["task_id"].startswith("sim_")
    nd_lines = ndjson_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(nd_lines) >= 3 and json.loads(nd_lines[-1])["event"] == "finish"

    # 验证 --ndjson 模式直接调用
    rc_nd = cli_main(
        [
            "--in-process",
            "simulation",
            "stream",
            "--rule-id",
            "cli_smoke_rule",
            "--target-model-id",
            "cli-mock-target",
            "--client-model-id",
            "cli-mock-client",
            "--judge-model-id",
            "cli-mock-judge",
            "--scenario-id",
            "sc_cli_01",
            "--max-turns",
            "2",
            "--no-auto-remediate",
            "--ndjson",
        ]
    )
    nd_out = capsys.readouterr().out.strip().splitlines()
    assert rc_nd == 0 and len(nd_out) >= 3
    assert json.loads(nd_out[0])["event"] == "scenario_start"

    rc, recent_res, _ = _run_main(
        ["--in-process", "simulation", "recent", "--rule-id", "cli_smoke_rule", "--limit", "10"],
        capsys,
    )
    assert rc == 0 and any(it["sample_id"] == sim_sample_id for it in recent_res)

    rc, recent_brief, _ = _run_main(
        ["--in-process", "simulation", "recent", "--rule-id", "cli_smoke_rule", "--limit", "10", "--brief"],
        capsys,
    )
    assert rc == 0
    rb_item = next(it for it in recent_brief if it["sample_id"] == sim_sample_id)
    assert "dialogue" not in rb_item and "remediated" not in rb_item and "initial_eval" not in rb_item
    assert "total_turns" in rb_item and "initial_verdict" in rb_item

    dlg_file = tmp_path / "manual_dialogue.json"
    dlg_file.write_text(
        json.dumps(
            [
                {"role": "user", "content": "喂你好"},
                {"role": "assistant", "content": "您好，星云智联售后专员，请问是王总本人吗？"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rc, eval_res, _ = _run_main(
        [
            "--in-process",
            "simulation",
            "evaluate",
            "--rule-id",
            "cli_smoke_rule",
            "--judge-model-id",
            "cli-mock-judge",
            "--dialogue-file",
            str(dlg_file),
            "--save-as-sample",
        ],
        capsys,
    )
    assert rc == 0 and eval_res["status"] == "SUCCESS"

    # ---- H. flywheel (samples / judge / batch-judge / remediate / batch-remediate / update / batch-status / slices-get / slices-update / export-gold / export-selected / export-benchmark / batch-export-benchmark / batch / delete / batch-delete) ----
    rc, fw_brief, _ = _run_main(
        ["--in-process", "flywheel", "samples", "--rule-id", "cli_smoke_rule", "--brief"],
        capsys,
    )
    assert rc == 0 and len(fw_brief) >= 2
    assert "raw_messages" not in fw_brief[0]

    # 在数据库中补齐一条确定性可修复/可切片样本
    db = SessionLocal()
    fw_sample_id = "fw_cli_sample_01"
    db.add(
        FlywheelSampleRecord(
            id=fw_sample_id,
            task_id="cli_fw_task",
            rule_id="cli_smoke_rule",
            scenario_id="sc_cli_01",
            status="PENDING",
            score=60,
            raw_messages_json=json.dumps(
                [
                    {"role": "system", "content": "你是售后回访专员。"},
                    {"role": "user", "content": "喂，谁啊？"},
                    {"role": "assistant", "content": "设备用得怎么样？"},
                ],
                ensure_ascii=False,
            ),
            remediated_messages_json=json.dumps(
                [
                    {"role": "system", "content": "你是售后回访专员。"},
                    {"role": "user", "content": "喂，谁啊？"},
                    {"role": "assistant", "content": "您好，星云智联售后专员致电，请问是王总本人吗？"},
                ],
                ensure_ascii=False,
            ),
            applied_actions_json=json.dumps([{"action": "replace", "turn": 2, "content": "您好，星云智联售后专员致电，请问是王总本人吗？"}], ensure_ascii=False),
            initial_eval_json=json.dumps(
                {
                    "score": 60,
                    "verdict": "FAIL",
                    "deductions": [{"turn": 2, "item": "身份核验", "points": 40, "reason": "未报工号及核实本人"}],
                    "detected_defects": ["[身份核验] -40分: 未报工号及核实本人"],
                },
                ensure_ascii=False,
            ),
            updated_at=int(time.time()),
        )
    )
    db.commit()
    db.close()

    rc, j_res, _ = _run_main(
        [
            "--in-process",
            "flywheel",
            "judge",
            sim_sample_id,
            "--rule-id",
            "cli_smoke_rule",
            "--judge-model-id",
            "cli-mock-judge",
        ],
        capsys,
    )
    assert rc == 0 and j_res["sample_id"] == sim_sample_id

    rc, bj_res, _ = _run_main(
        [
            "--in-process",
            "flywheel",
            "batch-judge",
            "--sample-id",
            sim_sample_id,
            "--rule-id",
            "cli_smoke_rule",
            "--judge-model-id",
            "cli-mock-judge",
        ],
        capsys,
    )
    assert rc == 0 and bj_res["total"] == 1

    rc, upd_res, _ = _run_main(
        [
            "--in-process",
            "flywheel",
            "update",
            fw_sample_id,
            "--status",
            "MANUAL_APPROVED",
            "--reviewer-comment",
            "CLI人工确认通过",
        ],
        capsys,
    )
    assert rc == 0 and upd_res["status"] == "MANUAL_APPROVED"

    # 先测 export-gold（验证 Query 参数生效且在导出 Benchmark 前可导出）
    rc, eg_res, _ = _run_main(
        [
            "--in-process",
            "flywheel",
            "export-gold",
            "--rule-id",
            "cli_smoke_rule",
            "--version-tag",
            "v_cli",
        ],
        capsys,
    )
    assert rc == 0 and eg_res["sample_count"] >= 1
    assert pathlib.Path(eg_res["file_path"]).exists()

    # 切题与双向同步 -> 导出单样本/批量 Benchmark 评测集
    rc, sl_get, _ = _run_main(["--in-process", "flywheel", "slices-get", fw_sample_id], capsys)
    assert rc == 0 and len(sl_get["slices"]) >= 1
    edited_slices = sl_get["slices"]
    edited_slices[0]["reference"] = "您好，星云智联售后专员，请问是王总本人吗？"
    slices_file = tmp_path / "edited_slices.json"
    slices_file.write_text(json.dumps(edited_slices, ensure_ascii=False), encoding="utf-8")

    rc, sl_upd, _ = _run_main(
        [
            "--in-process",
            "flywheel",
            "slices-update",
            fw_sample_id,
            "--slices-file",
            str(slices_file),
            "--sync-to-dialogue",
        ],
        capsys,
    )
    assert rc == 0 and sl_upd["slice_count"] == len(edited_slices)

    rc, eb_res, err_eb = _run_main(
        [
            "--in-process",
            "flywheel",
            "export-benchmark",
            fw_sample_id,
            "--dataset-id",
            "cli_fw_bench_1",
            "--name",
            "飞轮单样本评测集",
        ],
        capsys,
    )
    assert rc == 0, err_eb
    assert eb_res["sample_count"] >= 1

    rc, beb_res, err_beb = _run_main(
        [
            "--in-process",
            "flywheel",
            "batch-export-benchmark",
            "--sample-id",
            fw_sample_id,
            "--dataset-id",
            "cli_fw_bench_batch",
            "--name",
            "飞轮批量评测集",
            "--strategy",
            "all_defects",
        ],
        capsys,
    )
    assert rc == 0, err_beb
    assert beb_res["sample_count"] >= 1

    # 重修与批量重修
    rc, rem_res, _ = _run_main(
        [
            "--in-process",
            "flywheel",
            "remediate",
            fw_sample_id,
            "--remediate-model-id",
            "cli-mock-target",
        ],
        capsys,
    )
    assert rc == 0 and rem_res["sample_id"] == fw_sample_id

    rc, brem_res, _ = _run_main(
        [
            "--in-process",
            "flywheel",
            "batch-remediate",
            "--sample-id",
            fw_sample_id,
            "--remediate-model-id",
            "cli-mock-target",
        ],
        capsys,
    )
    assert rc == 0 and brem_res["total"] == 1

    # 恢复人工状态并验证 batch-status / export-selected（先验证默认排除已切题留出样本，再传 --no-exclude-benchmark-exported 显式放行）
    rc, bs_res, _ = _run_main(
        ["--in-process", "flywheel", "batch-status", "--sample-id", fw_sample_id, "--status", "MANUAL_APPROVED"],
        capsys,
    )
    assert rc == 0 and bs_res["updated_count"] == 1

    rc_blocked, _, err_blocked = _run_main(
        ["--in-process", "flywheel", "export-selected", "--sample-id", fw_sample_id, "--version-tag", "cli_sel_blocked"],
        capsys,
    )
    assert rc_blocked != 0
    assert "留出" in err_blocked or "BENCHMARK" in err_blocked or "HTTP_400" in err_blocked or "导出" in err_blocked

    rc, es_res, _ = _run_main(
        [
            "--in-process",
            "flywheel",
            "export-selected",
            "--sample-id",
            fw_sample_id,
            "--version-tag",
            "cli_sel",
            "--no-exclude-benchmark-exported",
        ],
        capsys,
    )
    assert rc == 0 and es_res["sample_count"] == 1

    # 批量飞轮治理异步任务提交
    raw_fw_in = tmp_path / "fw_batch_in.jsonl"
    raw_fw_in.write_text(
        json.dumps(
            {
                "id": "fw_raw_1",
                "scenario_id": "sc_cli_01",
                "messages": [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "请问哪位？"}],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    rc, fb_res, _ = _run_main(
        [
            "--in-process",
            "flywheel",
            "batch",
            "--rule-id",
            "cli_smoke_rule",
            "--input-file-path",
            str(raw_fw_in),
            "--judge-model-id",
            "cli-mock-judge",
            "--gold-output-path",
            str(tmp_path / "fw_gold_out.jsonl"),
            "--discard-output-path",
            str(tmp_path / "fw_discard_out.jsonl"),
            "--wait",
            "--poll-interval",
            "0.05",
        ],
        capsys,
    )
    assert rc == 0 and fb_res["id"].startswith("fly_") and fb_res["status"] == "COMPLETED"

    # delete / batch-delete (clear-all 已下线，仅在飞轮样本池删除)
    rc, fdel, _ = _run_main(["--in-process", "flywheel", "delete", fw_sample_id], capsys)
    assert rc == 0

    rc, fbdel, _ = _run_main(["--in-process", "flywheel", "batch-delete", "--sample-id", sim_sample_id], capsys)
    assert rc == 0

    assert "clear-all" not in build_cli().commands["simulation"].commands

    # 清理数据集与模型
    for did in ("cli_bench_ds", "cli_fw_bench_1", "cli_fw_bench_batch"):
        rc, _, _ = _run_main(["--in-process", "dataset", "delete", did], capsys)
        assert rc == 0

    for mid in ("cli-mock-target", "cli-mock-client", "cli-mock-judge"):
        rc, _, _ = _run_main(["--in-process", "model", "delete", mid], capsys)
        assert rc == 0


# ---------------------------------------------------------------------------
# 4. 子进程入口验证 (`python -m cli` 与兼容 `python backend/cli.py`)
# ---------------------------------------------------------------------------
def test_subprocess_entrypoints():
    env = {k: v for k, v in os.environ.items() if not k.startswith("EVALCTL_")}
    env["PYTHONUTF8"] = "1"
    default_cfg = str(PROJECT_ROOT / "cli" / "policy.default.yaml")

    p1 = subprocess.run(
        [sys.executable, "-m", "cli", "--config", default_cfg, "--format", "compact", "system", "capabilities"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert p1.returncode == 0, p1.stderr
    d1 = json.loads(p1.stdout)
    assert d1["allowed_count"] == len(COMMAND_CATALOG)

    p2 = subprocess.run(
        [sys.executable, "backend/cli.py", "--config", default_cfg, "--profile", "readonly", "--format", "compact", "system", "capabilities"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert p2.returncode == 0, p2.stderr
    d2 = json.loads(p2.stdout)
    assert d2["active_profile"] == "readonly"
    assert all(c["risk"] == "read" for c in d2["commands"])

    p3 = subprocess.run(
        [sys.executable, "-m", "cli", "--config", default_cfg, "--profile", "readonly", "model", "delete", "m1"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert p3.returncode == EXIT_POLICY_DENIED
    assert json.loads(p3.stderr)["error"]["code"] == "POLICY_DENIED"
