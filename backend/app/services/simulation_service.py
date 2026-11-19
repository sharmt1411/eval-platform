# -*- coding: utf-8 -*-
"""
双 Agent 模拟对弈调度服务 (Simulation Service)
"""
import time
from typing import List, Dict, Any, Optional, Callable, Generator, Tuple
from backend.app.schemas.chat import (
    ChatMessage,
    ModelConfig,
    UnifiedChatRequest,
    UnifiedChatResponse,
)
from backend.app.schemas.rule import UniversalRuleSchema, PersonaDefinition
from backend.app.schemas.flywheel import InspectionReport
from backend.app.adapters.base import BaseModelAdapter
from backend.app.engine.simulator import UniversalSimulator
from backend.app.engine.judge import UniversalJudgeEngine


class SimulationService:
    """模拟对弈业务调度器"""

    def __init__(
        self,
        target_adapter: BaseModelAdapter,
        client_adapter: BaseModelAdapter,
        judge_adapter: BaseModelAdapter,
        judge_config: Optional[ModelConfig] = None,
    ):
        self.target_adapter = target_adapter
        self.client_simulator = UniversalSimulator(client_adapter)
        self.judge_engine = UniversalJudgeEngine(judge_adapter, default_config=judge_config)

    def run_battle_iter(
        self,
        rule: UniversalRuleSchema,
        scenario: PersonaDefinition,
        max_turns: Optional[int] = None,
        target_config: Optional[ModelConfig] = None,
        verbose: bool = False,
    ) -> Generator[Tuple[str, Any, int], None, None]:
        """
        生成器版本：逐轮 yield 对话事件，支持上层 SSE 实时流式推送。
        yield 格式: (event_type, payload, turn_index)
        - ('turn', ChatMessage, turn_count)
        - ('judging', None, turn_count)
        - ('done', battle_result_dict, turn_count)
        """
        max_turns = max_turns or rule.bot_config.max_turns
        closing_keywords = rule.bot_config.closing_keywords or []
        dialogue_history: List[ChatMessage] = []
        bot_cfg = target_config or ModelConfig(temperature=0.3, max_tokens=256)

        system_msg = ChatMessage(role="system", content=rule.bot_config.system_prompt)

        has_open_error = False
        # 首轮开场
        if rule.bot_config.initiator == "user":
            first_user = self.client_simulator.generate_reply(scenario, [])
            first_msg = ChatMessage(role="user", content=first_user)
            dialogue_history.append(first_msg)
            yield ("turn", first_msg, len(dialogue_history))
        else:
            # 机器人先开场
            open_req = UnifiedChatRequest(
                model_id=self.target_adapter.model_name,
                messages=[system_msg, ChatMessage(role="user", content="[电话接通，等待客服问候]")],
                config=bot_cfg,
            )
            try:
                resp = self.target_adapter.chat(open_req)
                first_bot = resp.content.strip()
                bot_thinking = resp.thinking
            except Exception as e:
                first_bot = f"[开场异常: {str(e)}]"
                bot_thinking = ""
                has_open_error = True
            first_msg = ChatMessage(role="assistant", content=first_bot, thinking=bot_thinking)
            dialogue_history.append(first_msg)
            yield ("turn", first_msg, len(dialogue_history))

        # 循环对弈 (若开场已严重异常则跳过后续对弈)
        if not has_open_error:
            for turn in range(1, max_turns + 1):
                last_role = dialogue_history[-1].role
                if last_role == "user":
                    # 轮到被测 Bot 作答
                    model_payload = [system_msg] + dialogue_history
                    bot_req = UnifiedChatRequest(
                        model_id=self.target_adapter.model_name,
                        messages=model_payload,
                        config=bot_cfg,
                    )
                    try:
                        bot_resp = self.target_adapter.chat(bot_req)
                        bot_text = bot_resp.content.strip()
                        bot_thinking = bot_resp.thinking
                    except Exception as e:
                        bot_text = f"[接口异常: {str(e)}]"
                        bot_thinking = ""
                        error_msg = ChatMessage(role="assistant", content=bot_text)
                        dialogue_history.append(error_msg)
                        yield ("turn", error_msg, len(dialogue_history))
                        break

                    bot_msg = ChatMessage(role="assistant", content=bot_text, thinking=bot_thinking)
                    dialogue_history.append(bot_msg)
                    yield ("turn", bot_msg, len(dialogue_history))

                    # 检查机器人是否道别挂断
                    if any(k in bot_text for k in closing_keywords):
                        if verbose:
                            print("🏁 检测到机器人主动礼貌道别。")
                        break
                else:
                    # 轮到客户回复
                    user_reply = self.client_simulator.generate_reply(scenario, dialogue_history)
                    user_msg = ChatMessage(role="user", content=user_reply)
                    dialogue_history.append(user_msg)
                    yield ("turn", user_msg, len(dialogue_history))

                    if "[挂断电话]" in user_reply:
                        if verbose:
                            print("🏁 客户已挂断电话。")
                        break

        # 对弈结束，通知正在质检
        yield ("judging", None, len(dialogue_history))

        # 启动白盒质检
        eval_report: InspectionReport = self.judge_engine.evaluate_dialogue(
            rule, scenario, dialogue_history
        )

        battle_result = {
            "scenario_id": scenario.id,
            "scenario_name": scenario.name,
            "dialogue": dialogue_history,
            "eval_report": eval_report,
            "total_turns": len(dialogue_history),
        }
        yield ("done", battle_result, len(dialogue_history))

    def run_battle(
        self,
        rule: UniversalRuleSchema,
        scenario: PersonaDefinition,
        max_turns: Optional[int] = None,
        target_config: Optional[ModelConfig] = None,
        verbose: bool = False,
        on_turn_callback: Optional[Callable[[ChatMessage, int], None]] = None,
    ) -> Dict[str, Any]:
        """同步运行完整单场对弈与白盒质检"""
        battle_result = None
        for ev_type, payload, turn_cnt in self.run_battle_iter(
            rule, scenario, max_turns=max_turns, target_config=target_config, verbose=verbose
        ):
            if ev_type == "turn" and on_turn_callback:
                on_turn_callback(payload, turn_cnt)
            elif ev_type == "done":
                battle_result = payload

        return battle_result or {
            "scenario_id": scenario.id,
            "scenario_name": scenario.name,
            "dialogue": [],
            "eval_report": None,
            "total_turns": 0,
        }
