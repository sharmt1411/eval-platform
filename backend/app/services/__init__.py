# -*- coding: utf-8 -*-
"""业务服务导出（PEP 562 惰性按需加载，避免包级预导入全部服务）"""
import importlib
from typing import Any, Dict, Tuple

_EXPORTS: Dict[str, Tuple[str, str]] = {
    "SimulationService": ("backend.app.services.simulation_service", "SimulationService"),
    "FlywheelPipelineService": ("backend.app.services.flywheel_service", "FlywheelPipelineService"),
}

__all__ = list(_EXPORTS.keys())


def __getattr__(name: str) -> Any:
    if name in _EXPORTS:
        mod_path, attr = _EXPORTS[name]
        return getattr(importlib.import_module(mod_path), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
