# -*- coding: utf-8 -*-
"""
动态阶梯降频抽取与复测战绩治理回归测试 (v2.1 流水账本 + 留出隔离 + 分层折算)
"""
import os
import json
import uuid
import tempfile
import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.models import SessionLocal, init_db, BenchmarkRunLedger, FlywheelSampleRecord
from backend.app.schemas.benchmark import (
    BenchmarkSample,
    SampleEvalTracker,
    SamplingFilterConfig,
    SampleEvalResult,
    BenchmarkSummary,
    ModelEvalConfig,
)
from backend.app.schemas.chat import ChatMessage, UnifiedChatResponse
from backend.app.engine.benchmark_sampler import BenchmarkSamplingEngine
from backend.app.engine.benchmark_runner import BenchmarkRunner


def test_target_interval_and_holdout_constraint():
    """测试单题目标复测间隔计算: 留出题与普通题均支持学会阶梯降频与遗忘唤醒 (与 is_holdout 解耦)"""
    t0 = SampleEvalTracker(consecutive_pass=0)
    assert BenchmarkSamplingEngine.compute_target_interval(t0, x=3, s=1, n=5, is_holdout=True) == 1
    assert BenchmarkSamplingEngine.compute_target_interval(t0, x=3, s=1, n=5, is_holdout=False) == 1

    t2 = SampleEvalTracker(consecutive_pass=2)
    assert BenchmarkSamplingEngine.compute_target_interval(t2, x=3, s=1, n=5, is_holdout=True) == 1
    assert BenchmarkSamplingEngine.compute_target_interval(t2, x=3, s=1, n=5, is_holdout=False) == 1

    t3 = SampleEvalTracker(consecutive_pass=3)
    assert BenchmarkSamplingEngine.compute_target_interval(t3, x=3, s=1, n=5, is_holdout=True) == 2
    assert BenchmarkSamplingEngine.compute_target_interval(t3, x=3, s=1, n=5, is_holdout=False) == 2

    t4 = SampleEvalTracker(consecutive_pass=4)
    assert BenchmarkSamplingEngine.compute_target_interval(t4, x=3, s=1, n=5, is_holdout=True) == 3
    assert BenchmarkSamplingEngine.compute_target_interval(t4, x=3, s=1, n=5, is_holdout=False) == 3

    t6 = SampleEvalTracker(consecutive_pass=6)
    assert BenchmarkSamplingEngine.compute_target_interval(t6, x=3, s=1, n=5, is_holdout=True) == 5

    t10 = SampleEvalTracker(consecutive_pass=10)
    assert BenchmarkSamplingEngine.compute_target_interval(t10, x=3, s=1, n=5, is_holdout=True) == 5
    # 普通题 (is_holdout=False) 连续通过同样可以学会并降频，绝非每轮必出
    assert BenchmarkSamplingEngine.compute_target_interval(t10, x=3, s=1, n=5, is_holdout=False) == 5

    # 被同组连坐唤醒 (force_due=True) 强制 I=1
    t_due = SampleEvalTracker(consecutive_pass=10, force_due=True)
    assert BenchmarkSamplingEngine.compute_target_interval(t_due, x=3, s=1, n=5, is_holdout=True) == 1
    assert BenchmarkSamplingEngine.compute_target_interval(t_due, x=3, s=1, n=5, is_holdout=False) == 1


def test_partition_samples_three_pools_and_steady_quota():
    """测试三池分流、封顶保底、留出与普通题均支持降频过滤及稳态配额精准收敛 (100题 I=5 -> 精准抽出20题)"""
    # 1. 混合池测试
    samples = []
    # 3道攻坚题 (c=0, 含留出题与普通题)
    for i in range(1, 4):
        samples.append(
            BenchmarkSample(
                id=f"q_active_{i}",
                is_holdout=(i != 3),
                messages=[ChatMessage(role="user", content=f"攻坚问题 {i}")],
                eval_tracker=SampleEvalTracker(consecutive_pass=0, miss_streak=0),
            )
        )
    # 1道普通题触顶熟题 (is_holdout=False, c=6, m=4 -> m+1=5 >= N=5，普通题同样进熟题池并支持触顶保底)
    samples.append(
        BenchmarkSample(
            id="q_non_holdout_hardcap",
            is_holdout=False,
            messages=[ChatMessage(role="user", content="普通触顶熟题")],
            eval_tracker=SampleEvalTracker(consecutive_pass=6, miss_streak=4),
        )
    )
    # 1道留出题触顶熟题 (is_holdout=True, c=6, m=4 -> m+1=5 >= N=5)
    samples.append(
        BenchmarkSample(
            id="q_hardcap_4",
            is_holdout=True,
            messages=[ChatMessage(role="user", content="留出触顶熟题")],
            eval_tracker=SampleEvalTracker(consecutive_pass=6, miss_streak=4),
        )
    )
    # 5道常规熟题 (含留出题与普通题, c=3 -> I=2，并非每轮必出)
    for i in range(5, 10):
        samples.append(
            BenchmarkSample(
                id=f"q_cooldown_{i}",
                is_holdout=(i % 2 == 1),
                messages=[ChatMessage(role="user", content=f"冷却熟题 {i}")],
                eval_tracker=SampleEvalTracker(consecutive_pass=3, miss_streak=(i % 2)),
            )
        )

    config = SamplingFilterConfig(enabled=True, pass_threshold_x=3, interval_step_s=1, max_interval_n=5)
    selected, skipped, stats = BenchmarkSamplingEngine.partition_samples(samples, config)

    selected_ids = {s.id for s in selected}
    skipped_ids = {s.id for s in skipped}
    assert len(selected_ids & skipped_ids) == 0
    assert len(selected) + len(skipped) == 10
    for i in range(1, 4):
        assert f"q_active_{i}" in selected_ids
    assert "q_non_holdout_hardcap" in selected_ids
    assert "q_hardcap_4" in selected_ids
    assert stats["active_pool_count"] == 3
    assert stats["hard_cap_pool_count"] == 2
    assert stats["cooldown_pool_total"] == 7
    assert stats["holdout_count"] == 6
    assert stats["non_holdout_count"] == 4
    # 验证普通题 (is_holdout=False) 在常规熟题池中可以被正常休眠跳过 (不是每轮必出)
    skipped_non_holdout = [s.id for s in skipped if not s.is_holdout]
    assert len(skipped_non_holdout) >= 1

    # 验证评测题型过滤 (holdout_filter)
    cfg_holdout_only = SamplingFilterConfig(enabled=True, pass_threshold_x=3, holdout_filter="holdout_only")
    sel_h, skp_h, st_h = BenchmarkSamplingEngine.partition_samples(samples, cfg_holdout_only)
    assert st_h["total_samples"] == 6
    assert all(s.is_holdout for s in sel_h + skp_h)

    cfg_regular_only = SamplingFilterConfig(enabled=True, pass_threshold_x=3, holdout_filter="non_holdout_only")
    sel_r, skp_r, st_r = BenchmarkSamplingEngine.partition_samples(samples, cfg_regular_only)
    assert st_r["total_samples"] == 4
    assert all(not s.is_holdout for s in sel_r + skp_r)

    # 2. 稳态配额精确度验证: 100道全熟题 I=5 -> 精确抽 20 题
    steady_samples = [
        BenchmarkSample(
            id=f"steady_{i}",
            is_holdout=(i % 2 == 0),
            messages=[ChatMessage(role="user", content=f"稳态题 {i}")],
            eval_tracker=SampleEvalTracker(consecutive_pass=6, miss_streak=(i % 4)),
        )
        for i in range(100)
    ]
    sel_steady, skip_steady, st_steady = BenchmarkSamplingEngine.partition_samples(steady_samples, config)
    assert len(sel_steady) == 20
    assert len(skip_steady) == 80
    assert st_steady["saved_compute_ratio"] == 80.0


def test_ledger_aggregation_version_set_rerun_fingerprint_and_revoke():
    """测试 SQLite 流水账本: .jsonl 只读不改、不重复版本集合、同版本快照复用、单题指纹变更重置、撤销回滚"""
    init_db()
    db = SessionLocal()
    unique_tag = uuid.uuid4().hex[:8]
    model_line_a = f"qwen_test_{unique_tag}"
    model_line_b = f"deepseek_test_{unique_tag}"

    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False, suffix=".jsonl") as f:
        temp_path = f.name
        samples_raw = [
            {"id": "s1", "is_holdout": True, "group_key": "G_A", "messages": [{"role": "user", "content": "问题1"}]},
            {"id": "s2", "is_holdout": True, "group_key": "G_A", "messages": [{"role": "user", "content": "问题2"}]},
            {"id": "s3", "is_holdout": True, "group_key": "G_B", "messages": [{"role": "user", "content": "问题3"}]},
            {"id": "s4", "is_holdout": False, "group_key": "G_B", "messages": [{"role": "user", "content": "非留出4"}]},
        ]
        for row in samples_raw:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    try:
        with open(temp_path, "r", encoding="utf-8") as rf:
            orig_file_content = rf.read()

        samples = [BenchmarkSample(**row) for row in samples_raw]
        dummy_resp = UnifiedChatResponse(content="OK", thinking="")

        def _make_res(sid: str, mid: str, status: str = "PASS"):
            return SampleEvalResult(
                sample_id=sid,
                category="test",
                model_id=mid,
                response=dummy_resp,
                objective_passed=(status == "PASS"),
                status=status,
            )

        # 1. 只读摸底模式 (model_line 为空): 绝不写入流水，也不改动 .jsonl
        ro_cfg = SamplingFilterConfig(enabled=True, model_line=None, version_tag=None)
        lid_ro = BenchmarkSamplingEngine.record_run_ledger(
            db=db,
            task_id=f"task_ro_{unique_tag}",
            dataset_path=temp_path,
            config=ro_cfg,
            all_samples=samples,
            selected_samples=samples,
            skipped_samples=[],
            results_by_sample_id={"s1": [_make_res("s1", "m_a", "PASS")]},
        )
        assert lid_ro is None

        # 2. 连续跑 3 次相同版本 v1.0 -> passed_versions 去重后 c 应为 1，绝不能刷成 3!
        for idx in range(3):
            cfg_v1 = SamplingFilterConfig(
                enabled=True,
                pass_threshold_x=3,
                model_line=model_line_a,
                version_tag="v1.0",
                benchmark_model_id="m_a",
            )
            sel, skp, st = BenchmarkSamplingEngine.partition_samples(samples, cfg_v1, db=db, dataset_path=temp_path)
            if idx > 0:
                # 第 2、3 次跑 v1.0 命中快照复用
                assert st["is_rerun_same_version"] is True
            res_map = {s.id: [_make_res(s.id, "m_a", "PASS")] for s in sel}
            BenchmarkSamplingEngine.record_run_ledger(
                db=db,
                task_id=f"task_v1_{idx}_{unique_tag}",
                dataset_path=temp_path,
                config=cfg_v1,
                all_samples=samples,
                selected_samples=sel,
                skipped_samples=skp,
                results_by_sample_id=res_map,
            )

        tr_after_v1 = BenchmarkSamplingEngine.aggregate_trackers_from_ledgers(
            db, temp_path, model_line_a, samples, pass_threshold_x=3
        )
        assert tr_after_v1["s1"].consecutive_pass == 1
        assert tr_after_v1["s1"].passed_versions == ["v1.0"]

        # 3. 再通过两个不同版本 v1.1 与 v1.2 -> c=3 达标进入降频池 (I=2)
        for v_tag in ("v1.1", "v1.2"):
            cfg_v = SamplingFilterConfig(
                enabled=True,
                pass_threshold_x=3,
                model_line=model_line_a,
                version_tag=v_tag,
                benchmark_model_id="m_a",
            )
            sel, skp, st = BenchmarkSamplingEngine.partition_samples(samples, cfg_v, db=db, dataset_path=temp_path)
            assert st["is_rerun_same_version"] is False
            res_map = {s.id: [_make_res(s.id, "m_a", "PASS")] for s in sel}
            BenchmarkSamplingEngine.record_run_ledger(
                db=db,
                task_id=f"task_{v_tag}_{unique_tag}",
                dataset_path=temp_path,
                config=cfg_v,
                all_samples=samples,
                selected_samples=sel,
                skipped_samples=skp,
                results_by_sample_id=res_map,
            )

        tr_after_v12 = BenchmarkSamplingEngine.aggregate_trackers_from_ledgers(
            db, temp_path, model_line_a, samples, pass_threshold_x=3
        )
        assert tr_after_v12["s1"].consecutive_pass == 3
        assert tr_after_v12["s1"].target_interval == 2
        assert tr_after_v12["s4"].consecutive_pass == 3
        assert tr_after_v12["s4"].target_interval == 2  # 普通题 (is_holdout=False) 同样可以学会进入降频池 I=2

        # 模型线 B 完全隔离不受 A 影响
        tr_line_b = BenchmarkSamplingEngine.aggregate_trackers_from_ledgers(
            db, temp_path, model_line_b, samples, pass_threshold_x=3
        )
        assert tr_line_b["s1"].consecutive_pass == 0

        # 4. v1.3 模拟: s1 被抽中且 FAIL，同时开启可选 group_key 连坐唤醒 -> 同组 G_A 的 s2 若在休眠应被标记 force_due=True
        cfg_v13 = SamplingFilterConfig(
            enabled=True,
            pass_threshold_x=3,
            model_line=model_line_a,
            version_tag="v1.3",
            benchmark_model_id="m_a",
            enable_group_regression_wakeup=True,
        )
        # 手动构造 s1 实测 FAIL, s2 休眠
        BenchmarkSamplingEngine.record_run_ledger(
            db=db,
            task_id=f"task_v13_mistake_{unique_tag}",
            dataset_path=temp_path,
            config=cfg_v13,
            all_samples=samples,
            selected_samples=[samples[0], samples[3]],
            skipped_samples=[samples[1], samples[2]],
            results_by_sample_id={
                "s1": [_make_res("s1", "m_a", "FAIL")],
                "s4": [_make_res("s4", "m_a", "PASS")],
            },
            cooldown_sample_ids={"s1"},
        )
        tr_after_fail = BenchmarkSamplingEngine.aggregate_trackers_from_ledgers(
            db, temp_path, model_line_a, samples, pass_threshold_x=3, enable_group_regression_wakeup=True
        )
        assert tr_after_fail["s1"].consecutive_pass == 0
        assert tr_after_fail["s2"].force_due is True
        assert tr_after_fail["s2"].target_interval == 1

        # 5. 一键撤销 task_v13_mistake -> s1 与 s2 的 force_due 完整回滚到 v1.2 状态!
        revoked = BenchmarkSamplingEngine.revoke_ledger_by_task(db, f"task_v13_mistake_{unique_tag}")
        assert revoked == 1
        tr_reverted = BenchmarkSamplingEngine.aggregate_trackers_from_ledgers(
            db, temp_path, model_line_a, samples, pass_threshold_x=3, enable_group_regression_wakeup=True
        )
        assert tr_reverted["s1"].consecutive_pass == 3
        assert tr_reverted["s2"].force_due is False

        # 6. 单题内容指纹变更: 修改 s1 题目内容，仅 s1 归零，s2/s3 不受任何影响
        modified_samples = [s.model_copy(deep=True) for s in samples]
        modified_samples[0].messages = [ChatMessage(role="user", content="问题1（已修订提示词）")]
        tr_fp = BenchmarkSamplingEngine.aggregate_trackers_from_ledgers(
            db, temp_path, model_line_a, modified_samples, pass_threshold_x=3
        )
        assert tr_fp["s1"].consecutive_pass == 0
        assert tr_fp["s2"].consecutive_pass == 3

        # 7. 验证原始 .jsonl 文件全程字节级零篡改
        with open(temp_path, "r", encoding="utf-8") as rf:
            assert rf.read() == orig_file_content

    finally:
        norm_p = BenchmarkSamplingEngine.normalize_dataset_path(temp_path)
        db.query(BenchmarkRunLedger).filter(BenchmarkRunLedger.dataset_path == norm_p).delete()
        db.commit()
        db.close()
        if os.path.exists(temp_path):
            os.remove(temp_path)


def test_estimated_full_pass_rate_r_cooldown_and_isolation():
    """测试分层折算等效通过率及评测报告按留出题 (Holdout) 与普通题 (Regular) 区分通过率"""
    runner = BenchmarkRunner()
    # 6道留出题 (s_0..s_5, 其中 s_0 必败 -> 留出题实测通过率 5/6 = 0.8333)
    # 4道普通题 (s_6..s_9, 其中 s_6, s_7 必败 -> 普通题实测通过率 2/4 = 0.5)
    # 总实测通过 7/10 = 0.7
    samples = [
        BenchmarkSample(
            id=f"s_{i}",
            is_holdout=(i < 6),
            messages=[ChatMessage(role="user", content="test")],
            regex_constraints=["MOCK_MUST_FAIL"] if i in (0, 6, 7) else [],
        )
        for i in range(10)
    ]
    # 假设 s_0, s_1, s_2 (留出) 与 s_8 (普通) 共 4 道属于抽中复测的熟题 (s_0 必败, 其余 3 道必过 -> R_cooldown = 3/4 = 0.75)
    cooldown_ids = {"s_0", "s_1", "s_2", "s_8"}
    # 全集 50 题, 跳过 40 题 (留出休眠 24 题, 普通休眠 16 题), 实测 10 题 (7 过, 3 败)
    # 期望全集等效 = (7 + 40 * 0.75) / 50 = 37 / 50 = 0.74
    bm_cfg = ModelEvalConfig(model_id="bm_model", display_name="基准模型", provider_type="mock")
    comp_cfg = ModelEvalConfig(model_id="comp_model", display_name="陪跑模型", provider_type="mock")

    sampling_cfg = SamplingFilterConfig(
        enabled=True,
        model_line="bm_line",
        version_tag="v1.0",
        benchmark_model_id="bm_model",
    )

    matrix = runner.run_multi_model_matrix(
        samples=samples,
        model_configs=[bm_cfg, comp_cfg],
        workers=2,
        dataset_total_samples=50,
        skipped_mastered_samples=40,
        sampling_config=sampling_cfg,
        cooldown_sample_ids=cooldown_ids,
        skipped_holdout_samples=24,
        skipped_regular_samples=16,
    )

    bm_sum = matrix["summaries"]["基准模型"]
    comp_sum = matrix["summaries"]["陪跑模型"]

    assert bm_sum["is_benchmark_model"] is True
    assert bm_sum["pass_rate"] == 0.7
    assert bm_sum["cooldown_tested_samples"] == 4
    assert bm_sum["cooldown_passed_samples"] == 3
    assert bm_sum["cooldown_failed_samples"] == 1
    assert bm_sum["cooldown_retention_rate"] == 0.75
    assert bm_sum["estimated_full_pass_rate"] == 0.74

    # 验证评测报告按留出题与普通题区分通过率
    assert bm_sum["holdout_total_samples"] == 6
    assert bm_sum["holdout_passed_samples"] == 5
    assert bm_sum["holdout_failed_samples"] == 1
    assert bm_sum["holdout_pass_rate"] == 0.8333
    assert bm_sum["regular_total_samples"] == 4
    assert bm_sum["regular_passed_samples"] == 2
    assert bm_sum["regular_failed_samples"] == 2
    assert bm_sum["regular_pass_rate"] == 0.5
    assert bm_sum["holdout_breakdown"]["holdout"]["pass_rate"] == 0.8333
    assert bm_sum["holdout_breakdown"]["regular"]["pass_rate"] == 0.5
    assert matrix["side_by_side"]["s_0"]["is_holdout"] is True
    assert matrix["side_by_side"]["s_6"]["is_holdout"] is False

    # 陪跑模型同样区分展示留出与普通实测通过率，但不计算虚高全集等效分
    assert comp_sum["is_benchmark_model"] is False
    assert comp_sum["holdout_pass_rate"] == 0.8333
    assert comp_sum["regular_pass_rate"] == 0.5
    assert comp_sum["estimated_full_pass_rate"] is None


def test_api_sampling_validation_flywheel_isolation_and_overwrite_reset(tmp_path):
    """测试 REST API 端到端: version_tag 必填校验、飞轮导出源头互斥、覆盖导入自然重置流水"""
    init_db()
    client = TestClient(app)
    unique_tag = uuid.uuid4().hex[:8]

    tmp_ds = tmp_path / "demo_sampling_bench.jsonl"
    tmp_ds.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "samp_1",
                        "category": "通用",
                        "is_holdout": True,
                        "messages": [{"role": "user", "content": "问题1"}],
                        "reference": "答案1",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "id": "samp_2",
                        "category": "通用",
                        "is_holdout": False,
                        "messages": [{"role": "user", "content": "问题2"}],
                        "reference": "答案2",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # 1. 指定了 model_line 但缺失 version_tag 应拦截返回 400
    bad_run_req = {
        "dataset_path": str(tmp_ds),
        "model_ids": ["mock_m"],
        "sampling_config": {
            "enabled": True,
            "model_line": "qwen_line",
            "version_tag": "",
            "benchmark_model_id": "mock_m",
        },
    }
    resp_bad = client.post("/api/v1/benchmarks/run", json=bad_run_req)
    assert resp_bad.status_code == 400

    bad_run_req_2 = {
        "dataset_path": str(tmp_ds),
        "model_ids": ["mock_m"],
        "sampling_config": {
            "enabled": True,
            "benchmark_model_id": "mock_m",
            "version_tag": "",
        },
    }
    resp_bad_2 = client.post("/api/v1/benchmarks/run", json=bad_run_req_2)
    assert resp_bad_2.status_code == 400

    bad_run_req_3 = {
        "dataset_path": str(tmp_ds),
        "model_ids": ["mock_m"],
        "sampling_config": {
            "enabled": True,
            "version_tag": "v1.0",
        },
    }
    resp_bad_3 = client.post("/api/v1/benchmarks/run", json=bad_run_req_3)
    assert resp_bad_3.status_code == 400

    # 2. 试算与重置 API 正常工作
    resp_prev = client.post(
        "/api/v1/benchmarks/preview-sampling",
        json={
            "dataset_path": str(tmp_ds),
            "sampling_config": {"enabled": True, "pass_threshold_x": 3},
        },
    )
    assert resp_prev.status_code == 200, resp_prev.text
    p_data = resp_prev.json()
    assert "holdout_count" in p_data
    assert "non_holdout_count" in p_data

    # 3. 飞轮样本导出 Benchmark 自动锁定源样本 exported_to_benchmark=True，SFT 导出自动排除
    db = SessionLocal()
    sample_id = f"fw_iso_{unique_tag}"
    rule_id = f"rule_iso_{unique_tag}"
    ds_id = f"eval_iso_{unique_tag}"
    try:
        fw_rec = FlywheelSampleRecord(
            id=sample_id,
            rule_id=rule_id,
            scenario_id="sc_1",
            status="REPAIRED_PASS",
            score=95,
            raw_messages_json=json.dumps([
                {"role": "system", "content": "你是客服"},
                {"role": "user", "content": "怎么保修"},
                {"role": "assistant", "content": "出错回复"},
            ], ensure_ascii=False),
            remediated_messages_json=json.dumps([
                {"role": "system", "content": "你是客服"},
                {"role": "user", "content": "怎么保修"},
                {"role": "assistant", "content": "请打开APP申请售后保修"},
            ], ensure_ascii=False),
            applied_actions_json=json.dumps([
                {"tool": "replace", "turn": 2, "text": "请打开APP申请售后保修", "reason": "合规"}
            ], ensure_ascii=False),
            benchmark_slices_json=json.dumps([
                {
                    "slice_id": f"slice_{sample_id}_turn2",
                    "sample_id": sample_id,
                    "target_turn": 2,
                    "defect_item": "合规",
                    "defect_reason": "缺引导",
                    "messages": [
                        {"role": "system", "content": "你是客服"},
                        {"role": "user", "content": "怎么保修"},
                    ],
                    "reference": "请打开APP申请售后保修",
                    "rubric": "合规: 缺引导",
                    "regex_constraints": [],
                    "category": "飞轮实战质检",
                }
            ], ensure_ascii=False),
            initial_eval_json=json.dumps({"deductions": [{"turn": 2, "item": "合规", "reason": "缺引导"}]}),
        )
        db.add(fw_rec)
        db.commit()

        # 导出为 Benchmark
        exp_resp = client.post(
            f"/api/v1/flywheel/samples/{sample_id}/export-benchmark",
            json={"dataset_id": ds_id, "name": "隔离测试集", "mode": "create"},
        )
        assert exp_resp.status_code == 200, exp_resp.text

        # 验证生成的 jsonl 带 is_holdout=True
        ds_file = exp_resp.json()["file_path"]
        with open(ds_file, "r", encoding="utf-8") as rf:
            rows = [json.loads(line) for line in rf if line.strip()]
        assert len(rows) >= 1
        assert rows[0]["is_holdout"] is True

        # 验证再导出 SFT 黄金训练集时默认排除该已进评测集样本 (无样本可导出 -> 400)
        gold_resp = client.post(
            "/api/v1/flywheel/export",
            params={"rule_id": rule_id, "exclude_benchmark_exported": True},
        )
        assert gold_resp.status_code == 400

        # 4. 覆盖导入同名数据集自动作废历史流水
        fake_ledger = BenchmarkRunLedger(
            id=f"ldg_{unique_tag}",
            task_id=f"tsk_{unique_tag}",
            dataset_path=BenchmarkSamplingEngine.normalize_dataset_path(ds_file),
            model_line="line_x",
            version_tag="v1",
            sampled_ids_json="[]",
            records_json="[]",
            is_revoked=False,
        )
        db.add(fake_ledger)
        db.commit()

        imp_resp = client.post(
            "/api/v1/datasets/import",
            json={
                "dataset_id": ds_id,
                "dataset_type": "benchmark",
                "mode": "overwrite",
                "mark_as_holdout": True,
                "samples": [
                    {"id": "new_1", "messages": [{"role": "user", "content": "hi"}], "reference": "ok"}
                ],
            },
        )
        assert imp_resp.status_code == 200, imp_resp.text
        db.expire_all()
        reloaded_ledger = db.query(BenchmarkRunLedger).filter(BenchmarkRunLedger.id == f"ldg_{unique_tag}").first()
        assert reloaded_ledger.is_revoked is True
    finally:
        db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).delete()
        db.query(BenchmarkRunLedger).filter(BenchmarkRunLedger.id == f"ldg_{unique_tag}").delete()
        db.commit()
        db.close()
        client.delete(f"/api/v1/datasets/{ds_id}")


def test_matrix_display_name_differs_from_model_id_records_ledger():
    """回归 P0: 当模型 display_name 与注册 model_id 不同时，run_multi_model_matrix 后仍能准确匹配基准模型并写入流水"""
    init_db()
    db = SessionLocal()
    unique_tag = uuid.uuid4().hex[:8]
    ds_path = f"data/datasets/temp_alias_{unique_tag}.jsonl"
    norm_p = BenchmarkSamplingEngine.normalize_dataset_path(ds_path)

    samples = [
        BenchmarkSample(
            id=f"al_{i}",
            is_holdout=True,
            messages=[ChatMessage(role="user", content=f"测试题 {i}")],
        )
        for i in range(3)
    ]
    bm_cfg = ModelEvalConfig(model_id=f"reg_id_{unique_tag}", display_name="通义千问微调中文别名", provider_type="mock")
    other_cfg = ModelEvalConfig(model_id=f"other_{unique_tag}", display_name="陪跑对比模型", provider_type="mock")

    sampling_cfg = SamplingFilterConfig(
        enabled=True,
        pass_threshold_x=2,
        model_line=f"qwen_alias_line_{unique_tag}",
        version_tag="v1.0",
        benchmark_model_id=f"reg_id_{unique_tag}",
    )

    try:
        runner = BenchmarkRunner()
        matrix = runner.run_multi_model_matrix(
            samples=samples,
            model_configs=[bm_cfg, other_cfg],
            workers=2,
            dataset_total_samples=len(samples),
            skipped_mastered_samples=0,
            sampling_config=sampling_cfg,
            cooldown_sample_ids=set(),
        )
        raw_results = matrix.pop("_raw_results_by_sample", {})
        ledger_id = BenchmarkSamplingEngine.record_run_ledger(
            db=db,
            task_id=f"tsk_alias_{unique_tag}",
            dataset_path=ds_path,
            config=sampling_cfg,
            all_samples=samples,
            selected_samples=samples,
            skipped_samples=[],
            results_by_sample_id=raw_results,
        )
        assert ledger_id is not None
        trackers = BenchmarkSamplingEngine.aggregate_trackers_from_ledgers(
            db=db,
            dataset_path=ds_path,
            model_line=f"qwen_alias_line_{unique_tag}",
            samples=samples,
            pass_threshold_x=2,
        )
        assert trackers["al_0"].consecutive_pass == 1
        assert trackers["al_0"].passed_versions == ["v1.0"]
    finally:
        db.query(BenchmarkRunLedger).filter(BenchmarkRunLedger.dataset_path == norm_p).delete()
        db.commit()
        db.close()


def test_skipped_fingerprint_reset_and_snapshot_new_sample_append():
    """回归 P1: 1) 被跳过的旧题改题后 miss_streak 归零; 2) 同版本重跑时后追加的 I=1 新题自动补入实测"""
    init_db()
    db = SessionLocal()
    unique_tag = uuid.uuid4().hex[:8]
    ds_path = f"data/datasets/temp_snap_{unique_tag}.jsonl"
    norm_p = BenchmarkSamplingEngine.normalize_dataset_path(ds_path)
    model_line = f"line_snap_{unique_tag}"

    s1 = BenchmarkSample(id="s1", is_holdout=True, messages=[ChatMessage(role="user", content="原题1")])
    s2 = BenchmarkSample(id="s2", is_holdout=True, messages=[ChatMessage(role="user", content="原题2")])
    dummy_resp = UnifiedChatResponse(content="OK", thinking="")

    def _pass(sid: str):
        return SampleEvalResult(
            sample_id=sid,
            category="test",
            model_id="m_1",
            response=dummy_resp,
            objective_passed=True,
            status="PASS",
        )

    try:
        cfg_v1 = SamplingFilterConfig(
            enabled=True, pass_threshold_x=2, model_line=model_line, version_tag="v1", benchmark_model_id="m_1"
        )
        # v1: s1 实测通过, s2 模拟休眠跳过
        BenchmarkSamplingEngine.record_run_ledger(
            db=db,
            task_id=f"tsk_1_{unique_tag}",
            dataset_path=ds_path,
            config=cfg_v1,
            all_samples=[s1, s2],
            selected_samples=[s1],
            skipped_samples=[s2],
            results_by_sample_id={"s1": [_pass("s1")]},
        )

        # 修改 s2 题面内容 -> 之前 skipped 记录指纹失效，miss_streak 必须为 0
        s2_edited = BenchmarkSample(id="s2", is_holdout=True, messages=[ChatMessage(role="user", content="修订后的题2")])
        tr_edited = BenchmarkSamplingEngine.aggregate_trackers_from_ledgers(
            db=db, dataset_path=ds_path, model_line=model_line, samples=[s1, s2_edited], pass_threshold_x=2
        )
        assert tr_edited["s2"].miss_streak == 0
        assert tr_edited["s2"].consecutive_pass == 0

        # 同版本 v1 重跑时数据集中追加了全新题目 s3 -> s3 必须补入 selected，不得被当成休眠熟题跳过
        s3_new = BenchmarkSample(id="s3", is_holdout=True, messages=[ChatMessage(role="user", content="新增题3")])
        sel, skp, st = BenchmarkSamplingEngine.partition_samples(
            [s1, s2_edited, s3_new], cfg_v1, db=db, dataset_path=ds_path
        )
        assert st["is_rerun_same_version"] is True
        sel_ids = {x.id for x in sel}
        skp_ids = {x.id for x in skp}
        assert "s1" in sel_ids
        assert "s3" in sel_ids
        assert "s2" in skp_ids
    finally:
        db.query(BenchmarkRunLedger).filter(BenchmarkRunLedger.dataset_path == norm_p).delete()
        db.commit()
        db.close()
