# -*- coding: utf-8 -*-
"""
常规数据集对比评测 API
"""
import os
import json
import time
import uuid
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from backend.app.config import settings
from backend.app.models import get_db, TaskRecord, RegisteredModel
from backend.app.api.deps import require_registered_adapter_and_config
from backend.app.schemas import (
    BenchmarkSample,
    ChatMessage,
    ModelEvalConfig,
    ModelConfig,
    BenchmarkRunRequest,
    SamplingFilterConfig,
    SamplingPreviewResponse,
    ResetTrackerRequest,
    ResetTrackerResponse,
    UnifiedChatRequest,
)
from backend.app.engine.benchmark_runner import BenchmarkRunner, has_dataset_eval_criteria
from backend.app.engine.benchmark_sampler import BenchmarkSamplingEngine
from backend.app.engine.judge import UniversalJudgeEngine
from backend.app.adapters.factory import AdapterFactory
from backend.app.adapters.base import BaseModelAdapter

router = APIRouter(prefix="/benchmarks", tags=["常规评测"])


def _mask_api_key(api_key: Optional[str]) -> Optional[str]:
    """对非空真实 API Key 执行脱敏掩码，防止密钥随任务记录明文泄漏。"""
    if api_key is None:
        return None
    raw = str(api_key).strip()
    if not raw or raw.upper() == "EMPTY" or "***" in raw:
        return api_key
    if len(raw) <= 8:
        return "***"
    return f"{raw[:3]}***{raw[-4:]}"


def _sanitize_model_configs(model_configs: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(model_configs, list):
        return model_configs
    sanitized: List[Dict[str, Any]] = []
    for item in model_configs:
        if isinstance(item, dict):
            copied = dict(item)
            if "api_key" in copied:
                copied["api_key"] = _mask_api_key(copied.get("api_key"))
            sanitized.append(copied)
        else:
            sanitized.append(item)
    return sanitized


def _load_benchmark_samples_from_file(real_path: str) -> List[BenchmarkSample]:
    """从 JSONL 文件解析评测样本；若纯对话语料缺少 id 字段则自动补全行号索引，确保格式解析与拦截提示一致。"""
    samples: List[BenchmarkSample] = []
    with open(real_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line_str = line.strip()
            if not line_str:
                continue
            obj = json.loads(line_str)
            if not isinstance(obj, dict):
                raise ValueError(f"第 {line_no} 行不是合法 JSON 对象")
            if not str(obj.get("id") or "").strip():
                orig_idx = obj.get("original_idx")
                obj["id"] = (
                    str(orig_idx).strip()
                    if orig_idx is not None and str(orig_idx).strip()
                    else f"row_{line_no}"
                )
            samples.append(BenchmarkSample(**obj))
    return samples


def _execute_benchmark_task(task_id: str, payload: BenchmarkRunRequest):
    from backend.app.models import SessionLocal

    db = SessionLocal()
    task = db.query(TaskRecord).filter(TaskRecord.id == task_id).first()
    if not task:
        db.close()
        return

    task.status = "RUNNING"
    db.commit()

    res_file = os.path.join(settings.RUNS_DIR, f"bench_{task_id}.json")

    try:
        # 加载数据集
        real_path = payload.dataset_path
        if not os.path.isabs(real_path):
            real_path = os.path.join(settings.BASE_DIR, real_path)

        samples = _load_benchmark_samples_from_file(real_path)

        total_dataset_samples = len(samples)
        samples_to_run = samples
        skipped_samples = []
        cooldown_sample_ids = set()
        sampling_stats = {}

        if payload.sampling_config and (
            payload.sampling_config.enabled
            or getattr(payload.sampling_config, "holdout_filter", "all") != "all"
        ):
            selected_samples, skipped_samples, sampling_stats = BenchmarkSamplingEngine.partition_samples(
                samples,
                payload.sampling_config,
                db=db,
                dataset_path=real_path,
            )
            samples_to_run = selected_samples
            total_dataset_samples = sampling_stats.get("total_samples", len(samples_to_run) + len(skipped_samples))
            cooldown_sample_ids = set(sampling_stats.get("cooldown_sample_ids", []))

        skipped_holdout_cnt = sum(1 for s in skipped_samples if s.is_holdout)
        skipped_regular_cnt = len(skipped_samples) - skipped_holdout_cnt

        # 查询并构建模型配置
        model_configs: List[ModelEvalConfig] = []
        if payload.model_configs:
            for m_cfg in payload.model_configs:
                incoming_max_tokens = m_cfg.config.max_tokens
                # 补全可能缺失的凭证与服务配置
                m_db = db.query(RegisteredModel).filter(RegisteredModel.id == m_cfg.model_id).first()
                db_max_tokens = None
                if m_db:
                    if not m_cfg.display_name:
                        m_cfg.display_name = m_db.name or m_db.id
                    if not m_cfg.api_url:
                        m_cfg.api_url = m_db.api_url
                    if not m_cfg.api_key or "***" in str(m_cfg.api_key):
                        m_cfg.api_key = m_db.api_key
                    if not m_cfg.model_name:
                        m_cfg.model_name = m_db.model_name or m_db.id
                    if m_cfg.provider_type == "openai" and m_db.provider_type != "openai":
                        m_cfg.provider_type = m_db.provider_type
                    # 合并默认配置与覆盖配置
                    if m_db.config_json:
                        base_cfg = json.loads(m_db.config_json)
                        db_max_tokens = base_cfg.get("max_tokens")
                        override_cfg = m_cfg.config.model_dump(exclude_unset=True)
                        merged = {**base_cfg, **override_cfg}
                        m_cfg.config = ModelConfig(**merged)
                # 若请求指定了全局 max_tokens：
                # 若无单独覆盖或与全局/数据库基准一致，统一定为 payload.max_tokens；
                # 仅当单独调参且与全局不同时保留独立值
                if payload.max_tokens and payload.max_tokens > 0:
                    if (
                        incoming_max_tokens
                        and incoming_max_tokens != payload.max_tokens
                        and incoming_max_tokens != db_max_tokens
                        and incoming_max_tokens != 4096
                    ):
                        m_cfg.config.max_tokens = incoming_max_tokens
                    else:
                        m_cfg.config.max_tokens = payload.max_tokens
                model_configs.append(m_cfg)

        existing_mids = {m.model_id for m in model_configs}
        for mid in (payload.model_ids or []):
            if mid in existing_mids:
                continue
            m_db = db.query(RegisteredModel).filter(RegisteredModel.id == mid).first()
            if m_db:
                cfg_dict = json.loads(m_db.config_json or "{}")
                if payload.max_tokens and payload.max_tokens > 0:
                    cfg_dict["max_tokens"] = payload.max_tokens
                model_configs.append(
                    ModelEvalConfig(
                        model_id=m_db.id,
                        display_name=m_db.name,
                        provider_type=m_db.provider_type,  # type: ignore
                        api_url=m_db.api_url,
                        api_key=m_db.api_key,
                        model_name=m_db.model_name or m_db.id,
                        config=ModelConfig(**cfg_dict),
                    )
                )
            else:
                cfg = ModelConfig(max_tokens=payload.max_tokens) if payload.max_tokens else ModelConfig()
                model_configs.append(
                    ModelEvalConfig(
                        model_id=mid,
                        provider_type="mock",
                        config=cfg,
                    )
                )

        judge_adapter = None
        judge_config = None
        if payload.judge_model_id:
            j_db = db.query(RegisteredModel).filter(RegisteredModel.id == payload.judge_model_id).first()
            if j_db:
                j_cfg = json.loads(j_db.config_json or "{}")
                judge_config = ModelConfig(**j_cfg)
                judge_adapter = AdapterFactory.create(
                    provider_type=j_db.provider_type,
                    model_name=j_db.model_name or j_db.id,
                    api_url=j_db.api_url,
                    api_key=j_db.api_key,
                    **j_cfg,
                )

        def _report_progress(completed: int, total: int, model_name: str, sample_id: str):
            from backend.app.models import SessionLocal
            p_db = SessionLocal()
            try:
                t = p_db.query(TaskRecord).filter(TaskRecord.id == task_id).first()
                if t:
                    t.progress = round(completed / max(total, 1), 4)
                    cur_res = json.loads(t.result_json or "{}")
                    cur_res["progress_detail"] = {
                        "completed": completed,
                        "total": total,
                        "current_model": model_name,
                        "current_sample_id": sample_id,
                    }
                    t.result_json = json.dumps(cur_res, ensure_ascii=False)
                    p_db.commit()
            except Exception:
                p_db.rollback()
            finally:
                p_db.close()

        def _flush_step_result(matrix_data: Dict[str, Any]):
            """即时落盘步进产物，确保任务执行期间与刷新页面立即可见"""
            try:
                clean_matrix = {k: v for k, v in matrix_data.items() if k != "_raw_results_by_sample"}
                with open(res_file, "w", encoding="utf-8") as f:
                    json.dump(clean_matrix, f, ensure_ascii=False, indent=2)
                from backend.app.models import SessionLocal
                p_db = SessionLocal()
                t = p_db.query(TaskRecord).filter(TaskRecord.id == task_id).first()
                if t:
                    cur_res = json.loads(t.result_json or "{}")
                    cur_res["summaries"] = matrix_data.get("summaries", {})
                    cur_res["result_file"] = res_file
                    cur_res["max_tokens"] = payload.max_tokens
                    cur_res["ignore_sample_max_tokens"] = payload.ignore_sample_max_tokens
                    cur_res["model_configs"] = _sanitize_model_configs(
                        [m.model_dump() for m in model_configs]
                    )
                    cur_res["dataset_total_samples"] = total_dataset_samples
                    cur_res["sampled_samples"] = len(samples_to_run)
                    cur_res["skipped_mastered_samples"] = len(skipped_samples)
                    cur_res["sampling_config"] = payload.sampling_config.model_dump() if payload.sampling_config else None
                    t.result_json = json.dumps(cur_res, ensure_ascii=False)
                    p_db.commit()
                p_db.close()
            except Exception as fe:
                print(f"⚠️ 即时落盘中间产物失败: {fe}")

        runner = BenchmarkRunner(judge_adapter=judge_adapter, judge_config=judge_config)
        matrix = runner.run_multi_model_matrix(
            samples_to_run,
            model_configs,
            workers=payload.workers,
            progress_callback=_report_progress,
            on_matrix_step=_flush_step_result,
            ignore_sample_max_tokens=payload.ignore_sample_max_tokens,
            dataset_total_samples=total_dataset_samples,
            skipped_mastered_samples=len(skipped_samples),
            sampling_config=payload.sampling_config,
            cooldown_sample_ids=cooldown_sample_ids,
            skipped_holdout_samples=skipped_holdout_cnt,
            skipped_regular_samples=skipped_regular_cnt,
        )

        # 战绩流水入账 (写入 SQLite benchmark_run_ledgers，保持 .jsonl 纯净只读)
        raw_results_by_sample = matrix.pop("_raw_results_by_sample", {})
        ledger_id = None
        if payload.sampling_config and payload.sampling_config.enabled:
            try:
                ledger_id = BenchmarkSamplingEngine.record_run_ledger(
                    db=db,
                    task_id=task_id,
                    dataset_path=real_path,
                    config=payload.sampling_config,
                    all_samples=samples,
                    selected_samples=samples_to_run,
                    skipped_samples=skipped_samples,
                    results_by_sample_id=raw_results_by_sample,
                    cooldown_sample_ids=cooldown_sample_ids,
                )
            except Exception as te:
                print(f"⚠️ 记录战绩流水失败: {te}")

        # 最终确保持久化落盘 (排除临时 raw 数据)
        clean_matrix = {k: v for k, v in matrix.items() if k != "_raw_results_by_sample"}
        with open(res_file, "w", encoding="utf-8") as f:
            json.dump(clean_matrix, f, ensure_ascii=False, indent=2)

        task.status = "COMPLETED"
        task.progress = 1.0
        cur_res = json.loads(task.result_json or "{}")
        cur_res.update({
            "summaries": matrix["summaries"],
            "result_file": res_file,
            "max_tokens": payload.max_tokens,
            "ignore_sample_max_tokens": payload.ignore_sample_max_tokens,
            "dataset_path": payload.dataset_path,
            "model_configs": _sanitize_model_configs([m.model_dump() for m in model_configs]),
            "dataset_total_samples": total_dataset_samples,
            "sampled_samples": len(samples_to_run),
            "skipped_mastered_samples": len(skipped_samples),
            "sampling_config": payload.sampling_config.model_dump() if payload.sampling_config else None,
            "ledger_id": ledger_id,
            "is_rerun_same_version": sampling_stats.get("is_rerun_same_version", False),
        })
        task.result_json = json.dumps(cur_res, ensure_ascii=False)
        task.finished_at = int(time.time())
        db.commit()
    except Exception as e:
        task.status = "FAILED"
        task.error_message = str(e)
        task.finished_at = int(time.time())
        cur_res = json.loads(task.result_json or "{}")
        cur_res["result_file"] = res_file
        task.result_json = json.dumps(cur_res, ensure_ascii=False)
        db.commit()
    finally:
        db.close()


def _ensure_demo_benchmark_task(db: Session):
    """如果任务表为空且存在 demo_benchmark_report.json，自动补建一条演示评测任务"""
    existing_count = db.query(TaskRecord).filter(TaskRecord.task_type == "benchmark").count()
    if existing_count == 0:
        demo_report = os.path.join(settings.RUNS_DIR, "demo_benchmark_report.json")
        if os.path.exists(demo_report):
            try:
                with open(demo_report, "r", encoding="utf-8") as f:
                    data = json.load(f)
                rec = TaskRecord(
                    id="demo_benchmark",
                    task_type="benchmark",
                    status="COMPLETED",
                    progress=1.0,
                    result_json=json.dumps({
                        "dataset_path": "data/datasets/demo_benchmark.jsonl",
                        "summaries": data.get("summaries", {}),
                        "result_file": demo_report,
                        "max_tokens": 4096,
                        "ignore_sample_max_tokens": False,
                    }, ensure_ascii=False),
                    created_at=int(time.time()),
                    finished_at=int(time.time()),
                )
                db.add(rec)
                db.commit()
            except Exception as e:
                print(f"⚠️ 导入演示评测任务失败: {e}")
                db.rollback()


@router.get("/tasks")
def list_benchmark_tasks(
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """查询历史评测任务列表"""
    _ensure_demo_benchmark_task(db)

    tasks = (
        db.query(TaskRecord)
        .filter(TaskRecord.task_type == "benchmark")
        .order_by(TaskRecord.created_at.desc())
        .limit(limit)
        .all()
    )
    result = []
    for t in tasks:
        parsed_res = json.loads(t.result_json or "{}")
        result.append({
            "id": t.id,
            "status": t.status,
            "progress": t.progress,
            "created_at": t.created_at,
            "finished_at": t.finished_at,
            "error_message": t.error_message,
            "dataset_path": parsed_res.get("dataset_path"),
            "max_tokens": parsed_res.get("max_tokens"),
            "ignore_sample_max_tokens": parsed_res.get("ignore_sample_max_tokens", False),
            "progress_detail": parsed_res.get("progress_detail"),
            "summaries": parsed_res.get("summaries", {}),
            "model_configs": _sanitize_model_configs(parsed_res.get("model_configs")),
            "sampling_config": parsed_res.get("sampling_config"),
            "dataset_total_samples": parsed_res.get("dataset_total_samples"),
            "sampled_samples": parsed_res.get("sampled_samples"),
            "skipped_mastered_samples": parsed_res.get("skipped_mastered_samples"),
        })
    return result


@router.post("/run")
def trigger_benchmark(
    payload: BenchmarkRunRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    if payload.sampling_config and payload.sampling_config.enabled:
        sc = payload.sampling_config
        has_model_line = bool((sc.model_line or "").strip())
        has_version_tag = bool((sc.version_tag or "").strip())
        has_bm_target = bool(
            (sc.benchmark_model_id or "").strip()
            or any((k or "").strip() for k in (sc.tracker_model_keys or []))
        )
        if (has_model_line or has_bm_target) and not has_version_tag:
            raise HTTPException(
                status_code=400,
                detail="开启智能降频并指定基准模型线时，必须填写本次评测版本号 (version_tag)",
            )
        if has_version_tag and not (has_model_line or has_bm_target):
            raise HTTPException(
                status_code=400,
                detail="填写评测版本号 (version_tag) 时，必须同时指定基准模型线或参评基准模型",
            )

    real_path = payload.dataset_path
    if not os.path.isabs(real_path):
        real_path = os.path.join(settings.BASE_DIR, real_path)
    if os.path.exists(real_path):
        try:
            preview_samples = _load_benchmark_samples_from_file(real_path)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"数据集格式解析失败，无法启动常规评测: {str(e)}",
            )

        hf = getattr(payload.sampling_config, "holdout_filter", "all") if payload.sampling_config else "all"
        if hf == "holdout_only":
            filtered_preview = [s for s in preview_samples if s.is_holdout]
            if not filtered_preview:
                raise HTTPException(
                    status_code=400,
                    detail="当前评测集内没有符合条件的留出题 (is_holdout=true)，无法发起仅留出题评测",
                )
        elif hf == "non_holdout_only":
            filtered_preview = [s for s in preview_samples if not s.is_holdout]
            if not filtered_preview:
                raise HTTPException(
                    status_code=400,
                    detail="当前评测集内没有符合条件的普通题 (is_holdout=false)，无法发起仅普通题评测",
                )
        else:
            filtered_preview = preview_samples

        if not payload.allow_unlabeled and filtered_preview and not has_dataset_eval_criteria(filtered_preview):
            raise HTTPException(
                status_code=400,
                detail=(
                    "当前数据集为无答案标准的纯对话语料（全部样本均缺少 reference、rubric 与 regex_constraints），"
                    "常规评测会跳过所有校验导致假阳性 100% PASS。请改用「规则质检打分」"
                    "（POST /api/v1/flywheel/inspect-dataset，或前往数据集中心「训练与对话语料 (SFT)」/「淘汰对话隔离池」页签点击「规则质检打分」；"
                    "若该文件此前误登记在评测集页签，请删除后按训练集重新导入）；"
                    "如仅需测试模型连通性或生成速度，请显式传入 allow_unlabeled=True。"
                ),
            )

    task_id = str(uuid.uuid4())[:8]
    res_file = os.path.join(settings.RUNS_DIR, f"bench_{task_id}.json")

    # 预创建空骨架文件，防止前端在执行中轮询 side_by_side 时触发 404
    try:
        with open(res_file, "w", encoding="utf-8") as f:
            json.dump({"summaries": {}, "side_by_side": {}}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 初始化评测骨架文件失败: {e}")

    initial_model_configs = []
    for m in (payload.model_configs or []):
        m_dump = m.model_dump()
        if payload.max_tokens and payload.max_tokens > 0:
            if not m.config.max_tokens or m.config.max_tokens == 4096:
                m_dump["config"]["max_tokens"] = payload.max_tokens
        initial_model_configs.append(m_dump)

    rec = TaskRecord(
        id=task_id,
        task_type="benchmark",
        status="PENDING",
        progress=0.0,
        result_json=json.dumps({
            "dataset_path": payload.dataset_path,
            "model_configs": _sanitize_model_configs(initial_model_configs),
            "model_ids": payload.model_ids or [],
            "judge_model_id": payload.judge_model_id,
            "workers": payload.workers,
            "summaries": {},
            "result_file": res_file,
            "max_tokens": payload.max_tokens,
            "ignore_sample_max_tokens": payload.ignore_sample_max_tokens,
            "sampling_config": payload.sampling_config.model_dump() if payload.sampling_config else None,
        }, ensure_ascii=False),
    )
    db.add(rec)
    db.commit()

    background_tasks.add_task(_execute_benchmark_task, task_id, payload)
    return {"message": "评测任务已提交", "task_id": task_id}


@router.get("/tasks/{task_id}")
def get_benchmark_task_status(task_id: str, db: Session = Depends(get_db)):
    task = db.query(TaskRecord).filter(TaskRecord.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="未找到该任务")

    parsed_res = json.loads(task.result_json or "{}")
    if isinstance(parsed_res, dict) and "model_configs" in parsed_res:
        parsed_res["model_configs"] = _sanitize_model_configs(parsed_res.get("model_configs"))
    res = {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "progress": task.progress,
        "error_message": task.error_message,
        "created_at": task.created_at,
        "finished_at": task.finished_at,
        "result": parsed_res,
    }
    return res


class BatchDeleteTasksRequest(BaseModel):
    task_ids: List[str]


@router.post("/tasks/batch-delete")
def batch_delete_benchmark_tasks(payload: BatchDeleteTasksRequest, db: Session = Depends(get_db)):
    """批量删除历史评测任务及关联产物报告文件"""
    if not payload.task_ids:
        return {"message": "未指定要删除的任务", "deleted_count": 0}

    deleted_count = 0
    for task_id in payload.task_ids:
        task = db.query(TaskRecord).filter(TaskRecord.id == task_id).first()
        if not task:
            continue
        try:
            res_data = json.loads(task.result_json or "{}")
            res_file = res_data.get("result_file")
            if res_file and os.path.exists(res_file) and not res_file.endswith("demo_benchmark_report.json"):
                os.remove(res_file)
            direct_file = os.path.join(settings.RUNS_DIR, f"bench_{task_id}.json")
            if os.path.exists(direct_file) and not direct_file.endswith("demo_benchmark_report.json"):
                os.remove(direct_file)
        except Exception as e:
            print(f"⚠️ 批量清理评测产物文件失败: {e}")

        BenchmarkSamplingEngine.revoke_ledger_by_task(db, task_id)
        db.delete(task)
        deleted_count += 1

    db.commit()
    return {"message": f"已成功批量删除 {deleted_count} 个评测任务", "deleted_count": deleted_count}


@router.delete("/tasks/{task_id}")
def delete_benchmark_task(task_id: str, db: Session = Depends(get_db)):
    """删除历史评测任务及关联产物报告文件"""
    task = db.query(TaskRecord).filter(TaskRecord.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="未找到该评测任务")

    # 尝试删除产物报告文件 (保护预置 demo 文件不被物理删除)
    try:
        res_data = json.loads(task.result_json or "{}")
        res_file = res_data.get("result_file")
        if res_file and os.path.exists(res_file) and not res_file.endswith("demo_benchmark_report.json"):
            os.remove(res_file)
        direct_file = os.path.join(settings.RUNS_DIR, f"bench_{task_id}.json")
        if os.path.exists(direct_file) and not direct_file.endswith("demo_benchmark_report.json"):
            os.remove(direct_file)
    except Exception as e:
        print(f"⚠️ 清理评测产物文件失败: {e}")

    BenchmarkSamplingEngine.revoke_ledger_by_task(db, task_id)
    db.delete(task)
    db.commit()
    return {"message": f"评测任务 [{task_id}] 已成功删除", "task_id": task_id}


@router.post("/tasks/{task_id}/revoke-ledger")
def revoke_task_ledger(task_id: str, db: Session = Depends(get_db)):
    """一键撤销指定评测任务的战绩入账流水 (is_revoked=True)"""
    task = db.query(TaskRecord).filter(TaskRecord.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="未找到该评测任务")

    revoked_count = BenchmarkSamplingEngine.revoke_ledger_by_task(db, task_id)
    if revoked_count > 0:
        try:
            res_data = json.loads(task.result_json or "{}")
            res_data["ledger_revoked"] = True
            task.result_json = json.dumps(res_data, ensure_ascii=False)
            db.commit()
        except Exception:
            pass
    return {
        "message": f"已撤销任务 [{task_id}] 的 {revoked_count} 条战绩流水记录" if revoked_count > 0 else "该任务暂无可撤销的有效战绩流水",
        "task_id": task_id,
        "revoked_count": revoked_count,
    }


@router.get("/tasks/{task_id}/side_by_side")
def get_side_by_side_detail(task_id: str, db: Session = Depends(get_db)):
    task = db.query(TaskRecord).filter(TaskRecord.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="未找到该任务")
    
    res_data = json.loads(task.result_json or "{}")
    res_file = res_data.get("result_file")
    if not res_file or not os.path.exists(res_file):
        # 兼容自动 fallback 查找 bench_{task_id}.json
        fallback_file = os.path.join(settings.RUNS_DIR, f"bench_{task_id}.json")
        if os.path.exists(fallback_file):
            res_file = fallback_file
        else:
            raise HTTPException(status_code=404, detail="报告文件未生成或不存在")

    with open(res_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("side_by_side", {})


class PreviewSamplingRequest(BaseModel):
    dataset_path: str
    sampling_config: SamplingFilterConfig


@router.post("/preview-sampling", response_model=SamplingPreviewResponse)
def preview_sampling(payload: PreviewSamplingRequest, db: Session = Depends(get_db)):
    """根据抽样配置与 SQLite 流水战绩试算并返回预计抽取的题量分布与算力节省度"""
    real_path = payload.dataset_path
    if not os.path.isabs(real_path):
        real_path = os.path.join(settings.BASE_DIR, real_path)

    if not os.path.exists(real_path):
        raise HTTPException(status_code=404, detail=f"数据集文件不存在: {payload.dataset_path}")

    samples = []
    with open(real_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                samples.append(BenchmarkSample(**json.loads(line)))

    return BenchmarkSamplingEngine.preview_sampling(
        samples=samples,
        config=payload.sampling_config,
        db=db,
        dataset_path=real_path,
    )


@router.post("/reset-tracker", response_model=ResetTrackerResponse)
def reset_tracker(payload: ResetTrackerRequest, db: Session = Depends(get_db)):
    """一键重置指定数据集（或指定模型线）的战绩流水记录"""
    real_path = payload.dataset_path
    if not os.path.isabs(real_path):
        real_path = os.path.join(settings.BASE_DIR, real_path)

    if not os.path.exists(real_path):
        raise HTTPException(status_code=404, detail=f"数据集文件不存在: {payload.dataset_path}")

    reset_count = BenchmarkSamplingEngine.reset_trackers_in_db(
        db=db,
        dataset_path=real_path,
        model_line=payload.model_line,
    )
    target_desc = f"模型线 [{payload.model_line}] " if payload.model_line else "全部模型线"
    return ResetTrackerResponse(
        message=f"已成功作废数据集 [{os.path.basename(payload.dataset_path)}] {target_desc}的 {reset_count} 条战绩流水，所有题目已回归必测起点",
        reset_count=reset_count,
    )


class DebugBenchmarkJudgeRequest(BaseModel):
    dataset_path: Optional[str] = None
    sample_id: Optional[str] = None
    sample: Optional[BenchmarkSample] = None
    model_answer: Optional[str] = None
    template_override: Optional[str] = None


class TestBenchmarkJudgeRequest(BaseModel):
    model_id: str
    assembled_prompt: str
    assembled_messages: Optional[List[Dict[str, str]]] = None


@router.post("/debug-judge-prompt")
def debug_benchmark_judge_prompt(payload: DebugBenchmarkJudgeRequest) -> Dict[str, Any]:
    """基于真实评测样本 (BenchmarkSample) 组装常规评测裁判 Prompt 报文。"""
    sample = payload.sample
    if sample is None and payload.dataset_path:
        real_path = payload.dataset_path
        if not os.path.isabs(real_path):
            real_path = os.path.join(settings.BASE_DIR, real_path)
        if not os.path.exists(real_path):
            raise HTTPException(status_code=404, detail=f"数据集文件不存在: {payload.dataset_path}")
        with open(real_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                cand = BenchmarkSample(**json.loads(line))
                if not payload.sample_id or cand.id == payload.sample_id:
                    sample = cand
                    break
        if sample is None:
            raise HTTPException(status_code=404, detail="数据集中未找到可用于调试的样本")

    if sample is None:
        sample = BenchmarkSample(
            id="demo_bench_sample",
            category="通用能力",
            sub_category="业务合规",
            difficulty="medium",
            messages=[ChatMessage(role="user", content="请简短回答：星云智联智能设备支持七天无理由退换货吗？")],
            reference="未拆封激活的商品支持签收起7天内无理由退换货；若因质量问题退换，往返运费由商家承担。",
            rubric="必须明确说明7天无理由退换条件及质量问题运费承担规则。",
            regex_constraints=["7天|七天", "!售后专员"],
        )

    answer_text = (
        payload.model_answer
        if payload.model_answer is not None and payload.model_answer.strip()
        else (sample.reference or "这是一个待测模型的示例回答。")
    )
    judge_engine = UniversalJudgeEngine(judge_adapter=None)  # type: ignore
    msgs = judge_engine.build_benchmark_judge_messages(
        sample=sample,
        model_answer=answer_text,
        template_override=payload.template_override,
    )
    return {
        "sample_id": sample.id,
        "sample": sample.model_dump(),
        "model_answer": answer_text,
        "assembled_prompt": "\n\n".join(m.content for m in msgs),
        "assembled_messages": [{"role": m.role, "content": m.content} for m in msgs],
    }


@router.post("/test-judge-prompt")
def test_benchmark_judge_prompt(
    payload: TestBenchmarkJudgeRequest,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """调用已注册裁判模型在线实测常规 Benchmark 裁判 Prompt。"""
    adapter: BaseModelAdapter
    adapter, registered_cfg = require_registered_adapter_and_config(db, payload.model_id)
    test_msgs = (
        [
            ChatMessage(role=m.get("role", "user"), content=m.get("content", ""))
            for m in payload.assembled_messages
            if m.get("content")
        ]
        if payload.assembled_messages
        else [ChatMessage(role="user", content=payload.assembled_prompt)]
    )
    req_cfg = ModelConfig(
        temperature=registered_cfg.temperature,
        top_p=registered_cfg.top_p,
        max_tokens=max(registered_cfg.max_tokens or 0, 2048),
        repetition_penalty=registered_cfg.repetition_penalty,
        thinking_enabled=registered_cfg.thinking_enabled,
        thinking_level=registered_cfg.thinking_level,
        vertex_thinking_mode=registered_cfg.vertex_thinking_mode,
        extra_body=dict(registered_cfg.extra_body or {}),
    )
    req = UnifiedChatRequest(model_id=payload.model_id, messages=test_msgs, config=req_cfg)
    t0 = time.time()
    try:
        resp = adapter.chat(req)
        latency_ms = int((time.time() - t0) * 1000)
    except Exception as e:
        return {
            "status": "error",
            "latency_ms": int((time.time() - t0) * 1000),
            "error": str(e),
            "raw_content": "",
            "thinking": "",
            "parsed_data": None,
            "effective_config": req_cfg.model_dump(),
        }

    judge_engine = UniversalJudgeEngine(adapter)
    raw_content = resp.content or ""
    parsed_data = judge_engine._extract_json_from_response(raw_content)
    return {
        "status": "success",
        "latency_ms": latency_ms,
        "raw_content": raw_content,
        "thinking": resp.thinking or "",
        "parsed_data": parsed_data,
        "effective_config": req_cfg.model_dump(),
    }
