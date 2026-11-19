# -*- coding: utf-8 -*-
"""
API 路由公共依赖助手 (模型适配器加载与场景规则读取)
"""
import os
import json
from typing import Optional, Tuple
from fastapi import HTTPException
from sqlalchemy.orm import Session
from backend.app.config import settings
from backend.app.models import RegisteredModel, RuleConfigRecord
from backend.app.schemas import UniversalRuleSchema, ModelConfig
from backend.app.adapters.factory import AdapterFactory
from backend.app.adapters.base import BaseModelAdapter


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
