# -*- coding: utf-8 -*-
"""
通用会话修复与 messages 格式清洗引擎 (Universal Data Remediator)
核心能力:
1. validate_and_sanitize_format: 工业级标准 SFT messages 格式清洗与交替性校验器;
2. apply_remediation_actions: 声明式双工具 (prune + replace) 串行执行器:
   - 原始序号不变性 (Original Turn Indexing): 轮次始终严格锚定原对话序号，消除剪枝位移错位;
   - 剪枝工具 prune: 必须成对删除原有问答 (user + assistant)，唯独允许单删末尾单个 user;
   - 替换工具 replace: 仅允许修改 assistant 话术，客户 (user) 文本 0 篡改绝对保护;
3. remediate_dialogue: 调用精修模型输出修复处方单并执行，失败降级清洗。
"""
import re
import json
from typing import List, Dict, Any, Optional, Tuple
from backend.app.adapters.base import BaseModelAdapter, build_downgraded_retry_config
from backend.app.schemas.chat import (
    ChatMessage,
    ModelConfig,
    UnifiedChatRequest,
    UnifiedChatResponse,
)
from backend.app.schemas.rule import (
    UniversalRuleSchema,
    PersonaDefinition,
    DEFAULT_REMEDIATION_PROMPT_TEMPLATE,
    split_prompt_into_system_and_user,
)
from backend.app.engine.prompt_registry import get_active_prompt_template, render_template_vars
from backend.app.schemas.flywheel import InspectionReport


class UniversalDataRemediator:
    """通用数据修复与格式规范化引擎"""

    def __init__(
        self,
        remediate_adapter: Optional[BaseModelAdapter] = None,
        default_config: Optional[ModelConfig] = None,
    ):
        self.adapter = remediate_adapter
        self.default_config = default_config

    @staticmethod
    def validate_and_sanitize_format(
        messages: List[ChatMessage],
        default_system_prompt: str = "你是专业的业务对话机器人，请按照业务规范严谨合规作答。",
        inject_empty_thinking: bool = False,
        require_assistant_ending: bool = True,
        strict_alternating: bool = True,
    ) -> Tuple[bool, List[ChatMessage], str]:
        """
        严格校验并规范化 SFT 对话 messages 数据格式:
        1. 顶层非空校验;
        2. 首轮角色必须为 system，若缺失或非 system 则自动注入;
        3. 角色合法性: 仅允许 system / user / assistant;
        4. 内容清洗: 清除 '[挂断电话]' 等控制指令与首尾空白;
        5. 交替性校验: user 与 assistant 必须交替出现，若有连续同角色则自动拼接换行合并;
        6. 结尾校验: 根据配置确保以 assistant 完美收口;
        7. 思考字段处理: 根据 inject_empty_thinking 配置是否为 assistant 保留或注入 thinking。
        """
        if not messages or not isinstance(messages, list):
            return False, [], "数据格式错误: messages 为空或不是列表"

        sanitized: List[ChatMessage] = []

        # 1. 确保首轮为合法的 system prompt
        first_msg = messages[0]
        if first_msg.role == "system":
            sys_content = first_msg.content.strip() or default_system_prompt
            sanitized.append(ChatMessage(role="system", content=sys_content))
            raw_turns = messages[1:]
        else:
            sanitized.append(ChatMessage(role="system", content=default_system_prompt))
            raw_turns = messages

        # 2. 清洗每个轮次并处理角色交替
        cleaned_turns: List[ChatMessage] = []
        for turn in raw_turns:
            role = turn.role
            content = turn.content if isinstance(turn.content, str) else str(turn.content)
            clean_content = content.replace("[挂断电话]", "").strip()
            if not clean_content:
                continue

            if role not in ("user", "assistant"):
                continue

            thinking_val = turn.thinking if turn.thinking else ("" if (inject_empty_thinking and role == "assistant") else None)

            # 连续同角色合并
            if strict_alternating and cleaned_turns and cleaned_turns[-1].role == role:
                cleaned_turns[-1].content += "\n" + clean_content
            else:
                cleaned_turns.append(
                    ChatMessage(
                        role=role,  # type: ignore
                        content=clean_content,
                        thinking=thinking_val,
                    )
                )

        if not cleaned_turns:
            return False, [], "有效对话轮次为空"

        # 3. 结尾收口校验
        if require_assistant_ending:
            while cleaned_turns and cleaned_turns[-1].role != "assistant":
                cleaned_turns.pop()

        if not cleaned_turns:
            return False, [], "有效问答对为空 (未找到合法的 assistant 收尾)"

        sanitized.extend(cleaned_turns)
        return True, sanitized, "格式清洗校验通过"

    @staticmethod
    def parse_turn_int(val: Any) -> Optional[int]:
        """安全解析轮次编号，兼容整数、数字字符串或 '#4'/'第4轮' 等形式"""
        if val is None or isinstance(val, bool):
            return None
        digits = re.findall(r"\d+", str(val))
        return int(digits[0]) if digits else None

    # 兼容历史私有方法调用
    _parse_turn_int = parse_turn_int

    @staticmethod
    def apply_remediation_actions(
        raw_dialogue: List[ChatMessage],
        actions: List[Dict[str, Any]],
    ) -> Tuple[List[ChatMessage], Dict[str, Any]]:
        """
        基于声明式双工具 (prune + replace) 串行执行精修处方单。
        保证原始序号不变性 (Original Turn Indexing):
        - 所有轮次编号 turn 严格对应剥离 system 后的原始对话轮次 (从 1 开始);
        - 替换工具 replace: 仅能动 assistant;
        - 剪枝工具 prune: 成对删除问答轮次，除末尾单条 user 外校验必须成对。
        """
        sys_msg = next((m for m in raw_dialogue if m.role == "system"), None)
        orig_turns = [m for m in raw_dialogue if m.role != "system"]

        # 构建带有不可变原始序号的状态项列表
        items = [
            {
                "orig_turn": i + 1,
                "role": m.role,
                "content": m.content,
                "thinking": m.thinking,
                "deleted": False,
                "patched": False,
                "patch_reason": "",
            }
            for i, m in enumerate(orig_turns)
        ]

        turn_map = {item["orig_turn"]: item for item in items}
        execution_log = []
        errors = []

        for act in actions:
            if not isinstance(act, dict):
                continue
            tool = act.get("tool") or act.get("action")
            reason = act.get("reason", "")

            # 1. 话术替换工具: replace (旧称 patch / patch_assistant)
            if tool in ("replace", "patch", "patch_assistant"):
                turn_idx = UniversalDataRemediator.parse_turn_int(act.get("turn") or act.get("turn_index"))
                new_text = act.get("text") or act.get("assistant_text") or act.get("content")
                if turn_idx is None or not new_text:
                    continue

                item = turn_map.get(turn_idx)
                if not item:
                    errors.append(f"replace 失败: 轮次 #{turn_idx} 超出原对话范围")
                    continue
                if item["deleted"]:
                    errors.append(f"replace 失败: 轮次 #{turn_idx} 已被剪枝删除，无法替换")
                    continue
                if item["role"] != "assistant":
                    errors.append(f"🚨【铁律拦截】replace 工具仅限修改 assistant，严禁修改 user 轮次 #{turn_idx}！")
                    continue

                item["content"] = str(new_text).strip()
                item["patched"] = True
                item["patch_reason"] = reason
                execution_log.append(f"✓ 轮次 #{turn_idx} (ASSISTANT) 话术替换成功: {reason}")

            # 2. 问答剪枝工具: prune (旧称 prune_turns / prune_turn_pair)
            elif tool in ("prune", "prune_turns", "prune_turn_pair"):
                turns_raw = act.get("turns") or act.get("turn_indices") or act.get("turn")
                if turns_raw is None:
                    continue
                if isinstance(turns_raw, (int, str)):
                    parsed = UniversalDataRemediator.parse_turn_int(turns_raw)
                    target_turns = [parsed] if parsed is not None else []
                elif isinstance(turns_raw, list):
                    target_turns = [
                        t_int for t in turns_raw
                        if (t_int := UniversalDataRemediator.parse_turn_int(t)) is not None
                    ]
                else:
                    continue
                if not target_turns:
                    continue

                # 提取目标轮次对象
                target_items = [turn_map.get(t) for t in target_turns if turn_map.get(t)]
                if len(target_items) != len(target_turns):
                    errors.append(f"prune 警告: 部分轮次 {target_turns} 不存在")
                    continue

                # 检查是否已被删除
                already_deleted = [it["orig_turn"] for it in target_items if it["deleted"]]
                if already_deleted:
                    errors.append(f"prune 警告: 轮次 {already_deleted} 此前已被删除，忽略")
                    continue

                # 成对性校验: 不限制剪枝位置 (开头/中间/结尾均可)，只要保证成对即可
                user_count = sum(1 for it in target_items if it["role"] == "user")
                assistant_count = sum(1 for it in target_items if it["role"] == "assistant")

                is_valid_prune = False
                if user_count > 0 and user_count == assistant_count:
                    # 只要包含相等数量的 user 与 assistant (1对、2对、多对)，不限制位置放行剪枝
                    is_valid_prune = True
                elif len(target_items) == 1 and user_count == 1:
                    # 单轮特例: 允许单删末尾未被客服回复的单个 user 轮次
                    active_items = [it for it in items if not it["deleted"]]
                    last_active = active_items[-1] if active_items else None
                    if last_active and last_active["orig_turn"] == target_items[0]["orig_turn"]:
                        is_valid_prune = True
                    else:
                        errors.append(f"🚨【剪枝校验失败】prune 单轮删除仅允许针对末尾未收口的单个 user 轮次（当前轮次 #{target_items[0]['orig_turn']} 不是末轮）")
                else:
                    errors.append(
                        f"🚨【剪枝校验失败】prune 必须成对操作（包含相等数量的 user 与 assistant 轮次，当前为 {user_count} 个 user 与 {assistant_count} 个 assistant）"
                    )

                if is_valid_prune:
                    for it in target_items:
                        it["deleted"] = True
                    execution_log.append(f"✂️ 剪枝成功: 删除了轮次 {target_turns}，原因: {reason}")

        # 组装未删除轮次
        active_turns = [
            ChatMessage(
                role=it["role"],
                content=it["content"],
                thinking=it["thinking"],
            )
            for it in items
            if not it["deleted"]
        ]

        final_dialogue = ([sys_msg] if sys_msg else []) + active_turns
        report = {
            "execution_log": execution_log,
            "errors": errors,
            "pruned_turns": [it["orig_turn"] for it in items if it["deleted"]],
            "patched_turns": [it["orig_turn"] for it in items if it["patched"]],
        }
        return final_dialogue, report

    @staticmethod
    def enforce_user_immutability(
        raw_dialogue: List[ChatMessage],
        remediated_msgs: List[ChatMessage],
    ) -> Tuple[List[ChatMessage], int]:
        """
        【最高防御断言】: 强制保护用户言论绝对未被篡改 (User Immutability Enforcer)。
        作为整篇覆盖降级分支时的终极保险。
        """
        raw_users = [m for m in raw_dialogue if m.role == "user"]
        if not raw_users:
            return remediated_msgs, 0

        remediated_user_indices = [i for i, m in enumerate(remediated_msgs) if m.role == "user"]
        tampered_count = 0

        if len(remediated_user_indices) == len(raw_users):
            protected_msgs = [ChatMessage(**m.to_dict()) for m in remediated_msgs]
            for u_idx, raw_u in zip(remediated_user_indices, raw_users):
                current_user_content = protected_msgs[u_idx].content
                if current_user_content.strip() != raw_u.content.strip():
                    tampered_count += 1
                protected_msgs[u_idx].content = raw_u.content
            return protected_msgs, tampered_count

        # 若轮次数量不匹配，安全拼接原 user
        tampered_count = abs(len(remediated_user_indices) - len(raw_users)) + 1
        safe_stitched: List[ChatMessage] = []
        remediated_assistants = [m for m in remediated_msgs if m.role == "assistant"]
        asst_pointer = 0

        for raw_msg in raw_dialogue:
            if raw_msg.role == "user":
                safe_stitched.append(ChatMessage(role="user", content=raw_msg.content))
            elif raw_msg.role == "assistant":
                if asst_pointer < len(remediated_assistants):
                    safe_stitched.append(remediated_assistants[asst_pointer])
                    asst_pointer += 1
                else:
                    safe_stitched.append(raw_msg)
            elif raw_msg.role == "system":
                safe_stitched.append(raw_msg)

        return safe_stitched, tampered_count

    def remediate_dialogue_with_actions(
        self,
        rule: UniversalRuleSchema,
        scenario: PersonaDefinition,
        raw_dialogue: List[ChatMessage],
        eval_report: InspectionReport,
    ) -> Tuple[List[ChatMessage], List[Dict[str, Any]], Dict[str, Any]]:
        """
        调用精修模型，输出声明式编辑处方单 (prune + replace) 并执行。
        返回: (finalized_messages, applied_actions, execution_report)
        """
        if not self.adapter:
            _, cleaned, _ = self.validate_and_sanitize_format(
                raw_dialogue,
                default_system_prompt=rule.bot_config.system_prompt,
                inject_empty_thinking=rule.remediation_config.inject_empty_thinking,
                require_assistant_ending=rule.remediation_config.require_assistant_ending,
            )
            return cleaned, [], {"status": "no_adapter"}

        remediation_messages = self.build_remediation_messages(
            rule, scenario, raw_dialogue, eval_report
        )

        base_cfg = self.default_config
        req_cfg = (
            ModelConfig(
                temperature=base_cfg.temperature,
                top_p=base_cfg.top_p,
                max_tokens=max(base_cfg.max_tokens or 0, 10240),
                repetition_penalty=base_cfg.repetition_penalty,
                thinking_enabled=base_cfg.thinking_enabled,
                thinking_level=base_cfg.thinking_level,
                vertex_thinking_mode=base_cfg.vertex_thinking_mode,
                extra_body=dict(base_cfg.extra_body or {}),
            )
            if base_cfg is not None
            else ModelConfig(
                temperature=0.1,
                max_tokens=10240,
                thinking_enabled=True,
                thinking_level="HIGH",
            )
        )

        req = UnifiedChatRequest(
            model_id=self.adapter.model_name,
            messages=remediation_messages,
            config=req_cfg,
        )

        remediation_debug: Dict[str, Any] = {
            "step": "remediate",
            "label": "AI 靶向微创修复",
            "model": req.model_id,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "config": req.config.model_dump(),
            "thinking": "",
            "response_content": "",
            "raw_output": "",
            "usage": None,
            "latency_ms": None,
        }

        def _record_resp_debug(cur_req: UnifiedChatRequest, cur_resp: UnifiedChatResponse) -> None:
            remediation_debug["config"] = cur_req.config.model_dump()
            remediation_debug["thinking"] = getattr(cur_resp, "thinking", "") or ""
            remediation_debug["response_content"] = getattr(cur_resp, "content", "") or ""
            remediation_debug["raw_output"] = getattr(cur_resp, "raw_output", "") or ""
            remediation_debug["usage"] = (
                cur_resp.usage.model_dump() if getattr(cur_resp, "usage", None) else None
            )
            remediation_debug["latency_ms"] = (
                round(cur_resp.metrics.total_latency_ms, 1)
                if getattr(cur_resp, "metrics", None)
                else None
            )

        notices: List[str] = []

        try:
            resp = self.adapter.chat(req)
            _record_resp_debug(req, resp)
            actions = self._extract_actions_from_response(resp.content)
            remediated_raw = (
                [] if actions else self._extract_messages_from_response(resp.content)
            )
            if not actions and not remediated_raw:
                # 首次返回非 JSON、空正文或退化内容时，通过统一降级助手构建纠偏重试配置
                retry_cfg, downgrade_notice = build_downgraded_retry_config(
                    req.config, resp, scene_label="AI 靶向微创修复"
                )
                remediation_debug["attempt_1_response_content"] = remediation_debug["response_content"]
                remediation_debug["attempt_1_error"] = (
                    downgrade_notice
                    or "模型首次回复未能解析为修复处方或消息数组，已自动触发退避重采样"
                )
                if downgrade_notice:
                    notices.append(downgrade_notice)
                    remediation_debug["downgrade_notice"] = downgrade_notice
                retry_req = UnifiedChatRequest(
                    model_id=req.model_id,
                    messages=req.messages,
                    config=retry_cfg,
                )
                resp = self.adapter.chat(retry_req)
                _record_resp_debug(retry_req, resp)
                remediation_debug["label"] = (
                    "AI 靶向微创修复(降级思考纠偏重试)"
                    if downgrade_notice
                    else "AI 靶向微创修复(纠偏重试)"
                )
                actions = self._extract_actions_from_response(resp.content)
                remediated_raw = (
                    [] if actions else self._extract_messages_from_response(resp.content)
                )
                if not actions and not remediated_raw:
                    retry_empty = not (resp.content or "").strip()
                    if retry_empty and getattr(resp, "thinking", ""):
                        remediation_debug["error"] = "模型思考过程耗尽 max_tokens 输出额度，导致修复正文为空"
                    elif retry_empty:
                        remediation_debug["error"] = "模型纠偏重试后仍返回空正文，未产出有效修复处方"
                    else:
                        remediation_debug["error"] = "模型纠偏重试后仍未能输出有效的修复处方或对话数组"

            if actions:
                # 路径 A: 声明式双工具处方单执行 (首选推荐)
                remediated_msgs, report = self.apply_remediation_actions(raw_dialogue, actions)
                report["remediation_debug"] = remediation_debug
                if notices:
                    report["notices"] = notices
                ok, finalized, err = self.validate_and_sanitize_format(
                    remediated_msgs,
                    default_system_prompt=rule.bot_config.system_prompt,
                    inject_empty_thinking=rule.remediation_config.inject_empty_thinking,
                    require_assistant_ending=rule.remediation_config.require_assistant_ending,
                )
                if ok:
                    return finalized, actions, report

            # 路径 B: 兼容整篇 messages 数组输出并应用强制 User 防御
            if remediated_raw:
                parsed_msgs = [
                    ChatMessage(
                        role=m.get("role", "user"),
                        content=m.get("content", ""),
                        thinking=m.get("thinking", ""),
                    )
                    for m in remediated_raw
                ]
                final_msgs, _ = self.enforce_user_immutability(raw_dialogue, parsed_msgs)
                ok, finalized, _ = self.validate_and_sanitize_format(
                    final_msgs,
                    default_system_prompt=rule.bot_config.system_prompt,
                    inject_empty_thinking=rule.remediation_config.inject_empty_thinking,
                    require_assistant_ending=rule.remediation_config.require_assistant_ending,
                )
                if ok:
                    return finalized, [], {
                        "status": "full_messages_replacement",
                        "remediation_debug": remediation_debug,
                        "notices": notices,
                    }
        except Exception as e:
            remediation_debug["error"] = str(e)
            print(f"⚠️ [UniversalDataRemediator] 修复过程异常: {e}")

        # 降级返回清洗后的原对话
        _, fallback_msgs, _ = self.validate_and_sanitize_format(
            raw_dialogue,
            default_system_prompt=rule.bot_config.system_prompt,
            inject_empty_thinking=rule.remediation_config.inject_empty_thinking,
            require_assistant_ending=rule.remediation_config.require_assistant_ending,
        )
        return fallback_msgs, [], {
            "status": "fallback_error",
            "remediation_debug": remediation_debug,
            "notices": notices,
        }

    def remediate_dialogue(
        self,
        rule: UniversalRuleSchema,
        scenario: PersonaDefinition,
        raw_dialogue: List[ChatMessage],
        eval_report: InspectionReport,
    ) -> List[ChatMessage]:
        """向后兼容的旧接口，直接返回清洗后的修复结果"""
        finalized, _, _ = self.remediate_dialogue_with_actions(rule, scenario, raw_dialogue, eval_report)
        return finalized

    @staticmethod
    def format_dialogue_with_indices(dialogue: List[ChatMessage]) -> str:
        """格式化对话供大模型查看，提供轮次、角色、内容，如 [#1] USER: xxx"""
        lines = []
        turn_num = 1
        for m in dialogue:
            if m.role == "system":
                continue
            role_str = m.role.upper()
            content = m.content.strip().replace("\r\n", " ").replace("\n", " ")
            lines.append(f"[#{turn_num}] {role_str}: {content}")
            turn_num += 1
        return "\n".join(lines)

    def build_remediation_messages(
        self,
        rule: UniversalRuleSchema,
        scenario: PersonaDefinition,
        raw_dialogue: List[ChatMessage],
        eval_report: InspectionReport,
        template_override: Optional[str] = None,
    ) -> List[ChatMessage]:
        """组装修复模型标准 [system, user] 消息列表：工具定义与数据治理铁律放 system，缺陷与原对话放 user"""
        tpl = (
            template_override
            or rule.remediation_config.remediation_prompt_template
            or get_active_prompt_template("remediate", DEFAULT_REMEDIATION_PROMPT_TEMPLATE)
        )
        if not tpl or not tpl.strip():
            tpl = DEFAULT_REMEDIATION_PROMPT_TEMPLATE

        mermaid_sop = rule.topology_sop.mermaid_graph if rule.topology_sop.enabled else "无状态机流程图"

        custom_instructions = rule.remediation_config.custom_instructions or ""
        custom_instructions_block = custom_instructions.strip() if custom_instructions.strip() else "按照业务流程规范与质检缺陷指引，自然合规推进对话。"

        dialogue_str = self.format_dialogue_with_indices(raw_dialogue)
        substantive_defects = [
            d for d in (eval_report.detected_defects or [])
            if not str(d).strip().startswith("【拓扑检测元数据】")
        ]
        if eval_report.deductions:
            # 透传结构化扣分项 (含轮次号与 FAQ 官方标准参考答案 reference_answer)，为三段式修复提供官方口径依据
            defects_payload = {
                "summary": substantive_defects,
                "deductions": eval_report.deductions,
            }
            defects_str = json.dumps(defects_payload, ensure_ascii=False, indent=2)
        else:
            defects_str = json.dumps(substantive_defects, ensure_ascii=False, indent=2)

        rendered = render_template_vars(
            tpl,
            {
                "scenario_id": scenario.id,
                "scenario_name": scenario.name,
                "system_prompt": rule.bot_config.system_prompt,
                "mermaid_sop": mermaid_sop,
                "defects": defects_str,
                "dialogue": dialogue_str,
                "custom_instructions_block": custom_instructions_block,
            },
        )
        sys_part, user_part = split_prompt_into_system_and_user(rendered)
        return [
            ChatMessage(role="system", content=sys_part),
            ChatMessage(role="user", content=user_part),
        ]

    def build_remediation_prompt(
        self,
        rule: UniversalRuleSchema,
        scenario: PersonaDefinition,
        raw_dialogue: List[ChatMessage],
        eval_report: InspectionReport,
        template_override: Optional[str] = None,
    ) -> str:
        """向后兼容的单字符串修复提示词接口"""
        msgs = self.build_remediation_messages(
            rule=rule,
            scenario=scenario,
            raw_dialogue=raw_dialogue,
            eval_report=eval_report,
            template_override=template_override,
        )
        return "\n\n".join(m.content for m in msgs)

    # 兼容历史私有方法调用
    _build_remediation_prompt = build_remediation_prompt

    def _extract_actions_from_response(self, text: str) -> List[Dict[str, Any]]:
        """从模型回复中提取 actions 处方单"""
        if not text:
            return []
        # 1. 尝试找带有 actions 的 json 块
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
        candidate = m.group(1) if m else None
        if not candidate:
            m2 = re.search(r"(\{\s*\"(?:thought|actions)\".*\})", text, flags=re.DOTALL)
            candidate = m2.group(1) if m2 else None

        if candidate:
            try:
                data = json.loads(candidate)
                if isinstance(data, dict) and "actions" in data and isinstance(data["actions"], list):
                    return data["actions"]
            except Exception:
                pass

        # 2. 尝试直接找 actions 数组
        m3 = re.search(r"```(?:json)?\s*(\[\s*\{.*?\}\s*\])\s*```", text, flags=re.DOTALL)
        if m3:
            try:
                data = json.loads(m3.group(1))
                if isinstance(data, list) and data and ("tool" in data[0] or "action" in data[0]):
                    return data
            except Exception:
                pass

        return []

    def _extract_messages_from_response(self, text: str) -> List[Dict[str, Any]]:
        if not text:
            return []
        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, flags=re.DOTALL)
        if m:
            text = m.group(1)
        else:
            m2 = re.search(r"(\[.*\])", text, flags=re.DOTALL)
            if m2:
                text = m2.group(1)
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
        except Exception:
            pass
        return []
