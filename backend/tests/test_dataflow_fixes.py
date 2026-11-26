# -*- coding: utf-8 -*-
"""
数据流第一组修复项（1~5 项）全隔离回归测试
严格在 tmp_path 隔离库与数据目录中执行，绝不修改真实 data/ 目录与 eval_platform.db。
"""
from __future__ import annotations

import json
import pathlib
import time
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.app.api.flywheel as api_flywheel
import backend.app.engine.prompt_registry as prompt_registry
import backend.app.models as models_pkg
import backend.app.models.base as models_base
from backend.app.adapters.mock_adapter import MockAdapter
from backend.app.config import settings
from backend.app.engine.benchmark_sampler import BenchmarkSamplingEngine
from backend.app.engine.judge import UniversalJudgeEngine
from backend.app.engine.remediator import UniversalDataRemediator
from backend.app.main import app
from backend.app.models.base import (
    Base,
    DatasetRecord,
    FlywheelSampleRecord,
    RegisteredModel,
    RuleConfigRecord,
    get_db,
    init_db,
)
from backend.app.schemas.benchmark import BenchmarkSample, SampleEvalTracker, SamplingFilterConfig
from backend.app.schemas.chat import ChatMessage
from backend.app.schemas.flywheel import InspectionReport
from backend.app.schemas.rule import BotConfig, InspectionRules, TopologySOPConfig, UniversalRuleSchema
from backend.app.services.flywheel_service import FlywheelPipelineService
from cli.main import build_cli

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture()
def isolated_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    rules_dir = data_dir / "rules"
    datasets_dir = data_dir / "datasets"
    runs_dir = data_dir / "runs"
    for d in (data_dir, rules_dir, datasets_dir, runs_dir):
        d.mkdir(parents=True, exist_ok=True)

    rule_obj = {
        "rule_id": "df_test_rule",
        "project_name": "数据流测试规则",
        "bot_config": {
            "system_prompt": "你是专业客服专员。",
            "initiator": "user",
            "max_turns": 20,
            "closing_keywords": ["再见"],
        },
        "topology_sop": {
            "enabled": False,
            "mermaid_graph": "",
        },
        "faq_inspection": {"enabled": False, "knowledge_base": []},
        "inspection_rules": {
            "pass_score_threshold": 85,
            "global_redlines": ["严禁辱骂客户"],
            "node_checkpoints": {},
        },
        "remediation_config": {"enabled": True, "freeze_user_input": True},
        "scenarios": [{"id": "sc_default", "name": "默认场景", "instruction": "常规咨询"}],
    }
    (rules_dir / "df_test_rule.json").write_text(
        json.dumps(rule_obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    db_path = data_dir / "test_dataflow.db"
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
    for mid in ("mock-target", "mock-judge", "mock-remediate"):
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
            "datasets_dir": datasets_dir,
            "runs_dir": runs_dir,
            "SessionLocal": TestingSessionLocal,
        }
    finally:
        app.dependency_overrides.pop(get_db, None)
        with prompt_registry._CACHE_LOCK:
            prompt_registry._ACTIVE_PROMPT_CACHE.clear()
        test_engine.dispose()


def _write_jsonl(path: pathlib.Path, rows: List[Dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. 淘汰对话文件不能被当成训练集
# ---------------------------------------------------------------------------
def test_item1_dataset_classification_by_content_and_discarded_pool(isolated_env, monkeypatch):
    client: TestClient = isolated_env["client"]
    datasets_dir: pathlib.Path = isolated_env["datasets_dir"]
    Session = isolated_env["SessionLocal"]

    # (a) 文件名不含 bench/eval，但每行都带答案 -> 应判为 benchmark
    _write_jsonl(
        datasets_dir / "custom_qa_corpus.jsonl",
        [
            {
                "id": "q1",
                "messages": [{"role": "user", "content": "1+1等于几"}],
                "reference": "2",
            }
        ],
    )

    # (b) 文件名含 eval，但为无答案纯对话 -> 绝不能判为 benchmark，应为 flywheel_gold
    _write_jsonl(
        datasets_dir / "raw_eval_dialogues.jsonl",
        [
            {
                "id": "d1",
                "messages": [
                    {"role": "user", "content": "你好"},
                    {"role": "assistant", "content": "您好"},
                ],
            }
        ],
    )

    # (c) 未登记的淘汰对话文件 -> 应判为 flywheel_discarded
    _write_jsonl(
        datasets_dir / "unregistered_discarded.jsonl",
        [
            {
                "id": "disc_1",
                "metadata": {"rule_id": "df_test_rule", "status": "DISCARDED", "initial_score": 40},
                "messages": [
                    {"role": "user", "content": "你好"},
                    {"role": "assistant", "content": "违规答复"},
                ],
            }
        ],
    )

    # (d) 模拟 3 个历史错登为 flywheel_gold 的淘汰文件 -> list_datasets 应自动纠偏为 flywheel_discarded
    db = Session()
    for i in range(1, 4):
        fpath = datasets_dir / f"legacy_rule_{i}_discarded.jsonl"
        _write_jsonl(
            fpath,
            [
                {
                    "id": f"old_disc_{i}",
                    "metadata": {"rule_id": f"rule_{i}", "status": "DISCARDED", "detected_defects": ["红线"]},
                    "messages": [
                        {"role": "user", "content": "问"},
                        {"role": "assistant", "content": "答"},
                    ],
                }
            ],
        )
        db.add(
            DatasetRecord(
                id=f"legacy_rule_{i}_discarded",
                name=fpath.name,
                file_path=str(fpath),
                sample_count=1,
                dataset_type="flywheel_gold",
            )
        )
    db.commit()
    db.close()

    resp = client.get("/api/v1/datasets/")
    assert resp.status_code == 200
    by_id = {d["id"]: d for d in resp.json()}

    assert by_id["custom_qa_corpus"]["dataset_type"] == "benchmark"
    assert by_id["raw_eval_dialogues"]["dataset_type"] == "flywheel_gold"
    assert by_id["unregistered_discarded"]["dataset_type"] == "flywheel_discarded"
    for i in range(1, 4):
        assert by_id[f"legacy_rule_{i}_discarded"]["dataset_type"] == "flywheel_discarded"

    # (e) /flywheel/batch 写出的淘汰文件应在写出时直接登记为 flywheel_discarded
    def _fail_judge(self, rule, scenario, dialogue):
        return InspectionReport(
            score=40,
            verdict="FAIL",
            detected_defects=["[违规] -60分: 未核实身份"],
            deductions=[{"item": "违规", "points": 60, "reason": "未核实身份", "turn": 2}],
            topology_valid=True,
            expert_comments="不达标",
        )

    def _no_op_remediate(self, rule, scenario, raw_messages, eval_report):
        return raw_messages, [], {}

    monkeypatch.setattr(UniversalJudgeEngine, "evaluate_dialogue", _fail_judge)
    monkeypatch.setattr(UniversalDataRemediator, "remediate_dialogue_with_actions", _no_op_remediate)

    batch_in = datasets_dir / "batch_src.jsonl"
    _write_jsonl(
        batch_in,
        [
            {
                "id": "b1",
                "messages": [
                    {"role": "user", "content": "查余额"},
                    {"role": "assistant", "content": "直接告诉你余额"},
                ],
            }
        ],
    )
    gold_out = datasets_dir / "batch_out_gold.jsonl"
    disc_out = datasets_dir / "batch_out_discarded.jsonl"

    r_batch = client.post(
        "/api/v1/flywheel/batch",
        json={
            "rule_id": "df_test_rule",
            "judge_model_id": "mock-judge",
            "remediate_model_id": "mock-remediate",
            "input_file_path": str(batch_in),
            "gold_output_path": str(gold_out),
            "discard_output_path": str(disc_out),
            "workers": 1,
        },
    )
    assert r_batch.status_code == 200
    assert disc_out.exists()

    r_after = client.get("/api/v1/datasets/")
    assert r_after.status_code == 200
    by_id_after = {d["id"]: d for d in r_after.json()}
    assert "batch_out_discarded" in by_id_after
    assert by_id_after["batch_out_discarded"]["dataset_type"] == "flywheel_discarded"

    # 前端 DatasetCenter 页签隔离检查：训练集过滤 flywheel_gold，淘汰对话过滤 flywheel_discarded 且无下载 SFT 按钮
    dc_src = (PROJECT_ROOT / "frontend" / "src" / "pages" / "DatasetCenter.tsx").read_text(encoding="utf-8")
    assert "d.dataset_type === 'flywheel_gold'" in dc_src
    assert "d.dataset_type === 'flywheel_discarded'" in dc_src
    assert "discarded" in dc_src


# ---------------------------------------------------------------------------
# 2. 评测集只能导入带答案的评测题
# ---------------------------------------------------------------------------
def test_item2_benchmark_import_requires_criteria_per_sample(isolated_env):
    client: TestClient = isolated_env["client"]
    datasets_dir: pathlib.Path = isolated_env["datasets_dir"]

    mixed_samples = [
        {
            "id": "ok_q1",
            "messages": [{"role": "user", "content": "问题1"}],
            "reference": "标准答案1",
        },
        {
            "id": "no_ans_q2",
            "messages": [
                {"role": "user", "content": "纯对话无答案"},
                {"role": "assistant", "content": "你好"},
            ],
        },
    ]

    # 按评测集导入必须整批拒绝，并指出第 2 条
    r_bad = client.post(
        "/api/v1/datasets/import",
        json={
            "dataset_id": "mixed_eval_reject",
            "dataset_type": "benchmark",
            "samples": mixed_samples,
        },
    )
    assert r_bad.status_code == 400
    assert "第 2 条" in r_bad.json()["detail"]

    # 同一批数据按训练集 (flywheel_gold) 导入成功
    r_train = client.post(
        "/api/v1/datasets/import",
        json={
            "dataset_id": "mixed_as_training",
            "dataset_type": "flywheel_gold",
            "samples": mixed_samples,
        },
    )
    assert r_train.status_code == 200
    assert r_train.json()["imported_count"] == 2

    # 发起评测时兜底拦截文案应与实际页签一致
    pure_path = datasets_dir / "pure_for_bench.jsonl"
    _write_jsonl(pure_path, [mixed_samples[1]])
    r_bench = client.post(
        "/api/v1/benchmarks/run",
        json={"dataset_path": str(pure_path), "model_ids": ["mock-target"], "workers": 1},
    )
    assert r_bench.status_code == 400
    detail = r_bench.json()["detail"]
    assert "训练与对话语料 (SFT)" in detail
    assert "淘汰对话隔离池" in detail

    # 只读读取真实 data/datasets 下的 3 个标准评测集（41 道题）在隔离环境重新导入应全部成功
    real_bench_files = [
        ("reimport_demo", "demo_benchmark.jsonl", 4),
        ("reimport_general", "general_eval_simple_test.jsonl", 30),
        ("reimport_sft_eval", "gpt_qwen3_8sft_eval_v1.jsonl", 7),
    ]
    total_reimported = 0
    for did, fname, expected_cnt in real_bench_files:
        src = PROJECT_ROOT / "data" / "datasets" / fname
        if src.exists():
            content = src.read_text(encoding="utf-8")
            r_ok = client.post(
                "/api/v1/datasets/import",
                json={"dataset_id": did, "dataset_type": "benchmark", "content": content},
            )
            assert r_ok.status_code == 200, f"{fname} 导入失败: {r_ok.text}"
            assert r_ok.json()["imported_count"] == expected_cnt
            total_reimported += expected_cnt
    assert total_reimported == 41


# ---------------------------------------------------------------------------
# 3. 对练台不单独管理对练数据
# ---------------------------------------------------------------------------
def test_item3_simulation_delete_removed_and_readonly_ui(isolated_env):
    client: TestClient = isolated_env["client"]

    r_del = client.delete("/api/v1/simulation/battles/all")
    assert r_del.status_code in (404, 405)

    sim_cmd = build_cli().commands["simulation"]
    assert "clear-all" not in sim_cmd.commands

    sim_tsx = (PROJECT_ROOT / "frontend" / "src" / "pages" / "SimulationArena.tsx").read_text(encoding="utf-8")
    assert "deleteSample" not in sim_tsx
    assert "batchDeleteSamples" not in sim_tsx
    assert "clearBattles" not in sim_tsx
    assert "清空全部历史" not in sim_tsx
    assert "前往飞轮工作台管理样本池" in sim_tsx


# ---------------------------------------------------------------------------
# 4. 批量治理：质检出错的对话不能进淘汰文件
# ---------------------------------------------------------------------------
def test_item4_batch_judge_error_excluded_from_discarded_and_retried_on_resume(
    isolated_env, monkeypatch: pytest.MonkeyPatch
):
    datasets_dir: pathlib.Path = isolated_env["datasets_dir"]
    rule = UniversalRuleSchema(
        rule_id="df_batch_rule",
        project_name="批量质检测试",
        bot_config=BotConfig(system_prompt="你是客服"),
        topology_sop=TopologySOPConfig(enabled=False),
        inspection_rules=InspectionRules(pass_score_threshold=85),
    )

    records = [
        {
            "id": "conv_gold",
            "messages": [
                {"role": "user", "content": "正常咨询"},
                {"role": "assistant", "content": "标准回答"},
            ],
        },
        {
            "id": "conv_judge_err",
            "messages": [
                {"role": "user", "content": "触发裁判异常的对话"},
                {"role": "assistant", "content": "你好"},
            ],
        },
        {
            "id": "conv_unremediable_fail",
            "messages": [
                {"role": "user", "content": "无扣分项但低分"},
                {"role": "assistant", "content": "敷衍回复"},
            ],
        },
    ]

    gold_path = datasets_dir / "batch_item4_gold.jsonl"
    disc_path = datasets_dir / "batch_item4_discarded.jsonl"

    # 第一轮：conv_judge_err 模拟裁判出错
    def _judge_round1(self, r, sc, dialogue):
        u_txt = next((m.content for m in dialogue if m.role == "user"), "")
        if "裁判异常" in u_txt:
            return InspectionReport(
                score=0,
                verdict="FAIL",
                detected_defects=["质检裁判模型调用异常: timeout"],
                deductions=[],
                debug_requests=[{"step": "main_judge", "error": "timeout"}],
            )
        if "无扣分项" in u_txt:
            return InspectionReport(
                score=60,
                verdict="FAIL",
                detected_defects=[],
                deductions=[],
                expert_comments="整体不达标但未拆出扣分项",
            )
        return InspectionReport(score=95, verdict="PASS", detected_defects=[], deductions=[])

    monkeypatch.setattr(UniversalJudgeEngine, "evaluate_dialogue", _judge_round1)
    pipeline = FlywheelPipelineService(
        judge_adapter=MockAdapter("mock-judge"),
        remediate_adapter=MockAdapter("mock-remediate"),
    )
    st1 = pipeline.batch_process_dataset(
        rule=rule,
        input_records=records,
        gold_output_path=str(gold_path),
        discard_output_path=str(disc_path),
        workers=1,
    )
    assert st1["gold"] == 1
    assert st1["discarded"] == 1
    assert st1["judge_error"] == 1
    assert st1["skipped"] == 0

    gold_ids_1 = [json.loads(l)["id"] for l in gold_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    disc_rows_1 = [json.loads(l) for l in disc_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    disc_ids_1 = [r["id"] for r in disc_rows_1]
    assert gold_ids_1 == ["conv_gold"]
    assert disc_ids_1 == ["conv_unremediable_fail"]
    assert disc_rows_1[0]["metadata"]["status"] == "DISCARDED"
    assert "conv_judge_err" not in gold_ids_1 and "conv_judge_err" not in disc_ids_1

    # 第二轮：换成正常裁判续跑 -> conv_gold 与 conv_unremediable_fail 跳过，conv_judge_err 被重试并判定淘汰
    def _judge_round2(self, r, sc, dialogue):
        return InspectionReport(
            score=50,
            verdict="FAIL",
            detected_defects=["[红线] -50分: 违规回复"],
            deductions=[{"item": "红线", "points": 50, "reason": "违规", "turn": 2}],
        )

    monkeypatch.setattr(UniversalJudgeEngine, "evaluate_dialogue", _judge_round2)
    monkeypatch.setattr(
        UniversalDataRemediator,
        "remediate_dialogue_with_actions",
        lambda self, r, sc, msgs, ev: (msgs, [], {}),
    )

    st2 = pipeline.batch_process_dataset(
        rule=rule,
        input_records=records,
        gold_output_path=str(gold_path),
        discard_output_path=str(disc_path),
        workers=1,
    )
    assert st2["skipped"] == 2
    assert st2["judge_error"] == 0
    assert st2["discarded"] == 1

    disc_ids_2 = [json.loads(l)["id"] for l in disc_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert "conv_judge_err" in disc_ids_2


# ---------------------------------------------------------------------------
# 5. 评测集文件里的战绩快照不能影响评测
# ---------------------------------------------------------------------------
def test_item5_file_eval_tracker_ignored_and_stripped_on_import_or_save(isolated_env):
    client: TestClient = isolated_env["client"]
    datasets_dir: pathlib.Path = isolated_env["datasets_dir"]
    Session = isolated_env["SessionLocal"]

    stale_tracker = SampleEvalTracker(
        consecutive_pass=8,
        miss_streak=0,
        target_interval=5,
        last_status="PASS",
        last_score=100,
    )
    rows_with_tracker = [
        {
            "id": f"t_{i}",
            "is_holdout": True,
            "messages": [{"role": "user", "content": f"考题 {i}"}],
            "reference": f"答案 {i}",
            "eval_tracker": stale_tracker.model_dump(),
        }
        for i in range(6)
    ]
    rows_without_tracker = [
        {k: v for k, v in r.items() if k != "eval_tracker"}
        for r in rows_with_tracker
    ]

    f_with = datasets_dir / "with_tracker.jsonl"
    f_without = datasets_dir / "without_tracker.jsonl"
    _write_jsonl(f_with, rows_with_tracker)
    _write_jsonl(f_without, rows_without_tracker)

    # 1. 不填 model_line 时，带与不带 eval_tracker 的抽样预览与 partition_samples 结果必须完全一致
    samples_with = [BenchmarkSample(**r) for r in rows_with_tracker]
    samples_without = [BenchmarkSample(**r) for r in rows_without_tracker]
    cfg_no_line = SamplingFilterConfig(enabled=True, pass_threshold_x=2, interval_step_s=1, max_interval_n=5)

    sel1, skp1, st1 = BenchmarkSamplingEngine.partition_samples(samples_with, cfg_no_line)
    sel2, skp2, st2 = BenchmarkSamplingEngine.partition_samples(samples_without, cfg_no_line)
    assert [s.id for s in sel1] == [s.id for s in sel2]
    assert len(skp1) == 0 and len(skp2) == 0
    assert st1["skipped_samples"] == st2["skipped_samples"] == 0

    prev_with = client.post(
        "/api/v1/benchmarks/preview-sampling",
        json={"dataset_path": str(f_with), "sampling_config": cfg_no_line.model_dump()},
    )
    prev_without = client.post(
        "/api/v1/benchmarks/preview-sampling",
        json={"dataset_path": str(f_without), "sampling_config": cfg_no_line.model_dump()},
    )
    assert prev_with.status_code == 200 and prev_without.status_code == 200
    assert prev_with.json() == prev_without.json()

    # 2. 导入带 eval_tracker 的评测集时必须剥离 eval_tracker
    r_imp = client.post(
        "/api/v1/datasets/import",
        json={
            "dataset_id": "imported_clean_bench",
            "dataset_type": "benchmark",
            "samples": rows_with_tracker,
        },
    )
    assert r_imp.status_code == 200
    imp_file = pathlib.Path(r_imp.json()["dataset"]["file_path"])
    for line in imp_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            assert "eval_tracker" not in json.loads(line)

    # 3. 过滤导出: download + include_tracker=True 保留 eval_tracker；save_dataset 即使传 include_tracker=True 也必须剥离
    db = Session()
    cfg_with_line = SamplingFilterConfig(
        enabled=True,
        pass_threshold_x=1,
        interval_step_s=1,
        max_interval_n=3,
        model_line="line_check",
        version_tag="v1",
        benchmark_model_id="mock-target",
    )
    # 写入空白无战绩流水外，验证 download 与 save_dataset 差异
    r_dl = client.post(
        "/api/v1/datasets/imported_clean_bench/export",
        json={
            "sampling_config": cfg_with_line.model_dump(),
            "export_scope": "all",
            "include_tracker": True,
            "export_target": "download",
        },
    )
    assert r_dl.status_code == 200
    first_dl_row = json.loads(r_dl.json()["jsonl_content"].splitlines()[0])
    assert "eval_tracker" in first_dl_row

    r_save = client.post(
        "/api/v1/datasets/imported_clean_bench/export",
        json={
            "sampling_config": cfg_with_line.model_dump(),
            "export_scope": "all",
            "include_tracker": True,
            "export_target": "save_dataset",
            "target_dataset_id": "saved_clean_bench",
            "save_mode": "overwrite",
        },
    )
    assert r_save.status_code == 200
    saved_file = pathlib.Path(r_save.json()["saved_dataset"]["file_path"])
    for line in saved_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            assert "eval_tracker" not in json.loads(line)
    db.close()


# ---------------------------------------------------------------------------
# 6. 样本池导出留底、留出/普通题如实标记、联动改写与双向同步
# ---------------------------------------------------------------------------
def test_item6_export_lineage_truthful_holdout_and_retroactive_flip(isolated_env):
    client: TestClient = isolated_env["client"]
    datasets_dir: pathlib.Path = isolated_env["datasets_dir"]
    Session = isolated_env["SessionLocal"]

    db = Session()
    s1 = FlywheelSampleRecord(
        id="item6_train_first",
        task_id="t6",
        rule_id="df_test_rule",
        scenario_id="sc_default",
        status="REPAIRED_PASS",
        raw_verdict="FAIL",
        remediated_verdict="PASS",
        manual_decision="NONE",
        active_version="remediated",
        score=95,
        raw_messages_json=json.dumps(
            [
                {"role": "system", "content": "你是专业客服专员。"},
                {"role": "user", "content": "订单发货了吗"},
                {"role": "assistant", "content": "不知道"},
            ],
            ensure_ascii=False,
        ),
        remediated_messages_json=json.dumps(
            [
                {"role": "system", "content": "你是专业客服专员。"},
                {"role": "user", "content": "订单发货了吗"},
                {"role": "assistant", "content": "您好，马上为您查询订单物流状态。"},
            ],
            ensure_ascii=False,
        ),
        benchmark_slices_json=json.dumps(
            [
                {
                    "slice_id": "sl_i6_1",
                    "sample_id": "item6_train_first",
                    "target_turn": 2,
                    "defect_item": "态度规范",
                    "defect_reason": "不能敷衍",
                    "messages": [{"role": "user", "content": "订单发货了吗"}],
                    "reference": "您好，马上为您查询订单物流状态。",
                    "rubric": "礼貌回应并核实订单",
                    "regex_constraints": [],
                }
            ],
            ensure_ascii=False,
        ),
        updated_at=int(time.time()),
    )
    s2 = FlywheelSampleRecord(
        id="item6_bench_first",
        task_id="t6",
        rule_id="df_test_rule",
        scenario_id="sc_default",
        status="REPAIRED_PASS",
        raw_verdict="FAIL",
        remediated_verdict="PASS",
        manual_decision="NONE",
        active_version="remediated",
        score=92,
        raw_messages_json=json.dumps(
            [
                {"role": "system", "content": "你是专业客服专员。"},
                {"role": "user", "content": "如何开发票"},
                {"role": "assistant", "content": "开不了"},
            ],
            ensure_ascii=False,
        ),
        remediated_messages_json=json.dumps(
            [
                {"role": "system", "content": "你是专业客服专员。"},
                {"role": "user", "content": "如何开发票"},
                {"role": "assistant", "content": "您好，您可以在订单详情页申请电子发票。"},
            ],
            ensure_ascii=False,
        ),
        benchmark_slices_json=json.dumps(
            [
                {
                    "slice_id": "sl_i6_2",
                    "sample_id": "item6_bench_first",
                    "target_turn": 2,
                    "defect_item": "开票指引",
                    "defect_reason": "指引不准确",
                    "messages": [{"role": "user", "content": "如何开发票"}],
                    "reference": "您好，您可以在订单详情页申请电子发票。",
                    "rubric": "准确指引开发票",
                    "regex_constraints": [],
                }
            ],
            ensure_ascii=False,
        ),
        updated_at=int(time.time()),
    )
    s_pending = FlywheelSampleRecord(
        id="item6_pending",
        task_id="t6",
        rule_id="df_test_rule",
        scenario_id="sc_default",
        status="PENDING",
        raw_verdict="FAIL",
        remediated_verdict="NONE",
        manual_decision="NONE",
        active_version="raw",
        score=40,
        raw_messages_json=json.dumps(
            [
                {"role": "system", "content": "你是专业客服专员。"},
                {"role": "user", "content": "退货吗"},
                {"role": "assistant", "content": "不行"},
            ],
            ensure_ascii=False,
        ),
        updated_at=int(time.time()),
    )
    s_discarded = FlywheelSampleRecord(
        id="item6_discarded",
        task_id="t6",
        rule_id="df_test_rule",
        scenario_id="sc_default",
        status="DISCARDED",
        raw_verdict="FAIL",
        remediated_verdict="NONE",
        manual_decision="REJECTED",
        active_version="raw",
        score=20,
        raw_messages_json=json.dumps(
            [
                {"role": "system", "content": "你是专业客服专员。"},
                {"role": "user", "content": "投诉你"},
                {"role": "assistant", "content": "随便"},
            ],
            ensure_ascii=False,
        ),
        updated_at=int(time.time()),
    )
    db.add_all([s1, s2, s_pending, s_discarded])
    db.commit()
    db.close()

    # 1. export-selected 必须拦截 PENDING / DISCARDED 未达标样本
    r_unqual = client.post(
        "/api/v1/flywheel/samples/export-selected",
        json={"sample_ids": ["item6_train_first", "item6_pending"], "version_tag": "bad"},
    )
    assert r_unqual.status_code == 400
    assert "尚未达标" in r_unqual.json()["detail"]

    r_unqual_disc = client.post(
        "/api/v1/flywheel/samples/export-selected",
        json={"sample_ids": ["item6_discarded"], "version_tag": "bad2"},
    )
    assert r_unqual_disc.status_code == 400

    # 2. s1 先导出训练集（仅选中 s1） -> 写入 exported_training_datasets；切题导出评测集强制 is_holdout=False
    r_tr1 = client.post(
        "/api/v1/flywheel/samples/export-selected",
        json={
            "sample_ids": ["item6_train_first"],
            "version_tag": "i6_t1",
            "target_dataset_id": "train_ds_i6",
        },
    )
    assert r_tr1.status_code == 200
    samples_list = {x["id"]: x for x in client.get("/api/v1/flywheel/samples").json()}
    assert "train_ds_i6" in samples_list["item6_train_first"]["exported_training_datasets"]
    assert samples_list["item6_bench_first"]["exported_training_datasets"] == []

    # 显式强传 is_holdout=True 给已进过训练集的 s1 必须返回 400
    r_force_hold = client.post(
        "/api/v1/flywheel/samples/item6_train_first/export-benchmark",
        json={
            "dataset_id": "bench_s1_bad",
            "name": "非法强标留出",
            "is_holdout": True,
        },
    )
    assert r_force_hold.status_code == 400

    # 正常导出 s1 为评测集 -> 自动如实标记 is_holdout=False（普通题）
    r_bm1 = client.post(
        "/api/v1/flywheel/samples/item6_train_first/export-benchmark",
        json={"dataset_id": "bench_s1_reg", "name": "同源普通评测集", "mode": "create"},
    )
    assert r_bm1.status_code == 200
    assert r_bm1.json()["is_holdout"] is False
    bm1_rows = [json.loads(l) for l in (datasets_dir / "bench_s1_reg.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert bm1_rows[0]["is_holdout"] is False

    # 3. s2 未进过训练集：切题导出默认 is_holdout=True，若开启 reserve_for_training 则为 False
    r_bm2_hold = client.post(
        "/api/v1/flywheel/samples/item6_bench_first/export-benchmark",
        json={"dataset_id": "bench_s2_hold", "name": "s2留出评测集", "mode": "create"},
    )
    assert r_bm2_hold.status_code == 200 and r_bm2_hold.json()["is_holdout"] is True
    r_bm2_resv = client.post(
        "/api/v1/flywheel/samples/item6_bench_first/export-benchmark",
        json={
            "dataset_id": "bench_s2_reserved",
            "name": "s2预留普通评测集",
            "mode": "create",
            "reserve_for_training": True,
        },
    )
    assert r_bm2_resv.status_code == 200 and r_bm2_resv.json()["is_holdout"] is False

    # 4. s2 默认拦截进训练集；显式放行 exclude_benchmark_exported=False 后：
    #    不仅记入 s2 的训练集留底，还把 bench_s2_hold.jsonl 中的题目 is_holdout 从 True 改写为 False！
    r_blk = client.post(
        "/api/v1/flywheel/samples/export-selected",
        json={"sample_ids": ["item6_bench_first"], "version_tag": "blk"},
    )
    assert r_blk.status_code == 400

    r_allow = client.post(
        "/api/v1/flywheel/samples/export-selected",
        json={
            "sample_ids": ["item6_bench_first"],
            "version_tag": "allow_i6",
            "target_dataset_id": "train_ds_s2_leak",
            "exclude_benchmark_exported": False,
        },
    )
    assert r_allow.status_code == 200
    bm2_after_rows = [json.loads(l) for l in (datasets_dir / "bench_s2_hold.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert bm2_after_rows[0]["is_holdout"] is False, "已显式进训练集，原留出题文件必须改写为普通题"

    # 5. 删除训练集：清理 s2 的训练集留底，但不会把 bench_s2_hold.jsonl 改回留出题
    r_del_tr = client.delete("/api/v1/datasets/train_ds_s2_leak")
    assert r_del_tr.status_code == 200
    after_del_tr = {x["id"]: x for x in client.get("/api/v1/flywheel/samples").json()}
    assert "train_ds_s2_leak" not in after_del_tr["item6_bench_first"]["exported_training_datasets"]
    bm2_still_reg = [json.loads(l) for l in (datasets_dir / "bench_s2_hold.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert bm2_still_reg[0]["is_holdout"] is False

    # 6. 同名 overwrite 导入外部题覆盖 bench_s2_hold，及删除 bench_s2_reserved -> 清理 s2 的评测集留底
    r_ovw = client.post(
        "/api/v1/datasets/import",
        json={
            "dataset_id": "bench_s2_hold",
            "dataset_type": "benchmark",
            "mode": "overwrite",
            "samples": [{"id": "ext_q", "messages": [{"role": "user", "content": "外部题"}], "reference": "外部答案"}],
        },
    )
    assert r_ovw.status_code == 200
    assert client.delete("/api/v1/datasets/bench_s2_reserved").status_code == 200
    after_bm_clean = {x["id"]: x for x in client.get("/api/v1/flywheel/samples").json()}
    assert after_bm_clean["item6_bench_first"]["exported_benchmark_datasets"] == []
    assert after_bm_clean["item6_bench_first"]["exported_to_benchmark"] is False


# ---------------------------------------------------------------------------
# 7. 样本池三结论与生效版本解耦 + 规则变更降级 + 人工放行校验 + 按生效版导出
# ---------------------------------------------------------------------------
def test_item7_decoupled_verdicts_rule_invalidation_and_active_version_export(
    isolated_env, monkeypatch: pytest.MonkeyPatch
):
    client: TestClient = isolated_env["client"]
    Session = isolated_env["SessionLocal"]

    # 创建规则 v1（时间戳 t1=1000）与另一个规则 v2
    db = Session()
    rule_base = json.loads(
        (pathlib.Path(settings.RULES_DIR) / "df_test_rule.json").read_text(encoding="utf-8")
    )
    db.add(
        RuleConfigRecord(
            rule_id="df_test_rule",
            project_name="规则V1",
            description="",
            content_json=json.dumps(rule_base, ensure_ascii=False),
            created_at=1000,
            updated_at=1000,
        )
    )
    rule_alt = dict(rule_base)
    rule_alt["rule_id"] = "df_alt_rule"
    rule_alt["project_name"] = "替代规则V2"
    db.add(
        RuleConfigRecord(
            rule_id="df_alt_rule",
            project_name="替代规则V2",
            description="",
            content_json=json.dumps(rule_alt, ensure_ascii=False),
            created_at=2000,
            updated_at=2000,
        )
    )

    s_repaired = FlywheelSampleRecord(
        id="i7_repaired",
        task_id="t7",
        rule_id="df_test_rule",
        scenario_id="sc_default",
        status="REPAIRED_PASS",
        raw_verdict="FAIL",
        remediated_verdict="PASS",
        manual_decision="NONE",
        active_version="remediated",
        evaluated_rule_updated_at=1000,
        score=94,
        raw_messages_json=json.dumps(
            [
                {"role": "system", "content": "你是专业客服专员。"},
                {"role": "user", "content": "怎么查保修"},
                {"role": "assistant", "content": "不知道"},
            ],
            ensure_ascii=False,
        ),
        remediated_messages_json=json.dumps(
            [
                {"role": "system", "content": "你是专业客服专员。"},
                {"role": "user", "content": "怎么查保修"},
                {"role": "assistant", "content": "您好，官方小程序可查保修期。"},
            ],
            ensure_ascii=False,
        ),
        initial_eval_json=json.dumps({"score": 50, "verdict": "FAIL", "deductions": [{"item": "引导", "points": 50, "reason": "态度"}]}, ensure_ascii=False),
        verified_eval_json=json.dumps({"score": 94, "verdict": "PASS", "deductions": []}, ensure_ascii=False),
        updated_at=1000,
    )
    s_manual = FlywheelSampleRecord(
        id="i7_manual_raw",
        task_id="t7",
        rule_id="df_test_rule",
        scenario_id="sc_default",
        status="MANUAL_APPROVED",
        raw_verdict="FAIL",
        remediated_verdict="UNVERIFIED",
        manual_decision="APPROVED",
        active_version="raw",
        evaluated_rule_updated_at=1000,
        score=75,
        reviewer_comment="原对话实际可用",
        raw_messages_json=json.dumps(
            [
                {"role": "system", "content": "你是专业客服专员。"},
                {"role": "user", "content": "设备怎么重启"},
                {"role": "assistant", "content": "长按电源键十秒即可重启。"},
            ],
            ensure_ascii=False,
        ),
        remediated_messages_json=json.dumps(
            [
                {"role": "system", "content": "你是专业客服专员。"},
                {"role": "user", "content": "设备怎么重启"},
                {"role": "assistant", "content": "未经二次验收的修复草稿话术"},
            ],
            ensure_ascii=False,
        ),
        initial_eval_json=json.dumps({"score": 75, "verdict": "FAIL", "deductions": [{"item": "礼貌", "points": 25, "reason": "略简短"}]}, ensure_ascii=False),
        updated_at=1000,
    )
    s_unjudged = FlywheelSampleRecord(
        id="i7_unjudged",
        task_id="t7",
        rule_id="df_test_rule",
        scenario_id="sc_default",
        status="PENDING",
        raw_verdict="UNJUDGED",
        remediated_verdict="NONE",
        manual_decision="NONE",
        active_version="raw",
        score=0,
        raw_messages_json=json.dumps(
            [{"role": "user", "content": "未质检问题"}, {"role": "assistant", "content": "未质检回复"}],
            ensure_ascii=False,
        ),
        updated_at=1000,
    )
    s_error = FlywheelSampleRecord(
        id="i7_err",
        task_id="t7",
        rule_id="df_test_rule",
        scenario_id="sc_default",
        status="PENDING",
        raw_verdict="ERROR",
        remediated_verdict="NONE",
        manual_decision="NONE",
        active_version="raw",
        score=0,
        raw_messages_json=json.dumps(
            [{"role": "user", "content": "报错问题"}, {"role": "assistant", "content": "报错可用回复"}],
            ensure_ascii=False,
        ),
        initial_eval_json=json.dumps(
            {"score": 0, "verdict": "FAIL", "detected_defects": ["质检裁判模型调用异常: timeout"]},
            ensure_ascii=False,
        ),
        updated_at=1000,
    )
    db.add_all([s_repaired, s_manual, s_unjudged, s_error])
    db.commit()
    db.close()

    # 1. 验证按 active_version 导出：i7_manual_raw 有未验收修复草稿但 active_version='raw' -> 导出必须取 raw_messages！
    r_exp_raw = client.post(
        "/api/v1/flywheel/samples/export-selected",
        json={"sample_ids": ["i7_manual_raw"], "version_tag": "active_raw", "target_dataset_id": "ds_i7_raw"},
    )
    assert r_exp_raw.status_code == 200
    exported_txt = pathlib.Path(r_exp_raw.json()["file_path"]).read_text(encoding="utf-8")
    assert "长按电源键十秒即可重启" in exported_txt
    assert "未经二次验收的修复草稿话术" not in exported_txt

    # 2. batch-status 必须拦截无有效质检（UNJUDGED / ERROR）的人工放行
    assert (
        client.post(
            "/api/v1/flywheel/samples/batch-status",
            json={"sample_ids": ["i7_unjudged"], "status": "MANUAL_APPROVED"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/flywheel/samples/batch-status",
            json={"sample_ids": ["i7_err"], "status": "MANUAL_APPROVED"},
        ).status_code
        == 400
    )
    # 单条 PUT /samples/{id} 填写 reviewer_comment 后允许对质检报错记录人工兜底放行，仍拦截纯未质检记录
    assert (
        client.put(
            "/api/v1/flywheel/samples/i7_unjudged",
            json={"status": "MANUAL_APPROVED", "reviewer_comment": "还没质检也想放行"},
        ).status_code
        == 400
    )
    r_err_ok = client.put(
        "/api/v1/flywheel/samples/i7_err",
        json={"status": "MANUAL_APPROVED", "active_version": "raw", "reviewer_comment": "模型超时但人工复核话术可用"},
    )
    assert r_err_ok.status_code == 200 and r_err_ok.json()["status"] == "MANUAL_APPROVED"

    # 3. 在同一未变更规则(df_test_rule, ts=1000)下重检 raw 且判 FAIL -> REPAIRED_PASS 与 MANUAL_APPROVED 不被冲掉！
    def _fail_judge(self, r, sc, dialogue):
        return InspectionReport(
            score=45,
            verdict="FAIL",
            detected_defects=["[红线] -55分: 原对话不达标"],
            deductions=[{"turn": 2, "item": "红线", "points": 55, "reason": "不达标"}],
        )

    monkeypatch.setattr(UniversalJudgeEngine, "evaluate_dialogue", _fail_judge)
    r_same_rule = client.post(
        "/api/v1/flywheel/samples/batch-judge",
        json={"sample_ids": ["i7_repaired", "i7_manual_raw"], "rule_id": "df_test_rule", "judge_model_id": "mock-judge"},
    )
    assert r_same_rule.status_code == 200
    res_same = {r["sample_id"]: r for r in r_same_rule.json()["results"]}
    assert res_same["i7_repaired"]["new_status"] == "REPAIRED_PASS"
    assert res_same["i7_repaired"]["remediated_verdict"] == "PASS"
    assert res_same["i7_manual_raw"]["new_status"] == "MANUAL_APPROVED"
    assert res_same["i7_manual_raw"]["manual_decision"] == "APPROVED"

    # 4. 切换规则(df_alt_rule)或更新规则(updated_at 变新)后重检 raw 仍 FAIL：
    #    - i7_repaired 的 remediated_verdict 降为 UNVERIFIED（修复文本保留），状态变为 PENDING；
    #    - i7_manual_raw 的 manual_decision 降为 NONE（备注保留），状态变为 PENDING；
    r_new_rule = client.post(
        "/api/v1/flywheel/samples/batch-judge",
        json={"sample_ids": ["i7_repaired", "i7_manual_raw"], "rule_id": "df_alt_rule", "judge_model_id": "mock-judge"},
    )
    assert r_new_rule.status_code == 200
    res_new = {r["sample_id"]: r for r in r_new_rule.json()["results"]}
    assert res_new["i7_repaired"]["rule_changed"] is True
    assert res_new["i7_repaired"]["remediated_verdict"] == "UNVERIFIED"
    assert res_new["i7_repaired"]["new_status"] == "PENDING"
    assert res_new["i7_manual_raw"]["manual_decision"] == "NONE"
    assert res_new["i7_manual_raw"]["new_status"] == "PENDING"

    samples_now = {x["id"]: x for x in client.get("/api/v1/flywheel/samples").json()}
    assert samples_now["i7_repaired"]["remediated_messages"] is not None
    assert samples_now["i7_manual_raw"]["reviewer_comment"] == "原对话实际可用"

    # 5. 对 i7_repaired 修复版执行 target_data='remediated' 二次验收通过 -> 恢复 REPAIRED_PASS
    monkeypatch.setattr(
        UniversalJudgeEngine,
        "evaluate_dialogue",
        lambda self, r, sc, d: InspectionReport(score=96, verdict="PASS", detected_defects=[], deductions=[]),
    )
    r_ver_pass = client.post(
        "/api/v1/flywheel/samples/i7_repaired/judge",
        json={"rule_id": "df_alt_rule", "judge_model_id": "mock-judge", "target_data": "remediated"},
    )
    assert r_ver_pass.status_code == 200
    assert r_ver_pass.json()["status"] == "REPAIRED_PASS"
    assert r_ver_pass.json()["active_version"] == "remediated"

    # 6. 对原对话在新规则下直接达标 -> active_version 切回 raw，状态 GOLD_PASS；导出仅取 raw_messages
    r_raw_pass = client.post(
        "/api/v1/flywheel/samples/i7_manual_raw/judge",
        json={"rule_id": "df_alt_rule", "judge_model_id": "mock-judge", "target_data": "raw"},
    )
    assert r_raw_pass.status_code == 200
    assert r_raw_pass.json()["status"] == "GOLD_PASS"
    assert r_raw_pass.json()["active_version"] == "raw"


# ---------------------------------------------------------------------------
# 6 & 7 存量迁移测试：旧表结构缺列 + 历史训练集/评测集扫描与冲突留出改普通题
# ---------------------------------------------------------------------------
def test_items6_and_7_init_db_existing_data_migration(isolated_env):
    datasets_dir: pathlib.Path = isolated_env["datasets_dir"]
    Session = isolated_env["SessionLocal"]

    # 准备旧式存量样本（未写入新增解耦字段）
    raw_holdout_msgs = [
        {"role": "system", "content": "你是客服"},
        {"role": "user", "content": "先留出再进训练的客户问题"},
        {"role": "assistant", "content": "标准修复回答"},
    ]
    raw_untrained_msgs = [
        {"role": "system", "content": "你是客服"},
        {"role": "user", "content": "未导出训练集客户问题"},
        {"role": "assistant", "content": "回答"},
    ]
    db = Session()
    db.add_all(
        [
            FlywheelSampleRecord(
                id="mig_gold",
                rule_id="df_test_rule",
                scenario_id="sc_default",
                status="GOLD_PASS",
                score=96,
                raw_messages_json=json.dumps(raw_untrained_msgs, ensure_ascii=False),
                initial_eval_json=json.dumps({"score": 96, "verdict": "PASS"}, ensure_ascii=False),
                updated_at=100,
            ),
            FlywheelSampleRecord(
                id="mig_conflict",
                rule_id="df_test_rule",
                scenario_id="sc_default",
                status="REPAIRED_PASS",
                score=92,
                raw_messages_json=json.dumps(raw_holdout_msgs, ensure_ascii=False),
                remediated_messages_json=json.dumps(raw_holdout_msgs, ensure_ascii=False),
                initial_eval_json=json.dumps({"score": 50, "verdict": "FAIL"}, ensure_ascii=False),
                verified_eval_json=json.dumps({"score": 92, "verdict": "PASS"}, ensure_ascii=False),
                exported_to_benchmark=True,
                updated_at=100,
            ),
            FlywheelSampleRecord(
                id="mig_err_pending",
                rule_id="df_test_rule",
                scenario_id="sc_default",
                status="PENDING",
                score=0,
                raw_messages_json=json.dumps(raw_untrained_msgs, ensure_ascii=False),
                initial_eval_json=json.dumps(
                    {"score": 0, "verdict": "FAIL", "detected_defects": ["质检裁判模型调用异常: 502"]},
                    ensure_ascii=False,
                ),
                updated_at=100,
            ),
            DatasetRecord(
                id="mig_bench_file",
                name="mig_bench_file.jsonl",
                file_path=str(datasets_dir / "mig_bench_file.jsonl"),
                sample_count=1,
                dataset_type="benchmark",
            ),
            DatasetRecord(
                id="mig_train_file",
                name="mig_train_file.jsonl",
                file_path=str(datasets_dir / "mig_train_file.jsonl"),
                sample_count=1,
                dataset_type="flywheel_gold",
            ),
            DatasetRecord(
                id="mig_raw_uninspected",
                name="mig_raw_uninspected.jsonl",
                file_path=str(datasets_dir / "mig_raw_uninspected.jsonl"),
                sample_count=1,
                dataset_type="flywheel_gold",
            ),
        ]
    )
    db.commit()
    db.close()

    # 写入历史文件：mig_conflict 同时出现在历史评测集(is_holdout=True) 和 历史已达标训练集中；
    # mig_gold 仅出现在无 status 的“未质检原始对话语料”中（不得误记为已进训练集！）
    _write_jsonl(
        datasets_dir / "mig_bench_file.jsonl",
        [
            {
                "id": "sl_mig_1",
                "source": "flywheel_mig_conflict",
                "is_holdout": True,
                "messages": [{"role": "user", "content": "先留出再进训练的客户问题"}],
                "reference": "标准修复回答",
                "metadata": {"source_sample_id": "mig_conflict"},
            }
        ],
    )
    _write_jsonl(
        datasets_dir / "mig_train_file.jsonl",
        [
            {
                "id": "mig_conflict",
                "metadata": {"rule_id": "df_test_rule", "status": "REPAIRED_PASS"},
                "messages": raw_holdout_msgs,
            }
        ],
    )
    _write_jsonl(
        datasets_dir / "mig_raw_uninspected.jsonl",
        [{"id": "mig_gold", "messages": raw_untrained_msgs}],
    )

    # 执行幂等 init_db() 迁移（跑两次验证幂等性）
    init_db()
    init_db()

    db = Session()
    try:
        g = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == "mig_gold").first()
        c = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == "mig_conflict").first()
        e = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == "mig_err_pending").first()

        assert g.raw_verdict == "PASS" and g.active_version == "raw"
        assert json.loads(g.exported_training_datasets_json) == []  # 未质检原语料不计留底

        assert c.raw_verdict == "FAIL" and c.remediated_verdict == "PASS" and c.active_version == "remediated"
        assert json.loads(c.exported_training_datasets_json) == ["mig_train_file"]
        assert json.loads(c.exported_benchmark_datasets_json) == ["mig_bench_file"]

        assert e.raw_verdict == "ERROR" and e.status == "PENDING"
    finally:
        db.close()

    # 历史冲突评测题必须已被自动修正为普通题 is_holdout=False
    fixed_bench_row = json.loads((datasets_dir / "mig_bench_file.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert fixed_bench_row["is_holdout"] is False
