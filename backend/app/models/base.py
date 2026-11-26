# -*- coding: utf-8 -*-
"""
数据库实体与连接管理
"""
import os
import json
import time
from typing import Any, Dict, List, Optional
from sqlalchemy import create_engine, Column, String, Integer, Float, Text, Boolean, text
from sqlalchemy.orm import declarative_base, sessionmaker
from backend.app.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30} if "sqlite" in settings.DATABASE_URL else {},
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
    exported_training_datasets_json = Column(Text, default="[]")
    exported_benchmark_datasets_json = Column(Text, default="[]")
    raw_verdict = Column(String(32), nullable=True)          # UNJUDGED / PASS / FAIL / ERROR
    remediated_verdict = Column(String(32), nullable=True)   # NONE / UNVERIFIED / PASS / FAIL / ERROR
    manual_decision = Column(String(32), nullable=True)      # NONE / APPROVED / REJECTED
    active_version = Column(String(32), nullable=True)       # raw / remediated
    evaluated_rule_updated_at = Column(Integer, nullable=True)
    raw_user_hash = Column(String(64), nullable=True, index=True)
    remediated_user_hash = Column(String(64), nullable=True, index=True)
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


def _safe_json_load(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
        return val if type(val) is type(default) else default
    except Exception:
        return default


def _is_eval_dict_error(ev: Dict[str, Any]) -> bool:
    if not isinstance(ev, dict) or not ev:
        return False
    defects = [
        str(d).strip()
        for d in (ev.get("detected_defects") or [])
        if str(d).strip() and not str(d).strip().startswith("【拓扑检测元数据】")
    ]
    prefixes = (
        "模型未按标准 JSON 输出质检报告",
        "质检裁判模型调用异常",
        "模型返回的 JSON 缺少",
    )
    if any(d.startswith(prefixes) for d in defects):
        return True
    for dbg in (ev.get("debug_requests") or []):
        if isinstance(dbg, dict) and dbg.get("step") == "main_judge":
            if dbg.get("error") or (
                not str(dbg.get("response_content") or "").strip()
                and not str(dbg.get("raw_output") or "").strip()
            ):
                return True
    return False


def _verdict_from_eval_snapshot(
    ev: Dict[str, Any], threshold: int = 85, require_zero_defects: bool = False
) -> str:
    if not isinstance(ev, dict) or ("score" not in ev and "verdict" not in ev):
        return "UNJUDGED"
    if _is_eval_dict_error(ev):
        return "ERROR"
    verdict = str(ev.get("verdict") or "").upper()
    try:
        score = int(ev.get("score") or 0)
    except Exception:
        score = 0
    if verdict == "PASS" and score >= threshold:
        if require_zero_defects:
            defects = [
                d
                for d in (ev.get("detected_defects") or [])
                if str(d).strip() and not str(d).strip().startswith("【拓扑检测元数据】")
            ]
            if ev.get("deductions") or defects:
                return "FAIL"
        return "PASS"
    return "FAIL"


def _rewrite_benchmark_is_holdout(file_paths: List[str], sample_ids: set) -> int:
    import threading
    rewritten = 0
    prefixed = {f"flywheel_{sid}" for sid in sample_ids}
    for fpath in file_paths:
        if not fpath or not os.path.exists(fpath):
            continue
        changed = False
        new_lines: List[str] = []
        with open(fpath, "r", encoding="utf-8") as f:
            for line in f:
                raw_s = line.strip()
                if not raw_s:
                    continue
                try:
                    obj = json.loads(raw_s)
                except Exception:
                    new_lines.append(raw_s)
                    continue
                if isinstance(obj, dict):
                    meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
                    src_sid = str(meta.get("source_sample_id") or "").strip()
                    src_tag = str(obj.get("source") or "").strip()
                    if (src_sid in sample_ids or src_tag in prefixed) and obj.get("is_holdout") is True:
                        obj["is_holdout"] = False
                        changed = True
                        rewritten += 1
                    new_lines.append(json.dumps(obj, ensure_ascii=False))
                else:
                    new_lines.append(raw_s)
        if changed:
            tmp_path = f"{fpath}.tmp.{os.getpid()}.{threading.get_ident()}"
            try:
                with open(tmp_path, "w", encoding="utf-8") as wf:
                    for ln in new_lines:
                        wf.write(ln + "\n")
                os.replace(tmp_path, fpath)
            except Exception:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                raise
    return rewritten


def _migrate_flywheel_samples_and_lineage(db) -> None:
    """幂等迁移存量 flywheel_samples：补齐解耦结论、导出留底，并修正同源冲突的留出标记。"""
    from backend.app.engine.deduplicator import DatasetDeduplicator

    samples = db.query(FlywheelSampleRecord).all()
    if not samples:
        return

    # 收集各规则的最近更新时间与通过阈值
    rule_ts_map: Dict[str, int] = {}
    rule_threshold_map: Dict[str, int] = {}
    for rc in db.query(RuleConfigRecord).all():
        if rc.updated_at is not None:
            rule_ts_map[rc.rule_id] = int(rc.updated_at)
        try:
            content_d = json.loads(rc.content_json or "{}")
            th = content_d.get("inspection_rules", {}).get("pass_score_threshold")
            if th is not None:
                rule_threshold_map[rc.rule_id] = int(th)
        except Exception:
            pass
    if os.path.exists(settings.RULES_DIR):
        for fname in os.listdir(settings.RULES_DIR):
            if fname.endswith(".json"):
                fpath = os.path.join(settings.RULES_DIR, fname)
                rid = fname[:-5]
                if rid not in rule_ts_map:
                    try:
                        rule_ts_map[rid] = int(os.path.getmtime(fpath))
                    except Exception:
                        pass
                if rid not in rule_threshold_map:
                    try:
                        with open(fpath, "r", encoding="utf-8") as rf:
                            rd = json.load(rf)
                        rid_in = str(rd.get("rule_id") or rid)
                        th = rd.get("inspection_rules", {}).get("pass_score_threshold")
                        if th is not None:
                            rule_threshold_map[rid_in] = int(th)
                    except Exception:
                        pass

    by_id: Dict[str, FlywheelSampleRecord] = {}
    by_slice_id: Dict[str, FlywheelSampleRecord] = {}
    by_rule_user_hash: Dict[tuple, FlywheelSampleRecord] = {}
    changed = False

    for s in samples:
        by_id[s.id] = s
        raw_msgs = _safe_json_load(s.raw_messages_json, [])
        remed_msgs = _safe_json_load(s.remediated_messages_json, []) if s.remediated_messages_json else []
        has_remed = bool(remed_msgs)

        if not s.raw_user_hash or (has_remed and not s.remediated_user_hash):
            r_h, m_h = DatasetDeduplicator.compute_dialogue_user_hashes(
                raw_msgs, remed_msgs if has_remed else None
            )
            if not s.raw_user_hash:
                s.raw_user_hash = r_h
                changed = True
            if has_remed and not s.remediated_user_hash:
                s.remediated_user_hash = m_h
                changed = True

        if s.raw_user_hash:
            by_rule_user_hash.setdefault((s.rule_id, s.raw_user_hash), s)
        if s.remediated_user_hash:
            by_rule_user_hash.setdefault((s.rule_id, s.remediated_user_hash), s)

        for sl in _safe_json_load(s.benchmark_slices_json, []):
            if isinstance(sl, dict) and sl.get("slice_id"):
                by_slice_id[str(sl["slice_id"])] = s

        if s.exported_training_datasets_json is None:
            s.exported_training_datasets_json = "[]"
            changed = True
        if s.exported_benchmark_datasets_json is None:
            s.exported_benchmark_datasets_json = "[]"
            changed = True

        # 解耦结论幂等迁移（仅处理尚未写入 raw_verdict 的存量行）
        if not s.raw_verdict:
            th = rule_threshold_map.get(s.rule_id, 85)
            init_eval = _safe_json_load(s.initial_eval_json, {})
            ver_eval = _safe_json_load(s.verified_eval_json, {})
            st = (s.status or "PENDING").upper()

            inferred_raw = _verdict_from_eval_snapshot(init_eval, th, require_zero_defects=True)
            inferred_ver = (
                _verdict_from_eval_snapshot(ver_eval, th, require_zero_defects=False)
                if ver_eval
                else ("UNVERIFIED" if has_remed else "NONE")
            )

            if st == "GOLD_PASS":
                s.raw_verdict = "PASS"
                s.remediated_verdict = "NONE"
                s.manual_decision = "NONE"
                s.active_version = "raw"
            elif st == "REPAIRED_PASS":
                if not has_remed:
                    s.raw_verdict = "PASS"
                    s.remediated_verdict = "NONE"
                    s.manual_decision = "NONE"
                    s.active_version = "raw"
                else:
                    s.raw_verdict = inferred_raw if inferred_raw != "UNJUDGED" else "FAIL"
                    s.remediated_verdict = "PASS"
                    s.manual_decision = "NONE"
                    s.active_version = "remediated"
            elif st == "MANUAL_APPROVED":
                s.raw_verdict = inferred_raw
                s.remediated_verdict = inferred_ver if has_remed else "NONE"
                s.manual_decision = "APPROVED"
                s.active_version = "remediated" if has_remed else "raw"
            elif st == "DISCARDED":
                s.raw_verdict = inferred_raw if inferred_raw != "UNJUDGED" else "FAIL"
                if has_remed:
                    s.remediated_verdict = inferred_ver if inferred_ver != "UNVERIFIED" else "FAIL"
                    s.active_version = "remediated"
                    s.manual_decision = "REJECTED" if s.remediated_verdict != "FAIL" else "NONE"
                else:
                    s.remediated_verdict = "NONE"
                    s.active_version = "raw"
                    s.manual_decision = "REJECTED"
            else:  # PENDING 或未知
                s.raw_verdict = inferred_raw
                s.remediated_verdict = inferred_ver if has_remed else "NONE"
                s.manual_decision = "NONE"
                s.active_version = "remediated" if has_remed else "raw"

            if s.evaluated_rule_updated_at is None and (
                s.raw_verdict != "UNJUDGED"
                or s.remediated_verdict not in ("NONE", "UNVERIFIED")
                or s.manual_decision != "NONE"
            ):
                s.evaluated_rule_updated_at = rule_ts_map.get(s.rule_id) or s.updated_at
            changed = True

    # 扫描数据目录与 datasets 表回填导出留底
    ds_records = {d.id: d for d in db.query(DatasetRecord).all()}
    bench_files: Dict[str, str] = {}
    bench_adds: Dict[str, List[str]] = {}
    train_adds: Dict[str, List[str]] = {}

    candidate_files: Dict[str, tuple] = {}
    for did, drec in ds_records.items():
        if drec.file_path and os.path.exists(drec.file_path):
            candidate_files[did] = (drec.file_path, drec.dataset_type)
    if os.path.exists(settings.DATASETS_DIR):
        for fname in sorted(os.listdir(settings.DATASETS_DIR)):
            if not fname.endswith(".jsonl"):
                continue
            did = fname[:-6]
            fpath = os.path.join(settings.DATASETS_DIR, fname)
            if did not in candidate_files:
                candidate_files[did] = (fpath, None)

    for did, (fpath, declared_type) in candidate_files.items():
        rows: List[Dict[str, Any]] = []
        try:
            with open(fpath, "r", encoding="utf-8") as df:
                for ln in df:
                    ln_s = ln.strip()
                    if not ln_s:
                        continue
                    obj = json.loads(ln_s)
                    if isinstance(obj, dict):
                        rows.append(obj)
        except Exception:
            continue
        if not rows:
            continue

        eff_type = declared_type
        if eff_type is None:
            is_bench = all(
                isinstance(r.get("messages"), list)
                and len(r.get("messages") or []) > 0
                and (
                    bool(str(r.get("reference") or "").strip())
                    or bool(str(r.get("rubric") or "").strip())
                    or bool(r.get("regex_constraints"))
                )
                for r in rows
            )
            eff_type = "benchmark" if is_bench else "flywheel_gold"

        if eff_type == "benchmark":
            bench_files[did] = fpath
            for r in rows:
                meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
                matched_sample: Optional[FlywheelSampleRecord] = None
                src_sid = str(meta.get("source_sample_id") or "").strip()
                if src_sid and src_sid in by_id:
                    matched_sample = by_id[src_sid]
                if matched_sample is None:
                    src = str(r.get("source") or "").strip()
                    if src.startswith("flywheel_"):
                        cand_sid = src[len("flywheel_") :]
                        matched_sample = by_id.get(cand_sid)
                if matched_sample is None and r.get("id"):
                    matched_sample = by_slice_id.get(str(r["id"]))
                if matched_sample is not None:
                    lst = bench_adds.setdefault(matched_sample.id, [])
                    if did not in lst:
                        lst.append(did)
        elif eff_type == "flywheel_gold":
            for r in rows:
                meta = r.get("metadata")
                if not isinstance(meta, dict):
                    continue
                row_status = str(meta.get("status") or "").strip()
                if not meta.get("source_sample_id") and row_status not in (
                    "GOLD_PASS",
                    "REPAIRED_PASS",
                    "MANUAL_APPROVED",
                ):
                    # 未质检的原始对话语料不算“样本池导出训练集”
                    continue
                matched_sample = None
                src_sid = str(meta.get("source_sample_id") or "").strip()
                if src_sid and src_sid in by_id:
                    matched_sample = by_id[src_sid]
                if matched_sample is None and r.get("id"):
                    matched_sample = by_id.get(str(r["id"]))
                if matched_sample is None and meta.get("rule_id") and isinstance(r.get("messages"), list):
                    u_hash = DatasetDeduplicator.compute_user_sequence_hash(r["messages"])
                    matched_sample = by_rule_user_hash.get((str(meta["rule_id"]), u_hash))
                if matched_sample is not None:
                    lst = train_adds.setdefault(matched_sample.id, [])
                    if did not in lst:
                        lst.append(did)

    conflict_sample_ids: set = set()
    conflict_bench_ids: set = set()
    for s in samples:
        cur_train = [str(x) for x in _safe_json_load(s.exported_training_datasets_json, []) if x]
        cur_bench = [str(x) for x in _safe_json_load(s.exported_benchmark_datasets_json, []) if x]
        new_train = list(cur_train)
        for did in train_adds.get(s.id, []):
            if did not in new_train:
                new_train.append(did)
        new_bench = list(cur_bench)
        for did in bench_adds.get(s.id, []):
            if did not in new_bench:
                new_bench.append(did)

        if new_train != cur_train:
            s.exported_training_datasets_json = json.dumps(new_train, ensure_ascii=False)
            changed = True
        if new_bench != cur_bench:
            s.exported_benchmark_datasets_json = json.dumps(new_bench, ensure_ascii=False)
            changed = True
        if new_bench and not bool(s.exported_to_benchmark):
            s.exported_to_benchmark = True
            changed = True
        if new_train and new_bench:
            conflict_sample_ids.add(s.id)
            conflict_bench_ids.update(new_bench)

    if conflict_sample_ids and conflict_bench_ids:
        target_paths = [bench_files[did] for did in conflict_bench_ids if did in bench_files]
        _rewrite_benchmark_is_holdout(target_paths, conflict_sample_ids)

    if changed:
        db.commit()


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
            if "exported_training_datasets_json" not in existing_cols:
                conn.execute(text("ALTER TABLE flywheel_samples ADD COLUMN exported_training_datasets_json TEXT DEFAULT '[]'"))
                conn.commit()
            if "exported_benchmark_datasets_json" not in existing_cols:
                conn.execute(text("ALTER TABLE flywheel_samples ADD COLUMN exported_benchmark_datasets_json TEXT DEFAULT '[]'"))
                conn.commit()
            if "raw_verdict" not in existing_cols:
                conn.execute(text("ALTER TABLE flywheel_samples ADD COLUMN raw_verdict VARCHAR(32)"))
                conn.commit()
            if "remediated_verdict" not in existing_cols:
                conn.execute(text("ALTER TABLE flywheel_samples ADD COLUMN remediated_verdict VARCHAR(32)"))
                conn.commit()
            if "manual_decision" not in existing_cols:
                conn.execute(text("ALTER TABLE flywheel_samples ADD COLUMN manual_decision VARCHAR(32)"))
                conn.commit()
            if "active_version" not in existing_cols:
                conn.execute(text("ALTER TABLE flywheel_samples ADD COLUMN active_version VARCHAR(32)"))
                conn.commit()
            if "evaluated_rule_updated_at" not in existing_cols:
                conn.execute(text("ALTER TABLE flywheel_samples ADD COLUMN evaluated_rule_updated_at INTEGER"))
                conn.commit()
            if "raw_user_hash" not in existing_cols:
                conn.execute(text("ALTER TABLE flywheel_samples ADD COLUMN raw_user_hash VARCHAR(64)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_flywheel_samples_raw_user_hash ON flywheel_samples (raw_user_hash)"))
                conn.commit()
            if "remediated_user_hash" not in existing_cols:
                conn.execute(text("ALTER TABLE flywheel_samples ADD COLUMN remediated_user_hash VARCHAR(64)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_flywheel_samples_remediated_user_hash ON flywheel_samples (remediated_user_hash)"))
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
        _migrate_flywheel_samples_and_lineage(db)
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
