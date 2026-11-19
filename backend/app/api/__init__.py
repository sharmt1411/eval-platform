# -*- coding: utf-8 -*-
"""API 路由导出"""
from fastapi import APIRouter
from backend.app.api.models import router as models_router
from backend.app.api.rules import router as rules_router
from backend.app.api.datasets import router as datasets_router
from backend.app.api.benchmarks import router as benchmarks_router
from backend.app.api.simulation import router as simulation_router
from backend.app.api.flywheel import router as flywheel_router
from backend.app.api.prompts import router as prompts_router

api_router = APIRouter()
api_router.include_router(models_router)
api_router.include_router(rules_router)
api_router.include_router(datasets_router)
api_router.include_router(benchmarks_router)
api_router.include_router(simulation_router)
api_router.include_router(flywheel_router)
api_router.include_router(prompts_router)

__all__ = ["api_router"]
