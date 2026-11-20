# -*- coding: utf-8 -*-
"""
纯对话数据集批量规则质检打分（只检不修）与常规评测边界防御回归测试
覆盖:
1. POST /api/v1/benchmarks/run 默认拦截无 reference/rubric/regex_constraints 的纯对话集 (HTTP 400)；
2. allow_unlabeled=True 时放行纯连通/生成速度测试；
3. POST /api/v1/flywheel/inspect-dataset & GET /api/v1/flywheel/inspect-tasks/{task_id}：
   - 并发只检不修（不触发修复模型），兼容 metadata.scenario_id；
   - 汇总均分/通过率/拓扑连通率/高频扣分项排行；
   - 隔离裁判异常样本（不计入 pass_rate / avg_score 分母）；
   - 支持 limit 与 save_to_flywheel=True（多次质检入库无主键冲突）；
   - 报告不内嵌冗余 debug_requests；
4. evalctl CLI: flywheel inspect-dataset --wait --brief、flywheel inspect-report --only-fail、benchmark run --allow-unlabeled。
"""
import io
import json
import os
import time
from typing import Any, Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.api import flywheel as api_flywheel
from backend.app.config import settings
from backend.app.engine import prompt_registry
from backend.app.engine.judge import UniversalJudgeEngine
from backend.app.main import app
from backend.app.models import (
    Base,
    FlywheelSampleRecord,
    RegisteredModel,
    get_db,
    init_db,
)
from backend.app.models import base as models_base
from backend.app import models as models_pkg
from backend.app.schemas.flywheel import InspectionReport
from cli.main import main as cli_main


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    rules_dir = data_dir / "rules"
    datasets_dir = data_dir / "datasets"
    runs_dir = data_dir / "runs"
    for d in (data_dir, rules_dir, datasets_dir, runs_dir):
        d.mkdir(parents=True, exist_ok=True)

    rule_obj = {
        "rule_id": "test_inspect_sop",
        "project_name": "测试规则SOP",
        "description": "用于纯对话集批量规则质检单测",
        "bot_config": {
            "system_prompt": "你是专业客服专员。",
            "initiator": "assistant",
            "max_turns": 40,
            "closing_keywords": ["再见"],
        },
        "topology_sop": {
            "enabled": True,
            "mermaid_graph": 'flowchart TD\n  N1_1("1.1 身份核验") --> N2_1("2.1 业务确认") --> N4_1("4.1 礼貌收口")',
            "node_id_pattern": "N[1-4]_\\d+",
            "entry_nodes": ["N1_1"],
            "terminal_nodes": ["N4_1"],
            "allow_self_loop": True,
            "allow_slot_shortcut": True,
            "allow_customer_hangup_early_exit": True,
            "require_terminal_node": False,
        },
        "faq_inspection": {"enabled": False, "knowledge_base": []},
        "inspection_rules": {
            "pass_score_threshold": 85,
            "global_redlines": ["严禁泄露隐私", "未核实身份不得透露账户详情"],
            "node_checkpoints": {},
        },
        "remediation_config": {"enabled": True, "freeze_user_input": True},
        "scenarios": [
            {"id": "sc_ok", "name": "标准场景", "instruction": "配合沟通"},
            {"id": "sc_bad", "name": "违规场景", "instruction": "测试违规"},
            {"id": "sc_err", "name": "异常场景", "instruction": "模拟裁判异常"},
        ],
    }
    (rules_dir / "test_inspect_sop.json").write_text(
        json.dumps(rule_obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    pure_dialogue_rows = [
        {
            "id": "conv_pass_1",
            "metadata": {"rule_id": "test_inspect_sop", "scenario_id": "sc_ok"},
            "messages": [
                {"role": "system", "content": "你是专业客服专员。"},
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "您好，请问是本人吗？"},
                {"role": "user", "content": "是的，我确认还款。"},
                {"role": "assistant", "content": "好的，感谢配合，再见！"},
            ],
        },
        {
            "id": "conv_fail_2",
            "metadata": {"rule_id": "test_inspect_sop", "scenario_id": "sc_bad"},
            "messages": [
                {"role": "user", "content": "喂，谁啊"},
                {"role": "assistant", "content": "你欠款5万块今天必须还！"},
            ],
        },
        {
            "id": "conv_err_3",
            "metadata": {"rule_id": "test_inspect_sop", "scenario_id": "sc_err"},
            "messages": [
                {"role": "user", "content": "在吗"},
                {"role": "assistant", "content": "在的"},
            ],
        },
    ]
    pure_ds_path = datasets_dir / "pure_dialogue_gold.jsonl"
    pure_ds_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in pure_dialogue_rows) + "\n",
        encoding="utf-8",
    )

    db_path = data_dir / "test_inspect.db"
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

    db = TestingSessionLocal()
    for mid in ("mock-target", "mock-judge"):
        if not db.query(RegisteredModel).filter(RegisteredModel.id == mid).first():
            db.add(
                RegisteredModel(
                    id=mid,
                    name=mid,
                    provider_type="mock",
                    model_name=mid,
                    api_url="http://mock.local",
                    config_json="{}",
                )
            )
    db.commit()
    db.close()

    def _override_get_db():
        s = TestingSessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield {
            "client": TestClient(app),
            "pure_ds_path": str(pure_ds_path),
            "runs_dir": runs_dir,
            "SessionLocal": TestingSessionLocal,
        }
    finally:
        app.dependency_overrides.pop(get_db, None)
        with prompt_registry._CACHE_LOCK:
            prompt_registry._ACTIVE_PROMPT_CACHE.clear()


def _run_cli(args: List[str], capsys) -> Tuple[int, Any, str]:
    capsys.readouterr()
    rc = cli_main(args)
    captured = capsys.readouterr()
    out_str = (captured.out or "").strip()
    parsed = json.loads(out_str) if out_str else None
    return rc, parsed, captured.err or ""


def _install_stub_judge(monkeypatch):
    """Stub UniversalJudgeEngine.evaluate_dialogue 模拟 PASS / FAIL / ERROR 三种分支。"""
    def _fake_eval(self, rule, scenario, dialogue):
        if scenario.id == "sc_ok":
            return InspectionReport(
                score=95,
                verdict="PASS",
                detected_defects=[],
                deductions=[],
                node_trajectory=["N1_1", "N2_1", "N4_1"],
                topology_valid=True,
                expert_comments="流程规范合规",
                debug_requests=[
                    {
                        "step": "main_judge",
                        "messages": [{"role": "user", "content": "huge"}],
                        "response_content": '{"score": 95, "verdict": "PASS"}',
                    }
                ],
                request_messages=[{"role": "user", "content": "huge"}],
            )
        if scenario.id == "sc_bad":
            return InspectionReport(
                score=65,
                verdict="FAIL",
                detected_defects=["[未核实身份] -35分: 未确认本人即透露金额"],
                deductions=[{"item": "未核实身份", "points": 35, "reason": "首轮未确认机主身份", "turn": 2}],
                node_trajectory=["N2_1"],
                topology_valid=False,
                topology_error="跳步未经过 N1_1",
                expert_comments="存在严重身份核验缺失",
                debug_requests=[
                    {
                        "step": "main_judge",
                        "messages": [{"role": "user", "content": "huge"}],
                        "response_content": '{"score": 65, "verdict": "FAIL"}',
                    }
                ],
            )
        # 模拟裁判模型调用/JSON 异常兜底输出（验证 ERROR 隔离）
        return InspectionReport(
            score=0,
            verdict="FAIL",
            detected_defects=["质检裁判模型调用异常: simulated timeout"],
            deductions=[],
            node_trajectory=[],
            topology_valid=False,
            expert_comments="因接口异常未能完成全量白盒推演。",
            debug_requests=[{"step": "main_judge", "error": "simulated timeout"}],
        )

    monkeypatch.setattr(UniversalJudgeEngine, "evaluate_dialogue", _fake_eval)


def test_benchmark_rejects_unlabeled_dialogue_and_allows_explicit_override(isolated_env):
    client: TestClient = isolated_env["client"]
    ds_path = isolated_env["pure_ds_path"]

    # 1. 默认 allow_unlabeled=False 拦截纯对话集
    r_block = client.post(
        "/api/v1/benchmarks/run",
        json={
            "dataset_path": ds_path,
            "model_ids": ["mock-target"],
            "workers": 1,
        },
    )
    assert r_block.status_code == 400
    assert "纯对话" in r_block.json()["detail"]
    assert "inspect-dataset" in r_block.json()["detail"]

    # 2. 显式传入 allow_unlabeled=True 允许纯连通/速度测试
    r_ok = client.post(
        "/api/v1/benchmarks/run",
        json={
            "dataset_path": ds_path,
            "model_ids": ["mock-target"],
            "workers": 1,
            "allow_unlabeled": True,
        },
    )
    assert r_ok.status_code == 200, r_ok.text
    task_id = r_ok.json()["task_id"]

    status_data = None
    for _ in range(30):
        resp = client.get(f"/api/v1/benchmarks/tasks/{task_id}")
        assert resp.status_code == 200
        status_data = resp.json()
        if status_data["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(0.1)
    assert status_data is not None and status_data["status"] == "COMPLETED"

    # 3. 无 id 字段的外部纯对话 SFT 语料（仅含 messages），同样应精准命中纯对话拦截与 allow_unlabeled 放行
    no_id_path = os.path.join(os.path.dirname(ds_path), "no_id_sft_dialogue.jsonl")
    with open(no_id_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"messages": [{"role": "user", "content": "你好"}]}, ensure_ascii=False) + "\n")

    r_no_id_block = client.post(
        "/api/v1/benchmarks/run",
        json={"dataset_path": no_id_path, "model_ids": ["mock-target"], "workers": 1},
    )
    assert r_no_id_block.status_code == 400
    assert "纯对话" in r_no_id_block.json()["detail"]

    r_no_id_ok = client.post(
        "/api/v1/benchmarks/run",
        json={
            "dataset_path": no_id_path,
            "model_ids": ["mock-target"],
            "workers": 1,
            "allow_unlabeled": True,
        },
    )
    assert r_no_id_ok.status_code == 200, r_no_id_ok.text
    no_id_task = r_no_id_ok.json()["task_id"]
    no_id_status = None
    for _ in range(30):
        resp = client.get(f"/api/v1/benchmarks/tasks/{no_id_task}")
        no_id_status = resp.json()
        if no_id_status["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(0.1)
    assert no_id_status is not None and no_id_status["status"] == "COMPLETED"


def test_flywheel_inspect_dataset_api_and_flywheel_sync(isolated_env, monkeypatch):
    _install_stub_judge(monkeypatch)
    client: TestClient = isolated_env["client"]
    TestingSessionLocal = isolated_env["SessionLocal"]

    # 1. 提交批量规则质检打分并开启 save_to_flywheel=True
    r_sub = client.post(
        "/api/v1/flywheel/inspect-dataset",
        json={
            "dataset_id": "pure_dialogue_gold",
            "rule_id": "test_inspect_sop",
            "judge_model_id": "mock-judge",
            "workers": 2,
            "save_to_flywheel": True,
        },
    )
    assert r_sub.status_code == 200, r_sub.text
    task_id = r_sub.json()["task_id"]
    assert r_sub.json()["total_samples"] == 3

    report = None
    for _ in range(40):
        r_get = client.get(f"/api/v1/flywheel/inspect-tasks/{task_id}")
        assert r_get.status_code == 200
        report = r_get.json()
        if report["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(0.1)

    assert report is not None and report["status"] == "COMPLETED"
    summary = report["summary"]
    assert summary["total_samples"] == 3
    assert summary["pass_count"] == 1
    assert summary["fail_count"] == 1
    assert summary["error_count"] == 1
    assert summary["valid_count"] == 2
    # ERROR 样本已隔离，不拉低均分与通过率: (95 + 65) / 2 = 80.0, 1 / 2 = 0.5
    assert summary["pass_rate"] == 0.5
    assert summary["avg_score"] == 80.0
    assert summary["min_score"] == 65
    assert summary["max_score"] == 95
    assert summary["topology_pass_rate"] == 0.5
    assert summary["top_deductions"] == [{"item": "未核实身份", "count": 1, "total_points": 35}]

    samples_map = {s["sample_id"]: s for s in report["samples"]}
    assert samples_map["conv_pass_1"]["scenario_id"] == "sc_ok"
    assert samples_map["conv_pass_1"]["turn_count"] == 4
    assert samples_map["conv_pass_1"]["verdict"] == "PASS"
    assert "debug_requests" not in samples_map["conv_pass_1"]
    assert "request_messages" not in samples_map["conv_pass_1"]
    assert samples_map["conv_fail_2"]["verdict"] == "FAIL"
    assert samples_map["conv_err_3"]["verdict"] == "ERROR"
    assert samples_map["conv_err_3"]["score"] is None

    # 校验落盘报告文件存在
    assert os.path.exists(report["report_file"])

    # 校验有效样本已写入 FlywheelSampleRecord，ERROR 样本未污染入库
    db = TestingSessionLocal()
    fw_rows = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.task_id == task_id).all()
    assert len(fw_rows) == 2
    fw_by_sc = {r.scenario_id: r for r in fw_rows}
    assert fw_by_sc["sc_ok"].status == "GOLD_PASS"
    assert fw_by_sc["sc_bad"].status == "PENDING"
    db.close()

    # 2. 对同一数据集再次质检并 save_to_flywheel=True + limit=2，不应发生主键冲突
    r_sub2 = client.post(
        "/api/v1/flywheel/inspect-dataset",
        json={
            "dataset_id": "pure_dialogue_gold",
            "rule_id": "test_inspect_sop",
            "judge_model_id": "mock-judge",
            "workers": 1,
            "limit": 2,
            "save_to_flywheel": True,
        },
    )
    assert r_sub2.status_code == 200, r_sub2.text
    task_id2 = r_sub2.json()["task_id"]
    for _ in range(40):
        rep2 = client.get(f"/api/v1/flywheel/inspect-tasks/{task_id2}").json()
        if rep2["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(0.1)
    assert rep2["status"] == "COMPLETED"
    assert rep2["summary"]["total_samples"] == 2
    assert rep2["saved_sample_count"] == 2


def test_cli_inspect_dataset_and_benchmark_allow_unlabeled(isolated_env, monkeypatch, capsys):
    _install_stub_judge(monkeypatch)
    ds_path = isolated_env["pure_ds_path"]

    # 1. CLI: benchmark run 默认拦截无标准集 (退出码 1)
    rc_blk, _, err_blk = _run_cli(
        [
            "--in-process",
            "benchmark",
            "run",
            "--dataset-path",
            ds_path,
            "--model-id",
            "mock-target",
        ],
        capsys,
    )
    assert rc_blk == 1
    assert "纯对话" in err_blk

    # 2. CLI: benchmark run --allow-unlabeled 放行成功
    rc_ok, res_ok, _ = _run_cli(
        [
            "--in-process",
            "benchmark",
            "run",
            "--dataset-path",
            ds_path,
            "--model-id",
            "mock-target",
            "--allow-unlabeled",
            "--wait",
            "--poll-interval",
            "0.05",
        ],
        capsys,
    )
    assert rc_ok == 0 and res_ok["status"] == "COMPLETED"

    # 3. CLI: flywheel inspect-dataset --wait --brief
    rc_insp, insp_rep, _ = _run_cli(
        [
            "--in-process",
            "flywheel",
            "inspect-dataset",
            "--dataset-id",
            "pure_dialogue_gold",
            "--rule-id",
            "test_inspect_sop",
            "--judge-model-id",
            "mock-judge",
            "--workers",
            "2",
            "--wait",
            "--poll-interval",
            "0.05",
            "--brief",
        ],
        capsys,
    )
    assert rc_insp == 0
    assert insp_rep["status"] == "COMPLETED"
    assert insp_rep["summary"]["avg_score"] == 80.0
    assert len(insp_rep["samples"]) == 3
    assert "deduction_items" in insp_rep["samples"][0]

    # 4. CLI: flywheel inspect-report <task_id> --only-fail --brief
    rc_fail, fail_rep, _ = _run_cli(
        [
            "--in-process",
            "flywheel",
            "inspect-report",
            insp_rep["task_id"],
            "--only-fail",
            "--brief",
        ],
        capsys,
    )
    assert rc_fail == 0
    assert len(fail_rep["samples"]) == 2
    assert all(s["verdict"] != "PASS" for s in fail_rep["samples"])
