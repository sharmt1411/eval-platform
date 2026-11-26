# -*- coding: utf-8 -*-
"""
数据飞轮与人工复核工作台 API
"""
import os
import re
import json
import time
import uuid
import hashlib
import threading
from typing import List, Dict, Any, Optional, Tuple, Set, Literal
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Body
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
    BatchUpdateStatusRequest,
    BatchDeleteSamplesRequest,
    BatchExportSelectedRequest,
    BatchFlywheelRunRequest,
    BatchJudgeRequest,
    BatchRemediateRequest,
    DedupConfig,
    FlywheelRecord,
    InspectionReport,
    SingleJudgeRequest,
    SingleRemediateRequest,
    UpdateBenchmarkSlicesRequest,
    ExportBenchmarkDatasetRequest,
    BatchExportBenchmarkRequest,
    DatasetInspectRequest,
)
from backend.app.api.deps import (
    add_dataset_to_sample_lineage,
    ensure_sample_state_reconciled,
    get_adapter_and_config_by_model_id,
    get_rule_threshold,
    get_rule_updated_at,
    get_sample_benchmark_datasets,
    get_sample_training_datasets,
    is_sample_exported_to_benchmark,
    load_rule_schema,
    messages_to_dicts,
    sync_lineage_on_dataset_overwritten,
    sync_lineage_on_dataset_removed,
    upsert_flywheel_sample,
)
from backend.app.services.flywheel_service import FlywheelPipelineService
from backend.app.engine.benchmark_sampler import BenchmarkSamplingEngine

router = APIRouter(prefix="/flywheel", tags=["数据飞轮治理"])


class SampleReviewUpdateRequest(BaseModel):
    status: Optional[str] = None  # GOLD_PASS / REPAIRED_PASS / DISCARDED / MANUAL_APPROVED / PENDING
    active_version: Optional[Literal["raw", "remediated"]] = None
    remediated_messages: Optional[List[Dict[str, Any]]] = None
    reviewer_comment: Optional[str] = None


class ExportGoldOptionsBody(BaseModel):
    mode: Optional[str] = None
    target_dataset_id: Optional[str] = None
    dedup_config: Optional[DedupConfig] = None


def _refresh_sample_user_hashes(
    sample: FlywheelSampleRecord,
    raw_msgs: Optional[List[Any]] = None,
    remed_msgs: Optional[List[Any]] = None,
) -> None:
    """同步刷新 FlywheelSampleRecord 的原/修后客户输入序列双哈希。"""
    raw_src: List[Any] = (
        raw_msgs
        if raw_msgs is not None
        else json.loads(sample.raw_messages_json or "[]")
    )
    remed_src: Optional[List[Any]] = (
        remed_msgs
        if remed_msgs is not None
        else (json.loads(sample.remediated_messages_json) if sample.remediated_messages_json else None)
    )
    raw_h, remed_h = FlywheelPipelineService.compute_dialogue_user_hashes(raw_src, remed_src)
    sample.raw_user_hash = raw_h
    sample.remediated_user_hash = remed_h


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


def _has_nonempty_messages_json(raw_json: Optional[str]) -> bool:
    if not raw_json:
        return False
    try:
        arr = json.loads(raw_json)
        return isinstance(arr, list) and len(arr) > 0
    except Exception:
        return False


def _serialize_flywheel_sample(
    db: Session,
    r: FlywheelSampleRecord,
    threshold_cache: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    if threshold_cache is not None:
        if r.rule_id not in threshold_cache:
            threshold_cache[r.rule_id] = get_rule_threshold(db, r.rule_id)
        th = threshold_cache[r.rule_id]
    else:
        th = get_rule_threshold(db, r.rule_id)
    ensure_sample_state_reconciled(r, threshold=th)
    return {
        "id": r.id,
        "task_id": r.task_id,
        "rule_id": r.rule_id,
        "scenario_id": r.scenario_id,
        "status": r.status,
        "raw_verdict": r.raw_verdict or "UNJUDGED",
        "remediated_verdict": r.remediated_verdict or "NONE",
        "manual_decision": r.manual_decision or "NONE",
        "active_version": r.active_version or "raw",
        "evaluated_rule_updated_at": r.evaluated_rule_updated_at,
        "score": r.score,
        "raw_messages": json.loads(r.raw_messages_json or "[]"),
        "remediated_messages": json.loads(r.remediated_messages_json or "[]") if r.remediated_messages_json else None,
        "initial_eval": json.loads(r.initial_eval_json or "{}"),
        "verified_eval": json.loads(r.verified_eval_json or "{}"),
        "remediation_debug": json.loads(r.remediation_debug_json or "{}") if getattr(r, "remediation_debug_json", None) else None,
        "reviewer_comment": r.reviewer_comment,
        "exported_to_benchmark": is_sample_exported_to_benchmark(r),
        "exported_training_datasets": get_sample_training_datasets(r),
        "exported_benchmark_datasets": get_sample_benchmark_datasets(r),
        "updated_at": r.updated_at,
    }


def _apply_status_transition_for_sample(
    db: Session,
    sample: FlywheelSampleRecord,
    target_status: Optional[str],
    active_version: Optional[str],
    reviewer_comment_incoming: Optional[str],
    remediated_messages_edited: bool = False,
    default_sys_cache: Optional[Dict[str, str]] = None,
    allow_error_with_comment: bool = True,
) -> None:
    """统一执行单条或批量人工状态流转与版本/质检门禁校验。"""
    ensure_sample_state_reconciled(sample, threshold=get_rule_threshold(db, sample.rule_id))
    has_remed = _has_nonempty_messages_json(sample.remediated_messages_json)

    def _get_default_sys(rid: str) -> str:
        if default_sys_cache is not None and rid in default_sys_cache:
            return default_sys_cache[rid]
        try:
            sys_txt = load_rule_schema(db, rid).bot_config.system_prompt
        except Exception:
            sys_txt = "你是专业的业务对话机器人。"
        if default_sys_cache is not None:
            default_sys_cache[rid] = sys_txt
        return sys_txt

    if active_version is not None:
        if active_version == "remediated" and not has_remed:
            raise HTTPException(
                status_code=400,
                detail=f"样本 {sample.id} 尚无修复后对话，无法将生效版本切换为 remediated",
            )
        sample.active_version = active_version

    if not target_status:
        sample.status = FlywheelPipelineService.derive_sample_status(
            sample.raw_verdict, sample.remediated_verdict, sample.manual_decision, sample.active_version
        )
        return

    st = target_status.upper()
    if st == "MANUAL_APPROVED":
        has_valid_raw = (sample.raw_verdict in ("PASS", "FAIL"))
        has_valid_remed = (sample.remediated_verdict in ("PASS", "FAIL"))
        has_comment = bool((reviewer_comment_incoming if reviewer_comment_incoming is not None else sample.reviewer_comment or "").strip())

        if sample.raw_verdict == "UNJUDGED" and not has_valid_remed and not remediated_messages_edited:
            raise HTTPException(
                status_code=400,
                detail=f"样本 {sample.id} 尚未执行任何规则质检，禁止直接人工放行；请先执行规则质检",
            )
        if not has_valid_raw and not has_valid_remed:
            if not allow_error_with_comment or (not has_comment and not remediated_messages_edited):
                raise HTTPException(
                    status_code=400,
                    detail=f"样本 {sample.id} 质检异常或未完成有效质检（raw_verdict={sample.raw_verdict}），请先重检或在单条复核中填写备注/修订对话后再放行",
                )

        eff_active = active_version or ("remediated" if has_remed else "raw")
        if eff_active == "remediated" and not has_remed:
            raise HTTPException(
                status_code=400,
                detail=f"样本 {sample.id} 指定生效版本为 remediated，但修复后对话为空",
            )
        msgs_raw = (
            json.loads(sample.remediated_messages_json or "[]")
            if eff_active == "remediated"
            else json.loads(sample.raw_messages_json or "[]")
        )
        ok_fmt, fmt_err = FlywheelPipelineService.validate_manual_dialogue_messages(
            msgs_raw, default_system_prompt=_get_default_sys(sample.rule_id)
        )
        if not ok_fmt:
            raise HTTPException(
                status_code=400,
                detail=f"样本 {sample.id} ({eff_active}) 对话格式不合规，无法人工放行: {fmt_err}",
            )
        sample.active_version = eff_active
        sample.manual_decision = "APPROVED"
        rule_ts = get_rule_updated_at(db, sample.rule_id)
        if rule_ts is not None:
            sample.evaluated_rule_updated_at = rule_ts
    elif st == "DISCARDED":
        sample.manual_decision = "REJECTED"
    elif st == "PENDING":
        sample.manual_decision = "NONE"
    elif st == "GOLD_PASS":
        if sample.raw_verdict != "PASS":
            raise HTTPException(
                status_code=400,
                detail=f"样本 {sample.id} 原对话未通过规则质检 (raw_verdict={sample.raw_verdict})，不能直接标为直通达标 (GOLD_PASS)；人工确认放行请使用 MANUAL_APPROVED",
            )
        sample.manual_decision = "NONE"
        sample.active_version = "raw"
    elif st == "REPAIRED_PASS":
        if sample.remediated_verdict != "PASS":
            raise HTTPException(
                status_code=400,
                detail=f"样本 {sample.id} 修复后对话未通过二次验收 (remediated_verdict={sample.remediated_verdict})，不能标为 REPAIRED_PASS；请先验收或使用 MANUAL_APPROVED",
            )
        sample.manual_decision = "NONE"
        sample.active_version = "remediated"
    else:
        raise HTTPException(status_code=400, detail=f"不支持的样本状态 '{target_status}'")

    sample.status = FlywheelPipelineService.derive_sample_status(
        sample.raw_verdict, sample.remediated_verdict, sample.manual_decision, sample.active_version
    )


@router.post("/samples/batch-status")
def batch_update_sample_status(
    payload: BatchUpdateStatusRequest,
    db: Session = Depends(get_db),
):
    """
    批量更新选定样本的治理状态与评审备注（校验人工放行必须具备有效质检与非空合规对话）
    """
    records = (
        db.query(FlywheelSampleRecord)
        .filter(FlywheelSampleRecord.id.in_(payload.sample_ids))
        .all()
    )
    if not records:
        raise HTTPException(status_code=404, detail="未找到指定的样本记录")

    now = int(time.time())
    default_sys_cache: Dict[str, str] = {}
    for r in records:
        _apply_status_transition_for_sample(
            db=db,
            sample=r,
            target_status=payload.status,
            active_version=payload.active_version,
            reviewer_comment_incoming=payload.reviewer_comment,
            remediated_messages_edited=False,
            default_sys_cache=default_sys_cache,
            allow_error_with_comment=False,
        )
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
    current_rule_ts = get_rule_updated_at(db, rule.rule_id)

    results = []
    notices: List[str] = []
    now = int(time.time())

    for sample in samples:
        ensure_sample_state_reconciled(sample, threshold=threshold)
        old_status = sample.status
        old_score = sample.score
        rule_changed = (sample.rule_id != rule.rule_id) or (
            sample.evaluated_rule_updated_at is not None
            and current_rule_ts is not None
            and int(sample.evaluated_rule_updated_at) != int(current_rule_ts)
        )
        sample.rule_id = rule.rule_id

        matched_sc = next(
            (s for s in rule.scenarios if s.id == sample.scenario_id),
            PersonaDefinition(id=sample.scenario_id, name="复核样本场景", instruction="保持原意图"),
        )

        raw_msgs = [ChatMessage(**m) for m in json.loads(sample.raw_messages_json or "[]")]

        try:
            eval_rep = pipeline.judge_dialogue(rule, matched_sc, raw_msgs)
            has_remed = _has_nonempty_messages_json(sample.remediated_messages_json)
            trans = FlywheelPipelineService.apply_rejudge_transition(
                eval_report=eval_rep,
                threshold=threshold,
                target_data="raw",
                raw_verdict=sample.raw_verdict or "UNJUDGED",
                remediated_verdict=sample.remediated_verdict or "NONE",
                manual_decision=sample.manual_decision or "NONE",
                active_version=sample.active_version or "raw",
                has_remediated_messages=has_remed,
                rule_changed=rule_changed,
            )

            sample.raw_verdict = trans["raw_verdict"]
            sample.remediated_verdict = trans["remediated_verdict"]
            sample.manual_decision = trans["manual_decision"]
            sample.active_version = trans["active_version"]
            sample.status = trans["status"]
            sample.evaluated_rule_updated_at = current_rule_ts
            sample.score = eval_rep.score
            sample.initial_eval_json = json.dumps(eval_rep.model_dump(), ensure_ascii=False)
            sample.updated_at = now
            if eval_rep.notices:
                notices.extend(eval_rep.notices)

            results.append({
                "sample_id": sample.id,
                "old_status": old_status,
                "new_status": trans["status"],
                "raw_verdict": trans["raw_verdict"],
                "remediated_verdict": trans["remediated_verdict"],
                "manual_decision": trans["manual_decision"],
                "active_version": trans["active_version"],
                "rule_changed": rule_changed,
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
            sample.remediated_messages_json = json.dumps(messages_to_dicts(remed_msgs), ensure_ascii=False)
            _refresh_sample_user_hashes(sample, raw_msgs=raw_msgs, remed_msgs=remed_msgs)
            sample.verified_eval_json = (
                json.dumps(updated_rec.verified_eval.model_dump(), ensure_ascii=False)
                if updated_rec.verified_eval
                else "{}"
            )
            sample.applied_actions_json = json.dumps(updated_rec.applied_actions, ensure_ascii=False)
            sample.benchmark_slices_json = json.dumps([s.model_dump() for s in updated_rec.benchmark_slices], ensure_ascii=False)
            sample.remediation_debug_json = json.dumps(updated_rec.remediation_debug or {}, ensure_ascii=False)

            decoupled = FlywheelPipelineService.compute_record_decoupled_state(
                updated_rec, threshold=rule.inspection_rules.pass_score_threshold
            )
            sample.raw_verdict = decoupled["raw_verdict"]
            sample.remediated_verdict = decoupled["remediated_verdict"]
            sample.manual_decision = decoupled["manual_decision"]
            sample.active_version = decoupled["active_version"]
            sample.status = decoupled["status"]
            sample.evaluated_rule_updated_at = get_rule_updated_at(db, rule.rule_id)

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


def _export_training_rows_to_dataset(
    db: Session,
    samples: List[FlywheelSampleRecord],
    export_path: str,
    dataset_id: str,
    inject_thinking: bool,
    mode: str = "create",
    dedup_config: Optional[DedupConfig] = None,
) -> Tuple[int, Dict[str, Any]]:
    """
    按 active_version 严格提取对应版本的对话，构建带轨迹/FAQ 元数据的候选行，
    执行四步去重防稀释写盘、注册 DatasetRecord、回写样本池训练集留底，
    并在存在已切题进过评测集的同源样本时原子改写对应评测集中的 is_holdout 为 False。
    """
    default_sys_cache: Dict[str, str] = {}
    sample_by_id: Dict[str, FlywheelSampleRecord] = {}
    candidate_rows: List[Dict[str, Any]] = []

    for s in samples:
        ensure_sample_state_reconciled(s, threshold=get_rule_threshold(db, s.rule_id))
        sample_by_id[s.id] = s
        if s.rule_id not in default_sys_cache:
            try:
                default_sys_cache[s.rule_id] = load_rule_schema(db, s.rule_id).bot_config.system_prompt
            except Exception:
                default_sys_cache[s.rule_id] = "你是专业的业务对话机器人。"
        default_sys = default_sys_cache[s.rule_id]

        raw_msgs = json.loads(s.raw_messages_json or "[]")
        remed_msgs = json.loads(s.remediated_messages_json) if s.remediated_messages_json else None
        eff_version = s.active_version or "raw"

        ok_prep, clean_msgs, prep_err = FlywheelPipelineService.prepare_dialogue_for_training_export(
            raw_messages=raw_msgs,
            remediated_messages=remed_msgs,
            active_version=eff_version,
            default_system_prompt=default_sys,
            inject_thinking=inject_thinking,
        )
        if not ok_prep:
            raise HTTPException(
                status_code=400,
                detail=f"样本 {s.id} 按生效版本 ({eff_version}) 导出失败: {prep_err}",
            )

        init_eval: Dict[str, Any] = {}
        ver_eval: Dict[str, Any] = {}
        try:
            init_eval = json.loads(s.initial_eval_json or "{}")
        except Exception:
            pass
        try:
            ver_eval = json.loads(s.verified_eval_json or "{}")
        except Exception:
            pass

        dedup_meta: Dict[str, Any] = FlywheelPipelineService.extract_sample_dedup_meta(
            init_eval if isinstance(init_eval, dict) else None,
            ver_eval if isinstance(ver_eval, dict) else None,
        )
        candidate_rows.append({
            "id": s.id,
            "metadata": {
                "source_sample_id": s.id,
                "active_version": eff_version,
                "rule_id": s.rule_id,
                "scenario_id": s.scenario_id,
                "status": s.status,
                "score": s.score,
                "reviewer_comment": s.reviewer_comment,
            },
            "messages": clean_msgs,
            "_dedup_meta": dedup_meta,
        })

    existing_rows: Optional[List[Dict[str, Any]]] = None
    if mode == "append" and os.path.exists(export_path):
        loaded: List[Dict[str, Any]] = []
        with open(export_path, "r", encoding="utf-8") as ef:
            for line in ef:
                s_line = line.strip()
                if s_line:
                    try:
                        obj = json.loads(s_line)
                        if isinstance(obj, dict):
                            loaded.append(obj)
                    except Exception:
                        pass
        existing_rows = loaded

    final_rows, dedup_stats = FlywheelPipelineService.deduplicate_training_export(
        candidate_rows=candidate_rows,
        existing_rows=existing_rows,
        config=dedup_config,
        dataset_file_path=export_path,
    )

    os.makedirs(settings.DATASETS_DIR, exist_ok=True)
    tmp_path: str = f"{export_path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for row in final_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp_path, export_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise

    if mode == "create" and not bool(dedup_stats.get("enabled")):
        stale_cache = FlywheelPipelineService.get_dedup_cache_path(export_path)
        if os.path.exists(stale_cache):
            try:
                os.remove(stale_cache)
            except Exception:
                pass

    export_fname: str = os.path.basename(export_path)
    ds_rec = db.query(DatasetRecord).filter(DatasetRecord.id == dataset_id).first()
    if ds_rec is None:
        ds_rec = DatasetRecord(
            id=dataset_id,
            name=export_fname,
            file_path=export_path,
            sample_count=len(final_rows),
            dataset_type="flywheel_gold",
            created_at=int(time.time()),
        )
        db.add(ds_rec)
    else:
        ds_rec.name = export_fname
        ds_rec.file_path = export_path
        ds_rec.sample_count = len(final_rows)
        ds_rec.dataset_type = "flywheel_gold"
        ds_rec.created_at = int(time.time())

    # 回写训练集导出留底：仅记录去重防稀释后真正保留进目标文件的样本
    if mode == "create":
        sync_lineage_on_dataset_overwritten(
            db=db,
            dataset_id=dataset_id,
            dataset_type="flywheel_gold",
            final_rows=final_rows,
            commit=False,
        )
    else:
        for row in final_rows:
            if not isinstance(row, dict):
                continue
            r_meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            src_sid = str(r_meta.get("source_sample_id") or row.get("id") or "").strip()
            if src_sid in sample_by_id:
                add_dataset_to_sample_lineage(sample_by_id[src_sid], dataset_id, "training")

    # 联动改写：若本次真正保留进训练集的样本此前切题进过评测集，必须将其评测集同源题从留出题改写为普通题
    retained_conflict_ids: Set[str] = set()
    conflict_bench_ids: Set[str] = set()
    for row in final_rows:
        if not isinstance(row, dict):
            continue
        r_meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        src_sid = str(r_meta.get("source_sample_id") or row.get("id") or "").strip()
        cand_s = sample_by_id.get(src_sid)
        if cand_s is not None and is_sample_exported_to_benchmark(cand_s):
            retained_conflict_ids.add(cand_s.id)
            conflict_bench_ids.update(get_sample_benchmark_datasets(cand_s))

    if retained_conflict_ids and conflict_bench_ids:
        bench_paths: List[str] = []
        bench_recs = (
            db.query(DatasetRecord)
            .filter(DatasetRecord.id.in_(list(conflict_bench_ids)))
            .all()
        )
        found_ids: Set[str] = set()
        for br in bench_recs:
            found_ids.add(br.id)
            if br.file_path:
                bench_paths.append(br.file_path)
        for bid in conflict_bench_ids:
            if bid not in found_ids:
                bench_paths.append(os.path.join(settings.DATASETS_DIR, f"{bid}.jsonl"))
        FlywheelPipelineService.rewrite_benchmark_holdout_for_samples(
            bench_paths, retained_conflict_ids
        )

    db.commit()

    return len(final_rows), dedup_stats


@router.post("/samples/export-selected")
def export_selected_samples(
    payload: BatchExportSelectedRequest,
    db: Session = Depends(get_db),
):
    """
    从复核池中将选中的达标样本导出为标准 messages 格式的数据集 (.jsonl)，
    统一校验样本必须达标（GOLD_PASS / REPAIRED_PASS / MANUAL_APPROVED），
    默认排除已导出进评测集的留出题源对话（显式放行时同步把对应评测题改为普通题），并应用去重防稀释。
    """
    samples = (
        db.query(FlywheelSampleRecord)
        .filter(FlywheelSampleRecord.id.in_(payload.sample_ids))
        .all()
    )
    if not samples:
        raise HTTPException(status_code=400, detail="未找到选中的复核样本")

    allowed_statuses = {"GOLD_PASS", "REPAIRED_PASS", "MANUAL_APPROVED"}
    unqualified: List[str] = []
    for s in samples:
        ensure_sample_state_reconciled(s, threshold=get_rule_threshold(db, s.rule_id))
        if (s.status or "PENDING") not in allowed_statuses:
            unqualified.append(f"{s.id}({s.status})")
    if unqualified:
        raise HTTPException(
            status_code=400,
            detail=(
                "仅允许导出已达标或人工放行的对话（GOLD_PASS / REPAIRED_PASS / MANUAL_APPROVED），"
                f"以下选中样本尚未达标: {', '.join(unqualified[:8])}"
            ),
        )

    if payload.exclude_benchmark_exported:
        samples = [s for s in samples if not is_sample_exported_to_benchmark(s)]
        if not samples:
            raise HTTPException(
                status_code=400,
                detail="所选样本均已标记为留出评测集源对话 (exported_to_benchmark=True)，默认已拦截防数据泄漏",
            )

    raw_did: str = (payload.target_dataset_id or "").strip()
    if raw_did:
        dataset_id: str = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fa5]", "_", raw_did)
        if dataset_id.endswith(".jsonl"):
            dataset_id = dataset_id[:-6]
    else:
        dataset_id = f"flywheel_selected_{payload.version_tag}_{int(time.time())}"
    export_path = os.path.join(settings.DATASETS_DIR, f"{dataset_id}.jsonl")

    final_count, dedup_stats = _export_training_rows_to_dataset(
        db=db,
        samples=samples,
        export_path=export_path,
        dataset_id=dataset_id,
        inject_thinking=payload.inject_thinking,
        mode=payload.mode,
        dedup_config=payload.dedup_config,
    )

    return {
        "message": f"选中样本导出成功 (共 {final_count} 条，覆盖 {dedup_stats.get('cluster_count', 0)} 个情景簇)",
        "dataset_id": dataset_id,
        "file_path": export_path,
        "sample_count": final_count,
        "dedup_stats": dedup_stats,
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
    ensure_sample_state_reconciled(sample, threshold=threshold)

    is_remediated_target = (payload.target_data == "remediated")
    if is_remediated_target and not _has_nonempty_messages_json(sample.remediated_messages_json):
        raise HTTPException(status_code=400, detail="该样本尚无修复后对话，无法对修复版执行二次验收")

    current_rule_ts = get_rule_updated_at(db, rule.rule_id)
    rule_changed = (sample.rule_id != rule.rule_id) or (
        sample.evaluated_rule_updated_at is not None
        and current_rule_ts is not None
        and int(sample.evaluated_rule_updated_at) != int(current_rule_ts)
    )

    sample.rule_id = rule.rule_id
    matched_sc = next(
        (s for s in rule.scenarios if s.id == sample.scenario_id),
        PersonaDefinition(id=sample.scenario_id, name="复核样本场景", instruction="保持原意图"),
    )

    msgs_json = (
        sample.remediated_messages_json
        if is_remediated_target and sample.remediated_messages_json
        else sample.raw_messages_json
    )
    dialogue_msgs = [ChatMessage(**m) for m in json.loads(msgs_json or "[]")]

    eval_rep = pipeline.judge_dialogue(rule, matched_sc, dialogue_msgs)
    trans = FlywheelPipelineService.apply_rejudge_transition(
        eval_report=eval_rep,
        threshold=threshold,
        target_data=payload.target_data,
        raw_verdict=sample.raw_verdict or "UNJUDGED",
        remediated_verdict=sample.remediated_verdict or "NONE",
        manual_decision=sample.manual_decision or "NONE",
        active_version=sample.active_version or "raw",
        has_remediated_messages=_has_nonempty_messages_json(sample.remediated_messages_json),
        rule_changed=rule_changed,
    )

    now = int(time.time())
    sample.raw_verdict = trans["raw_verdict"]
    sample.remediated_verdict = trans["remediated_verdict"]
    sample.manual_decision = trans["manual_decision"]
    sample.active_version = trans["active_version"]
    sample.status = trans["status"]
    sample.evaluated_rule_updated_at = current_rule_ts
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
        "raw_verdict": sample.raw_verdict,
        "remediated_verdict": sample.remediated_verdict,
        "manual_decision": sample.manual_decision,
        "active_version": sample.active_version,
        "rule_changed": rule_changed,
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
        if updated_rec.status == "GOLD_PASS":
            sample.raw_verdict = "PASS"
            sample.manual_decision = "NONE"
            sample.active_version = "raw"
            sample.evaluated_rule_updated_at = get_rule_updated_at(db, rule.rule_id)
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
            "raw_verdict": sample.raw_verdict,
            "remediated_verdict": sample.remediated_verdict,
            "manual_decision": sample.manual_decision,
            "active_version": sample.active_version,
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
    remed_dicts: List[Dict[str, Any]] = messages_to_dicts(remed_msgs)
    sample.remediated_messages_json = json.dumps(remed_dicts, ensure_ascii=False)
    _refresh_sample_user_hashes(sample, raw_msgs=raw_msgs, remed_msgs=remed_msgs)
    sample.verified_eval_json = (
        json.dumps(updated_rec.verified_eval.model_dump(), ensure_ascii=False)
        if updated_rec.verified_eval
        else "{}"
    )
    sample.applied_actions_json = json.dumps(updated_rec.applied_actions, ensure_ascii=False)
    sample.benchmark_slices_json = json.dumps([s.model_dump() for s in updated_rec.benchmark_slices], ensure_ascii=False)
    sample.remediation_debug_json = json.dumps(updated_rec.remediation_debug or {}, ensure_ascii=False)
    decoupled = FlywheelPipelineService.compute_record_decoupled_state(
        updated_rec, threshold=rule.inspection_rules.pass_score_threshold
    )
    sample.raw_verdict = decoupled["raw_verdict"]
    sample.remediated_verdict = decoupled["remediated_verdict"]
    sample.manual_decision = decoupled["manual_decision"]
    sample.active_version = decoupled["active_version"]
    sample.status = decoupled["status"]
    sample.evaluated_rule_updated_at = get_rule_updated_at(db, rule.rule_id)
    db.commit()

    return {
        "message": "样本重新修复完成",
        "sample_id": sample_id,
        "rule_id": sample.rule_id,
        "status": sample.status,
        "raw_verdict": sample.raw_verdict,
        "remediated_verdict": sample.remediated_verdict,
        "manual_decision": sample.manual_decision,
        "active_version": sample.active_version,
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

            def _on_record_done(f_rec: FlywheelRecord, _raw_msgs: List[ChatMessage]) -> bool:
                p_db = SessionLocal()
                try:
                    final_score = (
                        f_rec.verified_eval.score
                        if f_rec.verified_eval
                        else (f_rec.initial_eval.score if f_rec.initial_eval else 0)
                    )
                    decoupled = FlywheelPipelineService.compute_record_decoupled_state(
                        f_rec, threshold=rule.inspection_rules.pass_score_threshold
                    )
                    db_row, _ = upsert_flywheel_sample(
                        p_db,
                        sample_id=f_rec.id,
                        source_id=f_rec.id,
                        task_id=task_id,
                        rule_id=rule.rule_id,
                        scenario_id=f_rec.scenario_id,
                        status=decoupled["status"],
                        score=final_score,
                        raw_messages=f_rec.raw_messages,
                        remediated_messages=f_rec.remediated_messages,
                        applied_actions=f_rec.applied_actions,
                        benchmark_slices=[s.model_dump() for s in f_rec.benchmark_slices],
                        remediation_debug=f_rec.remediation_debug or {},
                        initial_eval=f_rec.initial_eval.model_dump() if f_rec.initial_eval else {},
                        verified_eval=f_rec.verified_eval.model_dump() if f_rec.verified_eval else {},
                        reviewer_comment=f"来自批量飞轮治理 ({task_id})",
                        updated_at=f_rec.updated_at,
                        raw_verdict=decoupled["raw_verdict"],
                        remediated_verdict=decoupled["remediated_verdict"],
                        manual_decision=decoupled["manual_decision"],
                        active_version=decoupled["active_version"],
                        evaluated_rule_updated_at=get_rule_updated_at(p_db, rule.rule_id),
                        commit=True,
                    )
                    return is_sample_exported_to_benchmark(db_row)
                except Exception as exc:
                    p_db.rollback()
                    # 严禁吞错放行！先只读核查同规则下是否存在匹配的留出源样本，查不到或查库异常则直接抛出阻断写盘
                    raw_hash, remed_hash = FlywheelPipelineService.compute_dialogue_user_hashes(
                        f_rec.raw_messages, f_rec.remediated_messages
                    )
                    hashes: List[str] = [h for h in (raw_hash, remed_hash) if h]
                    ro_query = p_db.query(FlywheelSampleRecord).filter(
                        FlywheelSampleRecord.rule_id == rule.rule_id,
                        FlywheelSampleRecord.exported_to_benchmark == True,  # noqa: E712
                    )
                    matched_holdout = ro_query.filter(
                        FlywheelSampleRecord.id == f_rec.id
                    ).first()
                    if matched_holdout is None and hashes:
                        matched_holdout = ro_query.filter(
                            FlywheelSampleRecord.raw_user_hash.in_(hashes)
                            | FlywheelSampleRecord.remediated_user_hash.in_(hashes)
                        ).first()
                    if matched_holdout is not None:
                        print(
                            f"⚠️ [FlywheelBatch] 样本 {f_rec.id} 写库异常({exc})，只读核查命中留出样本 {matched_holdout.id}，已隔离排除"
                        )
                        return True
                    print(f"❌ [FlywheelBatch] 样本 {f_rec.id} 写库异常且无法核销安全状态，已阻断写入黄金集: {exc}")
                    raise
                finally:
                    p_db.close()

            stats = pipeline.batch_process_dataset(
                rule=rule,
                input_records=records,
                gold_output_path=gold_out,
                discard_output_path=discard_out,
                workers=payload.workers,
                on_record_done=_on_record_done,
                dedup_config=payload.dedup_config,
            )

            # 同步注册黄金训练集与淘汰对话集至 DatasetRecord（含绝对路径/默认目录）
            if os.path.exists(gold_out):
                gold_cnt: int = int(stats.get("final_gold_count") or 0)
                if gold_cnt > 0:
                    gold_fname: str = os.path.basename(gold_out)
                    gold_did: str = gold_fname[:-6] if gold_fname.endswith(".jsonl") else gold_fname
                    ds_rec = bg_db.query(DatasetRecord).filter(DatasetRecord.id == gold_did).first()
                    if ds_rec is None:
                        ds_rec = DatasetRecord(
                            id=gold_did,
                            name=gold_fname,
                            file_path=gold_out,
                            sample_count=gold_cnt,
                            dataset_type="flywheel_gold",
                            created_at=int(time.time()),
                        )
                        bg_db.add(ds_rec)
                    else:
                        ds_rec.name = gold_fname
                        ds_rec.file_path = gold_out
                        ds_rec.sample_count = gold_cnt
                        ds_rec.dataset_type = "flywheel_gold"
                        ds_rec.created_at = int(time.time())
                    gold_final_rows: List[Dict[str, Any]] = []
                    with open(gold_out, "r", encoding="utf-8") as gf_r:
                        for gln in gf_r:
                            gs = gln.strip()
                            if gs:
                                try:
                                    gobj = json.loads(gs)
                                    if isinstance(gobj, dict):
                                        gold_final_rows.append(gobj)
                                except Exception:
                                    pass
                    sync_lineage_on_dataset_overwritten(
                        db=bg_db,
                        dataset_id=gold_did,
                        dataset_type="flywheel_gold",
                        final_rows=gold_final_rows,
                        commit=False,
                    )

            if os.path.exists(discard_out):
                discard_cnt = 0
                with open(discard_out, "r", encoding="utf-8") as df:
                    for line in df:
                        if line.strip():
                            discard_cnt += 1
                if discard_cnt > 0:
                    discard_fname: str = os.path.basename(discard_out)
                    discard_did: str = (
                        discard_fname[:-6] if discard_fname.endswith(".jsonl") else discard_fname
                    )
                    disc_rec = bg_db.query(DatasetRecord).filter(DatasetRecord.id == discard_did).first()
                    if disc_rec is None:
                        disc_rec = DatasetRecord(
                            id=discard_did,
                            name=discard_fname,
                            file_path=discard_out,
                            sample_count=discard_cnt,
                            dataset_type="flywheel_discarded",
                            created_at=int(time.time()),
                        )
                        bg_db.add(disc_rec)
                    else:
                        disc_rec.name = discard_fname
                        disc_rec.file_path = discard_out
                        disc_rec.sample_count = discard_cnt
                        disc_rec.dataset_type = "flywheel_discarded"
                        disc_rec.created_at = int(time.time())

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


def _make_flywheel_inspect_sample_id(task_id: str, sample_id: str) -> str:
    """生成不超过 64 字符的唯一飞轮样本主键，避免同数据集多次质检入库发生主键冲突。"""
    candidate = f"{task_id}_{sample_id}"
    if len(candidate) <= 64:
        return candidate
    digest = hashlib.md5(sample_id.encode("utf-8")).hexdigest()[:16]
    return f"{task_id}_{digest}"


def _resolve_inspect_dataset_path(
    db: Session,
    dataset_id: Optional[str],
    dataset_path: Optional[str],
) -> Tuple[Optional[str], str]:
    """解析待质检对话数据集路径，支持传 dataset_id 或 dataset_path。"""
    did = (dataset_id or "").strip() or None
    dpath = (dataset_path or "").strip() or None
    if not did and not dpath:
        raise HTTPException(status_code=400, detail="请提供 dataset_id 或 dataset_path")

    if did and not dpath:
        rec = db.query(DatasetRecord).filter(DatasetRecord.id == did).first()
        real_path = rec.file_path if rec else os.path.join(settings.DATASETS_DIR, f"{did}.jsonl")
    else:
        real_path = str(dpath)
        if not os.path.isabs(real_path):
            real_path = os.path.join(settings.BASE_DIR, real_path)
        if not did:
            rec_by_path = db.query(DatasetRecord).filter(DatasetRecord.file_path == real_path).first()
            if rec_by_path:
                did = rec_by_path.id
            else:
                fname = os.path.basename(real_path)
                did = fname[:-6] if fname.endswith(".jsonl") else fname

    if not os.path.exists(real_path):
        raise HTTPException(status_code=404, detail=f"找不到指定数据集文件: {real_path}")
    return did, real_path


@router.post("/inspect-dataset")
def inspect_dataset_by_rule(
    payload: DatasetInspectRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    对纯对话数据集执行批量规则质检打分（只检不修）:
    - 复用 SOP 拓扑 + 节点检查点 + 红线 ∥ FAQ 双轨规则质检；
    - 步进落盘结构化汇总与逐样本打分明细至 data/runs/inspect_<task_id>.json；
    - 可选 save_to_flywheel=True 同步写入飞轮样本池供后续人工复核或修复。
    """
    rule = load_rule_schema(db, payload.rule_id)
    # 预检裁判模型是否存在，避免后台静默失败
    get_adapter_and_config_by_model_id(db, payload.judge_model_id)
    resolved_dataset_id, real_path = _resolve_inspect_dataset_path(
        db, payload.dataset_id, payload.dataset_path
    )

    records: List[Dict[str, Any]] = []
    try:
        with open(real_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line_s = line.strip()
                if not line_s:
                    continue
                obj = json.loads(line_s)
                if not isinstance(obj, dict):
                    raise ValueError(f"第 {line_no} 行不是合法 JSON 对象")
                records.append(obj)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"数据集解析失败: {str(e)}")

    if not records:
        raise HTTPException(status_code=400, detail="数据集为空，无可质检样本")

    target_count = min(len(records), payload.limit) if payload.limit else len(records)
    task_id = f"insp_{str(uuid.uuid4())[:8]}"
    report_file = os.path.join(settings.RUNS_DIR, f"inspect_{task_id}.json")
    os.makedirs(settings.RUNS_DIR, exist_ok=True)

    initial_meta = {
        "dataset_id": resolved_dataset_id,
        "dataset_path": real_path,
        "rule_id": rule.rule_id,
        "rule_name": rule.project_name,
        "judge_model_id": payload.judge_model_id,
        "report_file": report_file,
        "save_to_flywheel": bool(payload.save_to_flywheel),
        "saved_sample_count": 0,
        "progress_detail": {"completed": 0, "total": target_count, "current_sample_id": None},
    }
    rec = TaskRecord(
        id=task_id,
        task_type="flywheel_inspect",
        status="PENDING",
        progress=0.0,
        result_json=json.dumps(initial_meta, ensure_ascii=False),
    )
    db.add(rec)
    db.commit()

    def _run_inspect() -> None:
        bg_db = SessionLocal()
        task = bg_db.query(TaskRecord).filter(TaskRecord.id == task_id).first()
        if not task:
            bg_db.close()
            return
        task.status = "RUNNING"
        bg_db.commit()

        saved_count = 0
        try:
            judge_adapter, judge_config = get_adapter_and_config_by_model_id(
                bg_db, payload.judge_model_id
            )
            pipeline = FlywheelPipelineService(
                judge_adapter=judge_adapter,
                remediate_adapter=None,
                judge_config=judge_config,
            )

            def _on_sample_done(
                entry: Dict[str, Any],
                msgs: List[ChatMessage],
                rep: Optional[InspectionReport],
                completed: int,
                total: int,
            ) -> None:
                nonlocal saved_count
                p_db = SessionLocal()
                try:
                    t = p_db.query(TaskRecord).filter(TaskRecord.id == task_id).first()
                    if payload.save_to_flywheel and rep is not None and entry.get("verdict") != "ERROR":
                        fw_id = _make_flywheel_inspect_sample_id(task_id, str(entry["sample_id"]))
                        standard_raw: List[ChatMessage] = list(msgs)
                        if not standard_raw or standard_raw[0].role != "system":
                            standard_raw = [
                                ChatMessage(role="system", content=rule.bot_config.system_prompt)
                            ] + standard_raw
                        fw_status = (
                            "GOLD_PASS"
                            if FlywheelPipelineService.is_gold_pass(
                                rep, rule.inspection_rules.pass_score_threshold
                            )
                            else "PENDING"
                        )
                        upsert_flywheel_sample(
                            p_db,
                            sample_id=fw_id,
                            source_id=str(entry["sample_id"]),
                            task_id=task_id,
                            rule_id=rule.rule_id,
                            scenario_id=str(entry.get("scenario_id") or "default"),
                            status=fw_status,
                            score=int(rep.score),
                            raw_messages=standard_raw,
                            initial_eval=rep.model_dump(),
                            reviewer_comment=f"来自数据集规则质检 ({resolved_dataset_id or task_id})",
                            updated_at=int(time.time()),
                            raw_verdict="PASS" if fw_status == "GOLD_PASS" else "FAIL",
                            remediated_verdict="NONE",
                            manual_decision="NONE",
                            active_version="raw",
                            evaluated_rule_updated_at=get_rule_updated_at(p_db, rule.rule_id),
                            commit=False,
                        )
                        saved_count += 1

                    if t:
                        t.progress = round(completed / max(total, 1), 4)
                        cur_res = json.loads(t.result_json or "{}")
                        cur_res["progress_detail"] = {
                            "completed": completed,
                            "total": total,
                            "current_sample_id": entry.get("sample_id"),
                        }
                        cur_res["saved_sample_count"] = saved_count
                        t.result_json = json.dumps(cur_res, ensure_ascii=False)
                    p_db.commit()
                except Exception:
                    p_db.rollback()
                finally:
                    p_db.close()

            report = pipeline.batch_inspect_dataset(
                rule=rule,
                input_records=records,
                task_id=task_id,
                dataset_id=resolved_dataset_id,
                dataset_path=real_path,
                judge_model_id=payload.judge_model_id,
                workers=payload.workers,
                limit=payload.limit,
                output_path=report_file,
                on_sample_done=_on_sample_done,
            )

            task.status = "COMPLETED"
            task.progress = 1.0
            cur_res = json.loads(task.result_json or "{}")
            cur_res.update({
                "summary": report.get("summary", {}),
                "report_file": report_file,
                "saved_sample_count": saved_count,
                "progress_detail": {
                    "completed": target_count,
                    "total": target_count,
                    "current_sample_id": None,
                },
            })
            task.result_json = json.dumps(cur_res, ensure_ascii=False)
            task.finished_at = int(time.time())
            bg_db.commit()
        except Exception as e:
            task.status = "FAILED"
            task.error_message = str(e)
            task.finished_at = int(time.time())
            bg_db.commit()
        finally:
            bg_db.close()

    background_tasks.add_task(_run_inspect)
    return {
        "message": "数据集批量规则质检任务已提交（只检不修）",
        "task_id": task_id,
        "dataset_id": resolved_dataset_id,
        "total_samples": target_count,
    }


@router.get("/inspect-tasks/{task_id}")
def get_inspect_dataset_task(task_id: str, db: Session = Depends(get_db)):
    """查询批量规则质检任务状态、汇总指标与逐样本打分报告。"""
    task = db.query(TaskRecord).filter(TaskRecord.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="未找到该质检任务")

    parsed_res = json.loads(task.result_json or "{}")
    report_file = parsed_res.get("report_file") or os.path.join(
        settings.RUNS_DIR, f"inspect_{task_id}.json"
    )

    report_data: Dict[str, Any] = {}
    if report_file and os.path.exists(report_file):
        try:
            with open(report_file, "r", encoding="utf-8") as f:
                report_data = json.load(f)
        except Exception:
            report_data = {}

    return {
        "id": task.id,
        "task_id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "progress": task.progress,
        "error_message": task.error_message,
        "created_at": task.created_at,
        "finished_at": task.finished_at,
        "dataset_id": report_data.get("dataset_id") or parsed_res.get("dataset_id"),
        "dataset_path": report_data.get("dataset_path") or parsed_res.get("dataset_path"),
        "rule_id": report_data.get("rule_id") or parsed_res.get("rule_id"),
        "rule_name": report_data.get("rule_name") or parsed_res.get("rule_name"),
        "judge_model_id": report_data.get("judge_model_id") or parsed_res.get("judge_model_id"),
        "pass_score_threshold": report_data.get("pass_score_threshold"),
        "save_to_flywheel": bool(parsed_res.get("save_to_flywheel", False)),
        "saved_sample_count": parsed_res.get("saved_sample_count", 0),
        "progress_detail": parsed_res.get("progress_detail"),
        "report_file": report_file,
        "summary": report_data.get("summary") or parsed_res.get("summary"),
        "samples": report_data.get("samples", []),
    }


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
    threshold_cache: Dict[str, int] = {}
    return [_serialize_flywheel_sample(db, r, threshold_cache=threshold_cache) for r in records]


@router.put("/samples/{sample_id}")
def update_sample_review(
    sample_id: str,
    payload: SampleReviewUpdateRequest,
    db: Session = Depends(get_db),
):
    sample = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).first()
    if not sample:
        raise HTTPException(status_code=404, detail="未找到该样本")

    ensure_sample_state_reconciled(sample, threshold=get_rule_threshold(db, sample.rule_id))

    remed_edited = False
    if payload.remediated_messages is not None:
        old_remed_json = sample.remediated_messages_json or "[]"
        new_remed_json = json.dumps(payload.remediated_messages, ensure_ascii=False)
        if new_remed_json != old_remed_json:
            remed_edited = True
        sample.remediated_messages_json = new_remed_json
        _refresh_sample_user_hashes(sample, remed_msgs=payload.remediated_messages)
        if remed_edited:
            sample.active_version = "remediated"
            if (payload.status or "").upper() != "MANUAL_APPROVED":
                sample.remediated_verdict = "UNVERIFIED"
                if sample.manual_decision == "APPROVED":
                    sample.manual_decision = "NONE"

    _apply_status_transition_for_sample(
        db=db,
        sample=sample,
        target_status=payload.status,
        active_version=payload.active_version,
        reviewer_comment_incoming=payload.reviewer_comment,
        remediated_messages_edited=remed_edited,
        allow_error_with_comment=True,
    )

    if payload.reviewer_comment is not None:
        sample.reviewer_comment = payload.reviewer_comment

    sample.updated_at = int(time.time())
    db.commit()
    return {
        "message": "样本复核更新成功",
        "id": sample_id,
        "status": sample.status,
        "raw_verdict": sample.raw_verdict,
        "remediated_verdict": sample.remediated_verdict,
        "manual_decision": sample.manual_decision,
        "active_version": sample.active_version,
    }


@router.post("/export")
def export_gold_dataset(
    rule_id: str,
    version_tag: str = "v1.0",
    include_manual_approved: bool = True,
    inject_thinking: bool = False,
    exclude_benchmark_exported: bool = True,
    mode: str = "create",
    target_dataset_id: Optional[str] = None,
    payload: Optional[ExportGoldOptionsBody] = Body(default=None),
    db: Session = Depends(get_db),
):
    """
    从复核池中一键导出符合标准 messages 格式的黄金微调训练集 (.jsonl)，
    统一仅导出达标对话、按 active_version 取对应版本对话，
    默认排除已进评测集的同源对话（显式放行时同步把对应评测题改为普通题）并去重防稀释。
    """
    allowed_statuses = {"GOLD_PASS", "REPAIRED_PASS"}
    if include_manual_approved:
        allowed_statuses.add("MANUAL_APPROVED")

    rule_samples = (
        db.query(FlywheelSampleRecord)
        .filter(FlywheelSampleRecord.rule_id == rule_id)
        .all()
    )
    th = get_rule_threshold(db, rule_id)
    samples: List[FlywheelSampleRecord] = []
    for s in rule_samples:
        ensure_sample_state_reconciled(s, threshold=th)
        if (s.status or "PENDING") not in allowed_statuses:
            continue
        if exclude_benchmark_exported and is_sample_exported_to_benchmark(s):
            continue
        samples.append(s)

    if not samples:
        raise HTTPException(status_code=400, detail="该规则下暂无可导出的达标黄金样本")

    eff_mode: str = (payload.mode if payload and payload.mode else mode) or "create"
    raw_target_id: str = (
        payload.target_dataset_id
        if payload and payload.target_dataset_id
        else (target_dataset_id or "")
    ).strip()
    if raw_target_id:
        dataset_id: str = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fa5]", "_", raw_target_id)
        if dataset_id.endswith(".jsonl"):
            dataset_id = dataset_id[:-6]
    else:
        dataset_id = f"flywheel_{rule_id}_{version_tag}_gold"
    export_path = os.path.join(settings.DATASETS_DIR, f"{dataset_id}.jsonl")

    final_count, dedup_stats = _export_training_rows_to_dataset(
        db=db,
        samples=samples,
        export_path=export_path,
        dataset_id=dataset_id,
        inject_thinking=inject_thinking,
        mode=eff_mode,
        dedup_config=payload.dedup_config if payload else None,
    )

    return {
        "message": f"黄金微调数据集导出成功 (共 {final_count} 条，覆盖 {dedup_stats.get('cluster_count', 0)} 个情景簇)",
        "dataset_id": dataset_id,
        "file_path": export_path,
        "sample_count": final_count,
        "dedup_stats": dedup_stats,
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
        trajectory_details=initial_eval.get("trajectory_details"),
        faq_details=initial_eval.get("faq_details"),
    )
    if generated:
        sample.benchmark_slices_json = json.dumps(slices_data, ensure_ascii=False)
    return slices_data, generated


def _resolve_sample_benchmark_holdout(
    sample: FlywheelSampleRecord,
    reserve_for_training: bool = False,
    explicit_is_holdout: Optional[bool] = None,
) -> bool:
    """
    判定某条样本切题导出评测题时应写入的 is_holdout 标记:
    - 若来源对话已进过训练集 (exported_training_datasets 非空): 严禁强标 is_holdout=True，强制标为普通题 (False);
    - 若尚未进过训练集: 除非用户开启 reserve_for_training 或显式传 is_holdout=False，否则默认标为留出题 (True)。
    """
    train_ds = get_sample_training_datasets(sample)
    if train_ds:
        if explicit_is_holdout is True and not reserve_for_training:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"样本 {sample.id} 已导出至训练集 ({', '.join(train_ds)})，"
                    "禁止强行标记为留出题 (is_holdout=True)，请以普通题导出"
                ),
            )
        return False
    if reserve_for_training or explicit_is_holdout is False:
        return False
    return True


def _build_benchmark_rows_from_slices(
    sample_id: str,
    slices: List[Dict[str, Any]],
    rule_id: Optional[str] = None,
    is_holdout: bool = True,
) -> List[Dict[str, Any]]:
    """将飞轮评测切片字典列表转换为标准 Benchmark JSONL 行格式（保留定位元数据并如实写 is_holdout）。"""
    rows: List[Dict[str, Any]] = []
    for sl in slices:
        meta: Dict[str, Any] = {"source_sample_id": sample_id}
        eff_rule: Optional[str] = sl.get("rule_id") or rule_id
        if eff_rule:
            meta["rule_id"] = eff_rule
        if sl.get("node_id"):
            meta["node_id"] = sl["node_id"]
        if sl.get("faq_id"):
            meta["faq_id"] = sl["faq_id"]
        if sl.get("is_faq_ood"):
            meta["is_faq_ood"] = True
        if sl.get("target_turn") is not None:
            meta["target_turn"] = sl["target_turn"]

        rows.append({
            "id": sl["slice_id"],
            "category": sl.get("category") or "飞轮实战质检",
            "sub_category": sl.get("sub_category") or sl.get("defect_item"),
            "difficulty": "medium",
            "source": f"flywheel_{sample_id}",
            "is_holdout": bool(is_holdout),
            "messages": sl["messages"],
            "reference": sl["reference"],
            "rubric": sl["rubric"],
            "regex_constraints": sl.get("regex_constraints", []),
            "metadata": meta,
        })
    return rows


def _write_and_register_benchmark_dataset(
    db: Session,
    dataset_id: str,
    dataset_name: str,
    mode: str,
    bench_rows: List[Dict[str, Any]],
    exported_samples: List[FlywheelSampleRecord],
    dedup_config: Optional[DedupConfig] = None,
) -> Tuple[str, int, Dict[str, Any]]:
    """合并写入 Benchmark JSONL 文件（接入四步去重防稀释）、注册 DatasetRecord 并回写评测集导出留底。"""
    file_path = os.path.join(settings.DATASETS_DIR, f"{dataset_id}.jsonl")

    existing_rows: Optional[List[Dict[str, Any]]] = None
    if mode == "append" and os.path.exists(file_path):
        loaded: List[Dict[str, Any]] = []
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                s_line = line.strip()
                if s_line:
                    try:
                        d = json.loads(s_line)
                        if isinstance(d, dict):
                            loaded.append(d)
                    except Exception:
                        pass
        existing_rows = loaded

    final_rows, dedup_stats = FlywheelPipelineService.deduplicate_benchmark_export(
        candidate_rows=bench_rows,
        existing_rows=existing_rows,
        config=dedup_config,
        dataset_file_path=file_path,
    )

    os.makedirs(settings.DATASETS_DIR, exist_ok=True)
    tmp_path: str = f"{file_path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for r in final_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp_path, file_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise

    if mode == "create" and not bool(dedup_stats.get("enabled")):
        stale_cache = FlywheelPipelineService.get_dedup_cache_path(file_path)
        if os.path.exists(stale_cache):
            try:
                os.remove(stale_cache)
            except Exception:
                pass

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

    if mode == "create":
        sync_lineage_on_dataset_removed(db, dataset_id, commit=False)

    # 仅对去重后最终保留至少 1 道评测题的源样本记录 exported_benchmark_datasets 与 exported_to_benchmark
    retained_sources: Set[str] = set()
    retained_ids: Set[str] = set()
    for r in final_rows:
        if r.get("id"):
            retained_ids.add(str(r["id"]))
        src = r.get("source")
        if isinstance(src, str) and src.startswith("flywheel_"):
            retained_sources.add(src[len("flywheel_") :])
        r_meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        if r_meta.get("source_sample_id"):
            retained_sources.add(str(r_meta["source_sample_id"]))

    now_ts = int(time.time())
    for s in exported_samples:
        has_slice_kept = s.id in retained_sources
        if not has_slice_kept:
            try:
                s_slices = json.loads(s.benchmark_slices_json or "[]")
                has_slice_kept = any(
                    isinstance(sl, dict) and str(sl.get("slice_id") or "") in retained_ids
                    for sl in s_slices
                )
            except Exception:
                has_slice_kept = False
        if has_slice_kept:
            add_dataset_to_sample_lineage(s, dataset_id, "benchmark")
            s.updated_at = now_ts

    db.commit()

    if mode == "create":
        BenchmarkSamplingEngine.reset_trackers_in_db(db=db, dataset_path=file_path)

    return file_path, len(final_rows), dedup_stats


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
            ensure_sample_state_reconciled(sample, threshold=rule.inspection_rules.pass_score_threshold)
            sanitized_dicts: List[Dict[str, Any]] = messages_to_dicts(sanitized_msgs)
            sample.remediated_messages_json = json.dumps(sanitized_dicts, ensure_ascii=False)
            _refresh_sample_user_hashes(sample, raw_msgs=raw_msgs, remed_msgs=sanitized_msgs)
            sample.active_version = "remediated"
            if (sample.remediated_verdict or "NONE") == "NONE":
                sample.remediated_verdict = "UNVERIFIED"
            sample.status = FlywheelPipelineService.derive_sample_status(
                sample.raw_verdict,
                sample.remediated_verdict,
                sample.manual_decision,
                sample.active_version,
            )

    sample.updated_at = int(time.time())
    db.commit()
    return {"message": "切片保存与对话双向同步成功", "slice_count": len(payload.slices)}


@router.post("/samples/{sample_id}/export-benchmark")
def export_single_sample_benchmark(
    sample_id: str,
    payload: ExportBenchmarkDatasetRequest,
    db: Session = Depends(get_db),
):
    """将单个样本的切片导出为标准 Benchmark 评测集文件并在 datasets 表中注册（按训练集留底如实标 is_holdout）"""
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

    sample_is_holdout = _resolve_sample_benchmark_holdout(
        sample,
        reserve_for_training=bool(payload.reserve_for_training),
        explicit_is_holdout=payload.is_holdout,
    )
    dataset_id = payload.dataset_id.strip() or f"flywheel_{sample_id}_eval"
    dataset_name = payload.name.strip() or f"飞轮样本评测集 ({sample_id})"
    bench_rows = _build_benchmark_rows_from_slices(
        sample_id, selected_slices, rule_id=sample.rule_id, is_holdout=sample_is_holdout
    )

    file_path, sample_count, dedup_stats = _write_and_register_benchmark_dataset(
        db=db,
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        mode=payload.mode,
        bench_rows=bench_rows,
        exported_samples=[sample],
        dedup_config=payload.dedup_config,
    )

    return {
        "message": (
            "评测集导出成功（已标记为纯评测留出题并与训练集隔离）"
            if sample_is_holdout
            else "评测集导出成功（已如实标记为普通题 is_holdout=False）"
        ),
        "dataset_id": dataset_id,
        "file_path": file_path,
        "sample_count": sample_count,
        "is_holdout": sample_is_holdout,
        "dedup_stats": dedup_stats,
    }


@router.post("/batch-export-benchmark")
def batch_export_benchmark_dataset(
    payload: BatchExportBenchmarkRequest,
    db: Session = Depends(get_db),
):
    """批量将多个勾选样本的切片合并导出为标准 Benchmark 评测集（逐条按训练集留底如实标 is_holdout）"""
    samples = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id.in_(payload.sample_ids)).all()
    if not samples:
        raise HTTPException(status_code=404, detail="未找到选中的任何样本")

    dataset_id = payload.dataset_id.strip()
    dataset_name = payload.name.strip()

    all_bench_rows: List[Dict[str, Any]] = []
    exported_samples: List[FlywheelSampleRecord] = []
    rule_cache: Dict[str, UniversalRuleSchema] = {}
    holdout_count = 0
    regular_count = 0

    for s in samples:
        slices_raw, _ = _ensure_db_sample_slices(db, s, rule_cache=rule_cache)
        if not slices_raw:
            continue

        sample_is_holdout = _resolve_sample_benchmark_holdout(
            s,
            reserve_for_training=bool(payload.reserve_for_training),
            explicit_is_holdout=payload.is_holdout,
        )
        if sample_is_holdout:
            holdout_count += 1
        else:
            regular_count += 1

        target_slices = (
            [slices_raw[0]]
            if payload.strategy == "primary_defect_only"
            else slices_raw
        )
        all_bench_rows.extend(
            _build_benchmark_rows_from_slices(
                s.id, target_slices, rule_id=s.rule_id, is_holdout=sample_is_holdout
            )
        )
        exported_samples.append(s)

    if not all_bench_rows:
        raise HTTPException(status_code=400, detail="所选样本中未生成任何有效评测切片")

    file_path, sample_count, dedup_stats = _write_and_register_benchmark_dataset(
        db=db,
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        mode=payload.mode,
        bench_rows=all_bench_rows,
        exported_samples=exported_samples,
        dedup_config=payload.dedup_config,
    )

    return {
        "message": f"批量导出评测集成功（留出题源 {holdout_count} 条，普通题源 {regular_count} 条）",
        "dataset_id": dataset_id,
        "file_path": file_path,
        "sample_count": sample_count,
        "holdout_source_count": holdout_count,
        "regular_source_count": regular_count,
        "dedup_stats": dedup_stats,
    }
