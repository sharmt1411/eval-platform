# -*- coding: utf-8 -*-
"""模型适配器模块导出（PEP 562 惰性按需加载，避免包级预导入全部实现）"""
import importlib
from typing import Any, Dict, Tuple

_EXPORTS: Dict[str, Tuple[str, str]] = {
    "BaseModelAdapter": ("backend.app.adapters.base", "BaseModelAdapter"),
    "split_thinking_and_content": ("backend.app.adapters.base", "split_thinking_and_content"),
    "OpenAIAdapter": ("backend.app.adapters.openai_adapter", "OpenAIAdapter"),
    "HarmonyAdapter": ("backend.app.adapters.harmony_adapter", "HarmonyAdapter"),
    "clean_harmony_prefix": ("backend.app.adapters.harmony_adapter", "clean_harmony_prefix"),
    "VertexGeminiAdapter": ("backend.app.adapters.vertex_adapter", "VertexGeminiAdapter"),
    "MockAdapter": ("backend.app.adapters.mock_adapter", "MockAdapter"),
    "AdapterFactory": ("backend.app.adapters.factory", "AdapterFactory"),
}

__all__ = list(_EXPORTS.keys())


def __getattr__(name: str) -> Any:
    if name in _EXPORTS:
        mod_path, attr = _EXPORTS[name]
        return getattr(importlib.import_module(mod_path), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
