# -*- coding: utf-8 -*-
"""
通用质检与 Rubric 裁判引擎 (Universal Judge Engine)
支持:
1. 对话机器人 SOP 状态机 + 白盒红线质检模式 (联动 TopologyValidator 确定性防手软)
2. 常规 Benchmark 参考答案比对与 Rubric 打分模式
"""
import json
import re
from typing import List, Dict, Any, Optional
from backend.app.adapters.base import BaseModelAdapter, build_downgraded_retry_config
from backend.app.engine.topology import UniversalTopologyValidator
from backend.app.schemas.chat import (
    ChatMessage,
    ModelConfig,
    UnifiedChatRequest,
    UnifiedChatResponse,
)
from backend.app.schemas.rule import (
    UniversalRuleSchema,
    PersonaDefinition,
    DEFAULT_TRAJECTORY_EXTRACT_PROMPT_TEMPLATE,
    DEFAULT_SOP_DYNAMICS_BLOCK,
    DEFAULT_JUDGE_PROMPT_TEMPLATE,
    DEFAULT_PLAIN_JUDGE_PROMPT_TEMPLATE,
    DEFAULT_FAQ_EXTRACT_PROMPT_TEMPLATE,
    DEFAULT_FAQ_JUDGE_PROMPT_TEMPLATE,
    DEFAULT_BENCHMARK_JUDGE_PROMPT_TEMPLATE,
    PROMPT_USER_SPLIT_MARKER,
    split_prompt_into_system_and_user,
)
from backend.app.engine.prompt_registry import get_active_prompt_template, render_template_vars
from backend.app.engine.faq_retriever import UniversalFAQRetriever
from backend.app.schemas.benchmark import BenchmarkSample, RubricConfig
from backend.app.schemas.flywheel import InspectionReport


class UniversalJudgeEngine:
    """通用质检裁判引擎"""

    def __init__(
        self,
        judge_adapter: BaseModelAdapter,
        default_config: Optional[ModelConfig] = None,
    ):
        self.adapter = judge_adapter
        self.default_config = default_config

    def _build_step_config(
        self,
        temperature: float,
        max_tokens: int,
        thinking_enabled: bool,
        thinking_level: str,
        respect_model_config: bool = False,
    ) -> ModelConfig:
        """构建单步质检请求参数；主裁决步骤优先继承模型中心注册配置，防止硬编码覆盖。"""
        base = self.default_config
        if base is None:
            return ModelConfig(
                temperature=temperature,
                max_tokens=max_tokens,
                thinking_enabled=thinking_enabled,
                thinking_level=thinking_level,
            )
        if respect_model_config:
            return ModelConfig(
                temperature=base.temperature,
                top_p=base.top_p,
                max_tokens=max(base.max_tokens or 0, max_tokens),
                repetition_penalty=base.repetition_penalty,
                thinking_enabled=base.thinking_enabled,
                thinking_level=base.thinking_level,
                vertex_thinking_mode=base.vertex_thinking_mode,
                extra_body=dict(base.extra_body or {}),
            )
        return ModelConfig(
            temperature=temperature,
            top_p=base.top_p,
            max_tokens=max_tokens,
            repetition_penalty=base.repetition_penalty,
            thinking_enabled=thinking_enabled,
            thinking_level=thinking_level,
            vertex_thinking_mode=base.vertex_thinking_mode,
            extra_body=dict(base.extra_body or {}),
        )

    @staticmethod
    def _format_dialogue_with_turns(dialogue: List[ChatMessage]) -> str:
        """统一格式化纯对话文本：剔除 system 指令，按 [#轮次] 连续编号。"""
        lines = []
        turn_num = 1
        for m in dialogue:
            if m.role == "system":
                continue
            lines.append(f"[#{turn_num}] {m.role.upper()}: {m.content.strip()}")
            turn_num += 1
        return "\n".join(lines)

    @staticmethod
    def _safe_score_int(val: Any, default: int = 100) -> int:
        """安全解析整型分值并钳位至 [0, 100]"""
        try:
            if val is None or isinstance(val, bool):
                return default
            parsed = int(round(float(val)))
            return max(0, min(100, parsed))
        except Exception:
            return default

    @staticmethod
    def detect_customer_hangup(dialogue: List[ChatMessage]) -> bool:
        """
        确定性检测客户在会话结尾阶段是否明确提出了挂机、忙碌、开会或中断意图。
        用于向拓扑校验器传递 is_customer_hangup 豁免信号，防止合法早退被误判为断路。
        """
        # 提取最后 3 轮 user 消息
        user_msgs = [m.content for m in dialogue if m.role == "user"][-3:]
        hangup_keywords = [
            "挂了", "挂电话", "挂机", "开会", "在忙", "有事", "在开车", "先这样", "再见", "拜拜",
            "不说了", "打住", "别打了", "不要再打", "没空", "不方便", "正在开会", "有别的事",
            "待会儿", "回头再", "先不聊", "行了行了", "好了好了", "还要忙"
        ]
        for content in user_msgs:
            c = content.strip().lower()
            if any(kw in c for kw in hangup_keywords):
                return True
        return False

    @staticmethod
    def _build_debug_request_entry(
        step: str,
        label: str,
        req: UnifiedChatRequest,
        resp: Optional[UnifiedChatResponse] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """构建标准可复现的 API 调试请求与响应快照"""
        entry: Dict[str, Any] = {
            "step": step,
            "label": label,
            "model": req.model_id,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "config": req.config.model_dump(),
            "thinking": (getattr(resp, "thinking", "") or "") if resp is not None else "",
            "response_content": (getattr(resp, "content", "") or "") if resp is not None else "",
            "raw_output": (getattr(resp, "raw_output", "") or "") if resp is not None else "",
            "usage": resp.usage.model_dump() if (resp is not None and getattr(resp, "usage", None)) else None,
            "latency_ms": (
                round(resp.metrics.total_latency_ms, 1)
                if (resp is not None and getattr(resp, "metrics", None))
                else None
            ),
        }
        if error:
            entry["error"] = error
        return entry

    def build_trajectory_extract_messages(
        self,
        rule: UniversalRuleSchema,
        dialogue: List[ChatMessage],
        template_override: Optional[str] = None,
    ) -> List[ChatMessage]:
        """组装 SOP 拓扑轨迹检测与槽位抽取 [system, user] 消息列表"""
        dialogue_formatted = (
            self._format_dialogue_with_turns(dialogue)
            or json.dumps([m.model_dump() for m in dialogue], ensure_ascii=False, indent=2)
        )
        mermaid_sop = (
            rule.topology_sop.mermaid_graph.strip()
            if (rule.topology_sop.enabled and rule.topology_sop.mermaid_graph)
            else "无状态机流程图"
        )
        node_checkpoints_summary = {
            k: v.name or k for k, v in rule.inspection_rules.node_checkpoints.items()
        }
        tpl = (
            template_override
            or getattr(rule.topology_sop, "trajectory_extract_prompt_template", "")
            or get_active_prompt_template(
                "trajectory_extract", DEFAULT_TRAJECTORY_EXTRACT_PROMPT_TEMPLATE
            )
        )
        if not tpl or not tpl.strip():
            tpl = DEFAULT_TRAJECTORY_EXTRACT_PROMPT_TEMPLATE

        rendered = render_template_vars(
            tpl.strip(),
            {
                "mermaid_sop": mermaid_sop,
                "node_checkpoints_summary": json.dumps(
                    node_checkpoints_summary, ensure_ascii=False, indent=2
                ),
                "dialogue": dialogue_formatted,
            },
        )
        sys_part, user_part = split_prompt_into_system_and_user(
            rendered,
            fallback_user_prompt=f"## 被测实际对话记录 (Dialogue)\n<dialogue>\n{dialogue_formatted}\n</dialogue>",
        )
        return [
            ChatMessage(role="system", content=sys_part),
            ChatMessage(role="user", content=user_part),
        ]

    def extract_trajectory_and_slots(
        self,
        rule: UniversalRuleSchema,
        dialogue: List[ChatMessage],
        debug_collector: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Step 1 独立任务: 专职将多轮对话记录抽取为 Mermaid 节点轨迹与客户提前槽位
        原则:
        1. 轨迹主体针对 assistant: 助手提问只会有一个主节点 (node_id);
        2. user 回复可能顺带提供其他节点数据，记入 user_slots;
        3. 提取简单客观证据 evidence。
        """
        if not rule.topology_sop.enabled or not rule.topology_sop.mermaid_graph:
            return []

        dialogue_formatted = self._format_dialogue_with_turns(dialogue)
        if not dialogue_formatted:
            return []

        msgs = self.build_trajectory_extract_messages(rule, dialogue)

        req = UnifiedChatRequest(
            model_id=self.adapter.model_name,
            messages=msgs,
            config=self._build_step_config(
                temperature=0.0,
                max_tokens=4096,
                thinking_enabled=False,
                thinking_level="NONE",
                respect_model_config=False,
            ),
        )

        try:
            resp = self.adapter.chat(req)
            if debug_collector is not None:
                debug_collector.append(
                    self._build_debug_request_entry("trajectory_extract", "SOP 轨迹抽取", req, resp=resp)
                )
            result = self._extract_json_from_response(resp.content)
            raw_list = result.get("trajectory", [])
            if not isinstance(raw_list, list):
                return []
            return raw_list
        except Exception as e:
            if debug_collector is not None:
                debug_collector.append(
                    self._build_debug_request_entry("trajectory_extract", "SOP 轨迹抽取", req, error=str(e))
                )
            return []

    @staticmethod
    def _resolve_fallback_policy(rule: UniversalRuleSchema) -> str:
        """解析规则级 FAQ 兜底规范，纯空白时回退为内置安全默认值"""
        faq_cfg = getattr(rule, "faq_inspection", None)
        raw_policy = getattr(faq_cfg, "fallback_policy", None) if faq_cfg else None
        if isinstance(raw_policy, str) and raw_policy.strip():
            return raw_policy.strip()
        return "若问题超出知识库范围，必须委婉说明并指引联系官方售后热线或登录官方 App 查询，严禁凭空捏造政策。"

    def build_faq_extract_messages(
        self,
        rule: UniversalRuleSchema,
        dialogue: List[ChatMessage],
        scenario: Optional[PersonaDefinition] = None,
        template_override: Optional[str] = None,
    ) -> List[ChatMessage]:
        """组装 FAQ 提问轮次抽取与检索改写 [system, user] 消息列表"""
        dialogue_text = (
            self._format_dialogue_with_turns(dialogue)
            or json.dumps([m.model_dump() for m in dialogue], ensure_ascii=False, indent=2)
        )
        faq_cfg = getattr(rule, "faq_inspection", None)
        tpl = (
            template_override
            or (getattr(faq_cfg, "faq_extract_prompt_template", "") if faq_cfg else "")
            or get_active_prompt_template("faq_extract", DEFAULT_FAQ_EXTRACT_PROMPT_TEMPLATE)
        )
        if not tpl or not tpl.strip():
            tpl = DEFAULT_FAQ_EXTRACT_PROMPT_TEMPLATE

        fallback_pol = self._resolve_fallback_policy(rule)
        rendered = render_template_vars(
            tpl.strip(),
            {
                "scenario_id": scenario.id if scenario else "",
                "scenario_name": scenario.name if scenario else "",
                "fallback_policy": fallback_pol,
                "dialogue": dialogue_text,
            },
        )
        sys_part, user_part = split_prompt_into_system_and_user(
            rendered,
            fallback_user_prompt=f"## 被测实际对话记录 (Dialogue)\n<dialogue>\n{dialogue_text}\n</dialogue>",
        )
        return [
            ChatMessage(role="system", content=sys_part),
            ChatMessage(role="user", content=user_part),
        ]

    def extract_faq_rounds_and_rewrite(
        self,
        rule: UniversalRuleSchema,
        dialogue: List[ChatMessage],
        debug_collector: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Step B1: 专职提取客户插播的 FAQ 提问轮次并完成检索词改写 (Query Rewrite)
        """
        if not getattr(rule, "faq_inspection", None) or not rule.faq_inspection.enabled:
            return []
        if not rule.faq_inspection.knowledge_base:
            return []

        dialogue_text = self._format_dialogue_with_turns(dialogue)
        if not dialogue_text:
            return []

        msgs = self.build_faq_extract_messages(rule, dialogue)

        req = UnifiedChatRequest(
            model_id=self.adapter.model_name,
            messages=msgs,
            config=self._build_step_config(
                temperature=0.0,
                max_tokens=4096,
                thinking_enabled=False,
                thinking_level="NONE",
                respect_model_config=False,
            ),
        )

        try:
            resp = self.adapter.chat(req)
            if debug_collector is not None:
                debug_collector.append(
                    self._build_debug_request_entry("faq_extract", "FAQ 提问轮次抽取与改写", req, resp=resp)
                )
            result = self._extract_json_from_response(resp.content)
            raw_list = result.get("faq_rounds", [])
            if not isinstance(raw_list, list):
                return []
            return raw_list
        except Exception as e:
            if debug_collector is not None:
                debug_collector.append(
                    self._build_debug_request_entry("faq_extract", "FAQ 提问轮次抽取与改写", req, error=str(e))
                )
            print(f"⚠️ [UniversalJudgeEngine] extract_faq_rounds_and_rewrite 异常: {e}")
            return []

    def build_faq_judge_messages(
        self,
        rule: UniversalRuleSchema,
        scenario: PersonaDefinition,
        faq_round: Dict[str, Any],
        retrieved_candidates: List[Dict[str, Any]],
        dialogue: List[ChatMessage],
        template_override: Optional[str] = None,
    ) -> List[ChatMessage]:
        """组装专职 FAQ 准确度质检 [system, user] 消息列表"""
        orig_turns = [m for m in dialogue if m.role != "system"]
        turn_map = {i + 1: m for i, m in enumerate(orig_turns)}

        def _safe_int(val, default=0) -> int:
            try:
                if val is None:
                    return default
                if isinstance(val, str):
                    digits = re.findall(r"\d+", val)
                    return int(digits[0]) if digits else default
                return int(val)
            except Exception:
                return default

        u_turn = _safe_int(faq_round.get("user_turn", 0))
        a_turn = _safe_int(faq_round.get("assistant_turn", 0))

        u_msg = turn_map.get(u_turn)
        a_msg = turn_map.get(a_turn)

        u_content = u_msg.content.strip() if u_msg else str(faq_round.get("raw_user_question", ""))
        a_content = a_msg.content.strip() if a_msg else str(faq_round.get("raw_assistant_answer", ""))
        rewritten_q = str(faq_round.get("rewritten_query", u_content))

        dialogue_slice = f"[#{u_turn}] USER: {u_content}\n[#{a_turn}] ASSISTANT: {a_content}"

        candidate_lines = []
        for c in retrieved_candidates:
            faq_item = c.get("faq")
            if not faq_item:
                continue
            sc = c.get("score", 0.0)
            src = c.get("retrieval_source", "unknown")
            q_txt = faq_item.question if hasattr(faq_item, "question") else faq_item.get("question", "")
            a_txt = faq_item.answer if hasattr(faq_item, "answer") else faq_item.get("answer", "")
            cid = faq_item.id if hasattr(faq_item, "id") else faq_item.get("id", "FAQ")
            cat = (faq_item.category if hasattr(faq_item, "category") else faq_item.get("category")) or "通用"
            candidate_lines.append(
                f"- [ID: {cid}] (分类: {cat})\n"
                f"  标准问题: {q_txt}\n"
                f"  官方标准答案口径: {a_txt}\n"
                f"  (检索匹配得分: {sc}, 检索源: {src})"
            )
        candidate_faqs_str = "\n".join(candidate_lines) if candidate_lines else "【未召回匹配的知识条目】"

        faq_cfg = getattr(rule, "faq_inspection", None)
        tpl = (
            template_override
            or (getattr(faq_cfg, "faq_judge_prompt_template", "") if faq_cfg else "")
            or get_active_prompt_template("faq_judge", DEFAULT_FAQ_JUDGE_PROMPT_TEMPLATE)
        )
        if not tpl or not tpl.strip():
            tpl = DEFAULT_FAQ_JUDGE_PROMPT_TEMPLATE

        fallback_pol = self._resolve_fallback_policy(rule)
        prompt = render_template_vars(
            tpl,
            {
                "scenario_id": scenario.id,
                "scenario_name": scenario.name,
                "user_turn": str(u_turn),
                "assistant_turn": str(a_turn),
                "user_question": u_content,
                "assistant_answer": a_content,
                "rewritten_query": rewritten_q,
                "faq_dialogue_slice": dialogue_slice,
                "candidate_faqs": candidate_faqs_str,
                "fallback_policy": fallback_pol,
            },
        )

        sys_part, user_part = split_prompt_into_system_and_user(
            prompt,
            fallback_user_prompt=f"请严格依据系统指令核查以下问答轮次：\n{dialogue_slice}\n\n{candidate_faqs_str}",
        )
        return [
            ChatMessage(role="system", content=sys_part),
            ChatMessage(role="user", content=user_part),
        ]

    def evaluate_faq_accuracy(
        self,
        rule: UniversalRuleSchema,
        scenario: PersonaDefinition,
        faq_round: Dict[str, Any],
        retrieved_candidates: List[Dict[str, Any]],
        dialogue: List[ChatMessage],
        debug_collector: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Step B2: 专职 FAQ 准确度法官裁决
        """
        a_turn: int = 0
        req: Optional[UnifiedChatRequest] = None
        try:
            def _safe_int(val, default=0) -> int:
                try:
                    if val is None:
                        return default
                    if isinstance(val, str):
                        digits = re.findall(r"\d+", val)
                        return int(digits[0]) if digits else default
                    return int(val)
                except Exception:
                    return default

            a_turn = _safe_int(faq_round.get("assistant_turn", 0))
            msgs = self.build_faq_judge_messages(
                rule=rule,
                scenario=scenario,
                faq_round=faq_round,
                retrieved_candidates=retrieved_candidates,
                dialogue=dialogue,
            )

            req = UnifiedChatRequest(
                model_id=self.adapter.model_name,
                messages=msgs,
                config=self._build_step_config(
                    temperature=0.1,
                    max_tokens=10240,
                    thinking_enabled=True,
                    thinking_level="HIGH",
                    respect_model_config=True,
                ),
            )

            resp = self.adapter.chat(req)
            if debug_collector is not None:
                debug_collector.append(
                    self._build_debug_request_entry(
                        f"faq_judge_turn_{a_turn}",
                        f"FAQ 准确度质检 (#{a_turn}轮)",
                        req,
                        resp=resp,
                    )
                )
            result = self._extract_json_from_response(resp.content)
            deductions = result.get("faq_deductions", [])
            if isinstance(deductions, list):
                for d in deductions:
                    if isinstance(d, dict) and "turn" not in d:
                        d["turn"] = a_turn
                return deductions
        except Exception as e:
            print(f"⚠️ [UniversalJudgeEngine] evaluate_faq_accuracy 异常: {e}")
            if debug_collector is not None and req is not None:
                debug_collector.append(
                    self._build_debug_request_entry(
                        f"faq_judge_turn_{a_turn}",
                        f"FAQ 准确度质检 (#{a_turn}轮)",
                        req,
                        error=str(e),
                    )
                )

        return []

    def evaluate_dialogue(
        self,
        rule: UniversalRuleSchema,
        scenario: PersonaDefinition,
        dialogue: List[ChatMessage],
    ) -> InspectionReport:
        """
        模式一: 对多轮会话执行深度白盒质检
        双轨并行解耦架构:
        1. 流程轨 (Mermaid / 基础红线):
           - 若启用 Mermaid: Step A1 专职抽取轨迹 ➔ Step A2 拓扑校验 ➔ Step A3 流程与红线法官打分
           - 若未启用 Mermaid: 仅执行基础红线与通用质检打分
        2. 问答轨 (FAQ 准确度):
           - 若启用 FAQ 质检: Step B1 提取 FAQ 提问轮次 + Query Rewrite ➔ Top-K 检索 ➔ Step B2 专职法官判断
           - 若未启用 FAQ 质检: 零开销直接跳过
        3. 代码层统一汇合: 合并所有带 #turn 的扣分单 (flow_deductions + faq_deductions)，重算综合得分
        """
        has_mermaid = bool(rule.topology_sop.enabled and rule.topology_sop.mermaid_graph.strip())
        has_faq = bool(
            getattr(rule, "faq_inspection", None)
            and rule.faq_inspection.enabled
            and rule.faq_inspection.knowledge_base
        )

        clean_nodes: List[str] = []
        trajectory_details: List[Dict[str, Any]] = []
        collected_slots: set = set()
        is_topo_valid = True
        topo_err: Optional[str] = None
        debug_requests: List[Dict[str, Any]] = []

        # ------------------ 轨道一: Mermaid / 流程图与红线质检 ------------------
        if has_mermaid:
            # Step A1: 专职提取轨迹与槽位
            raw_traj_details = self.extract_trajectory_and_slots(
                rule, dialogue, debug_collector=debug_requests
            )
            validator = UniversalTopologyValidator(rule.topology_sop)

            extracted_nodes = []
            for item in raw_traj_details:
                raw_node = validator.clean_node_id(item.get("node_id", ""))
                if raw_node:
                    extracted_nodes.append(raw_node)
                    slots = [validator.clean_node_id(s) for s in item.get("user_slots", []) if s]
                    for s in slots:
                        collected_slots.add(s)
                    trajectory_details.append({
                        "turn": item.get("turn", 0),
                        "node_id": raw_node,
                        "user_slots": slots,
                        "evidence": item.get("evidence", ""),
                    })

            # Step A2: 确定性拓扑校验 (传入 is_customer_hangup 豁免终结节点检查)
            is_customer_hangup = self.detect_customer_hangup(dialogue)
            is_topo_valid, topo_err, clean_nodes = validator.validate_trajectory(
                extracted_nodes,
                collected_slots=collected_slots,
                is_customer_hangup=is_customer_hangup,
            )

        # Step A3: 流程与红线法官
        judge_messages = self.build_judge_messages(
            rule=rule,
            scenario=scenario,
            dialogue=dialogue,
            confirmed_trajectory=clean_nodes if has_mermaid else None,
            trajectory_details=trajectory_details if has_mermaid else None,
            topology_valid=is_topo_valid if has_mermaid else None,
            topology_error=topo_err if has_mermaid else None,
        )

        req = UnifiedChatRequest(
            model_id=self.adapter.model_name,
            messages=judge_messages,
            config=self._build_step_config(
                temperature=0.1,
                max_tokens=10240,
                thinking_enabled=True,
                thinking_level="HIGH",
                respect_model_config=True,
            ),
        )

        notices: List[str] = []

        try:
            resp = self.adapter.chat(req)
            parsed = self._try_parse_json_dict(resp.content)
            if parsed is not None:
                debug_requests.append(
                    self._build_debug_request_entry("main_judge", "主流程与红线质检", req, resp=resp)
                )
                eval_data = parsed
            else:
                # 首次返回非 JSON、空正文或退化重复内容：保留异常现场并通过统一降级助手纠偏重试 1 次
                retry_cfg, downgrade_notice = build_downgraded_retry_config(
                    req.config, resp, scene_label="主流程质检"
                )
                if downgrade_notice:
                    notices.append(downgrade_notice)
                attempt_1_err = (
                    downgrade_notice
                    or "模型首次回复未能解析为标准 JSON，已自动触发退避重采样"
                )
                debug_requests.append(
                    self._build_debug_request_entry(
                        "main_judge_attempt_1",
                        "主流程质检(首次非JSON异常)",
                        req,
                        resp=resp,
                        error=attempt_1_err,
                    )
                )
                retry_req = UnifiedChatRequest(
                    model_id=req.model_id,
                    messages=req.messages,
                    config=retry_cfg,
                )
                retry_resp = self.adapter.chat(retry_req)
                retry_parsed = self._try_parse_json_dict(retry_resp.content)
                retry_label = (
                    "主流程与红线质检(降级思考纠偏重试)"
                    if downgrade_notice
                    else "主流程与红线质检(纠偏重试)"
                )
                if retry_parsed is not None:
                    retry_dbg = self._build_debug_request_entry(
                        "main_judge",
                        retry_label,
                        retry_req,
                        resp=retry_resp,
                    )
                    if downgrade_notice:
                        retry_dbg["downgrade_notice"] = downgrade_notice
                    debug_requests.append(retry_dbg)
                    req = retry_req
                    eval_data = retry_parsed
                else:
                    retry_empty = not (retry_resp.content or "").strip()
                    if retry_empty and getattr(retry_resp, "thinking", ""):
                        retry_err = "模型思考过程耗尽 max_tokens 输出额度，导致正文为空，无法解析 JSON 质检报告"
                    elif retry_empty:
                        retry_err = "模型纠偏重试后仍返回空正文，无法解析 JSON 质检报告"
                    else:
                        retry_err = "模型纠偏重试后仍未能输出标准 JSON 格式"
                    retry_dbg = self._build_debug_request_entry(
                        "main_judge",
                        retry_label,
                        retry_req,
                        resp=retry_resp,
                        error=retry_err,
                    )
                    if downgrade_notice:
                        retry_dbg["downgrade_notice"] = downgrade_notice
                    debug_requests.append(retry_dbg)
                    req = retry_req
                    eval_data = self._extract_json_from_response(retry_resp.content)
        except Exception as e:
            debug_requests.append(
                self._build_debug_request_entry("main_judge", "主流程与红线质检", req, error=str(e))
            )
            eval_data = {
                "score": 0,
                "verdict": "FAIL",
                "detected_defects": [f"质检裁判模型调用异常: {str(e)}"],
                "expert_comments": "因接口异常未能完成全量白盒推演。",
            }

        # 若模型返回了缺少 score / verdict / deductions 的空壳 JSON，严禁按 100 分 PASS 放行
        if (
            "score" not in eval_data
            and "verdict" not in eval_data
            and "deductions" not in eval_data
        ):
            eval_data = {
                "score": 0,
                "verdict": "FAIL",
                "detected_defects": ["模型返回的 JSON 缺少 score/verdict/deductions 核心评判字段"],
                "expert_comments": "质检结果字段缺失，统一判定失败。",
            }

        flow_deductions = eval_data.get("deductions", [])
        if not isinstance(flow_deductions, list):
            flow_deductions = []
        else:
            # 规范化 flow_deductions 的 turn 字段与正则容错提取
            for d in flow_deductions:
                if isinstance(d, dict):
                    if "turn" in d and d["turn"] is not None:
                        try:
                            d["turn"] = int(re.findall(r"\d+", str(d["turn"]))[0])
                        except Exception:
                            pass
                    else:
                        m_t = re.search(r"(?:#|第)\s*(\d+)\s*(?:轮)?", f"{d.get('item', '')} {d.get('reason', '')}")
                        if m_t:
                            try:
                                d["turn"] = int(m_t.group(1))
                            except Exception:
                                pass

        # ------------------ 轨道二: FAQ 问答准确度质检 (开启才触发) ------------------
        faq_deductions: List[Dict[str, Any]] = []
        if has_faq:
            # Step B1: 提取 FAQ 提问轮次 + 问题改写
            faq_rounds = self.extract_faq_rounds_and_rewrite(
                rule, dialogue, debug_collector=debug_requests
            )
            if faq_rounds:
                retriever = UniversalFAQRetriever(rule.faq_inspection)
                for fr in faq_rounds:
                    query = fr.get("rewritten_query") or fr.get("raw_user_question", "")
                    if not query:
                        continue
                    candidates = retriever.search(query, top_k=rule.faq_inspection.top_k)
                    r_deductions = self.evaluate_faq_accuracy(
                        rule, scenario, fr, candidates, dialogue, debug_collector=debug_requests
                    )
                    faq_deductions.extend(r_deductions)

        # ------------------ 代码层汇合: 双轨扣分合并与统一算分 ------------------
        all_deductions = list(flow_deductions) + list(faq_deductions)

        total_deduction_points = sum(
            self._safe_score_int(d.get("points", 0), default=0)
            for d in all_deductions
            if isinstance(d, dict)
        )
        raw_eval_score = (
            self._safe_score_int(eval_data.get("score"), default=100)
            if "score" in eval_data
            else None
        )
        if not flow_deductions and raw_eval_score is not None:
            # 非 Mermaid 模板或主质检异常未返回 flow_deductions 时，保留主质检自身扣分差值，避免被 FAQ 扣分公式抹零
            base_flow_deduction = max(0, 100 - raw_eval_score)
            if not faq_deductions:
                score = raw_eval_score
            else:
                score = max(0, min(100, 100 - base_flow_deduction - total_deduction_points))
        else:
            score = max(0, min(100, 100 - total_deduction_points))

        raw_verdict = str(eval_data.get("verdict") or "").strip().upper()
        if raw_eval_score is not None and not all_deductions and raw_verdict in ("PASS", "FAIL"):
            verdict = raw_verdict
        elif not flow_deductions and raw_verdict == "FAIL":
            # 主裁判未走结构化 deductions 但明确判定红线 FAIL 时，避免被轻微 FAQ 扣分后按分数线误翻为 PASS
            verdict = "FAIL"
        else:
            verdict = "PASS" if score >= rule.inspection_rules.pass_score_threshold else "FAIL"

        defects = [
            f"[{d.get('item', '扣分项')}] -{d.get('points', 0)}分: {d.get('reason', '')}"
            for d in all_deductions
            if isinstance(d, dict)
        ]

        # 若流程轨未走结构化 deductions (如非 Mermaid 模式或主质检调用异常)，保留其 detected_defects
        if not flow_deductions and isinstance(eval_data.get("detected_defects"), list):
            for raw_def in eval_data["detected_defects"]:
                if isinstance(raw_def, str) and raw_def.strip() and raw_def.strip() not in defects:
                    defects.append(raw_def.strip())

        if has_mermaid and not is_topo_valid and topo_err:
            topo_notice = f"【拓扑检测元数据】{topo_err}"
            if topo_notice not in defects:
                defects.append(topo_notice)

        expert_comments = eval_data.get("expert_comments", "")
        if faq_deductions:
            expert_comments += f" (经专职 FAQ 质检核查，发现 {len(faq_deductions)} 处政策或回答口径偏差)"
        if notices:
            notices_text = " ".join(notices)
            expert_comments = f"{expert_comments} {notices_text}".strip()

        return InspectionReport(
            score=score,
            verdict=verdict,  # type: ignore
            detected_defects=defects,
            deductions=all_deductions,
            node_trajectory=clean_nodes,
            trajectory_details=trajectory_details,
            topology_valid=is_topo_valid,
            topology_error=topo_err if not is_topo_valid else None,
            flow_checkpoints=eval_data.get("flow_checkpoints", {}),
            expert_comments=expert_comments,
            notices=notices,
            request_messages=[{"role": m.role, "content": m.content} for m in req.messages],
            debug_requests=debug_requests,
        )

    def build_benchmark_judge_messages(
        self,
        sample: BenchmarkSample,
        model_answer: str,
        template_override: Optional[str] = None,
        constraints_override: Optional[str] = None,
    ) -> List[ChatMessage]:
        """组装常规数据集评测裁判消息列表"""
        if isinstance(sample.rubric, str) and sample.rubric.strip():
            criteria = sample.rubric.strip()
        elif isinstance(sample.rubric, RubricConfig) and sample.rubric.judge_criteria:
            criteria = sample.rubric.judge_criteria
        else:
            criteria = "回答准确、完整、无幻觉与逻辑自洽。"

        user_input = ""
        for m in sample.messages:
            if m.role == "user":
                user_input += m.content + "\n"

        if constraints_override is not None:
            constraints_desc = constraints_override
        elif sample.regex_constraints:
            constraints_desc = f"\n【客观正则/禁忌约束】: {json.dumps(sample.regex_constraints, ensure_ascii=False)}"
        else:
            constraints_desc = ""

        category_display = f"{sample.category}{f' / {sample.sub_category}' if sample.sub_category else ''}"
        difficulty_display = sample.difficulty or "未指定"
        reference_display = sample.reference or "无固定唯一标准答案，重点考查逻辑与合规性"

        tpl = (
            template_override
            or get_active_prompt_template("benchmark_judge", DEFAULT_BENCHMARK_JUDGE_PROMPT_TEMPLATE)
        )
        if not tpl or not tpl.strip():
            tpl = DEFAULT_BENCHMARK_JUDGE_PROMPT_TEMPLATE

        prompt = render_template_vars(
            tpl.strip(),
            {
                "category_display": category_display,
                "difficulty": difficulty_display,
                "user_input": user_input.strip(),
                "reference": reference_display,
                "criteria": criteria,
                "constraints_desc": constraints_desc,
                "model_answer": model_answer,
            },
        )

        if PROMPT_USER_SPLIT_MARKER in prompt:
            sys_part, user_part = split_prompt_into_system_and_user(prompt)
            return [
                ChatMessage(role="system", content=sys_part),
                ChatMessage(role="user", content=user_part),
            ]
        return [ChatMessage(role="user", content=prompt)]

    def evaluate_benchmark_response(
        self,
        sample: BenchmarkSample,
        model_answer: str,
    ) -> Dict[str, Any]:
        """
        模式二: 常规数据集评测 LLM 裁判打分 (针对参考答案与 Rubric 细则)
        """
        msgs = self.build_benchmark_judge_messages(sample, model_answer)
        req = UnifiedChatRequest(
            model_id=self.adapter.model_name,
            messages=msgs,
            config=self._build_step_config(
                temperature=0.1,
                max_tokens=2048,
                thinking_enabled=True,
                thinking_level="MEDIUM",
                respect_model_config=True,
            ),
        )

        try:
            resp = self.adapter.chat(req)
            result = self._extract_json_from_response(resp.content)
            score = float(result.get("score", 0.0))
            verdict = str(result.get("verdict", "FAIL")).upper()
            feedback = result.get("feedback") or result.get("expert_comments", "")
            return {"score": score, "verdict": verdict, "feedback": feedback}
        except Exception as e:
            return {"score": 0.0, "verdict": "FAIL", "feedback": f"裁判模型打分异常: {str(e)}"}

    def build_judge_messages(
        self,
        rule: UniversalRuleSchema,
        scenario: PersonaDefinition,
        dialogue: List[ChatMessage],
        confirmed_trajectory: Optional[List[str]] = None,
        trajectory_details: Optional[List[Dict[str, Any]]] = None,
        topology_valid: Optional[bool] = None,
        topology_error: Optional[str] = None,
        template_override: Optional[str] = None,
    ) -> List[ChatMessage]:
        """组装质检裁判标准 [system, user] 消息列表：静态规则/SOP/铁律放 system，动态上下文/对话放 user"""
        use_mermaid = bool(rule.topology_sop.enabled and rule.topology_sop.mermaid_graph.strip())
        if not use_mermaid:
            # 关闭 Mermaid 时严禁复用规则里残留的 Mermaid 质检模板，直接走纯规则质检分支
            return self.build_plain_judge_messages(
                rule=rule,
                scenario=scenario,
                dialogue=dialogue,
                template_override=template_override,
            )

        tpl = (
            template_override
            or rule.inspection_rules.judge_prompt_template
            or get_active_prompt_template("judge", DEFAULT_JUDGE_PROMPT_TEMPLATE)
        )
        if not tpl or not tpl.strip():
            tpl = DEFAULT_JUDGE_PROMPT_TEMPLATE

        redlines_str = "\n".join(f"- {r}" for r in rule.inspection_rules.global_redlines) if rule.inspection_rules.global_redlines else "无特殊全局红线"
        custom_rubric_str = rule.inspection_rules.custom_rubric or "专业严谨、态度温和、无答非所问"

        # 格式化对话（过滤 system，严格使用 [#轮次] 编号）
        dialogue_formatted = (
            self._format_dialogue_with_turns(dialogue)
            or json.dumps([m.model_dump() for m in dialogue], ensure_ascii=False, indent=2)
        )

        mermaid_sop = rule.topology_sop.mermaid_graph
        checkpoints_str = json.dumps(
            {k: v.model_dump() for k, v in rule.inspection_rules.node_checkpoints.items()},
            ensure_ascii=False,
            indent=2,
        )
        traj_str = json.dumps(confirmed_trajectory or [], ensure_ascii=False)
        traj_detail_str = json.dumps(trajectory_details or [], ensure_ascii=False, indent=2)

        # 拓扑检测客观元数据
        if topology_valid is None:
            topology_status = "未核验 / 默认放行"
            topology_err_str = "无"
        elif topology_valid:
            topology_status = "连通合规 (VALID)"
            topology_err_str = "无断路异常，状态转移连通合规"
        else:
            topology_status = "检测到拓扑断路/跨分支窜流 (INVALID)"
            topology_err_str = topology_error or "状态机拓扑转移存在断路"

        rendered = render_template_vars(
            tpl.strip(),
            {
                "scenario_id": scenario.id,
                "scenario_name": scenario.name,
                "mermaid_sop": mermaid_sop,
                "sop_dynamics_block": DEFAULT_SOP_DYNAMICS_BLOCK,
                "topology_status": topology_status,
                "trajectory": traj_str,
                "topology_error": topology_err_str,
                "trajectory_details": traj_detail_str,
                "checkpoints": checkpoints_str,
                "redlines": redlines_str,
                "custom_rubric": custom_rubric_str,
                "dialogue": dialogue_formatted,
            },
        )
        sys_part, user_part = split_prompt_into_system_and_user(rendered)
        return [
            ChatMessage(role="system", content=sys_part),
            ChatMessage(role="user", content=user_part),
        ]

    def build_plain_judge_messages(
        self,
        rule: UniversalRuleSchema,
        scenario: PersonaDefinition,
        dialogue: List[ChatMessage],
        template_override: Optional[str] = None,
    ) -> List[ChatMessage]:
        """组装纯规则与红线质检 (无 Mermaid) [system, user] 消息列表"""
        redlines_str = (
            "\n".join(f"- {r}" for r in rule.inspection_rules.global_redlines)
            if rule.inspection_rules.global_redlines
            else "无特殊全局红线"
        )
        custom_rubric_str = rule.inspection_rules.custom_rubric or "专业严谨、态度温和、无答非所问"
        dialogue_formatted = (
            self._format_dialogue_with_turns(dialogue)
            or json.dumps([m.model_dump() for m in dialogue], ensure_ascii=False, indent=2)
        )
        plain_tpl = (
            template_override
            or get_active_prompt_template("judge_plain", DEFAULT_PLAIN_JUDGE_PROMPT_TEMPLATE)
        )
        if not plain_tpl or not plain_tpl.strip():
            plain_tpl = DEFAULT_PLAIN_JUDGE_PROMPT_TEMPLATE

        rendered_plain = render_template_vars(
            plain_tpl.strip(),
            {
                "pass_score_threshold": str(rule.inspection_rules.pass_score_threshold),
                "redlines": redlines_str,
                "custom_rubric": custom_rubric_str,
                "scenario_id": scenario.id,
                "scenario_name": scenario.name,
                "dialogue": dialogue_formatted,
            },
        )
        sys_part, user_part = split_prompt_into_system_and_user(rendered_plain)
        return [
            ChatMessage(role="system", content=sys_part),
            ChatMessage(role="user", content=user_part),
        ]

    def build_judge_prompt(
        self,
        rule: UniversalRuleSchema,
        scenario: PersonaDefinition,
        dialogue: List[ChatMessage],
        confirmed_trajectory: Optional[List[str]] = None,
        trajectory_details: Optional[List[Dict[str, Any]]] = None,
        topology_valid: Optional[bool] = None,
        topology_error: Optional[str] = None,
        template_override: Optional[str] = None,
    ) -> str:
        """向后兼容的单字符串组装接口"""
        msgs = self.build_judge_messages(
            rule=rule,
            scenario=scenario,
            dialogue=dialogue,
            confirmed_trajectory=confirmed_trajectory,
            trajectory_details=trajectory_details,
            topology_valid=topology_valid,
            topology_error=topology_error,
            template_override=template_override,
        )
        return "\n\n".join(m.content for m in msgs)

    @staticmethod
    def _try_parse_json_dict(text: str) -> Optional[Dict[str, Any]]:
        """尝试从模型回复中提取合法 JSON 字典，失败时返回 None。"""
        if not text:
            return None

        raw_trimmed = text.strip()
        candidates = []

        # 1. 尝试使用正则剥离代码块
        for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", raw_trimmed, flags=re.DOTALL):
            candidates.append(m.group(1))

        # 2. 尝试从第一个 { 到最后一个 } (处理带前后文字或多重代码块场景)
        first_brace = raw_trimmed.find("{")
        last_brace = raw_trimmed.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            candidates.append(raw_trimmed[first_brace : last_brace + 1])

        candidates.append(raw_trimmed)

        for cand in candidates:
            cand_clean = cand.strip()
            cand_clean = re.sub(r"^```(?:json)?\s*", "", cand_clean, flags=re.IGNORECASE).strip()
            cand_clean = re.sub(r"\s*```$", "", cand_clean).strip()

            try:
                data = json.loads(cand_clean, strict=False)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass

            # 尝试修复尾随逗号 (Trailing Comma: {"a": 1,} -> {"a": 1})
            try:
                fixed_commas = re.sub(r",\s*([\]}])", r"\1", cand_clean)
                data = json.loads(fixed_commas, strict=False)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass

        return None

    def _extract_json_from_response(self, text: str) -> Dict[str, Any]:
        """从模型回复中工业级鲁棒提取 JSON (支持 strict=False, 尾随逗号修复与宽容提取)"""
        raw_trimmed = (text or "").strip()
        if raw_trimmed:
            parsed = self._try_parse_json_dict(raw_trimmed)
            if parsed is not None:
                return parsed

        # 若全部尝试均失败（含空正文），统一返回 FAIL 与 0 分，绝不乱打分或误判为 PASS
        preview = raw_trimmed[:200].replace("\r\n", " ").replace("\n", " ") if raw_trimmed else "回复内容为空"
        return {
            "score": 0,
            "verdict": "FAIL",
            "detected_defects": [f"模型未按标准 JSON 输出质检报告 (原始回复: {preview})"],
            "node_trajectory": [],
            "expert_comments": f"模型原始输出未能成功解析为 JSON 结构: {preview}",
        }
