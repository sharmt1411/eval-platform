# -*- coding: utf-8 -*-
"""
模型接入管理 API
"""
import json
import time
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from backend.app.models import get_db, RegisteredModel
from backend.app.schemas import ModelConfig, UnifiedChatRequest, ChatMessage
from backend.app.adapters.factory import AdapterFactory

router = APIRouter(prefix="/models", tags=["模型管理"])


class ModelCreateRequest(BaseModel):
    id: str
    name: str
    provider_type: str = "openai"
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    model_name: Optional[str] = None
    config: Optional[ModelConfig] = None


class ModelUpdateRequest(BaseModel):
    name: Optional[str] = None
    provider_type: Optional[str] = None
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    model_name: Optional[str] = None
    config: Optional[ModelConfig] = None


@router.get("/")
def list_models(db: Session = Depends(get_db)):
    models = db.query(RegisteredModel).all()
    res = []
    for m in models:
        res.append({
            "id": m.id,
            "name": m.name,
            "provider_type": m.provider_type,
            "api_url": m.api_url,
            "model_name": m.model_name,
            "config": json.loads(m.config_json or "{}"),
            "created_at": m.created_at,
        })
    return res


def _probe_vertex_thinking_mode(
    provider_type: str,
    model_name: str,
    api_url: Optional[str],
    api_key: Optional[str],
    cfg_dict: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """对 Vertex 模型自适应探测实际支持的思考协议并写回 cfg_dict"""
    if provider_type.lower() not in ("vertex", "gemini"):
        return None
    try:
        adapter = AdapterFactory.create(
            provider_type=provider_type,
            model_name=model_name,
            api_url=api_url,
            api_key=api_key,
            **cfg_dict,
        )
        detect_fn = getattr(adapter, "detect_thinking_mode", None)
        if not callable(detect_fn):
            return None
        probe_res = detect_fn()
        detected_mode = probe_res.get("thinking_mode")
        if detected_mode:
            cfg_dict["vertex_thinking_mode"] = detected_mode
        return probe_res
    except Exception as e:
        return {"thinking_mode": None, "error": str(e)}


@router.post("/")
def register_model(payload: ModelCreateRequest, db: Session = Depends(get_db)):
    existing = db.query(RegisteredModel).filter(RegisteredModel.id == payload.id).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"模型 ID '{payload.id}' 已存在")

    cfg_dict = payload.config.model_dump() if payload.config else ModelConfig().model_dump()
    eff_model_name = payload.model_name or payload.id
    probe_info = None
    if payload.provider_type.lower() in ("vertex", "gemini") and (
        not cfg_dict.get("vertex_thinking_mode") or cfg_dict.get("vertex_thinking_mode") == "auto"
    ):
        probe_info = _probe_vertex_thinking_mode(
            provider_type=payload.provider_type,
            model_name=eff_model_name,
            api_url=payload.api_url,
            api_key=payload.api_key,
            cfg_dict=cfg_dict,
        )

    cfg_str = json.dumps(cfg_dict, ensure_ascii=False)
    m = RegisteredModel(
        id=payload.id,
        name=payload.name,
        provider_type=payload.provider_type,
        api_url=payload.api_url,
        api_key=payload.api_key,
        model_name=eff_model_name,
        config_json=cfg_str,
    )
    db.add(m)
    db.commit()
    return {
        "message": "模型注册成功",
        "id": payload.id,
        "vertex_thinking_mode": cfg_dict.get("vertex_thinking_mode"),
        "probe_info": probe_info,
    }


@router.put("/{model_id}")
def update_model(model_id: str, payload: ModelUpdateRequest, db: Session = Depends(get_db)):
    m = db.query(RegisteredModel).filter(RegisteredModel.id == model_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="未找到该模型")

    old_provider = (m.provider_type or "").lower()
    old_model_name = m.model_name
    old_api_url = m.api_url
    cfg_dict = json.loads(m.config_json or "{}")

    if payload.name is not None:
        m.name = payload.name
    if payload.provider_type is not None:
        m.provider_type = payload.provider_type
    if payload.api_url is not None:
        m.api_url = payload.api_url
    if payload.api_key is not None:
        m.api_key = payload.api_key
    if payload.model_name is not None:
        m.model_name = payload.model_name
    if payload.config is not None:
        new_cfg = payload.config.model_dump()
        if not new_cfg.get("vertex_thinking_mode") and cfg_dict.get("vertex_thinking_mode"):
            new_cfg["vertex_thinking_mode"] = cfg_dict["vertex_thinking_mode"]
        cfg_dict = new_cfg

    is_vertex = (m.provider_type or "").lower() in ("vertex", "gemini")
    target_changed = (
        old_provider != (m.provider_type or "").lower()
        or old_model_name != m.model_name
        or old_api_url != m.api_url
    )
    probe_info = None
    if is_vertex and (
        target_changed
        or not cfg_dict.get("vertex_thinking_mode")
        or cfg_dict.get("vertex_thinking_mode") == "auto"
    ):
        if target_changed:
            cfg_dict.pop("vertex_thinking_mode", None)
        probe_info = _probe_vertex_thinking_mode(
            provider_type=m.provider_type,
            model_name=m.model_name or m.id,
            api_url=m.api_url,
            api_key=m.api_key,
            cfg_dict=cfg_dict,
        )

    m.config_json = json.dumps(cfg_dict, ensure_ascii=False)
    db.commit()
    return {
        "message": f"模型 '{model_id}' 更新成功",
        "id": model_id,
        "vertex_thinking_mode": cfg_dict.get("vertex_thinking_mode"),
        "probe_info": probe_info,
    }


@router.delete("/{model_id}")
def delete_model(model_id: str, db: Session = Depends(get_db)):
    m = db.query(RegisteredModel).filter(RegisteredModel.id == model_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="未找到该模型")
    db.delete(m)
    db.commit()
    return {"message": f"模型 '{model_id}' 已删除"}


@router.post("/{model_id}/test")
def test_model_connection(model_id: str, db: Session = Depends(get_db)):
    m = db.query(RegisteredModel).filter(RegisteredModel.id == model_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="未找到该模型")

    try:
        saved_cfg = json.loads(m.config_json or "{}")
        probe_info = None
        if (m.provider_type or "").lower() in ("vertex", "gemini"):
            probe_info = _probe_vertex_thinking_mode(
                provider_type=m.provider_type,
                model_name=m.model_name or m.id,
                api_url=m.api_url,
                api_key=m.api_key,
                cfg_dict=saved_cfg,
            )
            if probe_info and probe_info.get("thinking_mode"):
                m.config_json = json.dumps(saved_cfg, ensure_ascii=False)
                db.commit()

        adapter = AdapterFactory.create(
            provider_type=m.provider_type,
            model_name=m.model_name or m.id,
            api_url=m.api_url,
            api_key=m.api_key,
            **saved_cfg,
        )
        test_cfg = ModelConfig(**saved_cfg)
        test_cfg.max_tokens = min(test_cfg.max_tokens or 256, 512)
        req = UnifiedChatRequest(
            model_id=m.id,
            messages=[ChatMessage(role="user", content="你好，测试连通性。")],
            config=test_cfg,
        )
        t0 = time.time()
        resp = adapter.chat(req)
        cost = (time.time() - t0) * 1000.0
        return {
            "status": "SUCCESS",
            "reply": resp.content,
            "latency_ms": round(cost, 2),
            "thinking": resp.thinking,
            "vertex_thinking_mode": saved_cfg.get("vertex_thinking_mode"),
            "probe_info": probe_info,
        }
    except Exception as e:
        return {"status": "FAILED", "error": str(e)}


class ModelChatPayload(BaseModel):
    messages: List[ChatMessage]
    system_prompt: Optional[str] = None
    config: Optional[ModelConfig] = None


@router.post("/{model_id}/chat")
def chat_with_model(model_id: str, payload: ModelChatPayload, db: Session = Depends(get_db)):
    """支持人工自由对话：与指定模型进行多轮上下文对话交互"""
    m = db.query(RegisteredModel).filter(RegisteredModel.id == model_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="未找到该模型")

    try:
        saved_cfg = json.loads(m.config_json or "{}")
        adapter = AdapterFactory.create(
            provider_type=m.provider_type,
            model_name=m.model_name or m.id,
            api_url=m.api_url,
            api_key=m.api_key,
            **saved_cfg,
        )
        merged_cfg = ModelConfig(**saved_cfg)
        if payload.config:
            merged_dict = payload.config.model_dump(exclude_unset=True)
            for k, v in merged_dict.items():
                if v is not None:
                    setattr(merged_cfg, k, v)

        full_msgs = []
        if payload.system_prompt:
            full_msgs.append(ChatMessage(role="system", content=payload.system_prompt))
        full_msgs.extend(payload.messages)

        req = UnifiedChatRequest(
            model_id=m.id,
            messages=full_msgs,
            config=merged_cfg,
        )
        t0 = time.time()
        resp = adapter.chat(req)
        cost = (time.time() - t0) * 1000.0
        return {
            "status": "SUCCESS",
            "reply": resp.content,
            "thinking": resp.thinking,
            "latency_ms": round(cost, 2),
            "usage": resp.usage,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"模型对话调用异常: {str(e)}")
