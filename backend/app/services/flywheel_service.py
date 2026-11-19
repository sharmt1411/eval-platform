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
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Set, Tuple
from backend.app.schemas.chat import ChatMessage, ModelConfig
from backend.app.schemas.rule import UniversalRuleSchema, PersonaDefinition
from backend.app.schemas.flywheel import (
    InspectionReport,
    FlywheelRecord,
    FlywheelBenchmarkSlice,
)
from backend.app.adapters.base import BaseModelAdapter
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
        if has_main_judge_error or has_error_defect:
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

    @staticmethod
    def resolve_rejudge_status(
        eval_report: InspectionReport,
        threshold: int,
        target_data: str = "raw",
        current_status: str = "PENDING",
    ) -> str:
        """根据重检目标 (raw / remediated) 与评估报告判定治理后的样本状态。"""
        if target_data == "remediated":
            is_pass = bool(
                eval_report.score >= threshold and eval_report.verdict == "PASS"
            )
            return "REPAIRED_PASS" if is_pass else "DISCARDED"
        if FlywheelPipelineService.is_gold_pass(eval_report, threshold):
            return "GOLD_PASS"
        return "DISCARDED" if current_status == "DISCARDED" else "PENDING"

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
    def ensure_sample_slices(
        sample_id: str,
        existing_slices: Optional[List[Dict[str, Any]]],
        raw_messages: List[ChatMessage],
        applied_actions: Optional[List[Dict[str, Any]]] = None,
        initial_eval_deductions: Optional[List[Dict[str, Any]]] = None,
        rule: Optional[UniversalRuleSchema] = None,
        scenario_id: Optional[str] = None,
        remediated_messages: Optional[List[ChatMessage]] = None,
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

            if second_eval.score >= threshold and second_eval.verdict == "PASS":
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
    ) -> Dict[str, int]:
        """
        批量并发治理流水线，支持断点续跑与边跑边落盘
        """
        os.makedirs(os.path.dirname(os.path.abspath(gold_output_path)), exist_ok=True)
        os.makedirs(os.path.dirname(os.path.abspath(discard_output_path)), exist_ok=True)

        # 提取已处理 ID
        processed_ids = self._get_processed_ids([gold_output_path, discard_output_path])

        todo_items = []
        for idx, item in enumerate(input_records):
            sid = str(item.get("id") or item.get("original_idx") or f"row_{idx}")
            if sid in processed_ids:
                continue

            # 场景定义查找
            scenario_id = item.get("scenario_id") or "default"
            matched_sc = next(
                (s for s in rule.scenarios if s.id == scenario_id),
                PersonaDefinition(id=scenario_id, name="导入会话场景", instruction="保持原对话意图"),
            )

            # 解析 messages
            raw_msg_dicts = item.get("messages", [])
            chat_msgs = [
                ChatMessage(
                    role=m.get("role", "user"),
                    content=m.get("content", ""),
                    thinking=m.get("thinking", ""),
                )
                for m in raw_msg_dicts
            ]

            todo_items.append((sid, matched_sc, chat_msgs))

        stats = {"total": len(input_records), "skipped": len(processed_ids), "gold": 0, "discarded": 0}

        def _worker_task(sid, sc, msgs):
            rec = self.process_single_dialogue_record(rule, sc, msgs, sample_id=sid)
            # 安全落盘
            self._flush_record(rec, gold_output_path, discard_output_path)
            return rec.status

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_id = {
                executor.submit(_worker_task, sid, sc, msgs): sid
                for sid, sc, msgs in todo_items
            }
            for future in as_completed(future_to_id):
                sid = future_to_id[future]
                try:
                    status = future.result()
                    if status in ("GOLD_PASS", "REPAIRED_PASS"):
                        stats["gold"] += 1
                    else:
                        stats["discarded"] += 1
                except Exception as e:
                    print(f"⚠️ [FlywheelPipeline] 样本 {sid} 处理异常: {e}")

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

    def _flush_record(self, record: FlywheelRecord, gold_path: str, discard_path: str):
        target_path = gold_path if record.status in ("GOLD_PASS", "REPAIRED_PASS") else discard_path

        out_obj = {
            "id": record.id,
            "metadata": {
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
            "messages": [
                m.to_dict()
                for m in (record.remediated_messages or record.raw_messages)
            ],
        }

        with self._lock:
            with open(target_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(out_obj, ensure_ascii=False) + "\n")
                f.flush()
