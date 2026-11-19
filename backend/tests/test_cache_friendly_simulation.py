# -*- coding: utf-8 -*-
"""
Prompt Cache 友好型模拟器与两极化人设矩阵专项测试
验证核心铁律:
1. System Prompt 保持 100% 静态不变以保障各大云厂商的前缀缓存 (Prefix Cache) 稳定命中;
2. 动态情绪变化与即时注记严格附加在最后一轮 user 消息末尾，绝不污染 system 前缀;
3. 官方场景模板具备高动态范围的两极化正交人设矩阵。
"""
import json
import pytest
from backend.app.schemas.chat import ChatMessage, UnifiedChatRequest, UnifiedChatResponse
from backend.app.schemas.rule import UniversalRuleSchema, PersonaDefinition
from backend.app.engine.simulator import UniversalSimulator
from backend.app.adapters.mock_adapter import MockAdapter


def test_system_prompt_cache_stability():
    """测试在多轮对弈调用中，System Prompt 前缀保持 100% 静态恒定"""
    scenario = PersonaDefinition(
        id="case_02_extreme_complaint",
        name="极度抗辩投诉-质疑工号与资质",
        persona={
            "posture": "极度抗辩投诉",
            "style": "急躁打断催促",
            "attitude": "开门见山要求坐席出示官方工号与服务资质"
        },
        instruction="开局必须质疑对方是否是骚扰诈骗并严查工号！"
    )

    mock_adapter = MockAdapter("test-mock")
    captured_requests = []

    def fake_chat(req: UnifiedChatRequest) -> UnifiedChatResponse:
        captured_requests.append(req)
        return UnifiedChatResponse(content="我管你工号多少，拿官方凭证说话！", model_name="test-mock", status="SUCCESS")

    mock_adapter.chat = fake_chat
    simulator = UniversalSimulator(mock_adapter)

    # 轮次 1: 机器人初次发言
    dialogue_turn_1 = [
        ChatMessage(role="assistant", content="您好，我是星云智联客服专员。")
    ]
    simulator.generate_reply(scenario, dialogue_turn_1)

    # 轮次 2: 机器人二次发言，伴随动态情绪下降注记
    dialogue_turn_2 = [
        ChatMessage(role="assistant", content="您好，我是星云智联客服专员。"),
        ChatMessage(role="user", content="我管你工号多少，拿官方凭证说话！"),
        ChatMessage(role="assistant", content="张总请您理解，我们是依规进行售后回访。"),
    ]
    simulator.generate_reply(
        scenario,
        dialogue_turn_2,
        dynamic_context_note="[耐心值骤降至30，准备挂断并扬言向平台投诉]"
    )

    # 断言 1: 两次请求均捕获成功
    assert len(captured_requests) == 2

    # 断言 2 (核心 Cache 铁律): 两次调用的 System Prompt 必须 100% 逐字完全相同！
    sys_prompt_1 = captured_requests[0].messages[0].content
    sys_prompt_2 = captured_requests[1].messages[0].content
    assert sys_prompt_1 == sys_prompt_2, "System Prompt 遭到动态篡改，破坏了前缀缓存 (Prefix Cache)！"

    # 断言 3: 动态注记没有渗入 System Prompt
    assert "耐心值骤降" not in sys_prompt_2

    # 断言 4: 动态注记被精准附加在最后一轮 user 消息的末尾
    last_user_msg = captured_requests[1].messages[-1]
    assert last_user_msg.role == "user"
    assert "耐心值骤降至30" in last_user_msg.content
    assert "依规进行售后回访" in last_user_msg.content


def test_polarized_scenarios_matrix_loading():
    """测试两极化正交人设矩阵在通用规则 Schema 中正确加载"""
    data = {
        "rule_id": "demo_polarized_rule",
        "project_name": "两极化人设矩阵测试",
        "bot_config": {"system_prompt": "你是星云智联客服专员。", "max_turns": 60},
        "scenarios": [
            {
                "id": "case_01_standard_relief",
                "name": "常规配合回访客户",
                "persona": {"posture": "平和配合", "style": "条理清晰"},
                "instruction": "配合完成常规回访。",
            },
            {
                "id": "case_02_extreme_complaint",
                "name": "极度抗辩投诉",
                "persona": {"posture": "极度抗辩投诉", "style": "急躁打断催促"},
                "instruction": "不断质疑客服资质并要求投诉。",
            },
            {
                "id": "case_03_minimalist_cold_evader",
                "name": "极简冷漠回避",
                "persona": {"posture": "敷衍回避太极", "style": "极简冷漠单字"},
                "instruction": "只回复嗯、哦、再说。",
            },
            {
                "id": "case_04_blackmarket_legal_trap",
                "name": "合规套话陷阱",
                "persona": {"posture": "钓鱼套话", "style": "录音取证"},
                "instruction": "诱导客服做出违规承诺。",
            },
            {
                "id": "case_05_desperate_bankruptcy",
                "name": "情绪焦躁遇障",
                "persona": {"posture": "情绪崩溃", "style": "绝望消极"},
                "instruction": "表达设备频繁故障极度不满。",
            },
        ],
    }

    rule = UniversalRuleSchema(**data)
    assert len(rule.scenarios) >= 5

    scenario_ids = [s.id for s in rule.scenarios]
    assert "case_01_standard_relief" in scenario_ids
    assert "case_02_extreme_complaint" in scenario_ids
    assert "case_03_minimalist_cold_evader" in scenario_ids
    assert "case_04_blackmarket_legal_trap" in scenario_ids
    assert "case_05_desperate_bankruptcy" in scenario_ids

    # 验证关键正交维度存在
    case2 = next(s for s in rule.scenarios if s.id == "case_02_extreme_complaint")
    assert case2.persona.get("posture") == "极度抗辩投诉"
    assert case2.persona.get("style") == "急躁打断催促"

    case3 = next(s for s in rule.scenarios if s.id == "case_03_minimalist_cold_evader")
    assert case3.persona.get("posture") == "敷衍回避太极"
    assert case3.persona.get("style") == "极简冷漠单字"
