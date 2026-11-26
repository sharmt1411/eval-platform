# -*- coding: utf-8 -*-
"""
数据飞轮全生命周期治理服务 (Flywheel Service)
完整闭环:
对话记录生成 / 导入 -> 白盒质检 -> AI 智能重塑修复 -> 全量二次质检验收 (及格直通/单次纠偏补考/坏样淘汰隔离) -> 导出标准 messages 黄金数据集
支持:
1. 工业级多线程并发处理;
2. 断点续跑 (基于样本 ID 扫描已完成文件自动跳过，零重复开销);
3. 边跑边即时落盘与线程安全写入;
4. 格式严格规范化校验 (validate_and_sanitize_format)。
"""
import os
import sys
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Set, Tuple, Callable
from backend.app.schemas.chat import ChatMessage, ModelConfig
from backend.app.schemas.rule import UniversalRuleSchema, PersonaDefinition
from backend.app.schemas.flywheel import (
    DedupConfig,
    InspectionReport,
    FlywheelRecord,
    FlywheelBenchmarkSlice,
)
from backend.app.adapters.base import BaseModelAdapter
from backend.app.engine.deduplicator import DatasetDeduplicator
from backend.app.engine.judge import UniversalJudgeEngine
from backend.app.engine.remediator import UniversalDataRemediator
from backend.app.engine.slicer import ProgressiveContextSlicer


class FlywheelPipelineService:
    """数据飞轮全流程管道服务"""

    def __init__(
        self,
        judge_adapter: Optional[BaseModelAdapter] = None,
        remediate_adapter: Optional[BaseModelAdapter] = None,
        judge_config: Optional[ModelConfig] = None,
        remediate_config: Optional[ModelConfig] = None,
    ):
        self.judge_engine = (
            UniversalJudgeEngine(judge_adapter, default_config=judge_config)
            if judge_adapter is not None
            else None
        )
        self.remediator = UniversalDataRemediator(remediate_adapter, default_config=remediate_config)
        self._lock = threading.Lock()

    @staticmethod
    def is_gold_pass(eval_report: InspectionReport, threshold: int) -> bool:
        """判定初检是否直接达到黄金直通标准（免修复）。
        以裁判模型裁决为准：若仅存在代码附带的【拓扑检测元数据】参考提示，
        但裁判未给出任何扣分或实质缺陷且打分达标，则免修复直通。
        """
        judge_defects = [
            d for d in (eval_report.detected_defects or [])
            if not str(d).strip().startswith("【拓扑检测元数据】")
        ]
        return bool(
            eval_report.score >= threshold
            and eval_report.verdict == "PASS"
            and not eval_report.deductions
            and not judge_defects
        )

    @staticmethod
    def _is_judge_error_report(eval_report: InspectionReport) -> bool:
        """检测质检报告是否为裁判模型调用异常、思考截断或 JSON 解析失败产生的兜底结果。"""
        substantive_defects = [
            str(d).strip()
            for d in (eval_report.detected_defects or [])
            if str(d).strip() and not str(d).strip().startswith("【拓扑检测元数据】")
        ]
        judge_error_prefixes = (
            "模型未按标准 JSON 输出质检报告",
            "质检裁判模型调用异常",
            "模型返回的 JSON 缺少",
        )
        has_main_judge_error = any(
            isinstance(dbg, dict)
            and dbg.get("step") == "main_judge"
            and (
                bool(dbg.get("error"))
                or (
                    not str(dbg.get("response_content") or "").strip()
                    and not str(dbg.get("raw_output") or "").strip()
                )
            )
            for dbg in (eval_report.debug_requests or [])
        )
        has_error_defect = any(
            d.startswith(judge_error_prefixes) for d in substantive_defects
        )
        return has_main_judge_error or has_error_defect

    @staticmethod
    def check_remediable_eval(
        eval_report: Optional[InspectionReport],
    ) -> Tuple[bool, str]:
        """
        校验质检报告是否具备可修复的实质异常:
        1. 尚未执行质检（None）-> 不可直接修复；
        2. 质检自身异常、思考截断（main_judge 正文为空/error）或 JSON 解析失败 -> 不可修复；
        3. 无扣分项 (deductions) 且剔除仅供参考的【拓扑检测元数据】后无实质缺陷 -> 提示没有异常可以修复。
        """
        if eval_report is None:
            return False, "当前样本尚未执行质检，暂无异常诊断可修复，请先质检或开启「修复前重新执行质检」。"

        substantive_defects = [
            str(d).strip()
            for d in (eval_report.detected_defects or [])
            if str(d).strip() and not str(d).strip().startswith("【拓扑检测元数据】")
        ]
        has_deductions = bool(eval_report.deductions)

        if FlywheelPipelineService._is_judge_error_report(eval_report):
            return (
                False,
                "当前质检结果异常或输出被截断，未产生有效缺陷诊断，无法直接修复；请先重新质检（或开启「修复前重新执行质检」）。",
            )

        if not has_deductions and not substantive_defects:
            has_only_topo_notice = any(
                str(d).strip().startswith("【拓扑检测元数据】")
                for d in (eval_report.detected_defects or [])
            )
            if has_only_topo_notice:
                return (
                    False,
                    "没有异常可以修复：当前质检无扣分项 (deductions) 且无实质业务缺陷（【拓扑检测元数据】仅供参考，不作为修复依据）。",
                )
            return (
                False,
                "没有异常可以修复：当前质检未检出任何扣分项 (deductions) 或实质业务缺陷。",
            )

        return True, ""

    @classmethod
    def _is_record_judge_error(cls, record: FlywheelRecord) -> bool:
        """判断单条飞轮记录在初检或二次质检阶段是否发生裁判模型调用/解析异常。"""
        if record.initial_eval is None or cls._is_judge_error_report(record.initial_eval):
            return True
        if record.verified_eval is not None and cls._is_judge_error_report(record.verified_eval):
            return True
        return False

    @classmethod
    def verdict_from_eval_dict(
        cls,
        eval_dict: Optional[Dict[str, Any]],
        threshold: int = 85,
        require_zero_defects: bool = False,
    ) -> str:
        """从质检报告字典推断解耦判定结论: UNJUDGED / PASS / FAIL / ERROR。"""
        if not isinstance(eval_dict, dict) or ("score" not in eval_dict and "verdict" not in eval_dict):
            return "UNJUDGED"
        try:
            rep = InspectionReport(**eval_dict)
        except Exception:
            ver = str(eval_dict.get("verdict") or "").upper()
            sc = int(eval_dict.get("score") or 0) if eval_dict.get("score") is not None else 0
            if ver == "PASS" and sc >= threshold:
                if require_zero_defects and eval_dict.get("deductions"):
                    return "FAIL"
                return "PASS"
            return "FAIL"

        if cls._is_judge_error_report(rep):
            return "ERROR"
        if require_zero_defects:
            return "PASS" if cls.is_gold_pass(rep, threshold) else "FAIL"
        return "PASS" if (rep.verdict == "PASS" and rep.score >= threshold) else "FAIL"

    @staticmethod
    def derive_sample_status(
        raw_verdict: Optional[str],
        remediated_verdict: Optional[str],
        manual_decision: Optional[str],
        active_version: Optional[str],
    ) -> str:
        """由解耦的原对话结论、修复后结论、人工复核结论与生效版本确定性派生顶层状态。"""
        rv = (raw_verdict or "UNJUDGED").upper()
        mv = (remediated_verdict or "NONE").upper()
        md = (manual_decision or "NONE").upper()
        av = (active_version or "raw").lower()

        if md == "REJECTED":
            return "DISCARDED"
        if md == "APPROVED":
            return "MANUAL_APPROVED"
        if av == "remediated":
            if mv == "PASS":
                return "REPAIRED_PASS"
            if mv == "FAIL" and rv == "FAIL":
                return "DISCARDED"
            return "PENDING"
        # active_version == "raw"
        if rv == "PASS":
            return "GOLD_PASS"
        if rv == "FAIL" and mv == "FAIL":
            return "DISCARDED"
        return "PENDING"

    @classmethod
    def infer_legacy_decoupled_state(
        cls,
        status: Optional[str],
        initial_eval: Optional[Dict[str, Any]],
        verified_eval: Optional[Dict[str, Any]],
        has_remediated: bool,
        threshold: int = 85,
    ) -> Dict[str, str]:
        """为旧式样本记录推断兼容的四元解耦状态字段。"""
        st = (status or "PENDING").upper()
        inferred_raw = cls.verdict_from_eval_dict(initial_eval, threshold, require_zero_defects=True)
        inferred_ver = (
            cls.verdict_from_eval_dict(verified_eval, threshold, require_zero_defects=False)
            if isinstance(verified_eval, dict) and verified_eval
            else ("UNVERIFIED" if has_remediated else "NONE")
        )

        if st == "GOLD_PASS":
            return {
                "raw_verdict": "PASS",
                "remediated_verdict": "NONE",
                "manual_decision": "NONE",
                "active_version": "raw",
            }
        if st == "REPAIRED_PASS":
            if not has_remediated:
                return {
                    "raw_verdict": "PASS",
                    "remediated_verdict": "NONE",
                    "manual_decision": "NONE",
                    "active_version": "raw",
                }
            return {
                "raw_verdict": inferred_raw if inferred_raw != "UNJUDGED" else "FAIL",
                "remediated_verdict": "PASS",
                "manual_decision": "NONE",
                "active_version": "remediated",
            }
        if st == "MANUAL_APPROVED":
            return {
                "raw_verdict": inferred_raw,
                "remediated_verdict": inferred_ver if has_remediated else "NONE",
                "manual_decision": "APPROVED",
                "active_version": "remediated" if has_remediated else "raw",
            }
        if st == "DISCARDED":
            rv = inferred_raw if inferred_raw != "UNJUDGED" else "FAIL"
            if has_remed := bool(has_remediated):
                mv = inferred_ver if inferred_ver != "UNVERIFIED" else "FAIL"
                md = "REJECTED" if mv != "FAIL" else "NONE"
                av = "remediated" if has_remed else "raw"
            else:
                mv = "NONE"
                md = "REJECTED"
                av = "raw"
            return {
                "raw_verdict": rv,
                "remediated_verdict": mv,
                "manual_decision": md,
                "active_version": av,
            }
        return {
            "raw_verdict": inferred_raw,
            "remediated_verdict": inferred_ver if has_remediated else "NONE",
            "manual_decision": "NONE",
            "active_version": "remediated" if has_remediated else "raw",
        }

    @classmethod
    def compute_record_decoupled_state(
        cls,
        record: FlywheelRecord,
        threshold: int = 85,
    ) -> Dict[str, str]:
        """根据单条飞轮处理产物 FlywheelRecord 计算入库四元解耦状态及派生 status。"""
        init_dict = record.initial_eval.model_dump() if record.initial_eval else None
        ver_dict = record.verified_eval.model_dump() if record.verified_eval else None
        has_remed = bool(record.remediated_messages)

        if record.status == "GOLD_PASS":
            raw_v = "PASS"
            remed_v = "NONE"
            manual_d = "NONE"
            active_v = "raw"
        else:
            raw_v = cls.verdict_from_eval_dict(init_dict, threshold, require_zero_defects=True)
            if has_remed:
                remed_v = (
                    cls.verdict_from_eval_dict(ver_dict, threshold, require_zero_defects=False)
                    if ver_dict
                    else "UNVERIFIED"
                )
                active_v = "remediated"
            else:
                remed_v = "NONE"
                active_v = "raw"
            manual_d = "REJECTED" if record.status == "DISCARDED" and not (raw_v == "FAIL" and remed_v == "FAIL") else "NONE"

        return {
            "raw_verdict": raw_v,
            "remediated_verdict": remed_v,
            "manual_decision": manual_d,
            "active_version": active_v,
            "status": cls.derive_sample_status(raw_v, remed_v, manual_d, active_v),
        }

    @classmethod
    def apply_rejudge_transition(
        cls,
        eval_report: InspectionReport,
        threshold: int,
        target_data: str = "raw",
        raw_verdict: str = "UNJUDGED",
        remediated_verdict: str = "NONE",
        manual_decision: str = "NONE",
        active_version: str = "raw",
        has_remediated_messages: bool = False,
        rule_changed: bool = False,
    ) -> Dict[str, str]:
        """
        根据重检目标 (raw / remediated)、新裁判报告与规则是否变更，计算解耦四元组与派生顶层 status:
        - 重检 raw 直接达标: raw_verdict="PASS", active_version="raw"；
        - 重检 raw 未直接达标但规则切换/更新: 旧 remediated PASS 降为 UNVERIFIED(保留文本), 旧 MANUAL_APPROVED 降为 NONE(保留备注)；
        - 重检 raw 在同一未变更规则下: 不冲掉已验收的 REPAIRED_PASS 或已人工放行的 MANUAL_APPROVED；
        - 重检 remediated: 仅刷新 remediated_verdict 并视结果切换 active_version。
        """
        new_raw = (raw_verdict or "UNJUDGED").upper()
        new_remed = (remediated_verdict or "NONE").upper()
        new_manual = (manual_decision or "NONE").upper()
        new_active = (active_version or "raw").lower()

        if target_data == "remediated":
            if cls._is_judge_error_report(eval_report):
                new_remed = "ERROR"
            elif eval_report.score >= threshold and eval_report.verdict == "PASS":
                new_remed = "PASS"
            else:
                new_remed = "FAIL"
            if has_remediated_messages:
                new_active = "remediated"
            if rule_changed and new_manual == "APPROVED":
                new_manual = "NONE"
            if new_remed == "PASS" and new_manual == "REJECTED":
                new_manual = "NONE"
        else:
            if cls._is_judge_error_report(eval_report):
                new_raw = "ERROR"
            elif cls.is_gold_pass(eval_report, threshold):
                new_raw = "PASS"
            else:
                new_raw = "FAIL"

            if new_raw == "PASS":
                new_active = "raw"
                if new_manual == "REJECTED":
                    new_manual = "NONE"
                if rule_changed:
                    if has_remediated_messages and new_remed in ("PASS", "FAIL"):
                        new_remed = "UNVERIFIED"
                    if new_manual == "APPROVED":
                        new_manual = "NONE"
            else:
                if rule_changed:
                    if has_remediated_messages and new_remed == "PASS":
                        new_remed = "UNVERIFIED"
                    if new_manual == "APPROVED":
                        new_manual = "NONE"

        return {
            "raw_verdict": new_raw,
            "remediated_verdict": new_remed,
            "manual_decision": new_manual,
            "active_version": new_active,
            "status": cls.derive_sample_status(new_raw, new_remed, new_manual, new_active),
        }

    @classmethod
    def resolve_rejudge_status(
        cls,
        eval_report: InspectionReport,
        threshold: int,
        target_data: str = "raw",
        current_status: str = "PENDING",
    ) -> str:
        """兼容包装：根据重检目标与评估报告判定治理后的样本状态。"""
        if target_data == "remediated":
            if cls._is_judge_error_report(eval_report):
                return "PENDING"
            is_pass = bool(
                eval_report.score >= threshold and eval_report.verdict == "PASS"
            )
            return "REPAIRED_PASS" if is_pass else "DISCARDED"
        if cls._is_judge_error_report(eval_report):
            return current_status if current_status in ("DISCARDED", "MANUAL_APPROVED") else "PENDING"
        if cls.is_gold_pass(eval_report, threshold):
            return "GOLD_PASS"
        return "DISCARDED" if current_status == "DISCARDED" else "PENDING"

    @staticmethod
    def validate_manual_dialogue_messages(
        messages: List[Any],
        default_system_prompt: str = "你是专业的业务对话机器人。",
    ) -> Tuple[bool, str]:
        """校验人工放行或保存时选定版本的对话消息非空且符合 SFT 角色要求。"""
        if not messages or not isinstance(messages, list):
            return False, "选定生效版本的对话消息为空，无法人工放行"
        chat_objs: List[ChatMessage] = []
        has_asst = False
        for m in messages:
            if isinstance(m, ChatMessage):
                role = m.role
                content = m.content
                thinking = m.thinking
            elif isinstance(m, dict):
                role = str(m.get("role") or "").strip().lower()
                content = str(m.get("content") or "")
                thinking = m.get("thinking")
            else:
                continue
            if role not in ("system", "user", "assistant"):
                return False, f"存在非法消息角色 '{role}'，仅允许 system / user / assistant"
            if role == "assistant" and content.replace("[挂断电话]", "").strip():
                has_asst = True
            if content.replace("[挂断电话]", "").strip() or role == "system":
                chat_objs.append(
                    ChatMessage(role=role, content=content, thinking=thinking)  # type: ignore[arg-type]
                )
        if not has_asst:
            return False, "选定生效版本缺少有效的 assistant 回复，无法人工放行"

        # 若最后一条在 user 之前没有任何 user->assistant 对且是外呼开场，不强制剔除末尾 user
        roles_non_sys = [c.role for c in chat_objs if c.role != "system"]
        req_asst_end = "user" in roles_non_sys and any(
            roles_non_sys[i] == "user" and any(r == "assistant" for r in roles_non_sys[i:])
            for i in range(len(roles_non_sys))
        )
        ok, _, reason = UniversalDataRemediator.validate_and_sanitize_format(
            chat_objs,
            default_system_prompt=default_system_prompt,
            inject_empty_thinking=False,
            require_assistant_ending=req_asst_end,
        )
        return ok, reason

    @staticmethod
    def prepare_dialogue_for_training_export(
        raw_messages: List[Any],
        remediated_messages: Optional[List[Any]],
        active_version: Optional[str],
        default_system_prompt: str,
        inject_thinking: bool = False,
    ) -> Tuple[bool, List[Dict[str, Any]], str]:
        """
        严格按 active_version 提取并清洗待导出训练集的对话消息:
        - active_version == 'remediated': 必须使用 remediated_messages，禁止回退 raw_messages;
        - active_version == 'raw': 必须使用 raw_messages，禁止混用未验收的 remediated_messages。
        """
        ver = (active_version or "raw").lower()
        if ver == "remediated":
            src_list = remediated_messages or []
            if not src_list:
                return False, [], "当前生效版本为修复后对话 (remediated)，但修复内容为空，无法导出"
        else:
            src_list = raw_messages or []
            if not src_list:
                return False, [], "原对话消息为空，无法导出"

        chat_objs: List[ChatMessage] = []
        for m in src_list:
            if isinstance(m, ChatMessage):
                chat_objs.append(m)
            elif isinstance(m, dict):
                role = str(m.get("role") or "").strip().lower()
                if role not in ("system", "user", "assistant"):
                    continue
                chat_objs.append(
                    ChatMessage(
                        role=role,  # type: ignore[arg-type]
                        content=str(m.get("content") or ""),
                        thinking=m.get("thinking"),
                    )
                )

        roles_non_sys = [c.role for c in chat_objs if c.role != "system"]
        req_asst_end = any(
            roles_non_sys[i] == "user" and any(r == "assistant" for r in roles_non_sys[i:])
            for i in range(len(roles_non_sys))
        )
        ok, sanitized, reason = UniversalDataRemediator.validate_and_sanitize_format(
            chat_objs,
            default_system_prompt=default_system_prompt,
            inject_empty_thinking=inject_thinking,
            require_assistant_ending=req_asst_end,
        )
        if not ok:
            return False, [], reason

        out_dicts: List[Dict[str, Any]] = []
        for m in sanitized:
            entry: Dict[str, Any] = {"role": m.role, "content": m.content}
            if inject_thinking and m.role == "assistant":
                entry["thinking"] = m.thinking or ""
            out_dicts.append(entry)
        return True, out_dicts, ""

    @staticmethod
    def rewrite_benchmark_holdout_for_samples(
        dataset_file_paths: List[str],
        sample_ids: Set[str],
    ) -> int:
        """
        将指定评测集 JSONL 文件中来源于 sample_ids 的留出题 (is_holdout=True) 原子改写为普通题 (is_holdout=False)。
        不触碰战绩流水表，保持连胜战绩完整。
        """
        if not dataset_file_paths or not sample_ids:
            return 0
        rewritten = 0
        prefixed = {f"flywheel_{sid}" for sid in sample_ids}
        seen_paths: Set[str] = set()
        for fpath in dataset_file_paths:
            if not fpath or fpath in seen_paths or not os.path.exists(fpath):
                continue
            seen_paths.add(fpath)
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

    def judge_dialogue(
        self,
        rule: UniversalRuleSchema,
        scenario: PersonaDefinition,
        dialogue: List[ChatMessage],
    ) -> InspectionReport:
        """
        只检不修：统一通过白盒裁判引擎评估给定对话，绝对不触发自动修复。
        """
        if self.judge_engine is None:
            raise RuntimeError("未提供质检法官模型适配器 (judge_adapter)，无法执行白盒质检")
        return self.judge_engine.evaluate_dialogue(rule, scenario, dialogue)

    @staticmethod
    def _messages_to_dicts(messages: List[ChatMessage]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for msg in messages:
            d: Dict[str, Any] = {"role": msg.role, "content": msg.content}
            if msg.thinking:
                d["thinking"] = msg.thinking
            out.append(d)
        return out

    @staticmethod
    def compute_dialogue_user_hashes(
        raw_messages: List[Any],
        remediated_messages: Optional[List[Any]] = None,
    ) -> Tuple[str, Optional[str]]:
        """计算原对话与修复后对话的客户输入序列双哈希。"""
        return DatasetDeduplicator.compute_dialogue_user_hashes(
            raw_messages=raw_messages,
            remediated_messages=remediated_messages,
        )

    @staticmethod
    def extract_sample_dedup_meta(
        initial_eval: Optional[Dict[str, Any]],
        verified_eval: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """提取用于训练集融合轨迹聚类的元数据（优先非空且含结构化轨迹/FAQ的 verified_eval，回退 initial_eval）。"""
        def _has_structured(ev: Optional[Dict[str, Any]]) -> bool:
            if not isinstance(ev, dict) or not ev:
                return False
            return bool(
                ev.get("trajectory_details")
                or ev.get("faq_details")
                or ev.get("node_trajectory")
            )

        if _has_structured(verified_eval):
            src: Dict[str, Any] = verified_eval or {}
        elif _has_structured(initial_eval):
            src = initial_eval or {}
        elif isinstance(verified_eval, dict) and verified_eval:
            src = verified_eval
        elif isinstance(initial_eval, dict) and initial_eval:
            src = initial_eval
        else:
            src = {}

        return {
            "trajectory_details": list(src.get("trajectory_details") or []),
            "faq_details": list(src.get("faq_details") or []),
            "node_trajectory": list(src.get("node_trajectory") or []),
        }

    @staticmethod
    def get_dedup_cache_path(dataset_file_path: str) -> str:
        """获取数据集对应的 .dedup_cache.json 旁路缓存路径。"""
        return DatasetDeduplicator.get_cache_path(dataset_file_path)

    @staticmethod
    def deduplicate_benchmark_export(
        candidate_rows: List[Dict[str, Any]],
        existing_rows: Optional[List[Dict[str, Any]]] = None,
        config: Optional[DedupConfig] = None,
        cache_path: Optional[str] = None,
        dataset_file_path: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """封装评测集导出四步去重防稀释编排。"""
        eff_cache: Optional[str] = cache_path or (
            DatasetDeduplicator.get_cache_path(dataset_file_path)
            if dataset_file_path
            else None
        )
        return DatasetDeduplicator().deduplicate_benchmark_rows(
            candidate_rows=candidate_rows,
            existing_rows=existing_rows,
            config=config,
            cache_path=eff_cache,
        )

    @staticmethod
    def deduplicate_training_export(
        candidate_rows: List[Dict[str, Any]],
        existing_rows: Optional[List[Dict[str, Any]]] = None,
        config: Optional[DedupConfig] = None,
        cache_path: Optional[str] = None,
        dataset_file_path: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """封装训练集导出四步去重防稀释编排。"""
        eff_cache: Optional[str] = cache_path or (
            DatasetDeduplicator.get_cache_path(dataset_file_path)
            if dataset_file_path
            else None
        )
        return DatasetDeduplicator().deduplicate_training_rows(
            candidate_rows=candidate_rows,
            existing_rows=existing_rows,
            config=config,
            cache_path=eff_cache,
        )

    @staticmethod
    def ensure_sample_slices(
        sample_id: str,
        existing_slices: Optional[List[Dict[str, Any]]],
        raw_messages: List[ChatMessage],
        applied_actions: Optional[List[Dict[str, Any]]] = None,
        initial_eval_deductions: Optional[List[Dict[str, Any]]] = None,
        rule: Optional[UniversalRuleSchema] = None,
        scenario_id: Optional[str] = None,
        remediated_messages: Optional[List[ChatMessage]] = None,
        trajectory_details: Optional[List[Dict[str, Any]]] = None,
        faq_details: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """
        统一补切题入口：
        若已有持久化切片则直接复用并标记 generated=False；
        否则调用 ProgressiveContextSlicer.slice_dialogue 生成并返回 (slices_dicts, True)。
        """
        if existing_slices:
            return existing_slices, False

        scenario: Optional[PersonaDefinition] = (
            next((s for s in rule.scenarios if s.id == scenario_id), None)
            if rule and scenario_id
            else None
        )
        slices: List[FlywheelBenchmarkSlice] = ProgressiveContextSlicer.slice_dialogue(
            sample_id=sample_id,
            raw_dialogue=raw_messages,
            actions=applied_actions or [],
            deductions=initial_eval_deductions or [],
            rule=rule,
            scenario=scenario,
            remediated_dialogue=remediated_messages,
            trajectory_details=trajectory_details,
            faq_details=faq_details,
        )
        return [s.model_dump() for s in slices], True

    @staticmethod
    def sync_slices_to_dialogue(
        slices: List[FlywheelBenchmarkSlice],
        raw_messages: List[ChatMessage],
        remediated_messages: List[ChatMessage],
        existing_actions: Optional[List[Dict[str, Any]]] = None,
        rule: Optional[UniversalRuleSchema] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[List[ChatMessage]]]:
        """
        切片双向同步重放：
        将人工微调后的切片 reference 同步写入 applied_actions，
        基于原始序号重放全部 prune/replace 处方单并执行 SFT 格式规范校验。
        返回: (updated_actions, sanitized_remediated_messages 或 None)
        """
        actions: List[Dict[str, Any]] = [
            dict(act) if isinstance(act, dict) else act
            for act in (existing_actions or [])
        ]
        had_saved_actions = len(actions) > 0

        slice_actions: List[Dict[str, Any]] = []
        for sl in slices:
            if not sl.reference or not sl.reference.strip():
                continue
            ref_text = sl.reference.strip()
            slice_actions.append({
                "tool": "replace",
                "turn": sl.target_turn,
                "text": ref_text,
                "reason": "人工微调切片同步",
            })

            matched_action: Optional[Dict[str, Any]] = None
            for act in actions:
                if not isinstance(act, dict):
                    continue
                tool = act.get("tool") or act.get("action")
                if tool in ("replace", "patch", "patch_assistant"):
                    act_turn = UniversalDataRemediator.parse_turn_int(
                        act.get("turn") or act.get("turn_index")
                    )
                    if act_turn == sl.target_turn:
                        matched_action = act
                        break

            if matched_action is not None:
                matched_action["text"] = ref_text
                if "assistant_text" in matched_action:
                    matched_action["assistant_text"] = ref_text
                if "content" in matched_action:
                    matched_action["content"] = ref_text
            else:
                actions.append({
                    "tool": "replace",
                    "turn": sl.target_turn,
                    "text": ref_text,
                    "reason": "人工微调切片同步",
                })

        # 若有历史 actions 则基于 raw_messages 全量重放（保留 prune 与未编辑 replace）；兼容无 actions 的存量数据
        raw_non_sys_len = sum(1 for m in raw_messages if m.role != "system")
        remed_non_sys_len = sum(1 for m in remediated_messages if m.role != "system")
        if not had_saved_actions and remediated_messages and raw_non_sys_len == remed_non_sys_len:
            replayed_msgs, _ = UniversalDataRemediator.apply_remediation_actions(
                remediated_messages, slice_actions
            )
        else:
            replayed_msgs, _ = UniversalDataRemediator.apply_remediation_actions(
                raw_messages, actions
            )

        default_sys = (
            rule.bot_config.system_prompt
            if (rule and rule.bot_config)
            else "你是专业的业务对话机器人。"
        )
        sys_m = next((m for m in raw_messages if m.role == "system"), None)
        if sys_m and sys_m.content.strip() and (not replayed_msgs or replayed_msgs[0].role != "system"):
            replayed_msgs = [ChatMessage(role="system", content=sys_m.content)] + replayed_msgs

        inject_thinking = (
            rule.remediation_config.inject_empty_thinking
            if (rule and rule.remediation_config)
            else False
        )
        require_asst_end = (
            rule.remediation_config.require_assistant_ending
            if (rule and rule.remediation_config)
            else True
        )
        ok, sanitized, _ = UniversalDataRemediator.validate_and_sanitize_format(
            replayed_msgs,
            default_system_prompt=default_sys,
            inject_empty_thinking=inject_thinking,
            require_assistant_ending=require_asst_end,
        )
        return actions, (sanitized if ok else None)

    def process_single_dialogue_record(
        self,
        rule: UniversalRuleSchema,
        scenario: PersonaDefinition,
        raw_messages: List[ChatMessage],
        sample_id: str,
        precomputed_initial_eval: Optional[InspectionReport] = None,
        auto_remediate: bool = True,
        skip_if_gold_pass: bool = True,
        run_verified_judge: bool = True,
    ) -> FlywheelRecord:
        """
        处理单通会话的飞轮治理生命周期:
        初次质检(支持复用外部预计算初检报告) -> 达标直通 GOLD_PASS (受 skip_if_gold_pass 控制);
        若未直通:
          - auto_remediate=False: 跳过修复与切题，直接落为 PENDING (已补齐首轮 system);
          - auto_remediate=True: AI 修复 (1次) -> 可选二次质检验收 (run_verified_judge=True 时判定 REPAIRED_PASS/DISCARDED，False 时落为 PENDING) -> 渐进切题。
        """
        now = int(time.time())
        standard_raw: List[ChatMessage] = list(raw_messages)
        if not standard_raw or standard_raw[0].role != "system":
            standard_raw = [
                ChatMessage(role="system", content=rule.bot_config.system_prompt)
            ] + standard_raw

        record = FlywheelRecord(
            id=sample_id,
            rule_id=rule.rule_id,
            scenario_id=scenario.id,
            status="PENDING",
            raw_messages=standard_raw,
            created_at=now,
            updated_at=now,
        )

        threshold = rule.inspection_rules.pass_score_threshold
        notices: List[str] = []

        # 1. 白盒初检（复用外部已生成的初检报告，避免重复调用模型）
        if precomputed_initial_eval is not None:
            initial_eval = precomputed_initial_eval
        else:
            if self.judge_engine is None:
                raise RuntimeError("未提供质检法官模型适配器 (judge_adapter)，无法执行白盒初检")
            initial_eval = self.judge_engine.evaluate_dialogue(rule, scenario, raw_messages)
            if initial_eval.notices:
                notices.extend(initial_eval.notices)
        record.initial_eval = initial_eval

        # 2. 判断初检是否直接满分直通 (手动修复时默认 skip_if_gold_pass=False，不因满分跳过修复)
        if skip_if_gold_pass and self.is_gold_pass(initial_eval, threshold):
            # 格式清洗后直接入库
            _, sanitized, _ = self.remediator.validate_and_sanitize_format(
                raw_messages,
                default_system_prompt=rule.bot_config.system_prompt,
                inject_empty_thinking=rule.remediation_config.inject_empty_thinking,
                require_assistant_ending=rule.remediation_config.require_assistant_ending,
            )
            record.remediated_messages = sanitized
            record.status = "GOLD_PASS"
            record.verified_eval = initial_eval
            record.notices = notices
            return record

        # 3. 仅初检模式且未直通：直接落为 PENDING 等待后续复核或定点补考
        if not auto_remediate:
            record.status = "PENDING"
            record.notices = notices
            return record

        # 3.5 防护守门：若质检本身异常截断，或没有任何扣分项/实质缺陷（仅拓扑参考提示），禁止送入修复模型
        remediable, skip_reason = self.check_remediable_eval(initial_eval)
        if not remediable:
            record.status = "PENDING"
            record.skipped_remediation = True
            record.skip_reason = skip_reason
            record.notices = notices
            return record

        # 4. 存在缺陷，启动 AI 智能重塑修复 (单次)
        remediated_msgs, applied_actions, remed_report = self.remediator.remediate_dialogue_with_actions(
            rule, scenario, raw_messages, initial_eval
        )
        record.remediated_messages = remediated_msgs
        record.applied_actions = applied_actions
        if isinstance(remed_report, dict):
            if remed_report.get("remediation_debug"):
                record.remediation_debug = remed_report["remediation_debug"]
            if remed_report.get("notices"):
                notices.extend(remed_report["notices"])

        # 5. 二次质检验收 (可选：手动修复默认不自动继续质检，直接落为 PENDING)
        if run_verified_judge:
            if self.judge_engine is None:
                raise RuntimeError("未提供质检法官模型适配器 (judge_adapter)，无法执行二次质检验收")
            second_eval = self.judge_engine.evaluate_dialogue(rule, scenario, remediated_msgs)
            record.verified_eval = second_eval
            if second_eval.notices:
                notices.extend(second_eval.notices)

            if self._is_judge_error_report(second_eval):
                record.status = "PENDING"
            elif second_eval.score >= threshold and second_eval.verdict == "PASS":
                record.status = "REPAIRED_PASS"
            else:
                record.status = "DISCARDED"
        else:
            record.verified_eval = None
            record.status = "PENDING"

        record.notices = notices

        # 6. 生成靶向评测切片 (以生效的 replace 轮次为真标答，累积纠正上文)
        slices = ProgressiveContextSlicer.slice_dialogue(
            sample_id=record.id,
            raw_dialogue=raw_messages,
            actions=record.applied_actions,
            deductions=initial_eval.deductions if initial_eval else [],
            rule=rule,
            scenario=scenario,
            remediated_dialogue=record.remediated_messages,
            trajectory_details=initial_eval.trajectory_details if initial_eval else None,
            faq_details=initial_eval.faq_details if initial_eval else None,
        )
        record.benchmark_slices = slices

        return record

    def batch_process_dataset(
        self,
        rule: UniversalRuleSchema,
        input_records: List[Dict[str, Any]],
        gold_output_path: str,
        discard_output_path: str,
        workers: int = 4,
        on_record_done: Optional[Callable[[FlywheelRecord, List[ChatMessage]], Optional[bool]]] = None,
        dedup_config: Optional[DedupConfig] = None,
    ) -> Dict[str, Any]:
        """
        批量并发治理流水线，支持断点续跑、同步回流样本池与训练集去重防稀释落盘
        """
        os.makedirs(os.path.dirname(os.path.abspath(gold_output_path)), exist_ok=True)
        os.makedirs(os.path.dirname(os.path.abspath(discard_output_path)), exist_ok=True)

        # 读取已有黄金文件作为增量去重基底（支持断点续跑与防稀释）
        preexisting_gold_rows: List[Dict[str, Any]] = []
        if os.path.exists(gold_output_path):
            with open(gold_output_path, "r", encoding="utf-8") as gf:
                for line in gf:
                    s_line = line.strip()
                    if s_line:
                        try:
                            obj = json.loads(s_line)
                            if isinstance(obj, dict):
                                preexisting_gold_rows.append(obj)
                        except Exception:
                            pass

        # 提取已处理 ID
        processed_ids = self._get_processed_ids([gold_output_path, discard_output_path])

        todo_items: List[Tuple[str, PersonaDefinition, List[ChatMessage]]] = []
        skipped_count = 0
        for idx, item in enumerate(input_records):
            sid, _, matched_sc, chat_msgs = self._parse_dialogue_record_item(rule, item, idx)
            if sid in processed_ids:
                skipped_count += 1
                continue
            todo_items.append((sid, matched_sc, chat_msgs))

        stats: Dict[str, Any] = {
            "total": len(input_records),
            "skipped": skipped_count,
            "gold": 0,
            "discarded": 0,
            "judge_error": 0,
            "excluded_holdout": 0,
            "failed_persist": 0,
            "final_gold_count": len(preexisting_gold_rows),
        }
        new_gold_rows: List[Dict[str, Any]] = []

        def _worker_task(
            sid: str, sc: PersonaDefinition, msgs: List[ChatMessage]
        ) -> Tuple[FlywheelRecord, List[ChatMessage]]:
            rec: FlywheelRecord = self.process_single_dialogue_record(rule, sc, msgs, sample_id=sid)
            return rec, msgs

        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            future_to_id = {
                executor.submit(_worker_task, sid, sc, msgs): sid
                for sid, sc, msgs in todo_items
            }
            for future in as_completed(future_to_id):
                sid = future_to_id[future]
                try:
                    rec, orig_msgs = future.result()
                except Exception as e:
                    print(f"⚠️ [FlywheelPipeline] 样本 {sid} 治理推理异常: {e}", file=sys.stderr)
                    stats["judge_error"] += 1
                    continue

                if self._is_record_judge_error(rec):
                    print(
                        f"⚠️ [FlywheelPipeline] 样本 {sid} 质检裁判异常，已跳过落盘以便续跑重试",
                        file=sys.stderr,
                    )
                    stats["judge_error"] += 1
                    continue

                is_holdout_source: bool = False
                if on_record_done is not None:
                    try:
                        is_holdout_source = bool(on_record_done(rec, orig_msgs))
                    except Exception as cb_err:
                        print(
                            f"⚠️ [FlywheelPipeline] 样本 {sid} 持久化/留出核查异常，已阻断写入黄金集: {cb_err}",
                            file=sys.stderr,
                        )
                        stats["failed_persist"] += 1
                        continue

                if rec.status in ("GOLD_PASS", "REPAIRED_PASS"):
                    if is_holdout_source:
                        stats["excluded_holdout"] += 1
                        continue
                    gold_row: Dict[str, Any] = self._build_output_row(rec)
                    eval_meta: Dict[str, Any] = self.extract_sample_dedup_meta(
                        rec.initial_eval.model_dump() if rec.initial_eval else None,
                        rec.verified_eval.model_dump() if rec.verified_eval else None,
                    )
                    gold_row["_dedup_meta"] = eval_meta
                    new_gold_rows.append(gold_row)
                    with self._lock:
                        self._append_row_unlocked(gold_output_path, self._strip_internal_meta(gold_row))
                    stats["gold"] += 1
                else:
                    self._flush_discarded_record(rec, discard_output_path)
                    stats["discarded"] += 1

        if new_gold_rows:
            final_rows, dedup_stats = self.deduplicate_training_export(
                candidate_rows=new_gold_rows,
                existing_rows=preexisting_gold_rows if preexisting_gold_rows else None,
                config=dedup_config,
                dataset_file_path=gold_output_path,
            )
            tmp_gold: str = f"{gold_output_path}.tmp.{os.getpid()}.{threading.get_ident()}"
            with self._lock:
                try:
                    with open(tmp_gold, "w", encoding="utf-8") as gf_out:
                        for row in final_rows:
                            gf_out.write(json.dumps(row, ensure_ascii=False) + "\n")
                    os.replace(tmp_gold, gold_output_path)
                except Exception:
                    if os.path.exists(tmp_gold):
                        try:
                            os.remove(tmp_gold)
                        except Exception:
                            pass
                    raise
            if not preexisting_gold_rows and not bool(dedup_stats.get("enabled")):
                stale_cache: str = self.get_dedup_cache_path(gold_output_path)
                if os.path.exists(stale_cache):
                    try:
                        os.remove(stale_cache)
                    except Exception:
                        pass
            stats["final_gold_count"] = len(final_rows)
            stats["dedup_stats"] = dedup_stats

        return stats

    def _get_processed_ids(self, file_paths: List[str]) -> Set[str]:
        processed = set()
        for fpath in file_paths:
            if os.path.exists(fpath):
                with open(fpath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                d = json.loads(line)
                                if "id" in d:
                                    processed.add(str(d["id"]))
                                elif "metadata" in d and "scenario_id" in d["metadata"]:
                                    processed.add(str(d["metadata"]["scenario_id"]))
                            except Exception:
                                pass
        return processed

    def _build_output_row(self, record: FlywheelRecord) -> Dict[str, Any]:
        is_remed = bool(record.remediated_messages and record.status != "GOLD_PASS")
        target_msgs: List[ChatMessage] = (
            record.remediated_messages if (is_remed and record.remediated_messages) else record.raw_messages
        )
        return {
            "id": record.id,
            "metadata": {
                "source_sample_id": record.id,
                "active_version": "remediated" if is_remed else "raw",
                "rule_id": record.rule_id,
                "scenario_id": record.scenario_id,
                "status": record.status,
                "initial_score": record.initial_eval.score if record.initial_eval else None,
                "final_score": record.verified_eval.score if record.verified_eval else None,
                "detected_defects": record.verified_eval.detected_defects if record.verified_eval else [],
                "updated_at": record.updated_at,
            },
            "applied_actions": record.applied_actions,
            "benchmark_slices": [s.model_dump() for s in record.benchmark_slices],
            "messages": self._messages_to_dicts(target_msgs),
        }

    @staticmethod
    def _strip_internal_meta(row: Dict[str, Any]) -> Dict[str, Any]:
        if "_dedup_meta" not in row:
            return row
        cleaned: Dict[str, Any] = dict(row)
        cleaned.pop("_dedup_meta", None)
        return cleaned

    @staticmethod
    def _append_row_unlocked(target_path: str, out_obj: Dict[str, Any]) -> None:
        with open(target_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(out_obj, ensure_ascii=False) + "\n")
            f.flush()

    def _flush_discarded_record(self, record: FlywheelRecord, discard_path: str) -> None:
        out_obj = self._build_output_row(record)
        meta = out_obj.setdefault("metadata", {})
        meta["status"] = "DISCARDED"
        if record.skip_reason:
            meta["skip_reason"] = record.skip_reason
        with self._lock:
            self._append_row_unlocked(discard_path, out_obj)

    def _flush_record(self, record: FlywheelRecord, gold_path: str, discard_path: str) -> None:
        target_path = gold_path if record.status in ("GOLD_PASS", "REPAIRED_PASS") else discard_path
        out_obj = self._build_output_row(record)
        with self._lock:
            self._append_row_unlocked(target_path, out_obj)

    @staticmethod
    def _parse_dialogue_record_item(
        rule: UniversalRuleSchema,
        item: Dict[str, Any],
        idx: int,
    ) -> Tuple[str, str, PersonaDefinition, List[ChatMessage]]:
        """解析单条对话数据集样本，兼容顶层及 metadata 中的 scenario_id。"""
        sid = str(item.get("id") or item.get("original_idx") or f"row_{idx}")
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        scenario_id = str(item.get("scenario_id") or meta.get("scenario_id") or "default")
        matched_sc = next(
            (s for s in rule.scenarios if s.id == scenario_id),
            PersonaDefinition(id=scenario_id, name="导入会话场景", instruction="保持原对话意图"),
        )
        raw_msg_dicts = item.get("messages") or []
        chat_msgs = [
            ChatMessage(
                role=m.get("role", "user"),
                content=m.get("content", ""),
                thinking=m.get("thinking", ""),
            )
            for m in raw_msg_dicts
            if isinstance(m, dict)
        ]
        return sid, scenario_id, matched_sc, chat_msgs

    @staticmethod
    def _build_inspect_report_snapshot(
        *,
        task_id: str,
        dataset_id: Optional[str],
        dataset_path: Optional[str],
        rule: UniversalRuleSchema,
        judge_model_id: Optional[str],
        total_target_samples: int,
        ordered_samples: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """聚合当前已完成样本的质检打分汇总看板与明细报告。"""
        pass_count = sum(1 for s in ordered_samples if s.get("verdict") == "PASS")
        fail_count = sum(1 for s in ordered_samples if s.get("verdict") == "FAIL")
        error_count = sum(1 for s in ordered_samples if s.get("verdict") == "ERROR")
        valid_samples = [s for s in ordered_samples if s.get("verdict") in ("PASS", "FAIL")]
        valid_count = len(valid_samples)

        scores = [
            int(s["score"])
            for s in valid_samples
            if s.get("score") is not None
        ]
        topo_pass_count = sum(1 for s in valid_samples if s.get("topology_valid") is True)

        deduction_agg: Dict[str, Dict[str, Any]] = {}
        for s in valid_samples:
            for d in (s.get("deductions") or []):
                if not isinstance(d, dict):
                    continue
                item_name = str(d.get("item") or "未命名扣分项").strip() or "未命名扣分项"
                try:
                    pts = int(round(float(d.get("points") or 0)))
                except Exception:
                    pts = 0
                bucket = deduction_agg.setdefault(
                    item_name, {"item": item_name, "count": 0, "total_points": 0}
                )
                bucket["count"] += 1
                bucket["total_points"] += max(0, pts)

        top_deductions = sorted(
            deduction_agg.values(),
            key=lambda x: (-x["count"], -x["total_points"], x["item"]),
        )[:15]

        summary = {
            "total_samples": total_target_samples,
            "completed_samples": len(ordered_samples),
            "pass_count": pass_count,
            "fail_count": fail_count,
            "error_count": error_count,
            "valid_count": valid_count,
            "pass_rate": round(pass_count / valid_count, 4) if valid_count > 0 else 0.0,
            "avg_score": round(sum(scores) / len(scores), 2) if scores else 0.0,
            "min_score": min(scores) if scores else None,
            "max_score": max(scores) if scores else None,
            "topology_pass_rate": round(topo_pass_count / valid_count, 4) if valid_count > 0 else 0.0,
            "top_deductions": top_deductions,
        }
        return {
            "task_id": task_id,
            "dataset_id": dataset_id,
            "dataset_path": dataset_path,
            "rule_id": rule.rule_id,
            "rule_name": rule.project_name,
            "judge_model_id": judge_model_id,
            "pass_score_threshold": rule.inspection_rules.pass_score_threshold,
            "summary": summary,
            "samples": ordered_samples,
        }

    def batch_inspect_dataset(
        self,
        rule: UniversalRuleSchema,
        input_records: List[Dict[str, Any]],
        task_id: str,
        dataset_id: Optional[str] = None,
        dataset_path: Optional[str] = None,
        judge_model_id: Optional[str] = None,
        workers: int = 4,
        limit: Optional[int] = None,
        output_path: Optional[str] = None,
        on_sample_done: Optional[
            Callable[[Dict[str, Any], List[ChatMessage], Optional[InspectionReport], int, int], None]
        ] = None,
    ) -> Dict[str, Any]:
        """
        对纯对话数据集执行批量规则质检打分（只检不修）:
        - 并发调用 UniversalJudgeEngine.evaluate_dialogue 执行 SOP 拓扑 + 红线 ∥ FAQ 双轨质检；
        - 识别并隔离裁判异常样本（不计入 pass_rate / avg_score 分母）；
        - 支持步进原子落盘至 output_path，并通过 on_sample_done 回调进度与可选飞轮入库。
        """
        if self.judge_engine is None:
            raise RuntimeError("未提供质检法官模型适配器 (judge_adapter)，无法执行批量规则质检")

        target_records = input_records[:limit] if limit and limit > 0 else input_records
        total = len(target_records)
        parsed_items = [
            (idx, *self._parse_dialogue_record_item(rule, item, idx))
            for idx, item in enumerate(target_records)
        ]

        results_by_idx: Dict[int, Dict[str, Any]] = {}
        completed = 0

        if output_path:
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        def _flush_snapshot(ordered: List[Dict[str, Any]]) -> Dict[str, Any]:
            snapshot = self._build_inspect_report_snapshot(
                task_id=task_id,
                dataset_id=dataset_id,
                dataset_path=dataset_path,
                rule=rule,
                judge_model_id=judge_model_id,
                total_target_samples=total,
                ordered_samples=ordered,
            )
            if output_path:
                tmp_path = f"{output_path}.tmp.{os.getpid()}.{threading.get_ident()}"
                try:
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        json.dump(snapshot, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, output_path)
                except Exception:
                    if os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass
                    raise
            return snapshot

        # 初始化骨架文件，确保轮询期间立即可读
        if output_path:
            with self._lock:
                _flush_snapshot([])

        def _inspect_one(
            idx: int,
            sid: str,
            scenario_id: str,
            sc: PersonaDefinition,
            msgs: List[ChatMessage],
        ) -> Tuple[int, Dict[str, Any], List[ChatMessage], Optional[InspectionReport]]:
            turn_count = sum(1 for m in msgs if m.role != "system")
            if not msgs or turn_count == 0:
                entry = {
                    "sample_id": sid,
                    "scenario_id": scenario_id,
                    "turn_count": 0,
                    "score": None,
                    "verdict": "ERROR",
                    "topology_valid": False,
                    "topology_error": "样本缺少有效对话消息",
                    "node_trajectory": [],
                    "deductions": [],
                    "detected_defects": ["样本缺少有效对话消息"],
                    "expert_comments": "样本 messages 为空或仅包含 system 消息，无法执行对话质检。",
                }
                return idx, entry, msgs, None

            try:
                rep = self.judge_engine.evaluate_dialogue(rule, sc, msgs)
                is_err = self._is_judge_error_report(rep)
                entry = {
                    "sample_id": sid,
                    "scenario_id": scenario_id,
                    "turn_count": turn_count,
                    "score": None if is_err else int(rep.score),
                    "verdict": "ERROR" if is_err else rep.verdict,
                    "topology_valid": False if is_err else bool(rep.topology_valid),
                    "topology_error": rep.topology_error,
                    "node_trajectory": list(rep.node_trajectory or []),
                    "deductions": list(rep.deductions or []),
                    "detected_defects": list(rep.detected_defects or []),
                    "expert_comments": rep.expert_comments or "",
                }
                return idx, entry, msgs, rep
            except Exception as exc:
                entry = {
                    "sample_id": sid,
                    "scenario_id": scenario_id,
                    "turn_count": turn_count,
                    "score": None,
                    "verdict": "ERROR",
                    "topology_valid": False,
                    "topology_error": str(exc),
                    "node_trajectory": [],
                    "deductions": [],
                    "detected_defects": [f"质检执行异常: {exc}"],
                    "expert_comments": f"质检执行异常: {exc}",
                }
                return idx, entry, msgs, None

        if total == 0:
            return _flush_snapshot([])

        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = [
                executor.submit(_inspect_one, idx, sid, sc_id, sc, msgs)
                for idx, sid, sc_id, sc, msgs in parsed_items
            ]
            for fut in as_completed(futures):
                idx, entry, msgs, rep = fut.result()
                with self._lock:
                    results_by_idx[idx] = entry
                    completed += 1
                    ordered = [results_by_idx[i] for i in sorted(results_by_idx.keys())]
                    _flush_snapshot(ordered)
                if on_sample_done is not None:
                    on_sample_done(entry, msgs, rep, completed, total)

        final_ordered = [results_by_idx[i] for i in sorted(results_by_idx.keys())]
        return _flush_snapshot(final_ordered)
