# -*- coding: utf-8 -*-
"""
数据飞轮与人工复核工作台 API
"""
import os
import json
import time
import uuid
from typing import List, Dict, Any, Optional, Tuple
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from backend.app.config import settings
from backend.app.models import (
    get_db,
    SessionLocal,
    TaskRecord,
    FlywheelSampleRecord,
    DatasetRecord,
)
from backend.app.schemas.chat import ChatMessage
from backend.app.schemas.rule import PersonaDefinition, UniversalRuleSchema
from backend.app.schemas.flywheel import (
    BatchJudgeRequest,
    BatchRemediateRequest,
    FlywheelRecord,
    InspectionReport,
    SingleJudgeRequest,
    SingleRemediateRequest,
    UpdateBenchmarkSlicesRequest,
    ExportBenchmarkDatasetRequest,
    BatchExportBenchmarkRequest,
)
from backend.app.api.deps import get_adapter_and_config_by_model_id, load_rule_schema
from backend.app.services.flywheel_service import FlywheelPipelineService
from backend.app.engine.benchmark_sampler import BenchmarkSamplingEngine

router = APIRouter(prefix="/flywheel", tags=["数据飞轮治理"])


class SampleReviewUpdateRequest(BaseModel):
    status: Optional[str] = None  # GOLD_PASS / REPAIRED_PASS / DISCARDED / MANUAL_APPROVED
    remediated_messages: Optional[List[Dict[str, Any]]] = None
    reviewer_comment: Optional[str] = None


class BatchUpdateStatusRequest(BaseModel):
    sample_ids: List[str] = Field(..., min_length=1)
    status: str  # GOLD_PASS / REPAIRED_PASS / DISCARDED / MANUAL_APPROVED
    reviewer_comment: Optional[str] = None


class BatchDeleteSamplesRequest(BaseModel):
    sample_ids: List[str] = Field(..., min_length=1)


def _parse_existing_initial_eval(sample: FlywheelSampleRecord) -> Optional[InspectionReport]:
    """当修复前不重新质检时，读取样本当前的初检结果；若尚未初检则返回 None。"""
    try:
        raw = json.loads(sample.initial_eval_json or "{}")
    except Exception:
        raw = {}
    if isinstance(raw, dict) and ("score" in raw or "verdict" in raw):
        try:
            return InspectionReport(**raw)
        except Exception:
            pass
    return None


class BatchExportSelectedRequest(BaseModel):
    sample_ids: List[str] = Field(..., min_length=1)
    version_tag: str = "v1.0"
    inject_thinking: bool = False


class BatchFlywheelRunRequest(BaseModel):
    rule_id: str
    input_file_path: str
    gold_output_path: Optional[str] = None
    discard_output_path: Optional[str] = None
    judge_model_id: str
    remediate_model_id: Optional[str] = None
    workers: int = 4


@router.post("/samples/batch-status")
def batch_update_sample_status(
    payload: BatchUpdateStatusRequest,
    db: Session = Depends(get_db),
):
    """
    批量更新选定样本的治理状态与评审备注
    """
    records = (
        db.query(FlywheelSampleRecord)
        .filter(FlywheelSampleRecord.id.in_(payload.sample_ids))
        .all()
    )
    if not records:
        raise HTTPException(status_code=404, detail="未找到指定的样本记录")

    now = int(time.time())
    for r in records:
        r.status = payload.status
        if payload.reviewer_comment is not None:
            r.reviewer_comment = payload.reviewer_comment
        r.updated_at = now

    db.commit()
    return {
        "message": f"成功批量更新 {len(records)} 条样本状态为 {payload.status}",
        "updated_count": len(records),
        "status": payload.status,
    }


@router.post("/samples/batch-delete")
def batch_delete_samples(
    payload: BatchDeleteSamplesRequest,
    db: Session = Depends(get_db),
):
    """
    批量删除选定的复核样本记录
    """
    query = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id.in_(payload.sample_ids))
    deleted_count = query.delete(synchronize_session=False)
    db.commit()
    return {
        "message": f"成功批量删除 {deleted_count} 条样本记录",
        "deleted_count": deleted_count,
    }


@router.post("/samples/batch-judge")
def batch_judge_samples(
    payload: BatchJudgeRequest,
    db: Session = Depends(get_db),
):
    """
    批量针对选定样本使用指定的标准规则 (rule_id) 与法官模型 (judge_model_id) 重新执行白盒质检:
    1. 重新验证拓扑连通性与节点红线;
    2. 若达标直接标定为 GOLD_PASS，防止因旧规则瑕疵误杀数据;
    3. 若未达标刷新缺陷诊断报告 initial_eval 与最新得分。
    """
    samples = (
        db.query(FlywheelSampleRecord)
        .filter(FlywheelSampleRecord.id.in_(payload.sample_ids))
        .all()
    )
    if not samples:
        raise HTTPException(status_code=404, detail="未找到待重新质检的样本")

    rule = load_rule_schema(db, payload.rule_id)
    judge_adapter, judge_config = get_adapter_and_config_by_model_id(db, payload.judge_model_id)
    pipeline = FlywheelPipelineService(
        judge_adapter=judge_adapter,
        remediate_adapter=None,
        judge_config=judge_config,
    )
    threshold = rule.inspection_rules.pass_score_threshold

    results = []
    notices: List[str] = []
    now = int(time.time())

    for sample in samples:
        old_status = sample.status
        old_score = sample.score
        sample.rule_id = rule.rule_id

        matched_sc = next(
            (s for s in rule.scenarios if s.id == sample.scenario_id),
            PersonaDefinition(id=sample.scenario_id, name="复核样本场景", instruction="保持原意图"),
        )

        raw_msgs = [ChatMessage(**m) for m in json.loads(sample.raw_messages_json or "[]")]

        try:
            eval_rep = pipeline.judge_dialogue(rule, matched_sc, raw_msgs)
            new_status = FlywheelPipelineService.resolve_rejudge_status(
                eval_rep, threshold, target_data="raw", current_status=old_status
            )

            sample.status = new_status
            sample.score = eval_rep.score
            sample.initial_eval_json = json.dumps(eval_rep.model_dump(), ensure_ascii=False)
            sample.updated_at = now
            if eval_rep.notices:
                notices.extend(eval_rep.notices)

            results.append({
                "sample_id": sample.id,
                "old_status": old_status,
                "new_status": new_status,
                "old_score": old_score,
                "new_score": eval_rep.score,
                "verdict": eval_rep.verdict,
                "defects": eval_rep.detected_defects,
                "notices": eval_rep.notices,
                "success": True,
            })
        except Exception as e:
            results.append({
                "sample_id": sample.id,
                "old_status": old_status,
                "new_status": old_status,
                "old_score": old_score,
                "new_score": old_score,
                "error": str(e),
                "success": False,
            })

    db.commit()
    succeeded = sum(1 for r in results if r.get("success", False))
    return {
        "message": f"批量重新质检已完成: 共 {len(samples)} 条，成功 {succeeded} 条",
        "total": len(samples),
        "succeeded": succeeded,
        "results": results,
        "notices": list(dict.fromkeys(notices)),
    }


@router.post("/samples/batch-remediate")
def batch_remediate_samples(
    payload: BatchRemediateRequest,
    db: Session = Depends(get_db),
):
    """
    批量针对选定样本触发定点修复:
    支持配置 run_initial_judge (修复前是否重检)、skip_if_gold_pass (满分是否免修直通)、
    run_verified_judge (修复后是否自动继续二次验收)。默认不质检、满分也不直接通过。
    """
    samples = (
        db.query(FlywheelSampleRecord)
        .filter(FlywheelSampleRecord.id.in_(payload.sample_ids))
        .all()
    )
    if not samples:
        raise HTTPException(status_code=404, detail="未找到待补考的样本")

    remediate_adapter, remediate_config = get_adapter_and_config_by_model_id(
        db, payload.remediate_model_id, fallback_mock_name="mock-remediate"
    )
    need_judge = bool(payload.run_initial_judge or payload.run_verified_judge)
    if need_judge:
        judge_adapter, judge_config = get_adapter_and_config_by_model_id(
            db, payload.judge_model_id, fallback_mock_name="mock-judge"
        )
    else:
        judge_adapter, judge_config = None, None

    pipeline = FlywheelPipelineService(
        judge_adapter=judge_adapter,
        remediate_adapter=remediate_adapter,
        judge_config=judge_config,
        remediate_config=remediate_config,
    )
    results = []
    notices: List[str] = []
    now = int(time.time())

    rules_cache = {}

    for sample in samples:
        rule_id = payload.rule_id or sample.rule_id
        if rule_id not in rules_cache:
            try:
                rules_cache[rule_id] = load_rule_schema(db, rule_id)
            except Exception:
                rules_cache[rule_id] = None

        rule = rules_cache.get(rule_id)
        if not rule:
            results.append({
                "sample_id": sample.id,
                "status": sample.status,
                "score": sample.score,
                "error": f"找不到规则 {rule_id}",
                "success": False,
            })
            continue

        sample.rule_id = rule.rule_id
        matched_sc = next(
            (s for s in rule.scenarios if s.id == sample.scenario_id),
            PersonaDefinition(id=sample.scenario_id, name="复核样本场景", instruction="保持原意图"),
        )

        raw_msgs = [ChatMessage(**m) for m in json.loads(sample.raw_messages_json or "[]")]
        old_status = sample.status
        old_score = sample.score
        precomputed_eval = None if payload.run_initial_judge else _parse_existing_initial_eval(sample)

        if not payload.run_initial_judge:
            remediable, skip_reason = FlywheelPipelineService.check_remediable_eval(precomputed_eval)
            if not remediable:
                results.append({
                    "sample_id": sample.id,
                    "old_status": old_status,
                    "new_status": old_status,
                    "old_score": old_score,
                    "new_score": old_score,
                    "skipped": True,
                    "success": False,
                    "error": skip_reason,
                })
                continue

        try:
            updated_rec: FlywheelRecord = pipeline.process_single_dialogue_record(
                rule,
                matched_sc,
                raw_msgs,
                sample_id=sample.id,
                precomputed_initial_eval=precomputed_eval,
                auto_remediate=True,
                skip_if_gold_pass=payload.skip_if_gold_pass,
                run_verified_judge=payload.run_verified_judge,
            )
            sample.status = updated_rec.status
            if updated_rec.verified_eval:
                sample.score = updated_rec.verified_eval.score
            elif payload.run_initial_judge and updated_rec.initial_eval:
                sample.score = updated_rec.initial_eval.score
            if payload.run_initial_judge and updated_rec.initial_eval:
                sample.initial_eval_json = json.dumps(updated_rec.initial_eval.model_dump(), ensure_ascii=False)
            sample.updated_at = now
            if updated_rec.notices:
                notices.extend(updated_rec.notices)

            if updated_rec.skipped_remediation:
                results.append({
                    "sample_id": sample.id,
                    "old_status": old_status,
                    "new_status": sample.status,
                    "old_score": old_score,
                    "new_score": sample.score,
                    "skipped": True,
                    "notices": updated_rec.notices,
                    "success": False,
                    "error": updated_rec.skip_reason,
                })
                continue

            remed_msgs: List[ChatMessage] = updated_rec.remediated_messages or []
            sample.remediated_messages_json = json.dumps([m.to_dict() for m in remed_msgs], ensure_ascii=False)
            sample.verified_eval_json = (
                json.dumps(updated_rec.verified_eval.model_dump(), ensure_ascii=False)
                if updated_rec.verified_eval
                else "{}"
            )
            sample.applied_actions_json = json.dumps(updated_rec.applied_actions, ensure_ascii=False)
            sample.benchmark_slices_json = json.dumps([s.model_dump() for s in updated_rec.benchmark_slices], ensure_ascii=False)
            sample.remediation_debug_json = json.dumps(updated_rec.remediation_debug or {}, ensure_ascii=False)

            results.append({
                "sample_id": sample.id,
                "old_status": old_status,
                "new_status": sample.status,
                "old_score": old_score,
                "new_score": sample.score,
                "notices": updated_rec.notices,
                "success": True,
            })
        except Exception as e:
            results.append({
                "sample_id": sample.id,
                "old_status": old_status,
                "new_status": old_status,
                "old_score": old_score,
                "new_score": old_score,
                "success": False,
                "error": str(e),
            })

    db.commit()
    succeeded = sum(1 for r in results if r.get("success", False))
    return {
        "message": f"批量修复已完成: 共 {len(samples)} 条，成功 {succeeded} 条",
        "total": len(samples),
        "succeeded": succeeded,
        "results": results,
        "notices": list(dict.fromkeys(notices)),
    }


@router.post("/samples/export-selected")
def export_selected_samples(
    payload: BatchExportSelectedRequest,
    db: Session = Depends(get_db),
):
    """
    从复核池中将选中的特定样本导出为标准 messages 格式的数据集 (.jsonl)
    """
    samples = (
        db.query(FlywheelSampleRecord)
        .filter(FlywheelSampleRecord.id.in_(payload.sample_ids))
        .all()
    )
    if not samples:
        raise HTTPException(status_code=400, detail="未找到选中的复核样本")

    export_fname = f"flywheel_selected_{payload.version_tag}_{int(time.time())}.jsonl"
    export_path = os.path.join(settings.DATASETS_DIR, export_fname)

    with open(export_path, "w", encoding="utf-8") as f:
        for s in samples:
            msgs = json.loads(s.remediated_messages_json or s.raw_messages_json or "[]")
            clean_msgs = []
            for m in msgs:
                entry = {"role": m.get("role"), "content": m.get("content")}
                if payload.inject_thinking and entry["role"] == "assistant":
                    entry["thinking"] = m.get("thinking", "")
                clean_msgs.append(entry)

            row = {
                "id": s.id,
                "metadata": {
                    "rule_id": s.rule_id,
                    "scenario_id": s.scenario_id,
                    "status": s.status,
                    "score": s.score,
                    "reviewer_comment": s.reviewer_comment,
                },
                "messages": clean_msgs,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return {
        "message": f"选中样本导出成功 (共 {len(samples)} 条)",
        "file_path": export_path,
        "sample_count": len(samples),
    }

@router.delete("/samples/{sample_id}")
def delete_flywheel_sample(
    sample_id: str,
    db: Session = Depends(get_db),
):
    """删除指定的对弈或复核样本记录"""
    sample = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).first()
    if not sample:
        raise HTTPException(status_code=404, detail="未找到该样本记录")
    db.delete(sample)
    db.commit()
    return {"message": "样本记录已成功删除", "sample_id": sample_id}


@router.post("/samples/{sample_id}/judge")
def judge_single_sample(
    sample_id: str,
    payload: SingleJudgeRequest,
    db: Session = Depends(get_db),
):
    """
    针对单个复核样本，重新使用指定的标准规则与法官模型执行白盒质检 (只检不修):
    - target_data="raw": 评估左侧【原实战会话】在新标准下能否免除修复直接达标 (更新 initial_eval, 达标晋升 GOLD_PASS);
    - target_data="remediated": 评估右侧【修复/微调后语料】的合规性 (更新 verified_eval, 达标晋升 REPAIRED_PASS).
    注意: 质检仅输出客观拓扑与红线扣分诊断报告，绝对不会自动重塑话术。
    """
    sample = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).first()
    if not sample:
        raise HTTPException(status_code=404, detail="未找到该样本")

    rule = load_rule_schema(db, payload.rule_id)
    judge_adapter, judge_config = get_adapter_and_config_by_model_id(db, payload.judge_model_id)
    pipeline = FlywheelPipelineService(
        judge_adapter=judge_adapter,
        remediate_adapter=None,
        judge_config=judge_config,
    )
    threshold = rule.inspection_rules.pass_score_threshold

    sample.rule_id = rule.rule_id
    matched_sc = next(
        (s for s in rule.scenarios if s.id == sample.scenario_id),
        PersonaDefinition(id=sample.scenario_id, name="复核样本场景", instruction="保持原意图"),
    )

    is_remediated_target = (payload.target_data == "remediated")
    msgs_json = (
        sample.remediated_messages_json
        if is_remediated_target and sample.remediated_messages_json
        else sample.raw_messages_json
    )
    dialogue_msgs = [ChatMessage(**m) for m in json.loads(msgs_json or "[]")]

    eval_rep = pipeline.judge_dialogue(rule, matched_sc, dialogue_msgs)
    new_status = FlywheelPipelineService.resolve_rejudge_status(
        eval_rep,
        threshold,
        target_data=payload.target_data,
        current_status=sample.status,
    )

    now = int(time.time())
    sample.status = new_status
    sample.score = eval_rep.score
    sample.updated_at = now

    if is_remediated_target:
        # 针对当前修复/微调语料做二次质检验收
        sample.verified_eval_json = json.dumps(eval_rep.model_dump(), ensure_ascii=False)
        msg_text = "修复后语料二次质检验收完成 (只检不修)"
    else:
        # 针对原实战会话做重新初检
        sample.initial_eval_json = json.dumps(eval_rep.model_dump(), ensure_ascii=False)
        msg_text = "原实战对话重新初检完成 (只检不修)"

    db.commit()

    return {
        "message": msg_text,
        "sample_id": sample_id,
        "rule_id": sample.rule_id,
        "status": sample.status,
        "score": sample.score,
        "target_data": payload.target_data,
        "initial_eval": json.loads(sample.initial_eval_json or "{}") if not is_remediated_target else None,
        "verified_eval": json.loads(sample.verified_eval_json or "{}") if is_remediated_target else None,
        "notices": eval_rep.notices,
    }


@router.post("/samples/{sample_id}/remediate")
def trigger_sample_remediation(
    sample_id: str,
    payload: Optional[SingleRemediateRequest] = None,
    rule_id: Optional[str] = None,
    judge_model_id: Optional[str] = None,
    remediate_model_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    针对单个复核样本触发修复:
    - run_initial_judge (默认 False): 默认不重新初检，直接使用当前初检结果执行修复；
    - skip_if_gold_pass (默认 False): 默认即使满分/达标也不跳过修复直接通过；
    - run_verified_judge (默认 False): 默认修复后不自动继续二次质检，状态置为 PENDING。
    """
    sample = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).first()
    if not sample:
        raise HTTPException(status_code=404, detail="未找到该样本")

    eff_rule_id = (payload.rule_id if payload and payload.rule_id else rule_id) or sample.rule_id
    eff_judge_model = (payload.judge_model_id if payload and payload.judge_model_id else judge_model_id)
    eff_remediate_model = (payload.remediate_model_id if payload and payload.remediate_model_id else remediate_model_id)
    eff_run_initial_judge = bool(payload.run_initial_judge) if payload else False
    eff_skip_if_gold_pass = bool(payload.skip_if_gold_pass) if payload else False
    eff_run_verified_judge = bool(payload.run_verified_judge) if payload else False

    rule = load_rule_schema(db, eff_rule_id)
    sample.rule_id = rule.rule_id

    # 查找对应场景
    matched_sc = next(
        (s for s in rule.scenarios if s.id == sample.scenario_id),
        PersonaDefinition(id=sample.scenario_id, name="复核样本场景", instruction="保持原意图"),
    )

    raw_msgs = [ChatMessage(**m) for m in json.loads(sample.raw_messages_json or "[]")]
    precomputed_eval = None if eff_run_initial_judge else _parse_existing_initial_eval(sample)

    if not eff_run_initial_judge:
        remediable, skip_reason = FlywheelPipelineService.check_remediable_eval(precomputed_eval)
        if not remediable:
            raise HTTPException(status_code=400, detail=skip_reason)

    remediate_adapter, remediate_config = get_adapter_and_config_by_model_id(
        db, eff_remediate_model, fallback_mock_name="mock-remediate"
    )
    if eff_run_initial_judge or eff_run_verified_judge:
        judge_adapter, judge_config = get_adapter_and_config_by_model_id(
            db, eff_judge_model, fallback_mock_name="mock-judge"
        )
    else:
        judge_adapter, judge_config = None, None

    pipeline = FlywheelPipelineService(
        judge_adapter=judge_adapter,
        remediate_adapter=remediate_adapter,
        judge_config=judge_config,
        remediate_config=remediate_config,
    )
    updated_rec: FlywheelRecord = pipeline.process_single_dialogue_record(
        rule,
        matched_sc,
        raw_msgs,
        sample_id=sample_id,
        precomputed_initial_eval=precomputed_eval,
        auto_remediate=True,
        skip_if_gold_pass=eff_skip_if_gold_pass,
        run_verified_judge=eff_run_verified_judge,
    )

    sample.status = updated_rec.status
    if updated_rec.verified_eval:
        sample.score = updated_rec.verified_eval.score
    elif eff_run_initial_judge and updated_rec.initial_eval:
        sample.score = updated_rec.initial_eval.score
    if eff_run_initial_judge and updated_rec.initial_eval:
        sample.initial_eval_json = json.dumps(updated_rec.initial_eval.model_dump(), ensure_ascii=False)
    sample.updated_at = int(time.time())

    if updated_rec.skipped_remediation:
        db.commit()
        existing_remed = (
            json.loads(sample.remediated_messages_json)
            if sample.remediated_messages_json
            else None
        )
        return {
            "message": updated_rec.skip_reason or "没有异常可以修复",
            "sample_id": sample_id,
            "rule_id": sample.rule_id,
            "status": sample.status,
            "score": sample.score,
            "remediated_messages": existing_remed,
            "initial_eval": json.loads(sample.initial_eval_json or "{}"),
            "verified_eval": (
                json.loads(sample.verified_eval_json)
                if sample.verified_eval_json and sample.verified_eval_json != "{}"
                else None
            ),
            "remediation_debug": json.loads(sample.remediation_debug_json or "{}") if sample.remediation_debug_json else None,
            "benchmark_slices": json.loads(sample.benchmark_slices_json or "[]"),
            "notices": updated_rec.notices,
            "skipped_remediation": True,
            "skip_reason": updated_rec.skip_reason,
        }

    remed_msgs: List[ChatMessage] = updated_rec.remediated_messages or []
    remed_dicts: List[Dict[str, Any]] = [m.to_dict() for m in remed_msgs]
    sample.remediated_messages_json = json.dumps(remed_dicts, ensure_ascii=False)
    sample.verified_eval_json = (
        json.dumps(updated_rec.verified_eval.model_dump(), ensure_ascii=False)
        if updated_rec.verified_eval
        else "{}"
    )
    sample.applied_actions_json = json.dumps(updated_rec.applied_actions, ensure_ascii=False)
    sample.benchmark_slices_json = json.dumps([s.model_dump() for s in updated_rec.benchmark_slices], ensure_ascii=False)
    sample.remediation_debug_json = json.dumps(updated_rec.remediation_debug or {}, ensure_ascii=False)
    db.commit()

    return {
        "message": "样本重新修复完成",
        "sample_id": sample_id,
        "rule_id": sample.rule_id,
        "status": sample.status,
        "score": sample.score,
        "remediated_messages": remed_dicts,
        "initial_eval": json.loads(sample.initial_eval_json or "{}"),
        "verified_eval": (
            json.loads(sample.verified_eval_json)
            if sample.verified_eval_json and sample.verified_eval_json != "{}"
            else None
        ),
        "remediation_debug": updated_rec.remediation_debug,
        "benchmark_slices": [s.model_dump() for s in updated_rec.benchmark_slices],
        "notices": updated_rec.notices,
        "skipped_remediation": False,
    }


@router.post("/batch")
def trigger_batch_flywheel(
    payload: BatchFlywheelRunRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    触发存量数据集批量飞轮治理任务 (初检 -> 修复 -> 二次质检验收 -> 优胜劣汰隔离):
    异步后台任务调度，支持断点续跑。
    """
    rule = load_rule_schema(db, payload.rule_id)
    task_id = f"fly_{str(uuid.uuid4())[:8]}"
    rec = TaskRecord(
        id=task_id,
        task_type="flywheel",
        status="PENDING",
        progress=0.0,
    )
    db.add(rec)
    db.commit()

    def _run_batch():
        bg_db = SessionLocal()
        task = bg_db.query(TaskRecord).filter(TaskRecord.id == task_id).first()
        task.status = "RUNNING"
        bg_db.commit()
        try:
            judge_adapter, judge_config = get_adapter_and_config_by_model_id(
                bg_db, payload.judge_model_id
            )
            remediate_adapter, remediate_config = (
                get_adapter_and_config_by_model_id(bg_db, payload.remediate_model_id)
                if payload.remediate_model_id
                else (None, None)
            )
            pipeline = FlywheelPipelineService(
                judge_adapter,
                remediate_adapter,
                judge_config=judge_config,
                remediate_config=remediate_config,
            )

            real_in = payload.input_file_path
            if not os.path.isabs(real_in):
                real_in = os.path.join(settings.BASE_DIR, real_in)

            records = []
            if os.path.exists(real_in):
                with open(real_in, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            records.append(json.loads(line))

            gold_out = payload.gold_output_path or os.path.join(settings.DATASETS_DIR, f"{payload.rule_id}_gold.jsonl")
            discard_out = payload.discard_output_path or os.path.join(settings.DATASETS_DIR, f"{payload.rule_id}_discarded.jsonl")

            stats = pipeline.batch_process_dataset(
                rule=rule,
                input_records=records,
                gold_output_path=gold_out,
                discard_output_path=discard_out,
                workers=payload.workers,
            )
            task.status = "COMPLETED"
            task.progress = 1.0
            task.result_json = json.dumps(stats, ensure_ascii=False)
            task.finished_at = int(time.time())
            bg_db.commit()
        except Exception as e:
            task.status = "FAILED"
            task.error_message = str(e)
            task.finished_at = int(time.time())
            bg_db.commit()
        finally:
            bg_db.close()

    background_tasks.add_task(_run_batch)
    return {"message": "批量飞轮治理任务已提交", "task_id": task_id}


@router.get("/samples")
def list_flywheel_samples(
    task_id: Optional[str] = None,
    status: Optional[str] = None,
    rule_id: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    query = db.query(FlywheelSampleRecord)
    if task_id:
        query = query.filter(FlywheelSampleRecord.task_id == task_id)
    if status and status != "ALL":
        query = query.filter(FlywheelSampleRecord.status == status)
    if rule_id and rule_id != "ALL":
        query = query.filter(FlywheelSampleRecord.rule_id == rule_id)

    records = query.order_by(FlywheelSampleRecord.updated_at.desc()).limit(limit).all()
    res = []
    for r in records:
        res.append({
            "id": r.id,
            "task_id": r.task_id,
            "rule_id": r.rule_id,
            "scenario_id": r.scenario_id,
            "status": r.status,
            "score": r.score,
            "raw_messages": json.loads(r.raw_messages_json or "[]"),
            "remediated_messages": json.loads(r.remediated_messages_json or "[]") if r.remediated_messages_json else None,
            "initial_eval": json.loads(r.initial_eval_json or "{}"),
            "verified_eval": json.loads(r.verified_eval_json or "{}"),
            "remediation_debug": json.loads(r.remediation_debug_json or "{}") if getattr(r, "remediation_debug_json", None) else None,
            "reviewer_comment": r.reviewer_comment,
            "exported_to_benchmark": bool(r.exported_to_benchmark),
            "updated_at": r.updated_at,
        })
    return res


@router.put("/samples/{sample_id}")
def update_sample_review(
    sample_id: str,
    payload: SampleReviewUpdateRequest,
    db: Session = Depends(get_db),
):
    sample = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).first()
    if not sample:
        raise HTTPException(status_code=404, detail="未找到该样本")

    if payload.status:
        sample.status = payload.status
    if payload.remediated_messages is not None:
        sample.remediated_messages_json = json.dumps(payload.remediated_messages, ensure_ascii=False)
    if payload.reviewer_comment is not None:
        sample.reviewer_comment = payload.reviewer_comment

    sample.updated_at = int(time.time())
    db.commit()
    return {"message": "样本复核更新成功", "id": sample_id, "status": sample.status}


@router.post("/export")
def export_gold_dataset(
    rule_id: str,
    version_tag: str = "v1.0",
    include_manual_approved: bool = True,
    inject_thinking: bool = False,
    exclude_benchmark_exported: bool = True,
    db: Session = Depends(get_db),
):
    """从复核池中一键导出符合标准 messages 格式的黄金微调训练集 (.jsonl)，默认排除已进评测集的同源对话"""
    allowed_statuses = ["GOLD_PASS", "REPAIRED_PASS"]
    if include_manual_approved:
        allowed_statuses.append("MANUAL_APPROVED")

    query = db.query(FlywheelSampleRecord).filter(
        FlywheelSampleRecord.rule_id == rule_id,
        FlywheelSampleRecord.status.in_(allowed_statuses),
    )
    if exclude_benchmark_exported:
        query = query.filter(
            (FlywheelSampleRecord.exported_to_benchmark == False) | (FlywheelSampleRecord.exported_to_benchmark.is_(None))  # noqa: E712
        )

    samples = query.all()

    if not samples:
        raise HTTPException(status_code=400, detail="该规则下暂无可导出的达标黄金样本")

    export_fname = f"flywheel_{rule_id}_{version_tag}_gold.jsonl"
    export_path = os.path.join(settings.DATASETS_DIR, export_fname)

    with open(export_path, "w", encoding="utf-8") as f:
        for s in samples:
            msgs = json.loads(s.remediated_messages_json or s.raw_messages_json or "[]")
            clean_msgs = []
            for m in msgs:
                entry = {"role": m.get("role"), "content": m.get("content")}
                if inject_thinking and entry["role"] == "assistant":
                    entry["thinking"] = m.get("thinking", "")
                clean_msgs.append(entry)

            row = {
                "id": s.id,
                "metadata": {
                    "rule_id": s.rule_id,
                    "scenario_id": s.scenario_id,
                    "status": s.status,
                    "score": s.score,
                    "reviewer_comment": s.reviewer_comment,
                },
                "messages": clean_msgs,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return {
        "message": "黄金微调数据集导出成功",
        "file_path": export_path,
        "sample_count": len(samples),
    }


def _ensure_db_sample_slices(
    db: Session,
    sample: FlywheelSampleRecord,
    rule_cache: Optional[Dict[str, UniversalRuleSchema]] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """读取或按需补生成单条飞轮样本的评测切片（不直接提交事务，由调用方统一控制落库时机）。"""
    existing_slices = json.loads(sample.benchmark_slices_json or "[]")
    if existing_slices:
        return existing_slices, False

    if rule_cache is not None:
        if sample.rule_id not in rule_cache:
            rule_cache[sample.rule_id] = load_rule_schema(db, sample.rule_id)
        rule = rule_cache[sample.rule_id]
    else:
        rule = load_rule_schema(db, sample.rule_id)

    raw_msgs = [ChatMessage(**m) for m in json.loads(sample.raw_messages_json or "[]")]
    actions = json.loads(sample.applied_actions_json or "[]")
    initial_eval = json.loads(sample.initial_eval_json or "{}")
    remed_msgs = (
        [ChatMessage(**m) for m in json.loads(sample.remediated_messages_json or "[]")]
        if sample.remediated_messages_json
        else None
    )

    slices_data, generated = FlywheelPipelineService.ensure_sample_slices(
        sample_id=sample.id,
        existing_slices=existing_slices,
        raw_messages=raw_msgs,
        applied_actions=actions,
        initial_eval_deductions=initial_eval.get("deductions", []),
        rule=rule,
        scenario_id=sample.scenario_id,
        remediated_messages=remed_msgs,
    )
    if generated:
        sample.benchmark_slices_json = json.dumps(slices_data, ensure_ascii=False)
    return slices_data, generated


def _build_benchmark_rows_from_slices(
    sample_id: str,
    slices: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """将飞轮评测切片字典列表转换为标准 Benchmark JSONL 行格式。"""
    return [
        {
            "id": sl["slice_id"],
            "category": sl.get("category") or "飞轮实战质检",
            "sub_category": sl.get("sub_category") or sl.get("defect_item"),
            "difficulty": "medium",
            "source": f"flywheel_{sample_id}",
            "is_holdout": True,
            "messages": sl["messages"],
            "reference": sl["reference"],
            "rubric": sl["rubric"],
            "regex_constraints": sl.get("regex_constraints", []),
        }
        for sl in slices
    ]


def _write_and_register_benchmark_dataset(
    db: Session,
    dataset_id: str,
    dataset_name: str,
    mode: str,
    bench_rows: List[Dict[str, Any]],
    exported_samples: List[FlywheelSampleRecord],
) -> Tuple[str, int]:
    """合并写入 Benchmark JSONL 文件、注册 DatasetRecord、锁定防泄漏标记并按需重置抽样账本。"""
    file_path = os.path.join(settings.DATASETS_DIR, f"{dataset_id}.jsonl")

    existing_rows_map: Dict[str, Dict[str, Any]] = {}
    if mode == "append" and os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        d = json.loads(line)
                        if "id" in d:
                            existing_rows_map[d["id"]] = d
                    except Exception:
                        pass

    for r in bench_rows:
        existing_rows_map[r["id"]] = r

    final_rows = list(existing_rows_map.values()) if existing_rows_map else bench_rows

    os.makedirs(settings.DATASETS_DIR, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        for r in final_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    ds_record = db.query(DatasetRecord).filter(DatasetRecord.id == dataset_id).first()
    if not ds_record:
        ds_record = DatasetRecord(
            id=dataset_id,
            name=dataset_name,
            file_path=file_path,
            sample_count=len(final_rows),
            dataset_type="benchmark",
        )
        db.add(ds_record)
    else:
        ds_record.name = dataset_name
        ds_record.file_path = file_path
        ds_record.sample_count = len(final_rows)
        ds_record.dataset_type = "benchmark"

    # 源头互斥锁定: 仅在文件落盘成功后，标记源样本已进入纯评测留出池
    now_ts = int(time.time())
    for s in exported_samples:
        s.exported_to_benchmark = True
        s.updated_at = now_ts

    db.commit()

    if mode == "create":
        BenchmarkSamplingEngine.reset_trackers_in_db(db=db, dataset_path=file_path)

    return file_path, len(final_rows)


@router.get("/samples/{sample_id}/slices")
def get_sample_slices(sample_id: str, db: Session = Depends(get_db)):
    """获取单个样本的评测切片：优先读库，未生成则即时切片并持久化"""
    sample = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).first()
    if not sample:
        raise HTTPException(status_code=404, detail="未找到该样本")

    slices_data, generated = _ensure_db_sample_slices(db, sample)
    if generated:
        db.commit()

    return {
        "sample_id": sample_id,
        "slices": slices_data,
        "source": "generated" if generated else "saved",
    }


@router.put("/samples/{sample_id}/slices")
def update_sample_slices(sample_id: str, payload: UpdateBenchmarkSlicesRequest, db: Session = Depends(get_db)):
    """保存人工微调后的评测切片，并双向原子化回写多轮会话 remediated_messages"""
    sample = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).first()
    if not sample:
        raise HTTPException(status_code=404, detail="未找到该样本")

    slices_dicts = [s.model_dump() for s in payload.slices]
    sample.benchmark_slices_json = json.dumps(slices_dicts, ensure_ascii=False)

    # 双向同步回写：若微调了切片的 reference，委托 FlywheelPipelineService 重放同步 remediated_messages 与 applied_actions
    if payload.sync_to_dialogue and sample.remediated_messages_json:
        raw_msgs = [ChatMessage(**m) for m in json.loads(sample.raw_messages_json or "[]")]
        remed_msgs = [ChatMessage(**m) for m in json.loads(sample.remediated_messages_json or "[]")]
        existing_actions = json.loads(sample.applied_actions_json or "[]")
        if not isinstance(existing_actions, list):
            existing_actions = []
        rule = load_rule_schema(db, sample.rule_id)

        updated_actions, sanitized_msgs = FlywheelPipelineService.sync_slices_to_dialogue(
            slices=payload.slices,
            raw_messages=raw_msgs,
            remediated_messages=remed_msgs,
            existing_actions=existing_actions,
            rule=rule,
        )
        if updated_actions:
            sample.applied_actions_json = json.dumps(updated_actions, ensure_ascii=False)
        if sanitized_msgs is not None:
            sanitized_dicts: List[Dict[str, Any]] = [m.to_dict() for m in sanitized_msgs]
            sample.remediated_messages_json = json.dumps(sanitized_dicts, ensure_ascii=False)

    sample.updated_at = int(time.time())
    db.commit()
    return {"message": "切片保存与对话双向同步成功", "slice_count": len(payload.slices)}


@router.post("/samples/{sample_id}/export-benchmark")
def export_single_sample_benchmark(
    sample_id: str,
    payload: ExportBenchmarkDatasetRequest,
    db: Session = Depends(get_db),
):
    """将单个样本的切片导出为标准 Benchmark 评测集文件并在 datasets 表中注册"""
    sample = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).first()
    if not sample:
        raise HTTPException(status_code=404, detail="未找到该样本")

    slices_raw, generated = _ensure_db_sample_slices(db, sample)
    if generated:
        db.commit()

    if not slices_raw:
        raise HTTPException(status_code=400, detail="该样本无有效切片可导出")

    if payload.slice_ids:
        selected_slices = [s for s in slices_raw if s["slice_id"] in payload.slice_ids]
    else:
        selected_slices = slices_raw

    if not selected_slices:
        raise HTTPException(status_code=400, detail="未选定任何有效切片")

    dataset_id = payload.dataset_id.strip() or f"flywheel_{sample_id}_eval"
    dataset_name = payload.name.strip() or f"飞轮样本评测集 ({sample_id})"
    bench_rows = _build_benchmark_rows_from_slices(sample_id, selected_slices)

    file_path, sample_count = _write_and_register_benchmark_dataset(
        db=db,
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        mode=payload.mode,
        bench_rows=bench_rows,
        exported_samples=[sample],
    )

    return {
        "message": "评测集导出成功（已标记为纯评测留出集并与训练集隔离）",
        "dataset_id": dataset_id,
        "file_path": file_path,
        "sample_count": sample_count,
    }


@router.post("/batch-export-benchmark")
def batch_export_benchmark_dataset(
    payload: BatchExportBenchmarkRequest,
    db: Session = Depends(get_db),
):
    """批量将多个勾选样本的切片合并导出为标准 Benchmark 评测集"""
    samples = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id.in_(payload.sample_ids)).all()
    if not samples:
        raise HTTPException(status_code=404, detail="未找到选中的任何样本")

    dataset_id = payload.dataset_id.strip()
    dataset_name = payload.name.strip()

    all_bench_rows: List[Dict[str, Any]] = []
    exported_samples: List[FlywheelSampleRecord] = []
    rule_cache: Dict[str, UniversalRuleSchema] = {}

    for s in samples:
        slices_raw, _ = _ensure_db_sample_slices(db, s, rule_cache=rule_cache)
        if not slices_raw:
            continue

        target_slices = (
            [slices_raw[0]]
            if payload.strategy == "primary_defect_only"
            else slices_raw
        )
        all_bench_rows.extend(_build_benchmark_rows_from_slices(s.id, target_slices))
        exported_samples.append(s)

    if not all_bench_rows:
        raise HTTPException(status_code=400, detail="所选样本中未生成任何有效评测切片")

    file_path, sample_count = _write_and_register_benchmark_dataset(
        db=db,
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        mode=payload.mode,
        bench_rows=all_bench_rows,
        exported_samples=exported_samples,
    )

    return {
        "message": "批量导出评测集成功（已锁定源样本不进训练集）",
        "dataset_id": dataset_id,
        "file_path": file_path,
        "sample_count": sample_count,
    }
