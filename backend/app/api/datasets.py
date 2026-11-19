# -*- coding: utf-8 -*-
"""
数据集资产管理 API (支持列表同步、样本抽检、外部评测集导入与删除)
"""
import os
import re
import json
import time
from typing import List, Dict, Any, Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from backend.app.config import settings
from backend.app.models import get_db, DatasetRecord
from backend.app.schemas.benchmark import (
    BenchmarkSample,
    BenchmarkExportFilterRequest,
    BenchmarkExportPreviewResponse,
    BenchmarkDatasetFilterOptionsResponse,
    SamplingPreviewResponse,
)
from backend.app.engine.benchmark_sampler import BenchmarkSamplingEngine

router = APIRouter(prefix="/datasets", tags=["数据集管理"])


class DatasetImportRequest(BaseModel):
    dataset_id: str = Field(..., description="数据集唯一标识或文件名(不含后缀)")
    dataset_type: Literal["benchmark", "flywheel_gold"] = "benchmark"
    mode: Literal["overwrite", "append"] = "overwrite"
    mark_as_holdout: bool = Field(default=False, description="导入 benchmark 时是否标记为纯评测留出题 (允许降频)")
    content: Optional[str] = Field(default=None, description="JSONL 或 JSON 数组原始文本内容")
    samples: Optional[List[Dict[str, Any]]] = Field(default=None, description="已解析的样本字典数组")
    source_file_path: Optional[str] = Field(default=None, description="服务端本地外部文件路径")


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
                    dataset_type="benchmark" if "bench" in fname or "eval" in fname else "flywheel_gold",
                )
                db.add(rec)
            else:
                existing.sample_count = cnt
                existing.file_path = fpath
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

    validated_lines: List[str] = []
    for idx, item in enumerate(raw_items, start=1):
        if payload.dataset_type == "benchmark":
            if "id" not in item or not str(item["id"]).strip():
                item["id"] = f"{clean_id}_{idx:03d}"
            if payload.mark_as_holdout and "is_holdout" not in item:
                item["is_holdout"] = True
            try:
                sample_obj = BenchmarkSample(**item)
                # 导出时保留紧凑无冗余 None 的标准字典，若核心字段存在则完整输出
                dump_dict = sample_obj.model_dump(exclude_none=True)
                validated_lines.append(json.dumps(dump_dict, ensure_ascii=False))
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
            validated_lines.append(json.dumps(item, ensure_ascii=False))

    os.makedirs(settings.DATASETS_DIR, exist_ok=True)
    fname = f"{clean_id}.jsonl"
    fpath = os.path.join(settings.DATASETS_DIR, fname)

    write_mode = "a" if (payload.mode == "append" and os.path.exists(fpath)) else "w"
    with open(fpath, write_mode, encoding="utf-8") as f:
        for line in validated_lines:
            f.write(line + "\n")

    # 覆盖重建同名数据集时，自然作废该路径下的历史评测流水，防止旧战绩残留
    if write_mode == "w":
        BenchmarkSamplingEngine.reset_trackers_in_db(db=db, dataset_path=fpath)

    total_cnt = _count_jsonl_lines(fpath)
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

    return {
        "message": f"成功导入 {len(validated_lines)} 条样本至数据集 [{clean_id}]",
        "imported_count": len(validated_lines),
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
    """删除指定数据集资产记录及本地文件"""
    rec = db.query(DatasetRecord).filter(DatasetRecord.id == dataset_id).first()
    fpath = rec.file_path if rec else os.path.join(settings.DATASETS_DIR, f"{dataset_id}.jsonl")

    if rec:
        db.delete(rec)
        db.commit()

    if os.path.exists(fpath):
        try:
            os.remove(fpath)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"删除本地文件失败: {str(e)}")

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

    def _serialize_sample(s: BenchmarkSample) -> Dict[str, Any]:
        d = s.model_dump(exclude_none=True)
        if not payload.include_tracker:
            d.pop("eval_tracker", None)
        return d

    exported_rows = [_serialize_sample(s) for s in filtered]

    if payload.export_target == "download":
        jsonl_content = "\n".join(json.dumps(r, ensure_ascii=False) for r in exported_rows)
        scope_tag = payload.export_scope
        line_tag = re.sub(r"[^A-Za-z0-9_\-]", "_", payload.sampling_config.model_line.strip()) if payload.sampling_config.model_line else ""
        suffix = f"_{line_tag}_{scope_tag}" if line_tag else f"_{scope_tag}"
        filename = f"{dataset_id}{suffix}.jsonl"
        return {
            "message": f"成功过滤导出 {len(exported_rows)} / {len(samples)} 道评测题",
            "dataset_id": dataset_id,
            "filename": filename,
            "total_samples": len(samples),
            "exported_count": len(exported_rows),
            "export_scope": payload.export_scope,
            "export_target": "download",
            "sampling_stats": stats,
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

    final_rows = exported_rows
    if payload.save_mode == "append" and os.path.exists(target_fpath):
        existing_map: Dict[str, Dict[str, Any]] = {}
        with open(target_fpath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict) and "id" in obj:
                            existing_map[str(obj["id"])] = obj
                    except Exception:
                        pass
        for r in exported_rows:
            existing_map[str(r["id"])] = r
        final_rows = list(existing_map.values())

    with open(target_fpath, "w", encoding="utf-8") as f:
        for r in final_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if payload.save_mode == "overwrite":
        BenchmarkSamplingEngine.reset_trackers_in_db(db=db, dataset_path=target_fpath)

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

    return {
        "message": f"已另存为新评测集 [{clean_target_id}]（共 {len(final_rows)} 道考题，本次导出 {len(exported_rows)} 道）",
        "dataset_id": dataset_id,
        "total_samples": len(samples),
        "exported_count": len(exported_rows),
        "export_scope": payload.export_scope,
        "export_target": "save_dataset",
        "sampling_stats": stats,
        "saved_dataset": {
            "id": rec.id,
            "name": rec.name,
            "file_path": rec.file_path,
            "sample_count": rec.sample_count,
            "dataset_type": rec.dataset_type,
            "created_at": rec.created_at,
        },
    }
