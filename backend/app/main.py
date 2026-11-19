# -*- coding: utf-8 -*-
"""
FastAPI 主服务入口
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.app.config import settings
from backend.app.models import init_db
from backend.app.api import api_router

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="通用大模型测试管理与数据飞轮平台后端 API",
    version="1.0.0",
)

# 允许跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载 API 路由
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/health", tags=["系统监控"])
def health_check():
    return {
        "status": "healthy",
        "project": settings.PROJECT_NAME,
        "version": "1.0.0",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app.main:app", host=settings.SERVER_HOST, port=settings.SERVER_PORT, reload=True)
