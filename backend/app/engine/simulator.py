# -*- coding: utf-8 -*-
"""
通用客户模拟对弈引擎 (Universal Client Simulator)
LLM-as-a-Client 模式: 根据给定的客户画像 (Persona) 与动机，口语化驱动与机器人的多轮实战对弈。

【Prompt Cache 优化铁律】:
1. System Prompt 保持 100% 静态 (Static System Prefix)，绝对严禁写入每轮变动的动态数字或状态，
   确保大模型厂商的前缀缓存 (Prefix Cache) 稳定命中 (降低 80%+ 延迟与成本);
2. 动态情绪演进与即时现场注记 (如耐心消耗、挂断意图)，统一且仅追加在最新一轮送入客户模型的 user 消息末尾;
3. 历史对话在翻转角色后保持严格不可变。
"""
import json
from typing import List, Dict, Any, Optional
from backend.app.adapters.base import BaseModelAdapter
from backend.app.schemas.chat import (
    ChatMessage,
    ModelConfig,
    UnifiedChatRequest,
    UnifiedChatResponse,
)
from backend.app.schemas.rule import (
    PersonaDefinition,
    DEFAULT_CLIENT_SIMULATOR_PROMPT_TEMPLATE,
)
from backend.app.engine.prompt_registry import get_active_prompt_template, render_template_vars


class UniversalSimulator:
    """通用客户模拟器 Agent"""

    def __init__(
        self,
        adapter: BaseModelAdapter,
        default_config: Optional[ModelConfig] = None,
    ):
        self.adapter = adapter
        self.default_config = default_config

    def generate_reply(
        self,
        scenario: PersonaDefinition,
        dialogue_history: List[ChatMessage],
        temperature: Optional[float] = None,
        dynamic_context_note: Optional[str] = None,
        client_config: Optional[ModelConfig] = None,
    ) -> str:
        """
        根据当前场景设定与历史对话，生成模拟客户的下一句真实口语化发言。

        【防角色反转铁律】:
        在全局对话记录中，机器人是 assistant，真实客户是 user。
        当送入模拟客户 LLM 时，必须做【视角角色翻转】(Role Inversion):
        - 机器人的历史发言 (assistant) ➔ 在模拟客户眼中是对方提问 (user);
        - 客户自己的历史发言 (user) ➔ 在模拟客户眼中是自己先前的回答 (assistant);
        - 确保模拟客户模型始终扮演真正的“客户”，绝不产生角色倒错！

        【Prompt Cache 友好型动态注记】:
        若有动态情绪值变化或突发情境注记，仅追加在最后一轮 user 消息末尾，
        保证 System Prompt 和 1~N-1 历史轮次完美击中 Prefix Cache。
        """
        prompt = self._build_client_prompt(scenario)

        # 1. 静态 System Prompt
        client_messages: List[ChatMessage] = [ChatMessage(role="system", content=prompt)]

        # 2. 对历史对话进行角色视角翻转
        inverted_history: List[ChatMessage] = []
        for msg in dialogue_history:
            content_str = (msg.content or "").strip()
            if not content_str:
                continue
            if msg.role == "assistant":
                # 机器人客服说的话 -> 客户眼中对方的发言
                target_role = "user"
            elif msg.role == "user":
                # 客户自己先前说的话 -> 客户眼中我方的历史回答
                target_role = "assistant"
            else:
                continue

            if inverted_history and inverted_history[-1].role == target_role:
                inverted_history[-1].content += f"\n{content_str}"
            else:
                inverted_history.append(ChatMessage(role=target_role, content=content_str))

        # 3. 保证末尾是合法的 user 轮次
        if not inverted_history:
            # 客户先开场的情况
            inverted_history.append(ChatMessage(role="user", content="[电话已接通，请以客户身份自然开场打招呼]"))
        elif inverted_history[-1].role != "user":
            inverted_history.append(ChatMessage(role="user", content="[对方等待您的回应]"))

        # 4. 【核心优化】动态注记仅附加在最新一轮 user 的末尾，绝不污染前面的前缀 Cache
        if dynamic_context_note and inverted_history:
            clean_note = dynamic_context_note.strip()
            if clean_note:
                inverted_history[-1].content += f"\n\n[临场动态状态注记 (仅供内心参考，严禁在台词中复述)]: {clean_note}"

        client_messages.extend(inverted_history)

        base_cfg = client_config or self.default_config
        if base_cfg is not None:
            req_cfg = base_cfg.model_copy(deep=True)
            if temperature is not None:
                req_cfg.temperature = temperature
            if not req_cfg.max_tokens or req_cfg.max_tokens <= 0:
                req_cfg.max_tokens = 2048
        else:
            req_cfg = ModelConfig(
                temperature=temperature if temperature is not None else 0.4,
                max_tokens=2048,
                thinking_enabled=False,
                thinking_level="NONE",
            )

        req = UnifiedChatRequest(
            model_id=self.adapter.model_name,
            messages=client_messages,
            config=req_cfg,
        )

        try:
            resp = self.adapter.chat(req)
            reply = resp.content.strip()
            return reply or "喂？在听吗？"
        except Exception as e:
            # 异常降级
            return self._mock_fallback_reply(scenario, dialogue_history)

    def _build_client_prompt(
        self,
        scenario: PersonaDefinition,
        template_override: Optional[str] = None,
    ) -> str:
        """
        构建绝对静态的 System Prompt，固化人设背景与两极化极性。
        """
        persona_dict = scenario.persona or {}
        persona_str = json.dumps(persona_dict, ensure_ascii=False, indent=2)
        tpl = (
            template_override
            or get_active_prompt_template(
                "client_simulator", DEFAULT_CLIENT_SIMULATOR_PROMPT_TEMPLATE
            )
        )
        if not tpl or not tpl.strip():
            tpl = DEFAULT_CLIENT_SIMULATOR_PROMPT_TEMPLATE

        return render_template_vars(
            tpl.strip(),
            {
                "persona_json": persona_str,
                "scenario_instruction": scenario.instruction or "根据自身背景与对方沟通。",
            },
        )

    def _mock_fallback_reply(self, scenario: PersonaDefinition, dialogue_history: List[ChatMessage]) -> str:
        """无网络或异常时的降级回复逻辑"""
        turn_count = len(dialogue_history)
        instruction = scenario.instruction.lower() if scenario.instruction else ""

        if "挂断" in instruction and turn_count > 4:
            return "我这边忙得很，别再打电话来了！[挂断电话]"
        if "抗辩" in instruction or "投诉" in instruction:
            return "你们工号多少？到底是不是官方客服？再打电话我直接去消协和平台投诉！"
        if "冷漠" in instruction or "单字" in instruction:
            return "哦，不需要。[挂断电话]"

        return "【Mock回复】已收到您的消息：[电话已接通，请以客户身份自然开场打招呼]"
