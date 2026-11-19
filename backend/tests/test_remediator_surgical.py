# -*- coding: utf-8 -*-
"""
客服专属靶向微创修复 (Assistant-Only Remediation) 专项测试
验证最高铁律:
1. 客户 (user) 文本无论粗糙、错别字或暴躁，100% 保持只读冻结，严禁篡改;
2. 大模型尝试篡改 user 时，enforce_user_immutability 自动拦截并强制覆盖还原;
3. 仅客服 (assistant) 轮次得到靶向纠偏重塑。
"""
import pytest
from backend.app.schemas.chat import ChatMessage, UnifiedChatResponse
from backend.app.schemas.rule import (
    UniversalRuleSchema,
    BotConfig,
    TopologySOPConfig,
    InspectionRules,
    RemediationConfig,
    PersonaDefinition,
)
from backend.app.schemas.flywheel import InspectionReport
from backend.app.engine.remediator import UniversalDataRemediator
from backend.app.adapters.mock_adapter import MockAdapter


def test_enforce_user_immutability_direct():
    """测试 enforce_user_immutability 防御性强制覆盖机制"""
    # 原始对话: 客户说话极其粗鲁暴躁且有错别字
    raw_dialogue = [
        ChatMessage(role="system", content="你是星云智联客服"),
        ChatMessage(role="user", content="喂？！别再打来了！我现在在开会没空！！"),
        ChatMessage(role="assistant", content="李先生您好，不处理退货后果自负，自己看着办！"), # 违规推诿恐吓
        ChatMessage(role="user", content="你说啥？？你敢威胁我？信不信我投诉消协！"),
        ChatMessage(role="assistant", content="投诉随便你，有本事你去告！"),
    ]

    # 模拟一个“善作主张”的大模型返回: 擅自把粗鲁客户改成了文明有礼貌的话术
    model_tampered_msgs = [
        ChatMessage(role="system", content="你是星云智联客服"),
        ChatMessage(role="user", content="您好，我现在正在开会，手头暂时有点忙。"), # 恶意篡改 1
        ChatMessage(role="assistant", content="李先生您好，理解您正在开会，按照星云智联售后政策，为您登记延后联系并介绍质保机制。"), # 合规修复
        ChatMessage(role="user", content="好的，请问有什么具体的退换方案吗？"), # 恶意篡改 2
        ChatMessage(role="assistant", content="好的，我们支持 7 天无理由退换保障机制，稍后为您跟进。"), # 合规修复
    ]

    # 执行防御校验
    protected_msgs, tampered_count = UniversalDataRemediator.enforce_user_immutability(
        raw_dialogue, model_tampered_msgs
    )

    # 断言 1: 成功捕获到了 2 处 user 篡改
    assert tampered_count == 2

    # 断言 2: 所有 user 轮次被强制 100% 还原为原始粗鲁发言
    user_turns = [m for m in protected_msgs if m.role == "user"]
    assert user_turns[0].content == "喂？！别再打来了！我现在在开会没空！！"
    assert user_turns[1].content == "你说啥？？你敢威胁我？信不信我投诉消协！"

    # 断言 3: 客服 assistant 轮次成功保留了模型的合规修复
    asst_turns = [m for m in protected_msgs if m.role == "assistant"]
    assert "售后政策" in asst_turns[0].content
    assert "保障机制" in asst_turns[1].content


def test_remediate_dialogue_pipeline_with_mock():
    """测试带有 MockAdapter 的端到端靶向修复链路"""
    rule = UniversalRuleSchema(
        rule_id="test_rule",
        project_name="测试微创修复",
        bot_config=BotConfig(system_prompt="你是专业合规的客服"),
        topology_sop=TopologySOPConfig(enabled=False),
        inspection_rules=InspectionRules(),
        remediation_config=RemediationConfig(freeze_user_input=True),
        scenarios=[PersonaDefinition(id="case1", name="测试客户")],
    )

    scenario = rule.scenarios[0]

    raw_dialogue = [
        ChatMessage(role="user", content="客户原始方言输入: 搞乜鬼啊，我冇钱啊！"),
        ChatMessage(role="assistant", content="原客服违规抢跑回复"),
    ]

    eval_report = InspectionReport(
        verdict="FAIL",
        score=50,
        detected_defects=["跳步抢跑", "未报工号"],
    )

    # 自定义 Mock 返回 (故意篡改 user)
    tampered_json_response = """```json
[
  {"role": "system", "content": "你是专业合规的客服"},
  {"role": "user", "content": "大模型自作聪明改写成普通话: 怎么回事，我没有钱。"},
  {"role": "assistant", "content": "售后专员为您服务，请问设备使用是否存在问题？"}
]
```"""

    mock_adapter = MockAdapter("test-mock")
    # 覆盖 mock chat 返回
    mock_adapter.chat = lambda req: UnifiedChatResponse(
        content=tampered_json_response,
        model_name="test-mock",
        status="SUCCESS",
    )

    remediator = UniversalDataRemediator(mock_adapter)
    final_msgs = remediator.remediate_dialogue(rule, scenario, raw_dialogue, eval_report)

    # 验证最终输出
    final_user = [m for m in final_msgs if m.role == "user"][0]
    final_asst = [m for m in final_msgs if m.role == "assistant"][0]

    # user 必须被 100% 强制还原为粤语原始输入
    assert final_user.content == "客户原始方言输入: 搞乜鬼啊，我冇钱啊！"
    # assistant 必须是修复后的内容
    assert "售后专员为您服务" in final_asst.content


def test_apply_remediation_actions_replace_and_interception():
    """测试 replace 工具仅限修改 assistant，对 user 的非法修改强制拦截"""
    raw_dialogue = [
        ChatMessage(role="user", content="你好，干嘛？"),  # 轮次 #1
        ChatMessage(role="assistant", content="我是专员，张女士吗？"),  # 轮次 #2
    ]

    actions = [
        # 合法替换 assistant
        {"tool": "replace", "turn": 2, "text": "您好，我是回访专员(客服编号A)，请问是张女士吗？", "reason": "补齐工号"},
        # 非法尝试替换 user (必须被拦截)
        {"tool": "replace", "turn": 1, "text": "您好，请问有什么事？", "reason": "润色客户发言"},
    ]

    result, report = UniversalDataRemediator.apply_remediation_actions(raw_dialogue, actions)

    # 1. 验证 user 绝对未被修改
    assert result[0].content == "你好，干嘛？"
    # 2. 验证 assistant 替换成功
    assert "客服编号A" in result[1].content
    # 3. 验证拦截报告
    assert any("严禁修改 user 轮次 #1" in err for err in report["errors"])
    assert 2 in report["patched_turns"]


def test_apply_remediation_actions_prune_pair_and_middle_intercept():
    """测试 prune 工具成对删除与拦截单删中间轮次"""
    raw_dialogue = [
        ChatMessage(role="user", content="设备怎么了？日常办公用的，刚买两天就黑屏了。"),  # #1 (抢答了故障现象)
        ChatMessage(role="assistant", content="设备具体故障现象是什么呢？"),  # #2 (重复提问)
        ChatMessage(role="user", content="都说了刚买两天就黑屏了。"),  # #3 (客户复读)
        ChatMessage(role="assistant", content="那充电指示灯亮吗？"),  # #4
        ChatMessage(role="user", content="亮着呢。"),  # #5
        ChatMessage(role="assistant", content="好的，再见！"),  # #6
    ]

    # 1. 尝试非法单删中间某轮 #2
    bad_actions = [
        {"tool": "prune", "turns": [2], "reason": "只删坐席重复问题"}
    ]
    _, bad_report = UniversalDataRemediator.apply_remediation_actions(raw_dialogue, bad_actions)
    assert any("必须成对" in err or "不允许单独删除" in err for err in bad_report["errors"])

    # 2. 合法成对删除 #2 和 #3
    good_actions = [
        {"tool": "prune", "turns": [2, 3], "reason": "消除坐席重复提问与客户复读"},
        {"tool": "replace", "turn": 4, "text": "了解，刚买两天就出现黑屏确实影响使用。那请问充电指示灯目前亮吗？", "reason": "承前启后提问指示灯状态"}
    ]
    res, good_report = UniversalDataRemediator.apply_remediation_actions(raw_dialogue, good_actions)

    # 验证剪枝后长度由 6 变成 4
    active_turns = [m for m in res if m.role != "system"]
    assert len(active_turns) == 4
    # 验证未删的轮次内容
    assert active_turns[0].content == "设备怎么了？日常办公用的，刚买两天就黑屏了。"
    assert "了解，刚买两天就出现黑屏确实影响使用" in active_turns[1].content
    assert active_turns[2].content == "亮着呢。"
    assert active_turns[3].content == "好的，再见！"
    # 验证原用户言论未被破坏
    assert 2 in good_report["pruned_turns"]
    assert 3 in good_report["pruned_turns"]


def test_apply_remediation_actions_prune_tail_user_allowed():
    """测试 prune 允许单删结尾单出的 user 轮次"""
    raw_dialogue = [
        ChatMessage(role="user", content="你好"),
        ChatMessage(role="assistant", content="您好，请问是张总吗？"),
        ChatMessage(role="user", content="是的，有事吗？挂机未回..."),  # 结尾单出 user
    ]

    actions = [
        {"tool": "prune", "turns": [3], "reason": "删除结尾未回复的孤立 user"}
    ]
    res, report = UniversalDataRemediator.apply_remediation_actions(raw_dialogue, actions)
    active_turns = [m for m in res if m.role != "system"]
    assert len(active_turns) == 2
    assert active_turns[-1].role == "assistant"
    assert 3 in report["pruned_turns"]


def test_apply_remediation_actions_prune_anywhere_and_multi_pairs():
    """测试 prune 工具不限制剪枝位置（开头/中间/多对均可），只要保证成对即可"""
    raw_dialogue = [
        ChatMessage(role="user", content="喂？在吗？"),  # #1 开场多余闲聊
        ChatMessage(role="assistant", content="在的在的，您好！"),  # #2 开场多余闲聊
        ChatMessage(role="user", content="请问你是哪里的？"),  # #3 开场多余闲聊2
        ChatMessage(role="assistant", content="我是星云智联客服。"),  # #4 开场多余闲聊2
        ChatMessage(role="user", content="有什么事吗？"),  # #5 真正业务开端
        ChatMessage(role="assistant", content="做个例行回访。请问设备运行正常吗？"),  # #6
        ChatMessage(role="user", content="运行挺稳定的，平时家用很顺手。"),  # #7
        ChatMessage(role="assistant", content="祝您生活愉快，再见！"),  # #8
    ]

    # 1. 一次性成对剪除开头的连续两对闲聊 [1, 2, 3, 4] (包含 2 个 user 和 2 个 assistant)
    actions = [
        {"tool": "prune", "turns": [1, 2, 3, 4], "reason": "剪除开场冗余客套问答，直奔主题"}
    ]
    res, report = UniversalDataRemediator.apply_remediation_actions(raw_dialogue, actions)

    active_turns = [m for m in res if m.role != "system"]
    assert len(active_turns) == 4
    # 剪枝后首轮直接从 #5 开始
    assert active_turns[0].content == "有什么事吗？"
    assert "例行回访" in active_turns[1].content
    assert set(report["pruned_turns"]) == {1, 2, 3, 4}
    assert len(report["errors"]) == 0


def test_remediator_inherits_default_config_and_retries_on_non_json():
    """验证修复器继承 default_config 参数、记录完整返回快照，并在首次非 JSON 退化时重试 1 次"""
    from backend.app.adapters.mock_adapter import MockAdapter
    from backend.app.schemas.chat import ModelConfig
    from backend.app.schemas.flywheel import InspectionReport
    from backend.app.schemas.rule import (
        UniversalRuleSchema,
        BotConfig,
        PersonaDefinition,
        TopologySOPConfig,
        InspectionRules,
    )

    rule = UniversalRuleSchema(
        rule_id="test_rem_retry",
        project_name="修复器配置与重试测试",
        bot_config=BotConfig(system_prompt="你是测试机器人。"),
        topology_sop=TopologySOPConfig(enabled=False, mermaid_graph=""),
        inspection_rules=InspectionRules(pass_score_threshold=80),
        scenarios=[PersonaDefinition(id="sc1", name="测试场景", instruction="测试")],
    )
    raw_dialogue = [
        ChatMessage(role="user", content="喂你好"),
        ChatMessage(role="assistant", content="你好！有什么事吗！"),
    ]
    eval_report = InspectionReport(
        score=70,
        verdict="FAIL",
        detected_defects=["[语气] 禁用感叹号"],
        deductions=[{"turn": 2, "item": "语气规范", "points": 30, "reason": "去掉感叹号"}],
    )

    captured_cfgs = []

    def mock_generator(req):
        captured_cfgs.append(req.config)
        if len(captured_cfgs) == 1:
            return "， ， ， ， ， ， "
        return '{"actions": [{"tool": "replace", "turn": 2, "text": "您好，请问有什么可以帮您。", "reason": "去掉感叹号"}]}'

    custom_cfg = ModelConfig(
        temperature=0.3,
        top_p=1.0,
        max_tokens=2048,
        thinking_enabled=False,
        thinking_level="NONE",
        vertex_thinking_mode="maas_chat_template",
    )
    remediator = UniversalDataRemediator(
        MockAdapter(response_generator=mock_generator),
        default_config=custom_cfg,
    )
    finalized, actions, report = remediator.remediate_dialogue_with_actions(
        rule, rule.scenarios[0], raw_dialogue, eval_report
    )

    # 1. 验证继承配置且 max_tokens 保底 >= 10240，且第 2 次重试温和抬高温度
    assert len(captured_cfgs) == 2
    assert captured_cfgs[0].temperature == 0.3
    assert captured_cfgs[0].top_p == 1.0
    assert captured_cfgs[0].max_tokens == 10240
    assert captured_cfgs[0].thinking_enabled is False
    assert captured_cfgs[0].vertex_thinking_mode == "maas_chat_template"
    assert captured_cfgs[1].temperature == 0.55

    # 2. 验证重试成功执行了 replace 且 user 0 篡改
    assert len(actions) == 1
    assert any(m.role == "assistant" and "请问有什么可以帮您" in m.content for m in finalized)
    assert any(m.role == "user" and m.content == "喂你好" for m in finalized)

    # 3. 验证 remediation_debug 记录了首轮异常、返回内容、usage 与耗时
    dbg = report.get("remediation_debug", {})
    assert "， ，" in (dbg.get("attempt_1_response_content") or "")
    assert "actions" in (dbg.get("response_content") or "")
    assert dbg.get("usage") is not None
    assert dbg.get("latency_ms") is not None


def test_remediator_downgrades_thinking_on_empty_content_and_records_error():
    """验证修复模型因思考耗尽 max_tokens 导致正文为空时，重试自动降级 thinking_level 并记录明确 error"""
    from backend.app.schemas.chat import ModelConfig, UnifiedChatResponse, TokenUsage, PerformanceMetrics

    rule = UniversalRuleSchema(
        rule_id="test_remed_thinking_exhaust",
        project_name="修复思考耗尽测试",
        bot_config=BotConfig(system_prompt="你是星云智联客服专员。"),
        scenarios=[PersonaDefinition(id="sc1", name="场景1", instruction="配合")],
    )
    raw_dialogue = [
        ChatMessage(role="system", content="你是星云智联客服专员。"),
        ChatMessage(role="user", content="喂你好"),
        ChatMessage(role="assistant", content="你好！有什么事吗！"),
    ]
    eval_report = InspectionReport(score=70, verdict="FAIL", detected_defects=["[语气] 禁用感叹号"])

    captured_levels = []
    captured_max_tokens = []

    class ThinkingExhaustedAdapter(MockAdapter):
        def chat(self, request):
            captured_levels.append(request.config.thinking_level)
            captured_max_tokens.append(request.config.max_tokens)
            return UnifiedChatResponse(
                content="",
                thinking="长思考耗尽输出额度...",
                raw_output="",
                usage=TokenUsage(prompt_tokens=100, completion_tokens=10240, thinking_tokens=10240, answer_tokens=0, total_tokens=10340),
                metrics=PerformanceMetrics(ttft_ms=10.0, total_latency_ms=50.0, tokens_per_second=100.0),
            )

    cfg = ModelConfig(temperature=0.3, max_tokens=10240, thinking_enabled=True, thinking_level="HIGH")
    remediator = UniversalDataRemediator(ThinkingExhaustedAdapter(), default_config=cfg)
    _, actions, report = remediator.remediate_dialogue_with_actions(
        rule, rule.scenarios[0], raw_dialogue, eval_report
    )

    assert captured_levels == ["HIGH", "LOW"]
    assert captured_max_tokens == [10240, 16384]
    assert actions == []
    assert report.get("status") == "fallback_error"
    assert len(report.get("notices") or []) == 1
    assert "HIGH 思考" in report["notices"][0] and "降级为 LOW" in report["notices"][0]
    dbg = report.get("remediation_debug", {})
    assert "降级为 LOW" in (dbg.get("attempt_1_error") or "")
    assert "降级为 LOW" in (dbg.get("downgrade_notice") or "")
    assert "耗尽" in (dbg.get("error") or "")

    # 验证 build_remediation_messages 自动剔除仅供参考的【拓扑检测元数据】，严禁将其当成缺陷传给修复模型
    topo_eval = InspectionReport(
        score=80,
        verdict="FAIL",
        detected_defects=[
            "[语气规范] -20分: 去掉感叹号",
            "【拓扑检测元数据】【拓扑断路/跨分支窜流】从节点 'N3_1' 无法到达节点 'N2_3'",
        ],
        deductions=[{"turn": 2, "item": "语气规范", "points": 20, "reason": "去掉感叹号"}],
    )
    msgs = remediator.build_remediation_messages(rule, rule.scenarios[0], raw_dialogue, topo_eval)
    assert "【拓扑检测元数据】" not in msgs[1].content
    assert "去掉感叹号" in msgs[1].content
