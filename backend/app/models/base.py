# -*- coding: utf-8 -*-
"""
数据库实体与连接管理
"""
import os
import time
from sqlalchemy import create_engine, Column, String, Integer, Float, Text, Boolean, text
from sqlalchemy.orm import declarative_base, sessionmaker
from backend.app.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class RegisteredModel(Base):
    """已注册模型表"""
    __tablename__ = "registered_models"

    id = Column(String(64), primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    provider_type = Column(String(32), default="openai")  # openai, harmony, vertex, mock
    api_url = Column(String(512), nullable=True)
    api_key = Column(String(256), nullable=True)
    model_name = Column(String(128), nullable=True)
    config_json = Column(Text, default="{}")
    created_at = Column(Integer, default=lambda: int(time.time()))


class RuleConfigRecord(Base):
    """场景规则配置表"""
    __tablename__ = "rule_configs"

    rule_id = Column(String(64), primary_key=True, index=True)
    project_name = Column(String(128), nullable=False)
    description = Column(String(256), default="")
    content_json = Column(Text, nullable=False)
    created_at = Column(Integer, default=lambda: int(time.time()))
    updated_at = Column(Integer, default=lambda: int(time.time()))


class DatasetRecord(Base):
    """数据集版本表"""
    __tablename__ = "datasets"

    id = Column(String(64), primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    file_path = Column(String(512), nullable=False)
    sample_count = Column(Integer, default=0)
    dataset_type = Column(String(32), default="benchmark")  # benchmark / flywheel_gold / flywheel_discarded
    created_at = Column(Integer, default=lambda: int(time.time()))


class TaskRecord(Base):
    """异步任务状态表"""
    __tablename__ = "tasks"

    id = Column(String(64), primary_key=True, index=True)
    task_type = Column(String(32), nullable=False)  # benchmark / simulation / flywheel
    status = Column(String(32), default="PENDING")  # PENDING / RUNNING / COMPLETED / FAILED
    progress = Column(Float, default=0.0)
    result_json = Column(Text, default="{}")
    error_message = Column(Text, default="")
    created_at = Column(Integer, default=lambda: int(time.time()))
    finished_at = Column(Integer, nullable=True)


class FlywheelSampleRecord(Base):
    """飞轮样本与人工复核表"""
    __tablename__ = "flywheel_samples"

    id = Column(String(64), primary_key=True, index=True)
    task_id = Column(String(64), nullable=True, index=True)
    rule_id = Column(String(64), nullable=False)
    scenario_id = Column(String(64), nullable=False)
    status = Column(String(32), default="PENDING")  # GOLD_PASS / REPAIRED_PASS / DISCARDED / MANUAL_APPROVED
    score = Column(Integer, default=0)
    raw_messages_json = Column(Text, nullable=False)
    remediated_messages_json = Column(Text, nullable=True)
    applied_actions_json = Column(Text, default="[]")
    benchmark_slices_json = Column(Text, default="[]")
    remediation_debug_json = Column(Text, default="{}")
    initial_eval_json = Column(Text, default="{}")
    verified_eval_json = Column(Text, default="{}")
    reviewer_comment = Column(Text, default="")
    exported_to_benchmark = Column(Boolean, default=False)
    updated_at = Column(Integer, default=lambda: int(time.time()))


class BenchmarkRunLedger(Base):
    """评测抽样与复测战绩只追加流水表 (按 dataset_path + model_line 隔离)"""
    __tablename__ = "benchmark_run_ledgers"

    id = Column(String(64), primary_key=True, index=True)
    task_id = Column(String(64), nullable=False, index=True)
    dataset_path = Column(String(512), nullable=False, index=True)
    model_line = Column(String(128), nullable=False, index=True)
    version_tag = Column(String(128), nullable=False, index=True)
    sampled_ids_json = Column(Text, default="[]")
    records_json = Column(Text, default="[]")
    is_revoked = Column(Boolean, default=False, index=True)
    created_at = Column(Integer, default=lambda: int(time.time()))


class PromptTemplateRecord(Base):
    """全平台提示词模板管理表"""
    __tablename__ = "prompt_templates"

    prompt_key = Column(String(64), primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    category = Column(String(64), default="通用")
    description = Column(Text, default="")
    engine_location = Column(String(256), default="")
    template_content = Column(Text, nullable=False)
    default_content = Column(Text, nullable=False)
    placeholders_json = Column(Text, default="[]")
    sample_vars_json = Column(Text, default="{}")
    is_customized = Column(Boolean, default=False)
    created_at = Column(Integer, default=lambda: int(time.time()))
    updated_at = Column(Integer, default=lambda: int(time.time()))


def init_db():
    import json
    Base.metadata.create_all(bind=engine)

    # 平滑检测并添加新增列 (SQLite 兼容)
    with engine.connect() as conn:
        try:
            res = conn.execute(text("PRAGMA table_info(flywheel_samples)")).fetchall()
            existing_cols = {row[1] for row in res}
            if "benchmark_slices_json" not in existing_cols:
                conn.execute(text("ALTER TABLE flywheel_samples ADD COLUMN benchmark_slices_json TEXT DEFAULT '[]'"))
                conn.commit()
            if "applied_actions_json" not in existing_cols:
                conn.execute(text("ALTER TABLE flywheel_samples ADD COLUMN applied_actions_json TEXT DEFAULT '[]'"))
                conn.commit()
            if "remediation_debug_json" not in existing_cols:
                conn.execute(text("ALTER TABLE flywheel_samples ADD COLUMN remediation_debug_json TEXT DEFAULT '{}'"))
                conn.commit()
            if "exported_to_benchmark" not in existing_cols:
                conn.execute(text("ALTER TABLE flywheel_samples ADD COLUMN exported_to_benchmark BOOLEAN DEFAULT 0"))
                conn.commit()
        except Exception as e:
            # 若表尚未创建或其他方言忽略
            pass

    # 自动预置核心示范模型配置 (若尚未注册)
    default_vertex_project = os.environ.get("VERTEX_PROJECT", "your-gcp-project-id")
    default_vertex_location = os.environ.get("VERTEX_LOCATION", "global")
    default_vertex_url = f"vertexai://{default_vertex_project}/{default_vertex_location}"

    db = SessionLocal()
    try:
        seed_models = [
            {
                "id": "vertex-gemini-flash-client",
                "name": "Vertex Gemini 3.8 Flash (模拟客户-LOW思考)",
                "provider_type": "vertex",
                "api_url": default_vertex_url,
                "api_key": "",
                "model_name": "gemini-3.8-flash",
                "config": {
                    "temperature": 0.6,
                    "top_p": 0.9,
                    "max_tokens": 2048,
                    "repetition_penalty": 1.0,
                    "thinking_enabled": False,
                    "thinking_level": "NONE",
                    "vertex_thinking_mode": "gemini_native",
                },
            },
            {
                "id": "vertex-gemini-flash-judge",
                "name": "Vertex Gemini 3.8 Flash (质检与修复-HIGH思考)",
                "provider_type": "vertex",
                "api_url": default_vertex_url,
                "api_key": "",
                "model_name": "gemini-3.8-flash",
                "config": {
                    "temperature": 0.1,
                    "top_p": 0.9,
                    "max_tokens": 10240,
                    "repetition_penalty": 1.0,
                    "thinking_enabled": True,
                    "thinking_level": "HIGH",
                    "vertex_thinking_mode": "gemini_native",
                },
            },
            {
                "id": "vertex-deepseek-v4-flash",
                "name": "Vertex DeepSeek V4.1 Flash MaaS (HIGH思考)",
                "provider_type": "vertex",
                "api_url": default_vertex_url,
                "api_key": "",
                "model_name": "deepseek-ai/deepseek-v4.1-flash-maas",
                "config": {
                    "temperature": 0.2,
                    "top_p": 0.9,
                    "max_tokens": 10240,
                    "repetition_penalty": 1.0,
                    "thinking_enabled": True,
                    "thinking_level": "HIGH",
                    "vertex_thinking_mode": "maas_reasoning_effort",
                },
            },
            {
                "id": "harmony-demo-target-model",
                "name": "通用对话被测模型 (Harmony 20B)",
                "provider_type": "harmony",
                "api_url": "http://127.0.0.1:8000/v1/chat/completions",
                "api_key": "EMPTY",
                "model_name": "demo-target-model",
                "config": {
                    "temperature": 0.3,
                    "top_p": 0.9,
                    "max_tokens": 512,
                    "repetition_penalty": 1.05,
                    "thinking_enabled": False,
                    "thinking_level": "NONE",
                },
            },
        ]
        for sm in seed_models:
            exists = db.query(RegisteredModel).filter(RegisteredModel.id == sm["id"]).first()
            if not exists:
                db.add(
                    RegisteredModel(
                        id=sm["id"],
                        name=sm["name"],
                        provider_type=sm["provider_type"],
                        api_url=sm["api_url"],
                        api_key=sm["api_key"],
                        model_name=sm["model_name"],
                        config_json=json.dumps(sm["config"], ensure_ascii=False),
                    )
                )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
