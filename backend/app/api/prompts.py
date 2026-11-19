# -*- coding: utf-8 -*-
"""
全平台提示词模板资产管理 API (/api/v1/prompts)
专注全局提示词模板的查阅、占位符静态校验、修改保存、恢复默认与纯静态占位符示例预览。
具体业务上下文的报文组装与在线模型实测，分别归属规则中心 (/api/v1/rules) 与常规评测 (/api/v1/benchmarks)。
"""
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.app.models import get_db
from backend.app.engine.prompt_registry import (
    get_prompt_template_detail,
    list_prompt_templates,
    preview_prompt_static,
    reset_prompt_template,
    update_prompt_template,
    validate_prompt_placeholders,
)

router = APIRouter(prefix="/prompts", tags=["提示词管理中心"])


class PromptUpdateRequest(BaseModel):
    template_content: str


class PromptStaticPreviewRequest(BaseModel):
    template_override: Optional[str] = None
    custom_vars: Optional[Dict[str, Any]] = None


@router.get("/")
def list_all_prompts(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    items = list_prompt_templates(db)
    for item in items:
        item["missing_placeholders"] = validate_prompt_placeholders(
            item["prompt_key"], item["template_content"]
        )
    return items


@router.get("/{prompt_key}")
def get_prompt_item(prompt_key: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    item = get_prompt_template_detail(db, prompt_key)
    if not item:
        raise HTTPException(status_code=404, detail=f"未找到提示词模块 '{prompt_key}'")
    item["missing_placeholders"] = validate_prompt_placeholders(
        item["prompt_key"], item["template_content"]
    )
    return item


@router.put("/{prompt_key}")
def save_prompt_item(
    prompt_key: str,
    payload: PromptUpdateRequest,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    if not payload.template_content or not payload.template_content.strip():
        raise HTTPException(status_code=400, detail="提示词模板内容不能为空")
    updated = update_prompt_template(db, prompt_key, payload.template_content)
    if not updated:
        raise HTTPException(status_code=404, detail=f"未找到提示词模块 '{prompt_key}'")
    updated["missing_placeholders"] = validate_prompt_placeholders(
        prompt_key, updated["template_content"]
    )
    return {"message": "提示词模板已保存并实时生效", "item": updated}


@router.post("/{prompt_key}/reset")
def reset_prompt_item(prompt_key: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    reset_item = reset_prompt_template(db, prompt_key)
    if not reset_item:
        raise HTTPException(status_code=404, detail=f"未找到提示词模块 '{prompt_key}'")
    reset_item["missing_placeholders"] = []
    return {"message": "已恢复为系统内置默认模板", "item": reset_item}


@router.post("/{prompt_key}/preview")
def preview_prompt_item(
    prompt_key: str,
    payload: PromptStaticPreviewRequest,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """纯静态占位符示例预览：验证模板中的占位符替换与 <!-- USER_INPUT_START --> 拆分效果，不触发业务引擎与外网调用。"""
    res = preview_prompt_static(
        db=db,
        prompt_key=prompt_key,
        template_override=payload.template_override,
        custom_vars=payload.custom_vars,
    )
    if not res:
        raise HTTPException(status_code=404, detail=f"未找到提示词模块 '{prompt_key}'")
    return res
