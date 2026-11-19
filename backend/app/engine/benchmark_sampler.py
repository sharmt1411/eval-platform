# -*- coding: utf-8 -*-
"""
常规评测集动态阶梯降频抽取与复测战绩治理引擎 (Benchmark Sampling Engine v2.1)
核心功能:
1. 数据集 (.jsonl) 保持 100% 纯净只读，战绩流水按 (dataset_path, model_line) 独立存储于 SQLite benchmark_run_ledgers;
2. 留出题 (is_holdout=True) 资格校验: 非留出题强制 I=1，杜绝训练集背诵污染降频;
3. 按 passed_versions 不重复版本集合计数，支持同 (dataset, model_line, version_tag) 题单快照复用，防止 A/B 版本交替刷分;
4. 单题内容指纹 (content_hash) 变更检测: 仅当题目自身内容修改时重置该题战绩，追加新题不误伤存量熟题;
5. 三池分流 + 紧迫度配额平滑 + 可选显式 group_key 连坐唤醒;
6. 支持按 task_id 一键撤销误操作流水 (is_revoked=True) 与按数据集/模型线重置战绩。
"""
import os
import json
import uuid
import time
import hashlib
import threading
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple, Set
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models.base import BenchmarkRunLedger
from backend.app.schemas.benchmark import (
    BenchmarkSample,
    SampleEvalTracker,
    SamplingFilterConfig,
    SamplingPreviewResponse,
    SampleEvalResult,
    BenchmarkExportFilterRequest,
)


class BenchmarkSamplingEngine:
    """常规评测集动态抽样与战绩治理核心引擎"""

    _ledger_seq_lock = threading.Lock()
    _last_ledger_ns = 0

    @classmethod
    def _next_ledger_id(cls) -> str:
        """生成严格单调递增的流水主键，保证同秒内写入的流水按真实先后顺序重放"""
        with cls._ledger_seq_lock:
            now_ns = time.time_ns()
            if now_ns <= cls._last_ledger_ns:
                now_ns = cls._last_ledger_ns + 1
            cls._last_ledger_ns = now_ns
            return f"ledger_{now_ns:020d}_{uuid.uuid4().hex[:6]}"

    @staticmethod
    def normalize_dataset_path(dataset_path: str) -> str:
        """统一规范化数据集路径键，避免相对/绝对路径或 Windows 反斜杠差异导致流水查漏"""
        if not dataset_path:
            return ""
        p = dataset_path.strip()
        abs_p = p if os.path.isabs(p) else os.path.abspath(os.path.join(settings.BASE_DIR, p))
        base_abs = os.path.abspath(settings.BASE_DIR)
        try:
            rel_p = os.path.relpath(abs_p, base_abs)
            if not rel_p.startswith(".."):
                return rel_p.replace("\\", "/")
        except ValueError:
            pass
        return abs_p.replace("\\", "/")

    @staticmethod
    def compute_sample_fingerprint(sample: BenchmarkSample) -> str:
        """计算单道评测样本的内容指纹 (messages + reference + rubric + regex_constraints)"""
        rubric_val = sample.rubric.model_dump() if hasattr(sample.rubric, "model_dump") else sample.rubric
        payload = {
            "messages": [{"role": m.role, "content": m.content} for m in sample.messages],
            "reference": sample.reference,
            "rubric": rubric_val,
            "regex_constraints": sample.regex_constraints,
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def get_or_create_tracker(sample: BenchmarkSample) -> SampleEvalTracker:
        """获取样本现存战绩，若缺失则返回初始状态 (c=0, m=0, I=1)"""
        if sample.eval_tracker is not None:
            return sample.eval_tracker.model_copy(deep=True)
        return SampleEvalTracker()

    @classmethod
    def compute_target_interval(
        cls,
        tracker: SampleEvalTracker,
        x: int = 3,
        s: int = 1,
        n: int = 5,
        is_holdout: bool = True,
    ) -> int:
        """
        计算单道样本的目标复测间隔 I_i:
        - 若非留出题 (is_holdout=False) 或被同组连坐唤醒 (force_due=True) 或 c < x: I_i = 1
        - 若 is_holdout=True 且 c >= x: I_i = min(N, 1 + (c - x + 1) * s)
        """
        if not is_holdout or tracker.force_due:
            return 1
        c = tracker.consecutive_pass
        if c < x:
            return 1
        interval = 1 + (c - x + 1) * s
        return min(n, interval)

    @classmethod
    def find_same_version_snapshot_detail(
        cls,
        db: Session,
        dataset_path: str,
        model_line: str,
        version_tag: str,
    ) -> Optional[Tuple[List[str], Set[str]]]:
        """查询同一 (dataset_path, model_line, version_tag) 是否已存在未撤销的抽题快照，返回 (sampled_ids, known_ids)"""
        norm_path = cls.normalize_dataset_path(dataset_path)
        existing = (
            db.query(BenchmarkRunLedger)
            .filter(
                BenchmarkRunLedger.dataset_path == norm_path,
                BenchmarkRunLedger.model_line == model_line,
                BenchmarkRunLedger.version_tag == version_tag,
                BenchmarkRunLedger.is_revoked == False,  # noqa: E712
            )
            .order_by(BenchmarkRunLedger.created_at.asc(), BenchmarkRunLedger.id.asc())
            .first()
        )
        if not existing:
            return None
        try:
            ids = json.loads(existing.sampled_ids_json or "[]")
            if not isinstance(ids, list):
                return None
            known_ids: Set[str] = set(ids)
            records = json.loads(existing.records_json or "[]")
            if isinstance(records, list):
                for rec in records:
                    if isinstance(rec, dict) and rec.get("sample_id"):
                        known_ids.add(rec["sample_id"])
            return ids, known_ids
        except Exception:
            return None

    @classmethod
    def find_same_version_snapshot(
        cls,
        db: Session,
        dataset_path: str,
        model_line: str,
        version_tag: str,
    ) -> Optional[List[str]]:
        """查询同一 (dataset_path, model_line, version_tag) 是否已存在未撤销的抽题快照"""
        detail = cls.find_same_version_snapshot_detail(db, dataset_path, model_line, version_tag)
        return detail[0] if detail is not None else None

    @classmethod
    def aggregate_trackers_from_ledgers(
        cls,
        db: Session,
        dataset_path: str,
        model_line: str,
        samples: List[BenchmarkSample],
        pass_threshold_x: int = 3,
        interval_step_s: int = 1,
        max_interval_n: int = 5,
        enable_group_regression_wakeup: bool = False,
    ) -> Dict[str, SampleEvalTracker]:
        """
        从 benchmark_run_ledgers 只追加流水表按时间顺序重放并聚合指定模型线的单题战绩。
        支持:
        - passed_versions 不重复版本集合计数
        - 同版本重跑不推进 miss_streak
        - 单题内容指纹变更自动重置该题战绩
        - 可选显式 group_key 熟题退化连坐唤醒
        """
        norm_path = cls.normalize_dataset_path(dataset_path)
        sample_map: Dict[str, BenchmarkSample] = {s.id: s for s in samples}
        current_fps: Dict[str, str] = {s.id: cls.compute_sample_fingerprint(s) for s in samples}
        trackers: Dict[str, SampleEvalTracker] = {s.id: SampleEvalTracker() for s in samples}

        group_to_ids: Dict[str, List[str]] = {}
        for s in samples:
            if s.group_key:
                group_to_ids.setdefault(s.group_key, []).append(s.id)

        ledgers = (
            db.query(BenchmarkRunLedger)
            .filter(
                BenchmarkRunLedger.dataset_path == norm_path,
                BenchmarkRunLedger.model_line == model_line,
                BenchmarkRunLedger.is_revoked == False,  # noqa: E712
            )
            .order_by(BenchmarkRunLedger.created_at.asc(), BenchmarkRunLedger.id.asc())
            .all()
        )

        seen_versions: Set[str] = set()

        for ledger in ledgers:
            v_tag = ledger.version_tag
            is_rerun = v_tag in seen_versions
            seen_versions.add(v_tag)

            try:
                records = json.loads(ledger.records_json or "[]")
            except Exception:
                records = []

            for rec in records:
                sid = rec.get("sample_id")
                if not sid or sid not in trackers:
                    continue
                tr = trackers[sid]

                rec_fp = rec.get("content_hash")
                curr_fp = current_fps.get(sid)
                if rec_fp and curr_fp and rec_fp != curr_fp:
                    # 该历史实测/休眠记录对应的题目内容已被修改，旧战绩作废
                    tr.passed_versions = []
                    tr.consecutive_pass = 0
                    tr.miss_streak = 0
                    tr.force_due = False
                    tr.last_status = "UNSEEN"
                    tr.last_score = None
                    tr.last_content_hash = curr_fp
                    continue

                if rec.get("skipped"):
                    if not is_rerun:
                        tr.miss_streak += 1
                    continue

                st = rec.get("status")
                if st == "API_ERROR":
                    # API 异常冻结保护，不计入通过也不清零
                    continue

                qualified = bool(rec.get("qualified", st == "PASS"))
                tr.total_eval_count += 1
                tr.last_eval_at = rec.get("eval_at")
                tr.last_score = rec.get("score")
                tr.last_content_hash = rec_fp or curr_fp

                if qualified:
                    if v_tag not in tr.passed_versions:
                        tr.passed_versions.append(v_tag)
                    tr.consecutive_pass = len(tr.passed_versions)
                    tr.miss_streak = 0
                    tr.force_due = False
                    tr.last_status = "PASS"
                else:
                    was_cooldown = tr.consecutive_pass >= pass_threshold_x
                    tr.passed_versions = []
                    tr.consecutive_pass = 0
                    tr.miss_streak = 0
                    tr.force_due = False
                    tr.last_status = "FAIL"

                    if was_cooldown and enable_group_regression_wakeup:
                        g_key = sample_map[sid].group_key
                        if g_key and g_key in group_to_ids:
                            for other_id in group_to_ids[g_key]:
                                if other_id != sid and trackers[other_id].consecutive_pass >= pass_threshold_x:
                                    trackers[other_id].force_due = True

        # 最终计算每题当前目标间隔
        for sid, tr in trackers.items():
            tr.target_interval = cls.compute_target_interval(
                tr,
                x=pass_threshold_x,
                s=interval_step_s,
                n=max_interval_n,
                is_holdout=sample_map[sid].is_holdout,
            )

        return trackers

    @classmethod
    def partition_samples(
        cls,
        samples: List[BenchmarkSample],
        config: SamplingFilterConfig,
        db: Optional[Session] = None,
        dataset_path: Optional[str] = None,
        trackers_map: Optional[Dict[str, SampleEvalTracker]] = None,
    ) -> Tuple[List[BenchmarkSample], List[BenchmarkSample], Dict[str, Any]]:
        """
        根据阶梯降频与紧迫度配额算法，将输入样本划分为:
        1. selected_samples: 本轮入选实测集
        2. skipped_samples: 本轮休眠免测熟题集
        3. stats: 分布统计、熟题ID集合、是否命中同版本快照等
        """
        x = config.pass_threshold_x
        s = config.interval_step_s
        n = config.max_interval_n
        model_line = (config.model_line or "").strip() or None
        version_tag = (config.version_tag or "").strip() or None

        holdout_count = sum(1 for item in samples if item.is_holdout)
        non_holdout_count = len(samples) - holdout_count

        # 解析当前各题战绩映射
        resolved_trackers: Dict[str, SampleEvalTracker] = {}
        if trackers_map is not None:
            resolved_trackers = {k: v.model_copy(deep=True) for k, v in trackers_map.items()}
        elif db is not None and dataset_path and model_line:
            resolved_trackers = cls.aggregate_trackers_from_ledgers(
                db=db,
                dataset_path=dataset_path,
                model_line=model_line,
                samples=samples,
                pass_threshold_x=x,
                interval_step_s=s,
                max_interval_n=n,
                enable_group_regression_wakeup=config.enable_group_regression_wakeup,
            )
        else:
            for item in samples:
                resolved_trackers[item.id] = cls.get_or_create_tracker(item)

        if not config.enabled:
            for sample in samples:
                tr = resolved_trackers.get(sample.id, SampleEvalTracker())
                tr.target_interval = cls.compute_target_interval(
                    tr, x=x, s=s, n=n, is_holdout=sample.is_holdout
                )
                sample.eval_tracker = tr

            stats = {
                "total_samples": len(samples),
                "sampled_samples": len(samples),
                "skipped_samples": 0,
                "saved_compute_ratio": 0.0,
                "active_pool_count": len(samples),
                "hard_cap_pool_count": 0,
                "regular_cooldown_count": 0,
                "cooldown_pool_total": 0,
                "holdout_count": holdout_count,
                "non_holdout_count": non_holdout_count,
                "is_rerun_same_version": False,
                "model_line": model_line,
                "version_tag": version_tag,
                "cooldown_sample_ids": [],
                "notice": "当前为全量评测模式 (100% 样本参评)",
            }
            return list(samples), [], stats

        # 计算每题目标间隔并识别当前属于熟题池的样本
        pool_active: List[BenchmarkSample] = []
        pool_cooldown: List[BenchmarkSample] = []
        cooldown_pool_ids: Set[str] = set()

        for sample in samples:
            tr = resolved_trackers.get(sample.id, SampleEvalTracker())
            target_interval = cls.compute_target_interval(
                tr, x=x, s=s, n=n, is_holdout=sample.is_holdout
            )
            tr.target_interval = target_interval
            resolved_trackers[sample.id] = tr
            sample.eval_tracker = tr

            if sample.is_holdout and tr.consecutive_pass >= x:
                cooldown_pool_ids.add(sample.id)

            if target_interval == 1:
                pool_active.append(sample)
            else:
                pool_cooldown.append(sample)

        # 检查是否命中同 (dataset_path, model_line, version_tag) 快照复用
        snapshot_detail: Optional[Tuple[List[str], Set[str]]] = None
        if db is not None and dataset_path and model_line and version_tag:
            snapshot_detail = cls.find_same_version_snapshot_detail(db, dataset_path, model_line, version_tag)

        if snapshot_detail is not None:
            snapshot_ids, known_ids = snapshot_detail
            snap_set = set(snapshot_ids)
            selected_samples = []
            skipped_samples = []
            for item in samples:
                if item.id in snap_set:
                    selected_samples.append(item)
                elif item.id not in known_ids and item.eval_tracker and item.eval_tracker.target_interval == 1:
                    # 原快照之后新追加的 I=1 必测题，自动补入实测以免被误当成休眠熟题跳过
                    selected_samples.append(item)
                else:
                    skipped_samples.append(item)

            total_count = len(samples)
            sampled_count = len(selected_samples)
            skipped_count = len(skipped_samples)
            saved_ratio = round((skipped_count / max(total_count, 1)) * 100.0, 1) if total_count > 0 else 0.0
            tested_cooldown_ids = [item.id for item in selected_samples if item.id in cooldown_pool_ids]

            stats = {
                "total_samples": total_count,
                "sampled_samples": sampled_count,
                "skipped_samples": skipped_count,
                "saved_compute_ratio": saved_ratio,
                "active_pool_count": sum(1 for item in selected_samples if item.id not in cooldown_pool_ids),
                "hard_cap_pool_count": 0,
                "regular_cooldown_count": len(tested_cooldown_ids),
                "cooldown_pool_total": len(cooldown_pool_ids),
                "holdout_count": holdout_count,
                "non_holdout_count": non_holdout_count,
                "is_rerun_same_version": True,
                "model_line": model_line,
                "version_tag": version_tag,
                "cooldown_sample_ids": tested_cooldown_ids,
                "notice": (
                    f"命中同版本 [{model_line} / {version_tag}] 题单快照复用: "
                    f"严格沿用首次抽出的 {sampled_count} 题，休眠 {skipped_count} 题（本轮不推进轮空计数 m）。"
                ),
            }
            return selected_samples, skipped_samples, stats

        # Step 2: 计算降频总池在本轮的理论应抽配额
        quota_cooldown = 0
        if pool_cooldown:
            sum_fraction = sum(1.0 / s_item.eval_tracker.target_interval for s_item in pool_cooldown)
            quota_cooldown = max(1, round(sum_fraction))

        # Step 3: 封顶保底优先 (m + 1 >= N)
        pool_hard_cap: List[BenchmarkSample] = []
        pool_candidate_cooldown: List[BenchmarkSample] = []

        for s_item in pool_cooldown:
            tr = s_item.eval_tracker
            if (tr.miss_streak + 1) >= n:
                pool_hard_cap.append(s_item)
            else:
                pool_candidate_cooldown.append(s_item)

        # Step 4: 紧迫度排队补齐
        selected_from_regular: List[BenchmarkSample] = []
        needed_from_regular = max(0, quota_cooldown - len(pool_hard_cap))

        if needed_from_regular > 0 and pool_candidate_cooldown:
            def _sort_key(item: BenchmarkSample):
                tr = item.eval_tracker
                urgency = (tr.miss_streak + 1) / max(tr.target_interval, 1)
                tie_breaker = int(hashlib.md5(item.id.encode("utf-8")).hexdigest(), 16) % 1000000 / 1000000.0
                return (urgency, tr.miss_streak, tie_breaker)

            pool_candidate_cooldown.sort(key=_sort_key, reverse=True)
            selected_from_regular = pool_candidate_cooldown[:needed_from_regular]

        selected_ids: Set[str] = {
            s_item.id for s_item in (pool_active + pool_hard_cap + selected_from_regular)
        }

        selected_samples: List[BenchmarkSample] = []
        skipped_samples: List[BenchmarkSample] = []

        for sample in samples:
            if sample.id in selected_ids:
                selected_samples.append(sample)
            else:
                skipped_samples.append(sample)

        total_count = len(samples)
        sampled_count = len(selected_samples)
        skipped_count = len(skipped_samples)
        saved_ratio = round((skipped_count / max(total_count, 1)) * 100.0, 1) if total_count > 0 else 0.0
        tested_cooldown_ids = [item.id for item in selected_samples if item.id in cooldown_pool_ids]

        stats = {
            "total_samples": total_count,
            "sampled_samples": sampled_count,
            "skipped_samples": skipped_count,
            "saved_compute_ratio": saved_ratio,
            "active_pool_count": len(pool_active),
            "hard_cap_pool_count": len(pool_hard_cap),
            "regular_cooldown_count": len(selected_from_regular),
            "cooldown_pool_total": len(pool_cooldown),
            "holdout_count": holdout_count,
            "non_holdout_count": non_holdout_count,
            "is_rerun_same_version": False,
            "model_line": model_line,
            "version_tag": version_tag,
            "cooldown_sample_ids": tested_cooldown_ids,
            "notice": (
                f"智能阶梯降频生效: 本次实测 {sampled_count} 题（留出题 {holdout_count} / 同源题 {non_holdout_count}），"
                f"休眠熟题 {skipped_count} 题，预计节省 {saved_ratio}% 推理算力。"
            ),
        }

        return selected_samples, skipped_samples, stats

    @classmethod
    def preview_sampling(
        cls,
        samples: List[BenchmarkSample],
        config: SamplingFilterConfig,
        db: Optional[Session] = None,
        dataset_path: Optional[str] = None,
    ) -> SamplingPreviewResponse:
        """试算抽样分布与预计节省算力比例"""
        _, _, stats = cls.partition_samples(
            samples=samples,
            config=config,
            db=db,
            dataset_path=dataset_path,
        )
        stats.pop("cooldown_sample_ids", None)
        return SamplingPreviewResponse(**stats)

    @classmethod
    def get_dataset_filter_options(
        cls,
        db: Session,
        dataset_path: str,
        samples: List[BenchmarkSample],
    ) -> Dict[str, Any]:
        """汇总评测集的固有过滤维度与在流水账本中存在历史战绩的模型线列表"""
        norm_path = cls.normalize_dataset_path(dataset_path)
        categories = sorted({s.category for s in samples if s.category})
        difficulties = sorted({s.difficulty for s in samples if s.difficulty})
        holdout_count = sum(1 for s in samples if s.is_holdout)
        non_holdout_count = len(samples) - holdout_count

        ledgers = (
            db.query(BenchmarkRunLedger)
            .filter(
                BenchmarkRunLedger.dataset_path == norm_path,
                BenchmarkRunLedger.is_revoked == False,  # noqa: E712
            )
            .order_by(BenchmarkRunLedger.created_at.desc(), BenchmarkRunLedger.id.desc())
            .all()
        )
        line_map: Dict[str, Dict[str, Any]] = {}
        for lg in ledgers:
            entry = line_map.setdefault(
                lg.model_line,
                {"model_line": lg.model_line, "version_tags": [], "ledger_count": 0},
            )
            entry["ledger_count"] += 1
            if lg.version_tag and lg.version_tag not in entry["version_tags"]:
                entry["version_tags"].append(lg.version_tag)

        return {
            "total_samples": len(samples),
            "holdout_count": holdout_count,
            "non_holdout_count": non_holdout_count,
            "categories": categories,
            "difficulties": difficulties,
            "model_lines": list(line_map.values()),
        }

    @classmethod
    def filter_samples_for_export(
        cls,
        samples: List[BenchmarkSample],
        export_req: BenchmarkExportFilterRequest,
        db: Optional[Session] = None,
        dataset_path: Optional[str] = None,
        trackers_map: Optional[Dict[str, SampleEvalTracker]] = None,
    ) -> Tuple[List[BenchmarkSample], Dict[str, Any]]:
        """
        参考评测抽样引擎的划分逻辑，按抽样池范围 (export_scope) 与基础属性筛选待导出评测样本。
        绝不修改原始数据集文件，也不写入任何流水账本。
        """
        selected_samples, skipped_samples, stats = cls.partition_samples(
            samples=samples,
            config=export_req.sampling_config,
            db=db,
            dataset_path=dataset_path,
            trackers_map=trackers_map,
        )

        scope = export_req.export_scope
        if scope == "sampled":
            pool = selected_samples
        elif scope == "skipped":
            pool = skipped_samples
        elif scope == "active_only":
            pool = [
                s for s in samples
                if s.eval_tracker is None or s.eval_tracker.target_interval == 1
            ]
        elif scope == "cooldown_all":
            pool = [
                s for s in samples
                if s.eval_tracker is not None and s.eval_tracker.target_interval > 1
            ]
        elif scope == "failed_only":
            pool = [
                s for s in samples
                if s.eval_tracker is not None and s.eval_tracker.last_status == "FAIL"
            ]
        else:
            pool = list(samples)

        cat_set = {c.strip() for c in export_req.categories if c and c.strip()}
        diff_set = {d.strip().lower() for d in export_req.difficulties if d and d.strip()}
        kw = (export_req.keyword or "").strip().lower()

        filtered: List[BenchmarkSample] = []
        for s in pool:
            if cat_set and (s.category or "") not in cat_set:
                continue
            if diff_set and (s.difficulty or "").strip().lower() not in diff_set:
                continue
            if export_req.holdout_filter == "holdout_only" and not s.is_holdout:
                continue
            if export_req.holdout_filter == "non_holdout_only" and s.is_holdout:
                continue
            if kw:
                haystack_parts = [
                    s.id or "",
                    s.category or "",
                    s.sub_category or "",
                    s.source or "",
                    s.reference or "",
                    " ".join(m.content or "" for m in (s.messages or [])),
                ]
                if kw not in "\n".join(haystack_parts).lower():
                    continue
            filtered.append(s)

        stats_clean = dict(stats)
        stats_clean.pop("cooldown_sample_ids", None)
        return filtered, stats_clean

    @classmethod
    def is_sample_qualified(
        cls,
        res: SampleEvalResult,
        min_pass_score: Optional[float] = None,
    ) -> bool:
        """判断单次评测结果是否达到晋级/保持熟题的分数线门槛"""
        if res.status != "PASS":
            return False
        if min_pass_score is not None:
            if res.judge_score is not None and res.judge_score < min_pass_score:
                return False
        return True

    @classmethod
    def matches_target_model(cls, res_model_id: str, target_keys: Set[str]) -> bool:
        """判断单个评测结果的 model_id 是否与目标基准模型标识集合匹配 (含后缀/别名容错)"""
        if res_model_id in target_keys:
            return True

        def _base_id(val: str) -> str:
            v = val.split(" (")[0].strip()
            v = v.split("__")[0].strip()
            return v

        res_base = _base_id(res_model_id)
        for target_key in target_keys:
            if target_key in (res_model_id, res_base):
                return True
            target_base = _base_id(target_key)
            if target_base and (target_base == res_base or target_base == res_model_id or res_base == target_key):
                return True
        return False

    @classmethod
    def resolve_benchmark_target_keys(cls, config: SamplingFilterConfig) -> Set[str]:
        """解析本次负责更新战绩的唯一基准模型键集合"""
        keys: Set[str] = set()
        if config.benchmark_model_id and config.benchmark_model_id.strip():
            keys.add(config.benchmark_model_id.strip())
        for k in (config.tracker_model_keys or []):
            if k and k.strip():
                keys.add(k.strip())
        # 兼容: 若只传了 model_line 且未显式填 benchmark_model_id，尝试按 model_line 匹配参评模型
        if not keys and config.model_line and config.model_line.strip():
            keys.add(config.model_line.strip())
        return keys

    @classmethod
    def record_run_ledger(
        cls,
        db: Session,
        task_id: str,
        dataset_path: str,
        config: SamplingFilterConfig,
        all_samples: List[BenchmarkSample],
        selected_samples: List[BenchmarkSample],
        skipped_samples: List[BenchmarkSample],
        results_by_sample_id: Dict[str, List[SampleEvalResult]],
        cooldown_sample_ids: Optional[Set[str]] = None,
    ) -> Optional[str]:
        """
        将本次基准模型线的实测与休眠结果追加写入 benchmark_run_ledgers。
        - 若未指定 model_line 或 version_tag，或未匹配到任何有效（非 API_ERROR）基准模型结果，则不入账（只读摸底零污染）。
        - 绝不修改原始 .jsonl 数据集文件。
        """
        model_line = (config.model_line or "").strip()
        version_tag = (config.version_tag or "").strip()
        target_keys = cls.resolve_benchmark_target_keys(config)

        if not model_line or not version_tag or not target_keys:
            return None

        norm_path = cls.normalize_dataset_path(dataset_path)
        now_iso = datetime.now().isoformat()
        min_score = config.min_pass_score
        cooldown_set = cooldown_sample_ids or set()
        fp_map = {s.id: cls.compute_sample_fingerprint(s) for s in all_samples}

        records: List[Dict[str, Any]] = []
        valid_eval_count = 0

        for s in selected_samples:
            sample_results = results_by_sample_id.get(s.id, [])
            matched = [
                r
                for r in sample_results
                if cls.matches_target_model(r.model_id, target_keys)
                or (getattr(r, "source_model_id", None) and cls.matches_target_model(r.source_model_id, target_keys))
            ]
            if not matched:
                continue

            # 若只选定一个基准模型，取第一个匹配；若历史兼容多个，全部合格才算合格
            has_api_error = any(r.status == "API_ERROR" for r in matched)
            if has_api_error:
                records.append({
                    "sample_id": s.id,
                    "content_hash": fp_map.get(s.id),
                    "skipped": False,
                    "was_cooldown": s.id in cooldown_set,
                    "status": "API_ERROR",
                    "qualified": False,
                    "score": None,
                    "eval_at": now_iso,
                })
                continue

            valid_eval_count += 1
            all_qualified = all(cls.is_sample_qualified(r, min_pass_score=min_score) for r in matched)
            scores = [r.judge_score for r in matched if r.judge_score is not None]
            min_judge = min(scores) if scores else None
            records.append({
                "sample_id": s.id,
                "content_hash": fp_map.get(s.id),
                "skipped": False,
                "was_cooldown": s.id in cooldown_set,
                "status": "PASS" if all_qualified else "FAIL",
                "qualified": all_qualified,
                "score": min_judge,
                "eval_at": now_iso,
            })

        # 只有至少有 1 条有效非 API_ERROR 结果时才落入流水，避免全挂任务占用快照和推进 m
        if valid_eval_count == 0:
            return None

        for s in skipped_samples:
            records.append({
                "sample_id": s.id,
                "content_hash": fp_map.get(s.id),
                "skipped": True,
                "was_cooldown": True,
                "status": "SKIPPED",
                "qualified": None,
                "score": None,
                "eval_at": now_iso,
            })

        ledger_id = cls._next_ledger_id()
        ledger = BenchmarkRunLedger(
            id=ledger_id,
            task_id=task_id,
            dataset_path=norm_path,
            model_line=model_line,
            version_tag=version_tag,
            sampled_ids_json=json.dumps([s.id for s in selected_samples], ensure_ascii=False),
            records_json=json.dumps(records, ensure_ascii=False),
            is_revoked=False,
            created_at=int(time.time()),
        )
        db.add(ledger)
        db.commit()
        return ledger_id

    @classmethod
    def revoke_ledger_by_task(cls, db: Session, task_id: str) -> int:
        """按评测任务 ID 一键撤销战绩入账流水 (is_revoked=True)"""
        rows = (
            db.query(BenchmarkRunLedger)
            .filter(
                BenchmarkRunLedger.task_id == task_id,
                BenchmarkRunLedger.is_revoked == False,  # noqa: E712
            )
            .all()
        )
        for r in rows:
            r.is_revoked = True
        db.commit()
        return len(rows)

    @classmethod
    def reset_trackers_in_db(
        cls,
        db: Session,
        dataset_path: str,
        model_line: Optional[str] = None,
    ) -> int:
        """将指定数据集（及可选指定模型线）在流水表中的战绩记录全部标记为已撤销，回到全新起跑线"""
        norm_path = cls.normalize_dataset_path(dataset_path)
        query = db.query(BenchmarkRunLedger).filter(
            BenchmarkRunLedger.dataset_path == norm_path,
            BenchmarkRunLedger.is_revoked == False,  # noqa: E712
        )
        if model_line and model_line.strip():
            query = query.filter(BenchmarkRunLedger.model_line == model_line.strip())

        rows = query.all()
        for r in rows:
            r.is_revoked = True
        db.commit()
        return len(rows)
