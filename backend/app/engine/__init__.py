# -*- coding: utf-8 -*-
"""评测与飞轮核心引擎导出（PEP 562 惰性按需加载，避免包级预导入全部引擎）"""
import importlib
from typing import Any, Dict, Tuple

_EXPORTS: Dict[str, Tuple[str, str]] = {
    "UniversalTopologyValidator": ("backend.app.engine.topology", "UniversalTopologyValidator"),
    "UniversalSimulator": ("backend.app.engine.simulator", "UniversalSimulator"),
    "UniversalJudgeEngine": ("backend.app.engine.judge", "UniversalJudgeEngine"),
    "UniversalDataRemediator": ("backend.app.engine.remediator", "UniversalDataRemediator"),
    "BenchmarkRunner": ("backend.app.engine.benchmark_runner", "BenchmarkRunner"),
    "BenchmarkSamplingEngine": ("backend.app.engine.benchmark_sampler", "BenchmarkSamplingEngine"),
    "ProgressiveContextSlicer": ("backend.app.engine.slicer", "ProgressiveContextSlicer"),
    "UniversalFAQRetriever": ("backend.app.engine.faq_retriever", "UniversalFAQRetriever"),
}

__all__ = list(_EXPORTS.keys())


def __getattr__(name: str) -> Any:
    if name in _EXPORTS:
        mod_path, attr = _EXPORTS[name]
        return getattr(importlib.import_module(mod_path), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
