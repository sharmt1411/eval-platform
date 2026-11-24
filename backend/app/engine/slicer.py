# -*- coding: utf-8 -*-
"""
核心测评切题引擎 (Progressive Context Slicer)
核心能力:
1. 原始序号不可变性 (Original Turn Indexing): 彻底消除剪枝 (prune) 引起的数组下标位移错位;
2. 累积纠正上文 (Progressive Slicing): 考题上下文继承前序已 replace 纠正的黄金话术，剔除已 prune 轮次;
3. 真实标答双源锚定: 仅以实际被 replace 纠正的坐席轮次为基准，杜绝未修错话变成标答;
4. 外呼首轮与角色交替防线: 外呼首轮自动补位呼通引导语，确保考题 messages 以 user 严格交替收尾;
5. 规则红线确定性继承: 自动继承业务规则中的违禁词约束，绝不瞎猜自然语言正则;
6. 旧数据智能 Diff 回退: 针对缺失 actions 的存量数据，支持语义比对自动推导靶向切片。
"""
import re
from typing import List, Dict, Any, Optional, Tuple
from backend.app.schemas.chat import ChatMessage
from backend.app.schemas.rule import UniversalRuleSchema, PersonaDefinition
from backend.app.schemas.flywheel import FlywheelBenchmarkSlice
from backend.app.engine.remediator import UniversalDataRemediator


class ProgressiveContextSlicer:
    """通用累积纠正考题切片引擎"""

    @staticmethod
    def _safe_turn_int(val: Any) -> Optional[int]:
        if val is None or isinstance(val, bool):
            return None
        if isinstance(val, int):
            return val
        digits: List[str] = re.findall(r"\d+", str(val))
        return int(digits[0]) if digits else None

    @staticmethod
    def slice_dialogue(
        sample_id: str,
        raw_dialogue: List[ChatMessage],
        actions: Optional[List[Dict[str, Any]]] = None,
        deductions: Optional[List[Dict[str, Any]]] = None,
        rule: Optional[UniversalRuleSchema] = None,
        scenario: Optional[PersonaDefinition] = None,
        category: str = "飞轮实战质检",
        remediated_dialogue: Optional[List[ChatMessage]] = None,
        trajectory_details: Optional[List[Dict[str, Any]]] = None,
        faq_details: Optional[List[Dict[str, Any]]] = None,
    ) -> List[FlywheelBenchmarkSlice]:
        """
        将单通多轮实战对弈及修复成果切割为标准 Benchmark 评测切片。
        """
        if not raw_dialogue:
            return []

        deductions = deductions or []
        actions = actions or []

        sys_msg = next((m for m in raw_dialogue if m.role == "system"), None)
        orig_turns = [m for m in raw_dialogue if m.role != "system"]

        # 1. 初始化带不可变原始序号的状态项列表
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
        turn_map = {it["orig_turn"]: it for it in items}

        # 2. 应用声明式处方单 (若存在 actions)
        has_valid_actions = False
        for act in actions:
            if not isinstance(act, dict):
                continue
            tool = act.get("tool") or act.get("action")
            reason = act.get("reason", "")

            if tool in ("replace", "patch", "patch_assistant"):
                turn_idx = act.get("turn") or act.get("turn_index")
                new_text = act.get("text") or act.get("assistant_text") or act.get("content")
                if turn_idx is not None and new_text:
                    try:
                        t_int = int(re.findall(r"\d+", str(turn_idx))[0])
                        it = turn_map.get(t_int)
                        if it and it["role"] == "assistant" and not it["deleted"]:
                            it["content"] = str(new_text).strip()
                            it["patched"] = True
                            it["patch_reason"] = reason
                            has_valid_actions = True
                    except Exception:
                        pass

            elif tool in ("prune", "prune_turns", "prune_turn_pair"):
                turns_raw = act.get("turns") or act.get("turn_indices") or act.get("turn")
                if turns_raw is not None:
                    if isinstance(turns_raw, (int, str)):
                        t_list = [int(re.findall(r"\d+", str(turns_raw))[0])]
                    elif isinstance(turns_raw, list):
                        t_list = []
                        for t in turns_raw:
                            digits = re.findall(r"\d+", str(t))
                            if digits:
                                t_list.append(int(digits[0]))
                    else:
                        t_list = []
                    for t in t_list:
                        it = turn_map.get(t)
                        if it:
                            it["deleted"] = True

        # 3. 旧数据/无 actions 场景回退推导: 借助 remediated_dialogue 比对推导被修复的 assistant 轮次
        if not has_valid_actions and remediated_dialogue:
            remed_assistant_turns = [m for m in remediated_dialogue if m.role == "assistant"]
            raw_assistant_turns = [it for it in items if it["role"] == "assistant"]

            # 若轮次相同，逐轮比对文本变化
            if len(remed_assistant_turns) == len(raw_assistant_turns):
                for raw_it, remed_m in zip(raw_assistant_turns, remed_assistant_turns):
                    if raw_it["content"].strip() != remed_m.content.strip():
                        raw_it["content"] = remed_m.content.strip()
                        raw_it["patched"] = True
                        raw_it["patch_reason"] = "通过修复语料比对提取优化话术"
                        has_valid_actions = True

        # 4. 锁定真正需要切题的目标坐席轮次 (必须为已成功 replace 的 assistant 轮次)
        target_items = [it for it in items if it["patched"] and not it["deleted"] and it["role"] == "assistant"]
        if not target_items:
            return []

        # 5. 规则红线与违禁词约束提取 (确定性继承)
        rule_regex_constraints: List[str] = []
        if rule and rule.inspection_rules:
            # 提取全局红线禁词 (若显式配置了以 ! 开头的禁含规则)
            for rl in rule.inspection_rules.global_redlines:
                if isinstance(rl, str) and rl.strip():
                    c_rl = rl.strip()
                    if c_rl.startswith("!"):
                        rule_regex_constraints.append(c_rl)
                elif isinstance(rl, dict) and "forbidden_keywords" in rl:
                    f_kws = rl.get("forbidden_keywords", [])
                    clean_kw = "|".join([str(k).strip() for k in f_kws if str(k).strip()])
                    if clean_kw:
                        rule_regex_constraints.append(f"!{clean_kw}")

        slices: List[FlywheelBenchmarkSlice] = []
        default_sys_content = rule.bot_config.system_prompt if (rule and rule.bot_config) else "你是专业的业务对话机器人，请按照业务规范严谨合规作答。"
        rule_id_val: Optional[str] = rule.rule_id if rule else None

        # 预处理 trajectory_details 按轮次升序，供定位 target_turn 所属节点
        parsed_traj: List[Tuple[int, str]] = []
        for td in trajectory_details or []:
            if not isinstance(td, dict):
                continue
            t_num = ProgressiveContextSlicer._safe_turn_int(td.get("turn"))
            nid_str = str(td.get("node_id") or "").strip()
            if t_num is not None and nid_str:
                parsed_traj.append((t_num, nid_str))
        parsed_traj.sort(key=lambda x: x[0])

        # 预处理 faq_details 按 assistant_turn 建立索引
        faq_by_turn: Dict[int, Dict[str, Any]] = {}
        for fd in faq_details or []:
            if not isinstance(fd, dict):
                continue
            a_t = ProgressiveContextSlicer._safe_turn_int(fd.get("assistant_turn"))
            if a_t is not None and a_t not in faq_by_turn:
                faq_by_turn[a_t] = fd

        for target_it in target_items:
            target_turn = target_it["orig_turn"]
            reference_text = target_it["content"].strip()

            # 5.1 匹配质检扣分项
            matched_deductions = []
            for d in deductions:
                if not isinstance(d, dict):
                    continue
                d_turn = d.get("turn")
                if d_turn is not None:
                    try:
                        d_turn_int = int(re.findall(r"\d+", str(d_turn))[0])
                        if d_turn_int == target_turn:
                            matched_deductions.append(d)
                    except Exception:
                        pass

            # 定位节点 ID（优先精确匹配 turn == target_turn，回退最近前置轮次）
            resolved_node_id: Optional[str] = None
            for t_num, nid_str in parsed_traj:
                if t_num == target_turn:
                    resolved_node_id = nid_str
                    break
                if t_num < target_turn:
                    resolved_node_id = nid_str

            # 定位 FAQ ID 与是否超纲（优先匹配 matched_deductions，回退查 faq_details）
            resolved_faq_id: Optional[str] = None
            resolved_is_faq_ood: bool = False
            for md in matched_deductions:
                cand_fid: str = str(md.get("matched_faq_id") or md.get("faq_id") or "").strip()
                if not cand_fid:
                    continue
                if cand_fid.upper() == "FAQ_OOD":
                    resolved_faq_id = "FAQ_OOD"
                    resolved_is_faq_ood = True
                elif cand_fid.upper() not in ("NONE", "NULL", "OOD"):
                    resolved_faq_id = cand_fid
                    resolved_is_faq_ood = False
                    break
            if not resolved_faq_id and target_turn in faq_by_turn:
                fd_match = faq_by_turn[target_turn]
                fd_fid: str = str(fd_match.get("matched_faq_id") or "").strip()
                fd_ood: bool = bool(fd_match.get("is_ood", False)) or fd_fid.upper() == "FAQ_OOD"
                if fd_ood:
                    resolved_faq_id = "FAQ_OOD"
                    resolved_is_faq_ood = True
                elif fd_fid and fd_fid.upper() not in ("NONE", "NULL"):
                    resolved_faq_id = fd_fid
                    resolved_is_faq_ood = False

            # 扣分原因与评分细则组装
            if matched_deductions:
                items_names = [str(d.get("item", "合规要素")).strip() for d in matched_deductions]
                reasons = [str(d.get("reason", "")).strip() for d in matched_deductions]
                defect_item = " & ".join(items_names)
                defect_reason = "；".join(reasons)
                rubric_str = "；".join([f"{it_name}: {r_text}" for it_name, r_text in zip(items_names, reasons)])
            else:
                defect_item = f"轮次 #{target_turn} 业务话术与规范"
                defect_reason = target_it.get("patch_reason") or "根据质检与业务规范纠正话术偏差"
                rubric_str = f"业务合规标准: 坐席需符合业务SOP与质检规范，正确推进流程。({defect_reason})"

            # 5.2 提取当前节点的违禁词 (若显式配置了属性)
            curr_regex_constraints = list(rule_regex_constraints)
            if rule and rule.inspection_rules and rule.inspection_rules.node_checkpoints:
                for node_id, cp in rule.inspection_rules.node_checkpoints.items():
                    if node_id in defect_item or (resolved_node_id and node_id == resolved_node_id):
                        f_kws = getattr(cp, "forbidden_keywords", None)
                        if isinstance(f_kws, list):
                            clean_kw = "|".join([str(k).strip() for k in f_kws if str(k).strip()])
                            if clean_kw:
                                curr_regex_constraints.append(f"!{clean_kw}")
            # 简单去重
            curr_regex_constraints = list(dict.fromkeys(curr_regex_constraints))

            # 5.3 累积考题上下文生成 (继承前序修复，过滤前序剪枝)
            context_turns: List[ChatMessage] = [
                ChatMessage(role=it["role"], content=it["content"])
                for it in items
                if it["orig_turn"] < target_turn and not it["deleted"]
            ]

            # 防线 A: 外呼首轮 / 无客户发言防御
            has_user_turn = any(m.role == "user" for m in context_turns)
            if not has_user_turn:
                # 注入外呼呼通事件提示，确保大模型评测时有前置 user 触发指令
                call_trigger = ChatMessage(
                    role="user",
                    content="[系统电话已接通，请开始外呼播报与身份核验]",
                )
                context_turns.insert(0, call_trigger)

            # 防线 B: 严格以 user 收尾 (修剪掉末尾非 user 消息)
            while context_turns and context_turns[-1].role != "user":
                context_turns.pop()

            if not context_turns:
                context_turns = [
                    ChatMessage(role="user", content="[系统电话已接通，请开始外呼播报与身份核验]")
                ]

            # 格式清洗校验 (使用系统标准的交替性校验器，确保以 user 结尾)
            raw_slice_msgs = [
                ChatMessage(role="system", content=sys_msg.content if sys_msg else default_sys_content)
            ] + context_turns

            _, sanitized_msgs, _ = UniversalDataRemediator.validate_and_sanitize_format(
                raw_slice_msgs,
                default_system_prompt=default_sys_content,
                require_assistant_ending=False,
                strict_alternating=True,
            )

            # 双重保险: 确保最终是以 user 结尾
            while sanitized_msgs and sanitized_msgs[-1].role != "user":
                sanitized_msgs.pop()

            if len(sanitized_msgs) <= 1:
                sanitized_msgs.append(
                    ChatMessage(role="user", content="[系统电话已接通，请开始外呼播报与身份核验]")
                )

            slice_obj = FlywheelBenchmarkSlice(
                slice_id=f"slice_{sample_id}_turn{target_turn}",
                sample_id=sample_id,
                target_turn=target_turn,
                defect_item=defect_item,
                defect_reason=defect_reason,
                messages=sanitized_msgs,
                reference=reference_text,
                rubric=rubric_str,
                regex_constraints=curr_regex_constraints,
                category=category,
                sub_category=scenario.name if scenario else None,
                rule_id=rule_id_val,
                node_id=resolved_node_id,
                faq_id=resolved_faq_id,
                is_faq_ood=resolved_is_faq_ood,
            )
            slices.append(slice_obj)

        return slices
