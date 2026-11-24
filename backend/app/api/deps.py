# -*- coding: utf-8 -*-
"""
API 路由公共依赖助手 (模型适配器加载与场景规则读取)
"""
import os
import json
import time
import uuid
import hashlib
from typing import Optional, Tuple, List, Dict, Any
from fastapi import HTTPException
from sqlalchemy.orm import Session
from backend.app.config import settings
from backend.app.models import RegisteredModel, RuleConfigRecord, FlywheelSampleRecord
from backend.app.schemas import UniversalRuleSchema, ModelConfig, ChatMessage
from backend.app.adapters.factory import AdapterFactory
from backend.app.adapters.base import BaseModelAdapter
from backend.app.services.flywheel_service import FlywheelPipelineService


def messages_to_dicts(messages: List[Any]) -> List[Dict[str, Any]]:
    """将 ChatMessage 或消息字典列表安全序列化为字典数组，避免动态调用盲区。"""
    out: List[Dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, dict):
            msg_dict: Dict[str, Any] = msg
            d: Dict[str, Any] = {
                "role": str(msg_dict.get("role") or ""),
                "content": str(msg_dict.get("content") or ""),
            }
            if msg_dict.get("thinking"):
                d["thinking"] = str(msg_dict["thinking"])
            out.append(d)
        elif isinstance(msg, ChatMessage):
            d = {"role": msg.role, "content": msg.content}
            if msg.thinking:
                d["thinking"] = msg.thinking
            out.append(d)
    return out


def upsert_flywheel_sample(
    db: Session,
    *,
    sample_id: str,
    task_id: Optional[str],
    rule_id: str,
    scenario_id: str,
    status: str,
    score: int,
    raw_messages: List[ChatMessage],
    remediated_messages: Optional[List[ChatMessage]] = None,
    applied_actions: Optional[List[Dict[str, Any]]] = None,
    benchmark_slices: Optional[List[Dict[str, Any]]] = None,
    remediation_debug: Optional[Dict[str, Any]] = None,
    initial_eval: Optional[Dict[str, Any]] = None,
    verified_eval: Optional[Dict[str, Any]] = None,
    reviewer_comment: Optional[str] = None,
    source_id: Optional[str] = None,
    updated_at: Optional[int] = None,
    commit: bool = True,
) -> Tuple[FlywheelSampleRecord, bool]:
    """
    按 rule_id 独立隔离的样本池 Upsert 入池函数:
    1. 在同一 rule_id 下先查 source_id / sample_id，再查 raw_user_hash 或 remediated_user_hash 命中;
    2. 对同 rule_id 下历史 raw_user_hash IS NULL 记录执行按需惰性回填并纳入比对;
    3. 命中则原地刷新状态、分数、消息、质检报告与双哈希，并保留原有 exported_to_benchmark 标记;
    4. 未命中则生成跨规则安全主键插入新行。
    返回 (FlywheelSampleRecord, is_created)。
    """
    now_ts: int = updated_at if updated_at is not None else int(time.time())
    raw_dicts: List[Dict[str, Any]] = messages_to_dicts(raw_messages)
    remed_dicts: Optional[List[Dict[str, Any]]] = (
        messages_to_dicts(remediated_messages) if remediated_messages is not None else None
    )
    raw_hash, remed_hash = FlywheelPipelineService.compute_dialogue_user_hashes(
        raw_dicts, remed_dicts
    )
    hash_candidates: List[str] = [raw_hash]
    if remed_hash and remed_hash not in hash_candidates:
        hash_candidates.append(remed_hash)

    existing: Optional[FlywheelSampleRecord] = None

    # 1. 同规则下优先匹配 source_id 或 sample_id
    if source_id:
        existing = (
            db.query(FlywheelSampleRecord)
            .filter(
                FlywheelSampleRecord.rule_id == rule_id,
                FlywheelSampleRecord.id == source_id,
            )
            .first()
        )
    if existing is None and sample_id:
        existing = (
            db.query(FlywheelSampleRecord)
            .filter(
                FlywheelSampleRecord.rule_id == rule_id,
                FlywheelSampleRecord.id == sample_id,
            )
            .first()
        )

    # 2. 同规则下匹配 raw_user_hash / remediated_user_hash 双哈希
    if existing is None:
        existing = (
            db.query(FlywheelSampleRecord)
            .filter(
                FlywheelSampleRecord.rule_id == rule_id,
                (
                    FlywheelSampleRecord.raw_user_hash.in_(hash_candidates)
                    | FlywheelSampleRecord.remediated_user_hash.in_(hash_candidates)
                ),
            )
            .order_by(FlywheelSampleRecord.updated_at.desc())
            .first()
        )

    # 3. 历史 raw_user_hash IS NULL 记录按需惰性回填与比对
    if existing is None:
        legacy_rows: List[FlywheelSampleRecord] = (
            db.query(FlywheelSampleRecord)
            .filter(
                FlywheelSampleRecord.rule_id == rule_id,
                FlywheelSampleRecord.raw_user_hash.is_(None),
            )
            .all()
        )
        for leg in legacy_rows:
            try:
                leg_raw: List[Any] = json.loads(leg.raw_messages_json or "[]")
            except Exception:
                leg_raw = []
            try:
                leg_remed: Optional[List[Any]] = (
                    json.loads(leg.remediated_messages_json)
                    if leg.remediated_messages_json
                    else None
                )
            except Exception:
                leg_remed = None
            l_raw_h, l_remed_h = FlywheelPipelineService.compute_dialogue_user_hashes(
                leg_raw, leg_remed
            )
            leg.raw_user_hash = l_raw_h
            leg.remediated_user_hash = l_remed_h
            if existing is None and (
                l_raw_h in hash_candidates
                or (l_remed_h is not None and l_remed_h in hash_candidates)
            ):
                existing = leg

    if existing is not None:
        if task_id is not None:
            existing.task_id = task_id
        existing.scenario_id = scenario_id
        existing.status = status
        existing.score = int(score)
        existing.raw_messages_json = json.dumps(raw_dicts, ensure_ascii=False)
        existing.raw_user_hash = raw_hash
        if remed_dicts is not None:
            existing.remediated_messages_json = json.dumps(remed_dicts, ensure_ascii=False)
            existing.remediated_user_hash = remed_hash
        if applied_actions is not None:
            existing.applied_actions_json = json.dumps(applied_actions, ensure_ascii=False)
        if benchmark_slices is not None:
            existing.benchmark_slices_json = json.dumps(benchmark_slices, ensure_ascii=False)
        if remediation_debug is not None:
            existing.remediation_debug_json = json.dumps(remediation_debug, ensure_ascii=False)
        if initial_eval is not None:
            existing.initial_eval_json = json.dumps(initial_eval, ensure_ascii=False)
        if verified_eval is not None:
            existing.verified_eval_json = json.dumps(verified_eval, ensure_ascii=False)
        if reviewer_comment is not None:
            existing.reviewer_comment = reviewer_comment
        # 严格保留既有 exported_to_benchmark 留出防泄漏标记
        existing.updated_at = now_ts
        if commit:
            db.commit()
            db.refresh(existing)
        return existing, False

    # 未命中：生成跨规则安全主键插入新行
    base_id: str = (sample_id or source_id or f"fw_{uuid.uuid4().hex[:12]}").strip()[:64]
    conflict: Optional[FlywheelSampleRecord] = (
        db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == base_id).first()
    )
    if conflict is not None:
        rule_digest: str = hashlib.md5(rule_id.encode("utf-8")).hexdigest()[:8]
        final_id: str = f"{base_id[:44]}_{rule_digest}_{uuid.uuid4().hex[:6]}"[:64]
    else:
        final_id = base_id

    db_row = FlywheelSampleRecord(
        id=final_id,
        task_id=task_id,
        rule_id=rule_id,
        scenario_id=scenario_id,
        status=status,
        score=int(score),
        raw_messages_json=json.dumps(raw_dicts, ensure_ascii=False),
        remediated_messages_json=(
            json.dumps(remed_dicts, ensure_ascii=False) if remed_dicts is not None else None
        ),
        applied_actions_json=json.dumps(applied_actions or [], ensure_ascii=False),
        benchmark_slices_json=json.dumps(benchmark_slices or [], ensure_ascii=False),
        remediation_debug_json=json.dumps(remediation_debug or {}, ensure_ascii=False),
        initial_eval_json=json.dumps(initial_eval or {}, ensure_ascii=False),
        verified_eval_json=json.dumps(verified_eval or {}, ensure_ascii=False),
        reviewer_comment=reviewer_comment or "",
        exported_to_benchmark=False,
        raw_user_hash=raw_hash,
        remediated_user_hash=remed_hash,
        updated_at=now_ts,
    )
    db.add(db_row)
    if commit:
        db.commit()
        db.refresh(db_row)
    return db_row, True


def get_adapter_and_config_by_model_id(
    db: Session,
    model_id: Optional[str],
    fallback_mock_name: str = "mock-model",
) -> Tuple[BaseModelAdapter, ModelConfig]:
    """根据已注册模型 ID 加载适配器实例与默认推理配置，为空或未注册时回退至 Mock 适配器。"""
    if not model_id:
        return AdapterFactory.create("mock", model_name=fallback_mock_name), ModelConfig()
    m_db = db.query(RegisteredModel).filter(RegisteredModel.id == model_id).first()
    if m_db:
        cfg = json.loads(m_db.config_json or "{}")
        adapter = AdapterFactory.create(
            provider_type=m_db.provider_type,
            model_name=m_db.model_name or m_db.id,
            api_url=m_db.api_url,
            api_key=m_db.api_key,
            **cfg,
        )
        return adapter, ModelConfig(**cfg)
    return AdapterFactory.create("mock", model_name=model_id), ModelConfig()


def require_registered_adapter_and_config(
    db: Session,
    model_id: str,
) -> Tuple[BaseModelAdapter, ModelConfig]:
    """严格要求模型已在注册表中存在，返回其适配器与默认推理配置。"""
    m_db: Optional[RegisteredModel] = (
        db.query(RegisteredModel).filter(RegisteredModel.id == model_id).first()
    )
    if not m_db:
        raise HTTPException(status_code=404, detail=f"未找到已注册模型 '{model_id}'")
    return get_adapter_and_config_by_model_id(db, model_id)


def get_adapter_by_model_id(
    db: Session,
    model_id: Optional[str],
    fallback_mock_name: str = "mock-model",
) -> BaseModelAdapter:
    """根据已注册模型 ID 加载适配器实例，为空时回退至指定名称的 Mock 适配器。"""
    adapter, _ = get_adapter_and_config_by_model_id(
        db, model_id, fallback_mock_name=fallback_mock_name
    )
    return adapter


def load_rule_schema(db: Session, rule_id: str) -> UniversalRuleSchema:
    """优先从数据库加载场景规则配置，缺失时回退读取 RULES_DIR 本地 JSON 模板。"""
    rec = db.query(RuleConfigRecord).filter(RuleConfigRecord.rule_id == rule_id).first()
    if rec:
        return UniversalRuleSchema(**json.loads(rec.content_json))
    fpath = os.path.join(settings.RULES_DIR, f"{rule_id}.json")
    if os.path.exists(fpath):
        with open(fpath, "r", encoding="utf-8") as f:
            return UniversalRuleSchema(**json.load(f))
    raise HTTPException(status_code=404, detail=f"未找到场景规则配置 '{rule_id}'")
