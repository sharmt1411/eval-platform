# -*- coding: utf-8 -*-
"""
评测集抽样与条件过滤导出回归测试
"""
import os
import json
import uuid
import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.config import settings
from backend.app.models import SessionLocal, init_db, BenchmarkRunLedger, DatasetRecord
from backend.app.schemas.benchmark import (
    BenchmarkSample,
    SamplingFilterConfig,
    SampleEvalResult,
    BenchmarkExportFilterRequest,
)
from backend.app.schemas.chat import ChatMessage, UnifiedChatResponse
from backend.app.engine.benchmark_sampler import BenchmarkSamplingEngine


def _make_sample(
    sid: str,
    category: str = "通用",
    difficulty: str = "easy",
    is_holdout: bool = True,
    content: str = "",
) -> BenchmarkSample:
    return BenchmarkSample(
        id=sid,
        category=category,
        difficulty=difficulty,
        is_holdout=is_holdout,
        messages=[ChatMessage(role="user", content=content or f"问题 {sid}")],
        reference=f"参考答案 {sid}",
    )


def _record_ledger_pass_fail(
    db,
    dataset_path: str,
    model_line: str,
    version_tag: str,
    all_samples,
    pass_ids,
    fail_ids,
    skipped_ids=None,
):
    skipped_set = set(skipped_ids or [])
    selected = [s for s in all_samples if s.id not in skipped_set]
    skipped = [s for s in all_samples if s.id in skipped_set]
    results_by_id = {}
    for s in selected:
        if s.id in pass_ids:
            st = "PASS"
            score = 95.0
        elif s.id in fail_ids:
            st = "FAIL"
            score = 40.0
        else:
            continue
        results_by_id[s.id] = [
            SampleEvalResult(
                sample_id=s.id,
                category=s.category,
                model_id="bm_model",
                source_model_id="bm_model",
                response=UnifiedChatResponse(content="ok", model_id="bm_model"),
                objective_passed=(st == "PASS"),
                judge_score=score,
                status=st,
            )
        ]

    cfg = SamplingFilterConfig(
        enabled=True,
        pass_threshold_x=2,
        interval_step_s=1,
        max_interval_n=4,
        model_line=model_line,
        version_tag=version_tag,
        benchmark_model_id="bm_model",
    )
    BenchmarkSamplingEngine.record_run_ledger(
        db=db,
        task_id=f"task_{uuid.uuid4().hex[:8]}",
        dataset_path=dataset_path,
        config=cfg,
        all_samples=all_samples,
        selected_samples=selected,
        skipped_samples=skipped,
        results_by_sample_id=results_by_id,
    )


def test_filter_samples_for_export_scopes_and_attributes():
    """验证按抽样池 (sampled/active_only/cooldown_all/skipped/failed_only) 与基础属性过滤导出"""
    init_db()
    db = SessionLocal()
    ds_id = f"test_export_unit_{uuid.uuid4().hex[:8]}"
    ds_path = os.path.join(settings.DATASETS_DIR, f"{ds_id}.jsonl")
    os.makedirs(settings.DATASETS_DIR, exist_ok=True)

    # 6 道题:
    # q1, q2, q3, q4: holdout=True, 连续通过 2 个版本 (v1, v2) -> c=2 >= x=2 -> I_i=2 (熟题池)
    # q5: holdout=True, v1 PASS, v2 FAIL -> c=0, last_status="FAIL" (必测攻坚池 + 失败池)
    # q6: holdout=False, 连续通过 2 个版本 -> 普通题同样可以学会 c=2 >= x=2 -> I_i=2 (熟题池)
    samples = [
        _make_sample("q1", category="常识", difficulty="easy", is_holdout=True, content="网络 HTTP"),
        _make_sample("q2", category="常识", difficulty="medium", is_holdout=True, content="数学推理"),
        _make_sample("q3", category="代码", difficulty="hard", is_holdout=True, content="Python 排序"),
        _make_sample("q4", category="代码", difficulty="easy", is_holdout=True, content="SQL 查询"),
        _make_sample("q5", category="逻辑", difficulty="hard", is_holdout=True, content="逻辑悖论"),
        _make_sample("q6", category="常识", difficulty="easy", is_holdout=False, content="同源基础题"),
    ]
    with open(ds_path, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s.model_dump(exclude_none=True), ensure_ascii=False) + "\n")

    try:
        model_line = f"line_{uuid.uuid4().hex[:6]}"
        _record_ledger_pass_fail(
            db, ds_path, model_line, "v1",
            samples,
            pass_ids={"q1", "q2", "q3", "q4", "q5", "q6"},
            fail_ids=set(),
        )
        _record_ledger_pass_fail(
            db, ds_path, model_line, "v2",
            samples,
            pass_ids={"q1", "q2", "q3", "q4", "q6"},
            fail_ids={"q5"},
        )

        base_sampling = SamplingFilterConfig(
            enabled=True,
            pass_threshold_x=2,
            interval_step_s=1,
            max_interval_n=4,
            model_line=model_line,
        )

        # 1. sampled: q5 (active=1) + round(5 * 0.5) = 2 from cooldown -> 共 3 题
        sampled, stats = BenchmarkSamplingEngine.filter_samples_for_export(
            samples=samples,
            export_req=BenchmarkExportFilterRequest(sampling_config=base_sampling, export_scope="sampled"),
            db=db,
            dataset_path=ds_path,
        )
        sampled_ids = {s.id for s in sampled}
        assert len(sampled) == 3
        assert "q5" in sampled_ids
        assert stats["active_pool_count"] == 1
        assert stats["skipped_samples"] == 3

        # 2. skipped: 剩余 3 道熟题
        skipped, _ = BenchmarkSamplingEngine.filter_samples_for_export(
            samples=samples,
            export_req=BenchmarkExportFilterRequest(sampling_config=base_sampling, export_scope="skipped"),
            db=db,
            dataset_path=ds_path,
        )
        skipped_ids = {s.id for s in skipped}
        assert len(skipped) == 3
        assert skipped_ids.isdisjoint(sampled_ids)
        assert sampled_ids | skipped_ids == {"q1", "q2", "q3", "q4", "q5", "q6"}

        # 3. active_only: 仅 q5 (因为普通题 q6 也学会进入了熟题池!)
        active_only, _ = BenchmarkSamplingEngine.filter_samples_for_export(
            samples=samples,
            export_req=BenchmarkExportFilterRequest(sampling_config=base_sampling, export_scope="active_only"),
            db=db,
            dataset_path=ds_path,
        )
        assert {s.id for s in active_only} == {"q5"}

        # 4. cooldown_all: q1, q2, q3, q4, q6 (留出熟题与普通熟题均在熟题池)
        cooldown_all, _ = BenchmarkSamplingEngine.filter_samples_for_export(
            samples=samples,
            export_req=BenchmarkExportFilterRequest(sampling_config=base_sampling, export_scope="cooldown_all"),
            db=db,
            dataset_path=ds_path,
        )
        assert {s.id for s in cooldown_all} == {"q1", "q2", "q3", "q4", "q6"}

        # 4b. cooldown_all + non_holdout_only: 普通熟题 q6 支持按熟题池过滤导出
        cooldown_regular, _ = BenchmarkSamplingEngine.filter_samples_for_export(
            samples=samples,
            export_req=BenchmarkExportFilterRequest(
                sampling_config=base_sampling,
                export_scope="cooldown_all",
                holdout_filter="non_holdout_only",
            ),
            db=db,
            dataset_path=ds_path,
        )
        assert [s.id for s in cooldown_regular] == ["q6"]

        # 5. failed_only (即使 sampling_config.enabled=False 也能根据 model_line 精准找出 FAIL 题)
        disabled_sampling = base_sampling.model_copy(update={"enabled": False})
        failed_only, _ = BenchmarkSamplingEngine.filter_samples_for_export(
            samples=samples,
            export_req=BenchmarkExportFilterRequest(sampling_config=disabled_sampling, export_scope="failed_only"),
            db=db,
            dataset_path=ds_path,
        )
        assert [s.id for s in failed_only] == ["q5"]

        # 6. 基础属性叠加过滤: cooldown_all + category=["代码"] + difficulty=["hard"] -> 仅 q3
        attr_filtered, _ = BenchmarkSamplingEngine.filter_samples_for_export(
            samples=samples,
            export_req=BenchmarkExportFilterRequest(
                sampling_config=base_sampling,
                export_scope="cooldown_all",
                categories=["代码"],
                difficulties=["hard"],
            ),
            db=db,
            dataset_path=ds_path,
        )
        assert [s.id for s in attr_filtered] == ["q3"]

        # 7. holdout_filter + keyword
        non_holdout, _ = BenchmarkSamplingEngine.filter_samples_for_export(
            samples=samples,
            export_req=BenchmarkExportFilterRequest(
                sampling_config=base_sampling,
                export_scope="all",
                holdout_filter="non_holdout_only",
                keyword="同源基础",
            ),
            db=db,
            dataset_path=ds_path,
        )
        assert [s.id for s in non_holdout] == ["q6"]

        # 8. 导出过程中不得产生新的流水记录
        ledger_count = (
            db.query(BenchmarkRunLedger)
            .filter(BenchmarkRunLedger.model_line == model_line)
            .count()
        )
        assert ledger_count == 2
    finally:
        BenchmarkSamplingEngine.reset_trackers_in_db(db=db, dataset_path=ds_path)
        if os.path.exists(ds_path):
            os.remove(ds_path)
        db.close()


def test_api_dataset_filter_options_preview_download_and_save_dataset():
    """测试数据集过滤选项、试算预览、JSONL下载导出与另存为新评测集 API 全链路"""
    init_db()
    client = TestClient(app)
    db = SessionLocal()

    src_id = f"test_api_src_{uuid.uuid4().hex[:8]}"
    saved_id = f"test_api_saved_{uuid.uuid4().hex[:8]}"
    src_path = os.path.join(settings.DATASETS_DIR, f"{src_id}.jsonl")
    saved_path = os.path.join(settings.DATASETS_DIR, f"{saved_id}.jsonl")
    os.makedirs(settings.DATASETS_DIR, exist_ok=True)

    samples = [
        _make_sample("a1", category="百科", difficulty="easy", is_holdout=True, content="地球半径"),
        _make_sample("a2", category="百科", difficulty="medium", is_holdout=True, content="光合作用"),
        _make_sample("a3", category="推理", difficulty="hard", is_holdout=True, content="海盗分金"),
    ]
    with open(src_path, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s.model_dump(exclude_none=True), ensure_ascii=False) + "\n")

    model_line = f"api_line_{uuid.uuid4().hex[:6]}"
    try:
        _record_ledger_pass_fail(db, src_path, model_line, "v1.0", samples, {"a1", "a2"}, {"a3"})
        _record_ledger_pass_fail(db, src_path, model_line, "v2.0", samples, {"a1", "a2"}, {"a3"})

        # 1. filter-options
        opt_resp = client.get(f"/api/v1/datasets/{src_id}/filter-options")
        assert opt_resp.status_code == 200
        opts = opt_resp.json()
        assert opts["total_samples"] == 3
        assert opts["holdout_count"] == 3
        assert set(opts["categories"]) == {"百科", "推理"}
        assert set(opts["difficulties"]) == {"easy", "medium", "hard"}
        assert any(ml["model_line"] == model_line and "v2.0" in ml["version_tags"] for ml in opts["model_lines"])

        # 2. export/preview (active_only 应该命中失败的 a3)
        req_payload = {
            "sampling_config": {
                "enabled": True,
                "pass_threshold_x": 2,
                "interval_step_s": 1,
                "max_interval_n": 4,
                "model_line": model_line,
            },
            "export_scope": "active_only",
            "export_target": "download",
            "include_tracker": False,
        }
        prev_resp = client.post(f"/api/v1/datasets/{src_id}/export/preview", json=req_payload)
        assert prev_resp.status_code == 200
        prev_data = prev_resp.json()
        assert prev_data["total_samples"] == 3
        assert prev_data["exported_count"] == 1
        assert prev_data["preview_sample_ids"] == ["a3"]

        # 3. export download (纯净不含 eval_tracker，再测 include_tracker=True)
        dl_resp = client.post(f"/api/v1/datasets/{src_id}/export", json=req_payload)
        assert dl_resp.status_code == 200
        dl_data = dl_resp.json()
        assert dl_data["exported_count"] == 1
        row_clean = json.loads(dl_data["jsonl_content"].strip())
        assert row_clean["id"] == "a3"
        assert "eval_tracker" not in row_clean

        req_with_tracker = {**req_payload, "include_tracker": True}
        dl_tr_resp = client.post(f"/api/v1/datasets/{src_id}/export", json=req_with_tracker)
        assert dl_tr_resp.status_code == 200
        row_tr = json.loads(dl_tr_resp.json()["jsonl_content"].strip())
        assert row_tr["eval_tracker"]["last_status"] == "FAIL"

        # 4. 禁止另存为同名覆盖源数据集
        bad_save = {
            **req_payload,
            "export_target": "save_dataset",
            "target_dataset_id": src_id,
        }
        bad_resp = client.post(f"/api/v1/datasets/{src_id}/export", json=bad_save)
        assert bad_resp.status_code == 400

        # 5. 另存为新评测集
        save_payload = {
            **req_payload,
            "export_target": "save_dataset",
            "target_dataset_id": saved_id,
            "target_dataset_name": "失败攻坚子集",
            "save_mode": "overwrite",
        }
        save_resp = client.post(f"/api/v1/datasets/{src_id}/export", json=save_payload)
        assert save_resp.status_code == 200
        save_data = save_resp.json()
        assert save_data["exported_count"] == 1
        assert save_data["saved_dataset"]["id"] == saved_id
        assert save_data["saved_dataset"]["sample_count"] == 1
        assert os.path.exists(saved_path)

        # 6. 命中 0 条时导出返回 400
        empty_payload = {
            **req_payload,
            "categories": ["不存在的分类"],
        }
        empty_resp = client.post(f"/api/v1/datasets/{src_id}/export", json=empty_payload)
        assert empty_resp.status_code == 400
    finally:
        BenchmarkSamplingEngine.reset_trackers_in_db(db=db, dataset_path=src_path)
        for did in (src_id, saved_id):
            rec = db.query(DatasetRecord).filter(DatasetRecord.id == did).first()
            if rec:
                db.delete(rec)
        db.commit()
        for p in (src_path, saved_path):
            if os.path.exists(p):
                os.remove(p)
        db.close()
