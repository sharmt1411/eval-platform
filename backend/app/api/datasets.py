# -*- coding: utf-8 -*-
"""
数据集资产管理 API (支持列表同步、样本抽检、外部评测集导入与删除)
"""
import os
import re
import json
import time
import threading
from typing import List, Dict, Any, Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from backend.app.config import settings
from backend.app.models import get_db, DatasetRecord
from backend.app.api.deps import (
    sync_lineage_on_dataset_overwritten,
    sync_lineage_on_dataset_removed,
)
from backend.app.schemas.benchmark import (
    BenchmarkSample,
    BenchmarkExportFilterRequest,
    BenchmarkExportPreviewResponse,
    BenchmarkDatasetFilterOptionsResponse,
    SamplingPreviewResponse,
)
from backend.app.schemas.flywheel import DedupConfig
from backend.app.engine.benchmark_sampler import BenchmarkSamplingEngine
from backend.app.engine.benchmark_runner import has_sample_eval_criteria
from backend.app.engine.deduplicator import DatasetDeduplicator

router = APIRouter(prefix="/datasets", tags=["数据集管理"])


class DatasetImportRequest(BaseModel):
    dataset_id: str = Field(..., description="数据集唯一标识或文件名(不含后缀)")
    dataset_type: Literal["benchmark", "flywheel_gold"] = "benchmark"
    mode: Literal["overwrite", "append"] = "overwrite"
    mark_as_holdout: bool = Field(default=False, description="导入 benchmark 时是否标记为纯评测留出题 (允许降频)")
    content: Optional[str] = Field(default=None, description="JSONL 或 JSON 数组原始文本内容")
    samples: Optional[List[Dict[str, Any]]] = Field(default=None, description="已解析的样本字典数组")
    source_file_path: Optional[str] = Field(default=None, description="服务端本地外部文件路径")
    dedup_config: Optional[DedupConfig] = Field(
        default=None,
        description="去重与防稀释配置（append 模式默认开启，overwrite 模式默认关闭除非显式启用）",
    )


def _load_existing_jsonl_rows(fpath: str) -> List[Dict[str, Any]]:
    """读取既有 JSONL 数据集文件为字典列表"""
    rows: List[Dict[str, Any]] = []
    if not os.path.exists(fpath):
        return rows
    with open(fpath, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    rows.append(obj)
            except Exception:
                pass
    return rows


def _write_jsonl_atomic(fpath: str, rows: List[Dict[str, Any]]) -> None:
    """原子写出 JSONL 文件 (.tmp.{pid}.{tid} + os.replace)"""
    parent_dir = os.path.dirname(os.path.abspath(fpath))
    os.makedirs(parent_dir, exist_ok=True)
    tmp_path = f"{fpath}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp_path, fpath)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise


def _count_jsonl_lines(fpath: str) -> int:
    cnt = 0
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    cnt += 1
    except Exception:
        pass
    return cnt


def _resolve_dataset_file(dataset_id: str, db: Session) -> str:
    rec = db.query(DatasetRecord).filter(DatasetRecord.id == dataset_id).first()
    fpath = rec.file_path if rec else os.path.join(settings.DATASETS_DIR, f"{dataset_id}.jsonl")
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail="数据集文件不存在")
    return fpath


def _load_benchmark_samples(fpath: str) -> List[BenchmarkSample]:
    samples: List[BenchmarkSample] = []
    with open(fpath, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                samples.append(BenchmarkSample(**json.loads(line)))
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"数据集第 {line_no} 行解析为评测样本失败: {str(e)}",
                )
    return samples


def _parse_raw_samples(
    content: Optional[str],
    samples: Optional[List[Dict[str, Any]]],
    source_file_path: Optional[str],
) -> List[Dict[str, Any]]:
    raw_list: List[Dict[str, Any]] = []

    if samples and isinstance(samples, list):
        raw_list.extend(samples)

    text_to_parse = content or ""
    if source_file_path:
        real_src = source_file_path.strip()
        if not os.path.isabs(real_src):
            real_src = os.path.join(settings.BASE_DIR, real_src)
        if not os.path.exists(real_src):
            raise HTTPException(status_code=400, detail=f"找不到指定的外部文件: {source_file_path}")
        try:
            with open(real_src, "r", encoding="utf-8") as f:
                text_to_parse = f.read()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"读取外部文件失败: {str(e)}")

    text_to_parse = text_to_parse.strip()
    if text_to_parse:
        # 优先尝试判断是否为整体 JSON 数组或单对象
        if text_to_parse.startswith("["):
            try:
                parsed_arr = json.loads(text_to_parse)
                if not isinstance(parsed_arr, list):
                    raise ValueError("顶层必须为 JSON 数组")
                for item in parsed_arr:
                    if isinstance(item, dict):
                        raw_list.append(item)
                return raw_list
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"JSON 数组解析失败: {str(e)}")

        # 按 JSONL 逐行解析
        for line_no, line in enumerate(text_to_parse.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("每行必须是一个 JSON 对象")
                raw_list.append(obj)
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"第 {line_no} 行 JSON 格式错误: {str(e)}",
                )

    return raw_list


def _infer_dataset_type_from_file(fpath: str) -> Literal["benchmark", "flywheel_gold", "flywheel_discarded"]:
    """完全基于文件内容判定数据资产类别（不依赖文件名）：
    1. 每行都可解析为合法评测题且具备 reference / rubric / regex_constraints 之一 -> benchmark；
    2. 非 benchmark 的飞轮输出文件中，全部记录为 DISCARDED / PENDING（且含 DISCARDED 或质检淘汰特征字段，无任何达标记录） -> flywheel_discarded；
    3. 其余对话文件 -> flywheel_gold。
    """
    rows = _load_existing_jsonl_rows(fpath)
    if not rows:
        return "flywheel_gold"

    all_benchmark = True
    for idx, row in enumerate(rows, start=1):
        cand = dict(row)
        if not str(cand.get("id") or "").strip():
            cand["id"] = f"row_{idx}"
        try:
            sample = BenchmarkSample(**cand)
        except Exception:
            all_benchmark = False
            break
        if not sample.messages or not has_sample_eval_criteria(sample):
            all_benchmark = False
            break

    if all_benchmark:
        return "benchmark"

    metas = [row.get("metadata") for row in rows]
    if all(isinstance(m, dict) for m in metas):
        statuses = {str(m.get("status") or "").strip() for m in metas if isinstance(m, dict)}
        if statuses and statuses.issubset({"DISCARDED", "PENDING"}):
            has_discarded_status = "DISCARDED" in statuses
            has_flywheel_inspect_meta = any(
                isinstance(m, dict) and ("detected_defects" in m or "initial_score" in m)
                for m in metas
            )
            if has_discarded_status or has_flywheel_inspect_meta:
                return "flywheel_discarded"

    return "flywheel_gold"


@router.get("/")
def list_datasets(db: Session = Depends(get_db)):
    os.makedirs(settings.DATASETS_DIR, exist_ok=True)
    # 扫描 datasets 目录同步
    for fname in os.listdir(settings.DATASETS_DIR):
        if fname.endswith(".jsonl"):
            fpath = os.path.join(settings.DATASETS_DIR, fname)
            did = fname[:-6]  # strip .jsonl
            cnt = _count_jsonl_lines(fpath)
            existing = db.query(DatasetRecord).filter(DatasetRecord.id == did).first()
            if not existing:
                rec = DatasetRecord(
                    id=did,
                    name=fname,
                    file_path=fpath,
                    sample_count=cnt,
                    dataset_type=_infer_dataset_type_from_file(fpath),
                )
                db.add(rec)
            else:
                existing.sample_count = cnt
                existing.file_path = fpath
                if (
                    existing.dataset_type == "flywheel_gold"
                    and _infer_dataset_type_from_file(fpath) == "flywheel_discarded"
                ):
                    existing.dataset_type = "flywheel_discarded"
    db.commit()

    records = db.query(DatasetRecord).order_by(DatasetRecord.created_at.desc()).all()
    valid_records = []
    for r in records:
        if os.path.exists(r.file_path):
            valid_records.append(
                {
                    "id": r.id,
                    "name": r.name,
                    "file_path": r.file_path,
                    "sample_count": r.sample_count,
                    "dataset_type": r.dataset_type,
                    "created_at": r.created_at,
                }
            )
    return valid_records


@router.get("/{dataset_id}/samples")
def preview_dataset_samples(
    dataset_id: str,
    limit: int = Query(default=20, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    rec = db.query(DatasetRecord).filter(DatasetRecord.id == dataset_id).first()
    fpath = rec.file_path if rec else os.path.join(settings.DATASETS_DIR, f"{dataset_id}.jsonl")
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail="数据集文件不存在")

    samples = []
    with open(fpath, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            if idx >= limit:
                break
            line = line.strip()
            if line:
                try:
                    samples.append(json.loads(line))
                except Exception:
                    pass
    return {"dataset_id": dataset_id, "samples": samples}


@router.post("/import")
def import_dataset(payload: DatasetImportRequest, db: Session = Depends(get_db)):
    """导入外部评测集或微调数据集 (支持文件文本上传、在线粘贴 JSONL、或指定本地路径)"""
    clean_id = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fa5]", "_", payload.dataset_id.strip())
    if clean_id.endswith(".jsonl"):
        clean_id = clean_id[:-6]
    elif clean_id.endswith(".json"):
        clean_id = clean_id[:-5]
    if not clean_id:
        raise HTTPException(status_code=400, detail="数据集标识 (dataset_id) 不能为空")

    raw_items = _parse_raw_samples(payload.content, payload.samples, payload.source_file_path)
    if not raw_items:
        raise HTTPException(status_code=400, detail="未解析到任何有效样本，请检查导入内容是否为空")

    candidate_rows: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_items, start=1):
        if payload.dataset_type == "benchmark":
            if "id" not in item or not str(item["id"]).strip():
                item["id"] = f"{clean_id}_{idx:03d}"
            if payload.mark_as_holdout and "is_holdout" not in item:
                item["is_holdout"] = True
            try:
                sample_obj = BenchmarkSample(**item)
                if not sample_obj.messages:
                    raise HTTPException(
                        status_code=400,
                        detail=f"第 {idx} 条评测题缺少有效的 messages 数组 (ID={sample_obj.id})",
                    )
                if not has_sample_eval_criteria(sample_obj):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"第 {idx} 条评测题缺少评判标准（必须至少包含 reference、rubric 或 regex_constraints 之一，ID={sample_obj.id}）。"
                            "若为无答案的对话文件，请按训练集 (flywheel_gold) 导入。"
                        ),
                    )
                # 导出时保留紧凑无冗余 None 的标准字典，并剥离文件自带的 eval_tracker 战绩快照
                dump_dict = sample_obj.model_dump(exclude_none=True)
                dump_dict.pop("eval_tracker", None)
                candidate_rows.append(dump_dict)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"第 {idx} 条评测样本格式校验失败 (ID={item.get('id')}): {str(e)}",
                )
        else:
            msgs = item.get("messages")
            if not isinstance(msgs, list) or len(msgs) == 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"第 {idx} 条样本缺少有效的 messages 数组",
                )
            candidate_rows.append(dict(item))

    os.makedirs(settings.DATASETS_DIR, exist_ok=True)
    fname = f"{clean_id}.jsonl"
    fpath = os.path.join(settings.DATASETS_DIR, fname)

    is_append = payload.mode == "append" and os.path.exists(fpath)
    existing_rows: Optional[List[Dict[str, Any]]] = (
        _load_existing_jsonl_rows(fpath) if is_append else None
    )

    if payload.dedup_config is not None:
        eff_dedup_cfg = payload.dedup_config
    elif is_append:
        eff_dedup_cfg = DedupConfig(enabled=True)
    else:
        eff_dedup_cfg = DedupConfig(enabled=False)

    cache_path = DatasetDeduplicator.get_cache_path(fpath)
    deduper = DatasetDeduplicator()
    if payload.dataset_type == "benchmark":
        final_rows, dedup_stats = deduper.deduplicate_benchmark_rows(
            candidate_rows=candidate_rows,
            existing_rows=existing_rows,
            config=eff_dedup_cfg,
            cache_path=cache_path if eff_dedup_cfg.enabled else None,
        )
    else:
        final_rows, dedup_stats = deduper.deduplicate_training_rows(
            candidate_rows=candidate_rows,
            existing_rows=existing_rows,
            config=eff_dedup_cfg,
            cache_path=cache_path if eff_dedup_cfg.enabled else None,
        )

    if not eff_dedup_cfg.enabled and not is_append and os.path.exists(cache_path):
        try:
            os.remove(cache_path)
        except Exception:
            pass

    _write_jsonl_atomic(fpath, final_rows)

    # 覆盖重建同名数据集时，自然作废该路径下的历史评测流水并重算样本池导出留底
    if not is_append:
        BenchmarkSamplingEngine.reset_trackers_in_db(db=db, dataset_path=fpath)
        sync_lineage_on_dataset_overwritten(
            db=db,
            dataset_id=clean_id,
            dataset_type=payload.dataset_type,
            final_rows=final_rows,
            commit=False,
        )

    total_cnt = len(final_rows)
    imported_cnt = (
        int(dedup_stats.get("kept_candidates", len(candidate_rows)))
        if eff_dedup_cfg.enabled
        else len(candidate_rows)
    )
    existing = db.query(DatasetRecord).filter(DatasetRecord.id == clean_id).first()
    if existing:
        existing.name = fname
        existing.file_path = fpath
        existing.sample_count = total_cnt
        existing.dataset_type = payload.dataset_type
        existing.created_at = int(time.time())
        rec = existing
    else:
        rec = DatasetRecord(
            id=clean_id,
            name=fname,
            file_path=fpath,
            sample_count=total_cnt,
            dataset_type=payload.dataset_type,
            created_at=int(time.time()),
        )
        db.add(rec)
    db.commit()

    dropped_cnt = (
        int(dedup_stats.get("exact_hash_dropped", 0))
        + int(dedup_stats.get("near_dup_dropped", 0))
        + int(dedup_stats.get("quota_dropped", 0))
    )
    msg = (
        f"成功导入 {imported_cnt} 条样本至数据集 [{clean_id}]"
        + (f"（去重防稀释过滤 {dropped_cnt} 条）" if eff_dedup_cfg.enabled and dropped_cnt > 0 else "")
    )
    return {
        "message": msg,
        "imported_count": imported_cnt,
        "dedup_stats": dedup_stats,
        "dataset": {
            "id": rec.id,
            "name": rec.name,
            "file_path": rec.file_path,
            "sample_count": rec.sample_count,
            "dataset_type": rec.dataset_type,
            "created_at": rec.created_at,
        },
    }


@router.delete("/{dataset_id}")
def delete_dataset(dataset_id: str, db: Session = Depends(get_db)):
    """删除指定数据集资产记录及本地文件（含去重向量旁路缓存并同步清理样本池导出留底）"""
    rec = db.query(DatasetRecord).filter(DatasetRecord.id == dataset_id).first()
    fpath = rec.file_path if rec else os.path.join(settings.DATASETS_DIR, f"{dataset_id}.jsonl")

    sync_lineage_on_dataset_removed(db, dataset_id, commit=False)
    if rec:
        db.delete(rec)
    db.commit()

    if os.path.exists(fpath):
        try:
            os.remove(fpath)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"删除本地文件失败: {str(e)}")

    cache_path = DatasetDeduplicator.get_cache_path(fpath)
    if os.path.exists(cache_path):
        try:
            os.remove(cache_path)
        except Exception:
            pass

    return {"message": f"数据集 {dataset_id} 已删除"}


@router.get("/{dataset_id}/filter-options", response_model=BenchmarkDatasetFilterOptionsResponse)
def get_dataset_filter_options(dataset_id: str, db: Session = Depends(get_db)):
    """获取评测集基础过滤维度与已有历史战绩的基准模型线选项"""
    fpath = _resolve_dataset_file(dataset_id, db)
    samples = _load_benchmark_samples(fpath)
    opts = BenchmarkSamplingEngine.get_dataset_filter_options(db=db, dataset_path=fpath, samples=samples)
    return BenchmarkDatasetFilterOptionsResponse(dataset_id=dataset_id, **opts)


@router.post("/{dataset_id}/export/preview", response_model=BenchmarkExportPreviewResponse)
def preview_filtered_export(
    dataset_id: str,
    payload: BenchmarkExportFilterRequest,
    db: Session = Depends(get_db),
):
    """参考评测抽样逻辑，试算过滤后预计导出的评测题量与分布"""
    fpath = _resolve_dataset_file(dataset_id, db)
    samples = _load_benchmark_samples(fpath)
    filtered, stats = BenchmarkSamplingEngine.filter_samples_for_export(
        samples=samples,
        export_req=payload,
        db=db,
        dataset_path=fpath,
    )
    return BenchmarkExportPreviewResponse(
        dataset_id=dataset_id,
        total_samples=len(samples),
        exported_count=len(filtered),
        export_scope=payload.export_scope,
        sampling_stats=SamplingPreviewResponse(**stats),
        preview_sample_ids=[s.id for s in filtered[:30]],
    )


@router.post("/{dataset_id}/export")
def export_filtered_benchmark_dataset(
    dataset_id: str,
    payload: BenchmarkExportFilterRequest,
    db: Session = Depends(get_db),
):
    """
    参考评测抽样测评逻辑过滤并导出评测集：
    - 支持 download (返回 JSONL 内容供本地下载)
    - 支持 save_dataset (另存为平台新评测集并在 DatasetRecord 中注册)
    原始数据集与战绩流水保持 100% 只读无污染。
    """
    fpath = _resolve_dataset_file(dataset_id, db)
    samples = _load_benchmark_samples(fpath)
    filtered, stats = BenchmarkSamplingEngine.filter_samples_for_export(
        samples=samples,
        export_req=payload,
        db=db,
        dataset_path=fpath,
    )

    if not filtered:
        raise HTTPException(status_code=400, detail="当前抽样及过滤条件下未命中任何可导出的评测样本")

    keep_tracker = payload.export_target == "download" and bool(payload.include_tracker)

    def _serialize_sample(s: BenchmarkSample) -> Dict[str, Any]:
        d = s.model_dump(exclude_none=True)
        if not keep_tracker:
            d.pop("eval_tracker", None)
        return d

    exported_rows = [_serialize_sample(s) for s in filtered]
    deduper = DatasetDeduplicator()

    if payload.export_target == "download":
        eff_dl_cfg = (
            payload.dedup_config
            if payload.dedup_config is not None
            else DedupConfig(enabled=False)
        )
        dl_rows, dedup_stats = deduper.deduplicate_benchmark_rows(
            candidate_rows=exported_rows,
            existing_rows=None,
            config=eff_dl_cfg,
            cache_path=None,
        )
        jsonl_content = "\n".join(json.dumps(r, ensure_ascii=False) for r in dl_rows)
        scope_tag = payload.export_scope
        line_tag = re.sub(r"[^A-Za-z0-9_\-]", "_", payload.sampling_config.model_line.strip()) if payload.sampling_config.model_line else ""
        suffix = f"_{line_tag}_{scope_tag}" if line_tag else f"_{scope_tag}"
        filename = f"{dataset_id}{suffix}.jsonl"
        return {
            "message": f"成功过滤导出 {len(dl_rows)} / {len(samples)} 道评测题",
            "dataset_id": dataset_id,
            "filename": filename,
            "total_samples": len(samples),
            "exported_count": len(dl_rows),
            "export_scope": payload.export_scope,
            "export_target": "download",
            "sampling_stats": stats,
            "dedup_stats": dedup_stats,
            "jsonl_content": jsonl_content,
        }

    # save_dataset: 另存为平台新评测集
    raw_target_id = (payload.target_dataset_id or "").strip() or f"{dataset_id}_{payload.export_scope}_{int(time.time())}"
    clean_target_id = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fa5]", "_", raw_target_id)
    if clean_target_id.endswith(".jsonl"):
        clean_target_id = clean_target_id[:-6]
    elif clean_target_id.endswith(".json"):
        clean_target_id = clean_target_id[:-5]

    if not clean_target_id:
        raise HTTPException(status_code=400, detail="目标评测集标识 (target_dataset_id) 不能为空")
    if clean_target_id == dataset_id:
        raise HTTPException(status_code=400, detail="另存的目标评测集 ID 不能与源评测集相同，请指定新的名称以保护原题库")

    os.makedirs(settings.DATASETS_DIR, exist_ok=True)
    target_fname = f"{clean_target_id}.jsonl"
    target_fpath = os.path.join(settings.DATASETS_DIR, target_fname)

    is_append = payload.save_mode == "append" and os.path.exists(target_fpath)
    existing_rows: Optional[List[Dict[str, Any]]] = (
        _load_existing_jsonl_rows(target_fpath) if is_append else None
    )
    if payload.dedup_config is not None:
        eff_save_cfg = payload.dedup_config
    elif is_append:
        eff_save_cfg = DedupConfig(enabled=True)
    else:
        eff_save_cfg = DedupConfig(enabled=False)

    cache_path = DatasetDeduplicator.get_cache_path(target_fpath)
    final_rows, dedup_stats = deduper.deduplicate_benchmark_rows(
        candidate_rows=exported_rows,
        existing_rows=existing_rows,
        config=eff_save_cfg,
        cache_path=cache_path if eff_save_cfg.enabled else None,
    )
    if not eff_save_cfg.enabled and payload.save_mode == "overwrite" and os.path.exists(cache_path):
        try:
            os.remove(cache_path)
        except Exception:
            pass

    _write_jsonl_atomic(target_fpath, final_rows)

    if payload.save_mode == "overwrite":
        BenchmarkSamplingEngine.reset_trackers_in_db(db=db, dataset_path=target_fpath)
    sync_lineage_on_dataset_overwritten(
        db=db,
        dataset_id=clean_target_id,
        dataset_type="benchmark",
        final_rows=final_rows,
        commit=False,
    )

    display_name = (payload.target_dataset_name or "").strip() or target_fname
    now_ts = int(time.time())
    existing_rec = db.query(DatasetRecord).filter(DatasetRecord.id == clean_target_id).first()
    if existing_rec:
        existing_rec.name = display_name
        existing_rec.file_path = target_fpath
        existing_rec.sample_count = len(final_rows)
        existing_rec.dataset_type = "benchmark"
        existing_rec.created_at = now_ts
        rec = existing_rec
    else:
        rec = DatasetRecord(
            id=clean_target_id,
            name=display_name,
            file_path=target_fpath,
            sample_count=len(final_rows),
            dataset_type="benchmark",
            created_at=now_ts,
        )
        db.add(rec)
    db.commit()

    exported_cnt = (
        int(dedup_stats.get("kept_candidates", len(exported_rows)))
        if eff_save_cfg.enabled
        else len(exported_rows)
    )
    return {
        "message": f"已另存为新评测集 [{clean_target_id}]（共 {len(final_rows)} 道考题，本次导出 {exported_cnt} 道）",
        "dataset_id": dataset_id,
        "total_samples": len(samples),
        "exported_count": exported_cnt,
        "export_scope": payload.export_scope,
        "export_target": "save_dataset",
        "sampling_stats": stats,
        "dedup_stats": dedup_stats,
        "saved_dataset": {
            "id": rec.id,
            "name": rec.name,
            "file_path": rec.file_path,
            "sample_count": rec.sample_count,
            "dataset_type": rec.dataset_type,
            "created_at": rec.created_at,
        },
    }
