# -*- coding: utf-8 -*-
"""
后端全局设置与路径配置
"""
import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "通用大模型测试管理与数据飞轮平台"
    API_V1_PREFIX: str = "/api/v1"
    
    # 项目根路径
    BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    DATA_DIR: str = os.path.join(BASE_DIR, "data")
    RULES_DIR: str = os.path.join(DATA_DIR, "rules")
    DATASETS_DIR: str = os.path.join(DATA_DIR, "datasets")
    RUNS_DIR: str = os.path.join(DATA_DIR, "runs")
    
    # 服务端口与监听地址
    SERVER_HOST: str = os.environ.get("SERVER_HOST", "127.0.0.1")
    SERVER_PORT: int = int(os.environ.get("SERVER_PORT", "9021"))
    
    # SQLite 数据库
    DATABASE_URL: str = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(DATA_DIR, 'eval_platform.db')}"
    )

    class Config:
        env_file = ".env"


settings = Settings()

os.makedirs(settings.DATA_DIR, exist_ok=True)
os.makedirs(settings.RULES_DIR, exist_ok=True)
os.makedirs(settings.DATASETS_DIR, exist_ok=True)
os.makedirs(settings.RUNS_DIR, exist_ok=True)
