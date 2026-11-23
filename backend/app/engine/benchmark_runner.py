# -*- coding: utf-8 -*-
"""
常规数据集多模型并发评测与指标聚合引擎 (Benchmark Runner)
支持:
1. 1 个标准评测集 × N 组 (模型 + 推理参数组合) 横向并发对比评测;
2. 采集推理工程性能: 端到端耗时 (Latency)、首字延迟 (TTFT)、吞吐量 (Tokens/s)、思考Token占比;
3. 多维客观规则算子: 必含词 (must_contain)、违禁词 (forbidden)、正则匹配 (regex)、JSON格式校验;
4. 主观 LLM 裁判算子: 联动 UniversalJudgeEngine 按 reference/rubric 打分 (0~100);
5. 严格区分 API 调用异常与模型回答业务错误，异常独立讨论与统计，支持最多 5 次重试;
6. 聚合各模型各分类指标，输出 Side-by-Side 样本级对比。
"""
import os
import re
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Union, Callable, Set
from backend.app.schemas.chat import (
    ChatMessage,
    ModelConfig,
    UnifiedChatRequest,
    UnifiedChatResponse,
)
from backend.app.schemas.benchmark import (
    BenchmarkSample,
    RubricConfig,
    ModelEvalConfig,
    SampleEvalResult,
    BenchmarkSummary,
    SamplingFilterConfig,
)
from backend.app.adapters.factory import AdapterFactory
from backend.app.adapters.base import BaseModelAdapter
from backend.app.engine.judge import UniversalJudgeEngine


def check_objective_rules(
    answer: str,
    rubric: Optional[Union[str, RubricConfig]] = None,
    regex_constraints: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    执行客观规则算子校验:
    1. 支持新版 regex_constraints 列表:
       - 普通正则字符串 (如 "未找到|不存在|Not Found"): 正向必配约束，未命中则判 FAIL;
       - 以 "!" 开头的正则字符串 (如 "!星云智联|售后专员|禁止词"): 反向禁含约束，命中则判 FAIL;
    2. 向下兼容旧版 RubricConfig 结构 (must_contain / forbidden / regex_patterns / require_json)。
    """
    details: Dict[str, Any] = {}
    passed = True

    # 1. 新版 regex_constraints 校验 (正向必配 + "!"前缀反向禁含)
    if regex_constraints:
        pos_patterns: List[str] = []
        neg_patterns: List[str] = []
        regex_missing: List[str] = []
        regex_violated: List[str] = []

        for raw_p in regex_constraints:
            if not raw_p or not isinstance(raw_p, str):
                continue
            if raw_p.startswith("!"):
                neg_p = raw_p[1:].strip()
                if not neg_p:
                    continue
                neg_patterns.append(raw_p)
                try:
                    m = re.search(neg_p, answer, flags=re.IGNORECASE | re.DOTALL)
                    if m:
                        regex_violated.append(f"{raw_p} (命中: {m.group(0)})")
                except re.error:
                    if neg_p in answer:
                        regex_violated.append(f"{raw_p} (命中: {neg_p})")
            else:
                pos_patterns.append(raw_p)
                try:
                    if not re.search(raw_p, answer, flags=re.IGNORECASE | re.DOTALL):
                        regex_missing.append(raw_p)
                except re.error:
                    if raw_p not in answer:
                        regex_missing.append(raw_p)

        details["regex_constraints"] = {
            "required": pos_patterns,
            "forbidden": neg_patterns,
            "missing": regex_missing,
            "violated": regex_violated,
        }
        if regex_missing:
            details["regex_missing"] = regex_missing
            passed = False
        if regex_violated:
            details["regex_violated"] = regex_violated
            details["forbidden_hit"] = regex_violated
            passed = False

    # 2. 兼容旧版 RubricConfig 对象
    if isinstance(rubric, RubricConfig):
        # 2.1 必含词
        if rubric.must_contain:
            missing = [w for w in rubric.must_contain if w not in answer]
            details["must_contain"] = {"required": rubric.must_contain, "missing": missing}
            if missing:
                details["must_contain_missing"] = missing
                passed = False

        # 2.2 违禁词
        if rubric.forbidden:
            hit = [w for w in rubric.forbidden if w in answer]
            details["forbidden"] = {"forbidden": rubric.forbidden, "hit": hit}
            if hit:
                details.setdefault("forbidden_hit", []).extend(hit)
                passed = False

        # 2.3 正则匹配
        if rubric.regex_patterns:
            regex_failed = []
            for p in rubric.regex_patterns:
                if not re.search(p, answer, flags=re.IGNORECASE | re.DOTALL):
                    regex_failed.append(p)
            details["regex"] = {"patterns": rubric.regex_patterns, "failed": regex_failed}
            if regex_failed:
                details.setdefault("regex_missing", []).extend(regex_failed)
                passed = False

        # 2.4 JSON 格式校验
        if rubric.require_json:
            is_json = False
            try:
                m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", answer, flags=re.DOTALL)
                raw_j = m.group(1) if m else answer.strip()
                json.loads(raw_j)
                is_json = True
            except Exception:
                is_json = False
            details["json_valid"] = is_json
            if not is_json:
                passed = False

    return {"passed": passed, "details": details}


def has_sample_eval_criteria(sample: BenchmarkSample) -> bool:
    """判断单条样本是否至少包含一项客观正则约束或主观裁判标准 (reference / rubric)。"""
    has_regex = any(isinstance(p, str) and bool(p.strip()) for p in (sample.regex_constraints or []))
    if has_regex:
        return True
    if sample.reference and str(sample.reference).strip():
        return True
    if isinstance(sample.rubric, str) and sample.rubric.strip():
        return True
    if isinstance(sample.rubric, RubricConfig):
        r = sample.rubric
        if (
            bool(r.must_contain)
            or bool(r.forbidden)
            or bool(r.regex_patterns)
            or bool(r.require_json)
            or bool(r.judge_criteria and str(r.judge_criteria).strip())
        ):
            return True
    return False


def has_dataset_eval_criteria(samples: List[BenchmarkSample]) -> bool:
    """判断评测集是否至少存在一条具备答案标准或正则约束的样本。"""
    return any(has_sample_eval_criteria(s) for s in samples)


class BenchmarkRunner:
    """常规数据集评测执行引擎"""

    def __init__(
        self,
        judge_adapter: Optional[BaseModelAdapter] = None,
        judge_config: Optional[ModelConfig] = None,
    ):
        self.judge_engine = (
            UniversalJudgeEngine(judge_adapter, default_config=judge_config)
            if judge_adapter
            else None
        )

    def evaluate_sample_single_model(
        self,
        sample: BenchmarkSample,
        model_config: ModelEvalConfig,
        adapter: BaseModelAdapter,
        ignore_sample_max_tokens: bool = False,
    ) -> SampleEvalResult:
        """评测单条样本在指定模型与参数配置下的表现，严格隔离 API 异常"""
        req_config = model_config.config.model_copy(deep=True)
        if not ignore_sample_max_tokens and sample.max_new_tokens is not None and sample.max_new_tokens > 0:
            if not req_config.thinking_enabled:
                req_config.max_tokens = sample.max_new_tokens
            else:
                req_config.max_tokens = max(req_config.max_tokens, sample.max_new_tokens)

        req = UnifiedChatRequest(
            model_id=model_config.model_id,
            messages=sample.messages,
            config=req_config,
        )

        is_api_error = False
        api_error_message = None
        try:
            resp = adapter.chat(req)
        except Exception as e:
            is_api_error = True
            api_error_message = str(e)
            resp = UnifiedChatResponse(
                content=f"[API调用异常: {api_error_message}]",
                thinking="",
                raw_output=f"API Error: {api_error_message}",
            )

        # 严格区分 API 异常与模型回答逻辑错误:
        # 若是 API 异常，绝不把接口挂掉或超时当成是模型的业务回答，不执行客观规则扣分与裁判打分
        if is_api_error:
            return SampleEvalResult(
                sample_id=sample.id,
                category=sample.category,
                sub_category=sample.sub_category,
                difficulty=sample.difficulty,
                source=sample.source,
                is_holdout=bool(sample.is_holdout),
                model_id=model_config.model_id,
                response=resp,
                objective_passed=False,
                objective_details={"api_error": api_error_message},
                judge_score=None,
                judge_verdict="API_ERROR",
                judge_feedback=f"API调用异常未执行主观裁判: {api_error_message}",
                status="API_ERROR",
                error_message=api_error_message,
            )

        # 1. 客观规则校验 (仅针对模型成功生成的回复)
        obj_res = check_objective_rules(
            answer=resp.content,
            rubric=sample.rubric,
            regex_constraints=sample.regex_constraints,
        )

        # 2. 主观 LLM 裁判打分
        judge_score = None
        judge_verdict = None
        judge_feedback = None
        has_judge_criteria = bool(
            sample.reference
            or (isinstance(sample.rubric, str) and sample.rubric.strip())
            or (isinstance(sample.rubric, RubricConfig) and sample.rubric.judge_criteria)
        )
        if self.judge_engine and has_judge_criteria:
            try:
                j_eval = self.judge_engine.evaluate_benchmark_response(sample, resp.content)
                judge_score = j_eval.get("score")
                judge_verdict = j_eval.get("verdict")
                judge_feedback = j_eval.get("feedback")
            except Exception as e:
                judge_feedback = f"裁判打分失败: {e}"

        # 判定状态: 正常通过 vs 客观/裁判不通过
        is_passed = obj_res["passed"]
        if judge_verdict and judge_verdict == "FAIL":
            is_passed = False
        final_status = "PASS" if is_passed else "FAIL"

        return SampleEvalResult(
            sample_id=sample.id,
            category=sample.category,
            sub_category=sample.sub_category,
            difficulty=sample.difficulty,
            source=sample.source,
            is_holdout=bool(sample.is_holdout),
            model_id=model_config.model_id,
            response=resp,
            objective_passed=obj_res["passed"],
            objective_details=obj_res["details"],
            judge_score=judge_score,
            judge_verdict=judge_verdict,
            judge_feedback=judge_feedback,
            status=final_status,
            error_message=None,
        )

    def run_benchmark_for_model(
        self,
        samples: List[BenchmarkSample],
        model_config: ModelEvalConfig,
        workers: int = 4,
        on_sample_done: Optional[Callable[[SampleEvalResult, int, int], None]] = None,
        ignore_sample_max_tokens: bool = False,
        dataset_total_samples: Optional[int] = None,
        skipped_mastered_samples: int = 0,
        cooldown_sample_ids: Optional[Set[str]] = None,
        is_benchmark_model: bool = True,
        min_pass_score: Optional[float] = None,
        skipped_holdout_samples: int = 0,
        skipped_regular_samples: int = 0,
    ) -> Dict[str, Any]:
        """对单一模型配置执行全量数据集并发评测并汇总指标"""
        adapter = AdapterFactory.create(
            provider_type=model_config.provider_type,
            model_name=model_config.model_name or model_config.model_id,
            api_url=model_config.api_url,
            api_key=model_config.api_key,
            vertex_thinking_mode=model_config.config.vertex_thinking_mode,
        )

        results: List[SampleEvalResult] = []
        completed_in_model = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_sample = {
                executor.submit(
                    self.evaluate_sample_single_model,
                    s,
                    model_config,
                    adapter,
                    ignore_sample_max_tokens,
                ): s
                for s in samples
            }
            for future in as_completed(future_to_sample):
                try:
                    res = future.result()
                    results.append(res)
                    completed_in_model += 1
                    if on_sample_done:
                        on_sample_done(res, completed_in_model, len(samples))
                except Exception as e:
                    s = future_to_sample[future]
                    print(f"⚠️ [BenchmarkRunner] 样本 {s.id} 评测中断: {e}")

        # 聚合指标 (严格统计 API 异常与有效模型样本)
        total = len(results)
        api_errors = sum(1 for r in results if r.status == "API_ERROR")
        valid_count = total - api_errors
        passed_obj = sum(1 for r in results if r.status == "PASS")
        failed_obj = sum(1 for r in results if r.status == "FAIL")

        valid_results = [r for r in results if r.status != "API_ERROR"]
        calc_res = valid_results if valid_results else results

        avg_latency = sum(r.response.metrics.total_latency_ms for r in calc_res) / max(len(calc_res), 1)
        avg_ttft = sum(r.response.metrics.ttft_ms for r in calc_res) / max(len(calc_res), 1)
        avg_tps = sum(r.response.metrics.tokens_per_second for r in calc_res) / max(len(calc_res), 1)

        total_comp_tokens = sum(r.response.usage.completion_tokens for r in calc_res)
        total_think_tokens = sum(r.response.usage.thinking_tokens for r in calc_res)
        think_ratio = (total_think_tokens / max(total_comp_tokens, 1))

        judge_scores = [r.judge_score for r in valid_results if r.judge_score is not None]
        avg_judge = sum(judge_scores) / max(len(judge_scores), 1) if judge_scores else 0.0

        # 按分类聚合
        cat_map: Dict[str, List[SampleEvalResult]] = {}
        for r in results:
            cat_map.setdefault(r.category, []).append(r)

        cat_breakdown = {}
        for cat, items in cat_map.items():
            c_tot = len(items)
            c_err = sum(1 for x in items if x.status == "API_ERROR")
            c_val = c_tot - c_err
            c_pass = sum(1 for x in items if x.status == "PASS")
            c_rate = round(c_pass / max(c_val, 1), 4) if c_val > 0 else 0.0
            c_j = [x.judge_score for x in items if x.judge_score is not None and x.status != "API_ERROR"]
            cat_breakdown[cat] = {
                "count": c_tot,
                "total": c_tot,
                "valid_count": c_val,
                "api_error_count": c_err,
                "passed": c_pass,
                "failed": c_val - c_pass,
                "pass_rate": c_rate,
                "objective_pass_rate": c_rate,
                "avg_judge_score": round(sum(c_j) / max(len(c_j), 1), 2) if c_j else None,
            }

        effective_pass_rate = round(passed_obj / max(valid_count if valid_count > 0 else total, 1), 4)
        full_total = dataset_total_samples if dataset_total_samples is not None else total

        # 熟题复测表现统计 (c_old >= x 的有效样本，包含留出题与普通题)
        cd_ids = cooldown_sample_ids or set()
        cd_valid_results = [r for r in valid_results if r.sample_id in cd_ids]
        cd_tested = len(cd_valid_results)
        cd_passed = 0
        for r in cd_valid_results:
            if r.status == "PASS" and (min_pass_score is None or r.judge_score is None or r.judge_score >= min_pass_score):
                cd_passed += 1
        cd_failed = cd_tested - cd_passed
        cd_retention: Optional[float] = round(cd_passed / cd_tested, 4) if cd_tested > 0 else None

        # 全集等效通过率: 仅对基准模型线按实测熟题真实保持率 R_cooldown 折算
        est_full_rate: Optional[float] = None
        if is_benchmark_model:
            if skipped_mastered_samples > 0:
                if cd_tested > 0 and cd_retention is not None:
                    effective_full_denom = max(full_total - api_errors, 1)
                    est_full_rate = round((passed_obj + skipped_mastered_samples * cd_retention) / effective_full_denom, 4)
                else:
                    # 抽中的熟题全量异常导致无有效熟题成绩时，不输出虚高估算
                    est_full_rate = None
            else:
                est_full_rate = effective_pass_rate

        # 按留出题 (is_holdout=True) 与普通题 (is_holdout=False) 分别计算通过率与细分统计
        def _build_holdout_group_metrics(
            group_label: str,
            group_results: List[SampleEvalResult],
            group_skipped: int,
        ) -> Dict[str, Any]:
            g_tot = len(group_results)
            g_ds_tot = g_tot + group_skipped
            g_err = sum(1 for r in group_results if r.status == "API_ERROR")
            g_val = g_tot - g_err
            g_pass = sum(1 for r in group_results if r.status == "PASS")
            g_fail = sum(1 for r in group_results if r.status == "FAIL")
            g_rate: Optional[float] = round(g_pass / g_val, 4) if g_val > 0 else None
            g_j = [r.judge_score for r in group_results if r.status != "API_ERROR" and r.judge_score is not None]
            g_avg_j: Optional[float] = round(sum(g_j) / len(g_j), 2) if g_j else None

            g_cd_valid = [r for r in group_results if r.status != "API_ERROR" and r.sample_id in cd_ids]
            g_cd_tested = len(g_cd_valid)
            g_cd_passed = sum(
                1
                for r in g_cd_valid
                if r.status == "PASS" and (min_pass_score is None or r.judge_score is None or r.judge_score >= min_pass_score)
            )
            g_cd_failed = g_cd_tested - g_cd_passed
            g_cd_ret: Optional[float] = round(g_cd_passed / g_cd_tested, 4) if g_cd_tested > 0 else None

            g_est_full: Optional[float] = None
            if is_benchmark_model and g_ds_tot > 0:
                if group_skipped > 0:
                    if g_cd_tested > 0 and g_cd_ret is not None:
                        eff_denom = max(g_ds_tot - g_err, 1)
                        g_est_full = round((g_pass + group_skipped * g_cd_ret) / eff_denom, 4)
                else:
                    g_est_full = g_rate

            return {
                "label": group_label,
                "count": g_tot,
                "total": g_tot,
                "dataset_total": g_ds_tot,
                "skipped": group_skipped,
                "valid_count": g_val,
                "api_error_count": g_err,
                "passed": g_pass,
                "failed": g_fail,
                "pass_rate": g_rate,
                "estimated_full_pass_rate": g_est_full,
                "avg_judge_score": g_avg_j,
                "cooldown_tested": g_cd_tested,
                "cooldown_passed": g_cd_passed,
                "cooldown_failed": g_cd_failed,
                "cooldown_retention_rate": g_cd_ret,
            }

        holdout_results = [r for r in results if r.is_holdout]
        regular_results = [r for r in results if not r.is_holdout]
        holdout_info = _build_holdout_group_metrics("留出题 (Holdout)", holdout_results, skipped_holdout_samples)
        regular_info = _build_holdout_group_metrics("普通题 (Regular)", regular_results, skipped_regular_samples)

        summary = BenchmarkSummary(
            model_id=model_config.model_id,
            total_samples=total,
            dataset_total_samples=full_total,
            skipped_mastered_samples=skipped_mastered_samples,
            passed_samples=passed_obj,
            failed_samples=failed_obj,
            api_error_samples=api_errors,
            api_error_rate=round(api_errors / max(total, 1), 4),
            valid_samples=valid_count,
            pass_rate=effective_pass_rate,
            estimated_full_pass_rate=est_full_rate,
            cooldown_tested_samples=cd_tested,
            cooldown_passed_samples=cd_passed,
            cooldown_failed_samples=cd_failed,
            cooldown_retention_rate=cd_retention,
            holdout_total_samples=holdout_info["total"],
            holdout_dataset_total_samples=holdout_info["dataset_total"],
            holdout_skipped_samples=holdout_info["skipped"],
            holdout_valid_samples=holdout_info["valid_count"],
            holdout_passed_samples=holdout_info["passed"],
            holdout_failed_samples=holdout_info["failed"],
            holdout_api_error_samples=holdout_info["api_error_count"],
            holdout_pass_rate=holdout_info["pass_rate"],
            holdout_estimated_full_pass_rate=holdout_info["estimated_full_pass_rate"],
            holdout_avg_judge_score=holdout_info["avg_judge_score"],
            regular_total_samples=regular_info["total"],
            regular_dataset_total_samples=regular_info["dataset_total"],
            regular_skipped_samples=regular_info["skipped"],
            regular_valid_samples=regular_info["valid_count"],
            regular_passed_samples=regular_info["passed"],
            regular_failed_samples=regular_info["failed"],
            regular_api_error_samples=regular_info["api_error_count"],
            regular_pass_rate=regular_info["pass_rate"],
            regular_estimated_full_pass_rate=regular_info["estimated_full_pass_rate"],
            regular_avg_judge_score=regular_info["avg_judge_score"],
            holdout_breakdown={"holdout": holdout_info, "regular": regular_info},
            is_benchmark_model=is_benchmark_model,
            avg_latency_ms=round(avg_latency, 2),
            avg_ttft_ms=round(avg_ttft, 2),
            avg_tps=round(avg_tps, 2),
            thinking_token_ratio=round(think_ratio, 4),
            objective_pass_rate=effective_pass_rate,
            avg_judge_score=round(avg_judge, 2),
            category_breakdown=cat_breakdown,
        )

        return {"summary": summary, "results": results}

    def run_multi_model_matrix(
        self,
        samples: List[BenchmarkSample],
        model_configs: List[ModelEvalConfig],
        workers: int = 4,
        progress_callback: Optional[Callable[[int, int, str, str], None]] = None,
        on_matrix_step: Optional[Callable[[Dict[str, Any]], None]] = None,
        ignore_sample_max_tokens: bool = False,
        dataset_total_samples: Optional[int] = None,
        skipped_mastered_samples: int = 0,
        sampling_config: Optional[SamplingFilterConfig] = None,
        cooldown_sample_ids: Optional[Set[str]] = None,
        skipped_holdout_samples: int = 0,
        skipped_regular_samples: int = 0,
    ) -> Dict[str, Any]:
        """评测矩阵: 1 个数据集 × N 组模型与配置"""
        from backend.app.engine.benchmark_sampler import BenchmarkSamplingEngine

        target_keys = (
            BenchmarkSamplingEngine.resolve_benchmark_target_keys(sampling_config)
            if sampling_config
            else set()
        )
        min_pass_score = sampling_config.min_pass_score if sampling_config else None
        sampled_holdout_cnt = sum(1 for s in samples if s.is_holdout)
        sampled_regular_cnt = len(samples) - sampled_holdout_cnt

        matrix_result: Dict[str, Any] = {
            "meta": {
                "ignore_sample_max_tokens": ignore_sample_max_tokens,
                "dataset_total_samples": dataset_total_samples or len(samples),
                "sampled_samples": len(samples),
                "skipped_mastered_samples": skipped_mastered_samples,
                "sampled_holdout_samples": sampled_holdout_cnt,
                "sampled_regular_samples": sampled_regular_cnt,
                "skipped_holdout_samples": skipped_holdout_samples,
                "skipped_regular_samples": skipped_regular_samples,
                "sampling_config": sampling_config.model_dump() if sampling_config else None,
            },
            "summaries": {},
            "side_by_side": {},
        }

        # 整理 side-by-side 骨架
        for s in samples:
            msg_dicts = [m.to_dict() for m in s.messages]
            rubric_val = s.rubric.model_dump() if isinstance(s.rubric, RubricConfig) else s.rubric
            matrix_result["side_by_side"][s.id] = {
                "sample_id": s.id,
                "category": s.category,
                "sub_category": s.sub_category,
                "difficulty": s.difficulty,
                "source": s.source,
                "is_holdout": bool(s.is_holdout),
                "max_new_tokens": s.max_new_tokens,
                "messages": msg_dicts,
                "input_prompt": msg_dicts,
                "reference": s.reference,
                "rubric": rubric_val,
                "regex_constraints": s.regex_constraints,
                "models": {},
                "results": {},
            }

        # 触发初始骨架落盘通知
        if on_matrix_step:
            on_matrix_step(matrix_result)

        used_keys = set()
        total_evaluations = len(samples) * len(model_configs)
        completed_overall = 0

        for idx, m_cfg in enumerate(model_configs):
            base_key = m_cfg.display_name or m_cfg.model_id
            cfg_key = base_key
            suffix = 2
            while cfg_key in used_keys:
                cfg_key = f"{base_key} ({suffix})"
                suffix += 1
            used_keys.add(cfg_key)

            def _handle_sample_done(sample_res: SampleEvalResult, m_done: int, m_tot: int):
                nonlocal completed_overall
                completed_overall += 1
                if progress_callback:
                    progress_callback(completed_overall, total_evaluations, cfg_key, sample_res.sample_id)

            is_bm_model = True
            if sampling_config and sampling_config.enabled:
                if target_keys:
                    is_bm_model = (
                        BenchmarkSamplingEngine.matches_target_model(cfg_key, target_keys)
                        or BenchmarkSamplingEngine.matches_target_model(m_cfg.model_id, target_keys)
                    )
                else:
                    # 只读摸底模式: 不展示虚高全集等效分
                    is_bm_model = skipped_mastered_samples == 0

            model_out = self.run_benchmark_for_model(
                samples,
                m_cfg,
                workers=workers,
                on_sample_done=_handle_sample_done,
                ignore_sample_max_tokens=ignore_sample_max_tokens,
                dataset_total_samples=dataset_total_samples,
                skipped_mastered_samples=skipped_mastered_samples,
                cooldown_sample_ids=cooldown_sample_ids,
                is_benchmark_model=is_bm_model,
                min_pass_score=min_pass_score,
                skipped_holdout_samples=skipped_holdout_samples,
                skipped_regular_samples=skipped_regular_samples,
            )
            summary: BenchmarkSummary = model_out["summary"]
            # 保证 summary 里的 model_id 和矩阵字典 key 保持一致，便于前端图表与卡片索引
            summary.model_id = cfg_key
            matrix_result["summaries"][cfg_key] = summary.model_dump()

            raw_sample_map = matrix_result.setdefault("_raw_results_by_sample", {})
            for res in model_out["results"]:
                res_record = res.model_copy(
                    update={"model_id": cfg_key, "source_model_id": m_cfg.model_id}
                )
                raw_sample_map.setdefault(res.sample_id, []).append(res_record)
                item_detail = {
                    "content": res.response.content,
                    "reply": res.response.content,
                    "thinking": res.response.thinking,
                    "latency_ms": res.response.metrics.total_latency_ms,
                    "ttft_ms": res.response.metrics.ttft_ms,
                    "tps": res.response.metrics.tokens_per_second,
                    "thinking_tokens": res.response.usage.thinking_tokens,
                    "metrics": res.response.metrics.model_dump(),
                    "usage": res.response.usage.model_dump(),
                    "objective_passed": res.objective_passed,
                    "objective_details": res.objective_details,
                    "judge_score": res.judge_score,
                    "judge_verdict": res.judge_verdict,
                    "judge_feedback": res.judge_feedback,
                    "status": res.status,
                    "error_message": res.error_message,
                }
                matrix_result["side_by_side"][res.sample_id]["models"][cfg_key] = item_detail
                matrix_result["side_by_side"][res.sample_id]["results"][cfg_key] = item_detail

            # 单个模型全部完成后，触发即时步进落盘
            if on_matrix_step:
                on_matrix_step(matrix_result)

        return matrix_result
