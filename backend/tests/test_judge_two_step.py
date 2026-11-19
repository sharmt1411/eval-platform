# -*- coding: utf-8 -*-
"""
测试质检裁判两步解耦架构 (Step 1: 专职轨迹与槽位抽取 -> Step 2: 确定性拓扑校验 -> Step 3: 注入事实质检打分)
"""
import pytest
from backend.app.adapters.mock_adapter import MockAdapter
from backend.app.engine.judge import UniversalJudgeEngine
from backend.app.engine.topology import UniversalTopologyValidator
from backend.app.schemas.chat import ChatMessage
from backend.app.schemas.rule import (
    UniversalRuleSchema,
    BotConfig,
    TopologySOPConfig,
    InspectionRules,
    NodeCheckpoint,
    PersonaDefinition,
)


def _build_test_rule(mermaid_enabled: bool = True) -> UniversalRuleSchema:
    return UniversalRuleSchema(
        rule_id="test_two_step_rule",
        project_name="两步解耦质检测试规则",
        bot_config=BotConfig(system_prompt="测试机器人"),
        topology_sop=TopologySOPConfig(
            enabled=mermaid_enabled,
            mermaid_graph="""flowchart TD
    N1_1("1.1 身份核验") --> N2_1("2.1 订单确认")
    N2_1 --> N2_2("2.2 设备版本")
    N2_2 --> N2_3("2.3 购买渠道")
    N2_3 --> N2_4("2.4 故障现象")
    N2_4 --> N2_5("2.5 使用频率")
    N2_5 --> N4_1("4.1 礼貌收尾")
""",
            entry_nodes=["N1_1"],
            terminal_nodes=["N4_1"],
            allow_self_loop=True,
            allow_slot_shortcut=True,
        ),
        inspection_rules=InspectionRules(
            pass_score_threshold=80,
            global_redlines=["严禁辱骂客户"],
            node_checkpoints={
                "N2_3": NodeCheckpoint(name="购买渠道", required_elements=["了解购买渠道信息"]),
                "N2_4": NodeCheckpoint(name="故障现象", required_elements=["了解设备故障现象"]),
            },
            custom_rubric="若客户明确说明没有故障现象(或运行正常)，视为已完成核查；客户要求挂断合规收尾不扣分。",
        ),
        scenarios=[
            PersonaDefinition(id="sc1", name="测试场景", instruction="测试说明")
        ],
    )


def test_topology_slot_shortcut_on_real_path():
    """测试在真实拓扑连线上，因客户交代槽位跳过节点放行，无连通关系则拦截"""
    rule = _build_test_rule(mermaid_enabled=True)
    validator = UniversalTopologyValidator(rule.topology_sop)

    # 1. 真实拓扑连线上跳过：N2_2 -> N2_4 (跳过 N2_3，N2_3 在 N2_2 到 N2_4 的有向连线上)
    trajectory = ["N1_1", "N2_1", "N2_2", "N2_4", "N2_5", "N4_1"]
    collected_slots = {"N2_3"}
    is_valid, err, clean_nodes = validator.validate_trajectory(trajectory, collected_slots=collected_slots)
    assert is_valid is True, f"真实拓扑路径连线上跳过槽位应放行: {err}"
    assert clean_nodes == trajectory

    # 2. 无连接关系跳跃：例如试图逆向或跳到互斥不存在的边
    invalid_trajectory = ["N1_1", "N2_4", "N2_1", "N4_1"]  # N2_4 到 N2_1 是逆向倒流
    is_valid2, err2, _ = validator.validate_trajectory(invalid_trajectory, collected_slots={"N2_1"})
    assert is_valid2 is False
    assert "拓扑断路/跨分支窜流" in err2


def test_judge_engine_two_step_with_mermaid():
    """测试启用 Mermaid 时，两步执行：抽取 -> 注入事实打分"""
    rule = _build_test_rule(mermaid_enabled=True)
    scenario = rule.scenarios[0]

    # 对话中，坐席问了 N2_4，客户回答了没有故障
    dialogue = [
        ChatMessage(role="assistant", content="您好，请问是张三本人吗？"),
        ChatMessage(role="user", content="是的"),
        ChatMessage(role="assistant", content="您之前登记的智能设备订单信息确认无误吧？"),
        ChatMessage(role="user", content="没错"),
        ChatMessage(role="assistant", content="那目前这台智能设备的固件版本是最新的吗？"),
        ChatMessage(role="user", content="是最新的，顺便说下我是在官方旗舰店买的"),  # 顺带提及 N2_3
        ChatMessage(role="assistant", content="收到！那目前使用中有遇到什么具体故障现象吗？"),  # N2_4
        ChatMessage(role="user", content="没有故障现象"),
        ChatMessage(role="assistant", content="好的，那平时日常使用频率是高、中还是低呢？"),  # N2_5
        ChatMessage(role="user", content="每天都在用"),
        ChatMessage(role="assistant", content="祝您生活愉快，再见！"),  # N4_1
    ]

    def mock_generator(req):
        all_msgs_text = "\n".join(m.content for m in req.messages)
        if "抽取分析铁律" in all_msgs_text:
            # Step 1: 专职轨迹与槽位抽取
            return """```json
{
  "trajectory": [
    {"turn": 1, "node_id": "N1_1", "user_slots": [], "evidence": "核实是否本人"},
    {"turn": 3, "node_id": "N2_1", "user_slots": [], "evidence": "确认订单信息"},
    {"turn": 5, "node_id": "N2_2", "user_slots": ["N2_3"], "evidence": "询问设备版本，客户透露购买渠道"},
    {"turn": 7, "node_id": "N2_4", "user_slots": [], "evidence": "询问故障现象，客户回答没有故障现象"},
    {"turn": 9, "node_id": "N2_5", "user_slots": [], "evidence": "询问日常使用频率"},
    {"turn": 11, "node_id": "N4_1", "user_slots": [], "evidence": "结束语收口道别"}
  ]
}
```"""
        else:
            # Step 2: 质检打分
            return """```json
{
  "score": 95,
  "verdict": "PASS",
  "detected_defects": [],
  "flow_checkpoints": {
    "N2_3": "合规(客户提前透露)",
    "N2_4": "合规(明确反馈无故障)"
  },
  "expert_comments": "坐席依规推进流程，客户透露无故障现象后顺利推进至使用频率询问，表现专业。"
}
```"""

    mock_adapter = MockAdapter(response_generator=mock_generator)
    engine = UniversalJudgeEngine(mock_adapter)

    report = engine.evaluate_dialogue(rule, scenario, dialogue)

    # 断言产出的报告具有确切的轨迹与拓扑合规
    assert report.topology_valid is True
    assert report.score == 95
    assert report.verdict == "PASS"
    assert report.node_trajectory == ["N1_1", "N2_1", "N2_2", "N2_4", "N2_5", "N4_1"]
    assert len(report.trajectory_details) == 6
    assert report.trajectory_details[2]["user_slots"] == ["N2_3"]


def test_judge_engine_without_mermaid():
    """测试未启用 Mermaid 时，跳过图提取，直接执行通用规则质检"""
    rule = _build_test_rule(mermaid_enabled=False)
    scenario = rule.scenarios[0]

    dialogue = [
        ChatMessage(role="assistant", content="您好，请问是张三本人吗？"),
        ChatMessage(role="user", content="不办了，我很忙，挂了！"),
        ChatMessage(role="assistant", content="好的，打扰您了，祝您生活愉快，再见！"),
    ]

    mock_adapter = MockAdapter()
    engine = UniversalJudgeEngine(mock_adapter)

    report = engine.evaluate_dialogue(rule, scenario, dialogue)

    # 未启用 Mermaid，node_trajectory 为空，拓扑默认放行
    assert report.topology_valid is True
    assert report.node_trajectory == []
    assert report.trajectory_details == []


def test_judge_customer_hangup_early_exit_exemption():
    """测试客户提出挂机时，未推进到终结节点（停在N2_4）也能被拓扑与裁判合法豁免"""
    rule = _build_test_rule(mermaid_enabled=True)
    rule.topology_sop.require_terminal_node = True  # 即使刚性开启了终结节点检查
    rule.topology_sop.allow_customer_hangup_early_exit = True
    scenario = rule.scenarios[0]

    # 对话在 N2_4 之后，客户表示在开会要挂了，客服礼貌顺应结束在 N2_4（未走到 N4_1）
    dialogue = [
        ChatMessage(role="assistant", content="您好，请问是张三本人吗？"),
        ChatMessage(role="user", content="是的"),
        ChatMessage(role="assistant", content="您之前登记的智能设备订单信息确认无误吧？"),
        ChatMessage(role="user", content="没错"),
        ChatMessage(role="assistant", content="收到！那目前使用中有遇到什么具体故障现象吗？"),
        ChatMessage(role="user", content="我现在正在开会，先挂了！"),
        ChatMessage(role="assistant", content="好的张先生，不打扰您开会，祝您工作顺利！"),
    ]

    # 1. 验证挂机意图识别
    assert UniversalJudgeEngine.detect_customer_hangup(dialogue) is True

    def mock_generator(req):
        all_msgs_text = "\n".join(m.content for m in req.messages)
        if "抽取分析铁律" in all_msgs_text:
            return """```json
{
  "trajectory": [
    {"turn": 1, "node_id": "N1_1", "user_slots": [], "evidence": "核实本人"},
    {"turn": 3, "node_id": "N2_1", "user_slots": [], "evidence": "确认订单信息"},
    {"turn": 5, "node_id": "N2_4", "user_slots": [], "evidence": "询问故障现象，客户提出开会挂机"}
  ]
}
```"""
        else:
            return """```json
{
  "score": 90,
  "verdict": "PASS",
  "detected_defects": [],
  "flow_checkpoints": {},
  "expert_comments": "客户因开会主动要求挂机，坐席礼貌顺应合规收尾，符合外呼防骚扰要求。"
}
```"""

    mock_adapter = MockAdapter(response_generator=mock_generator)
    engine = UniversalJudgeEngine(mock_adapter)

    report = engine.evaluate_dialogue(rule, scenario, dialogue)

    # 拓扑校验必须合规放行，且不能被强降为 50 分 FAIL
    assert report.topology_valid is True
    assert report.verdict == "PASS"
    assert report.score == 90
    assert report.topology_error is None
    assert "【刚性一票否决】" not in " ".join(report.detected_defects)


def test_judge_topology_metadata_only_score_decided_by_llm():
    """测试拓扑仅生成元数据供参考，打分全权交由质检大模型裁决，代码层不强行改分"""
    rule = _build_test_rule(mermaid_enabled=True)
    rule.topology_sop.require_terminal_node = True
    rule.topology_sop.allow_slot_shortcut = False  # 关闭跳步以模拟拓扑断路
    scenario = rule.scenarios[0]

    # 对话发生跳步断路：N1_1 -> N2_4 (缺少 N2_1 等)
    dialogue = [
        ChatMessage(role="assistant", content="您好，请问是张三本人吗？"),
        ChatMessage(role="user", content="是的"),
        ChatMessage(role="assistant", content="那目前使用中有遇到什么具体故障现象吗？"),
        ChatMessage(role="user", content="没有故障现象"),
        ChatMessage(role="assistant", content="祝您生活愉快，再见！"),
    ]

    def mock_generator(req):
        all_msgs_text = "\n".join(m.content for m in req.messages)
        if "抽取分析铁律" in all_msgs_text:
            return """```json
{
  "trajectory": [
    {"turn": 1, "node_id": "N1_1", "user_slots": [], "evidence": "核实本人"},
    {"turn": 3, "node_id": "N2_4", "user_slots": [], "evidence": "直接询问故障现象"}
  ]
}
```"""
        else:
            last_msg = req.messages[-1].content
            # 验证 prompt 中注入了拓扑元数据
            assert "<topology_metadata>" in last_msg
            assert "检测到拓扑断路" in last_msg
            # 大模型综合分析后认为虽然有轻微跳步但态度良好，给出了 82 分 PASS
            return """```json
{
  "score": 82,
  "verdict": "PASS",
  "detected_defects": ["坐席略微跳步但态度礼貌已收集到核心故障核查信息"],
  "flow_checkpoints": {},
  "expert_comments": "综合判定及格。"
}
```"""

    mock_adapter = MockAdapter(response_generator=mock_generator)
    engine = UniversalJudgeEngine(mock_adapter)

    report = engine.evaluate_dialogue(rule, scenario, dialogue)

    # 1. 元数据准确记录了拓扑断路
    assert report.topology_valid is False
    assert report.topology_error is not None
    assert "【拓扑断路】" in report.topology_error

    # 2. 打分全权遵照大模型裁决，绝未被代码强降为 50 分 FAIL！
    assert report.score == 82
    assert report.verdict == "PASS"
    # 3. 拓扑元数据作为诊断提示附带在 defects 中
    assert any("【拓扑检测元数据】" in d for d in report.detected_defects)


def test_evaluate_benchmark_response_no_nameerror_and_returns_score():
    """回归验证 evaluate_benchmark_response 不存在未定义变量 debug_collector / a_turn，且正确解析裁判 JSON"""
    from backend.app.schemas.benchmark import BenchmarkSample

    sample = BenchmarkSample(
        id="bm_test_01",
        category="reasoning",
        messages=[ChatMessage(role="user", content="1+1等于几？")],
        reference="2",
        rubric="回答准确",
    )

    def mock_generator(req):
        return '{"score": 96, "verdict": "PASS", "feedback": "回答精准"}'

    engine = UniversalJudgeEngine(MockAdapter(response_generator=mock_generator))
    res = engine.evaluate_benchmark_response(sample, "答案是 2。")
    assert res["score"] == 96.0
    assert res["verdict"] == "PASS"
    assert "精准" in res["feedback"]


def test_evaluate_faq_accuracy_exception_records_debug_snapshot():
    """回归验证 evaluate_faq_accuracy 异常时也会向 debug_collector 写入错误请求快照"""
    from backend.app.schemas.rule import FAQInspectionConfig, FAQItem

    rule = _build_test_rule(mermaid_enabled=False)
    rule.faq_inspection = FAQInspectionConfig(
        enabled=True,
        knowledge_base=[
            FAQItem(id="faq_1", question="7天内退换货运费谁承担", answer="质量问题退换货运费由星云智联全额承担。")
        ],
    )
    dialogue = [
        ChatMessage(role="user", content="我收到7天内退换货需要自付运费吗？"),
        ChatMessage(role="assistant", content="要自付，退货需要扣15元运费。"),
    ]

    class FailingAdapter(MockAdapter):
        def chat(self, req):
            raise RuntimeError("simulated faq judge timeout")

    engine = UniversalJudgeEngine(FailingAdapter())
    debug_collector = []
    deductions = engine.evaluate_faq_accuracy(
        rule=rule,
        scenario=rule.scenarios[0],
        faq_round={
            "user_turn": 1,
            "assistant_turn": 2,
            "raw_user_question": "我收到7天内退换货需要自付运费吗？",
            "rewritten_query": "7天内退换货 运费",
        },
        retrieved_candidates=[
            {"id": "faq_1", "question": "7天内退换货运费谁承担", "answer": "质量问题退换货运费由星云智联全额承担。", "score": 0.99}
        ],
        dialogue=dialogue,
        debug_collector=debug_collector,
    )
    assert deductions == []
    assert len(debug_collector) == 1
    assert debug_collector[0]["step"] == "faq_judge_turn_2"
    assert "simulated faq judge timeout" in (debug_collector[0].get("error") or "")


def test_trajectory_extract_uses_dialogue_format_without_system_and_captures_response():
    """验证 SOP 轨迹提取过滤 system、采用 [#轮次] Dialogue 格式，并记录返回内容与模型配置"""
    from backend.app.schemas.chat import ModelConfig

    rule = _build_test_rule(mermaid_enabled=True)
    scenario = rule.scenarios[0]
    dialogue = [
        ChatMessage(role="system", content="SECRET_SYSTEM_SHOULD_NOT_APPEAR_IN_TRAJ_USER"),
        ChatMessage(role="user", content="喂你好"),
        ChatMessage(role="assistant", content="您好，请问是张三本人吗？"),
        ChatMessage(role="user", content="是的"),
        ChatMessage(role="assistant", content="祝您生活愉快，再见！"),
    ]

    captured_reqs = []

    def mock_generator(req):
        captured_reqs.append(req)
        sys_text = req.messages[0].content
        if "抽取分析铁律" in sys_text:
            usr_text = req.messages[1].content
            # 1. 严禁出现 system 消息内容
            assert "SECRET_SYSTEM_SHOULD_NOT_APPEAR_IN_TRAJ_USER" not in usr_text
            # 2. 必须与主质检格式一致：## 被测实际对话记录 (Dialogue) + [#轮次]
            assert "## 被测实际对话记录 (Dialogue)" in usr_text
            assert "<dialogue>" in usr_text
            assert "[#1] USER: 喂你好" in usr_text
            assert "[#2] ASSISTANT: 您好，请问是张三本人吗？" in usr_text
            return '{"trajectory": [{"turn": 2, "node_id": "N1_1", "user_slots": [], "evidence": "核实本人"}, {"turn": 4, "node_id": "N4_1", "user_slots": [], "evidence": "结束"}]}'
        return '{"score": 98, "verdict": "PASS", "deductions": [], "expert_comments": "合规"}'

    custom_cfg = ModelConfig(
        temperature=0.3,
        top_p=1.0,
        max_tokens=10240,
        thinking_enabled=True,
        thinking_level="HIGH",
        vertex_thinking_mode="maas_reasoning_effort",
    )
    engine = UniversalJudgeEngine(
        MockAdapter(response_generator=mock_generator),
        default_config=custom_cfg,
    )
    report = engine.evaluate_dialogue(rule, scenario, dialogue)

    assert report.score == 98
    assert report.verdict == "PASS"
    assert len(report.debug_requests) == 2

    # 验证 main_judge 继承了模型中心配置，未被硬编码覆盖
    main_req = next(r for r in captured_reqs if "抽取分析铁律" not in r.messages[0].content)
    assert main_req.config.temperature == 0.3
    assert main_req.config.top_p == 1.0
    assert main_req.config.vertex_thinking_mode == "maas_reasoning_effort"

    # 验证 debug_requests 记录了返回正文、usage 与耗时
    main_dbg = next(d for d in report.debug_requests if d["step"] == "main_judge")
    assert '"score": 98' in main_dbg.get("response_content", "")
    assert main_dbg.get("usage") is not None
    assert main_dbg.get("latency_ms") is not None


def test_main_judge_retries_once_on_degenerate_non_json_output():
    """验证主质检遇到 '， ， ， ...' 退化或非 JSON 返回时保留异常现场并自动抬高温度重试 1 次"""
    rule = _build_test_rule(mermaid_enabled=False)
    scenario = rule.scenarios[0]
    dialogue = [
        ChatMessage(role="assistant", content="您好，请问是张三本人吗？"),
        ChatMessage(role="user", content="是的"),
    ]

    attempts = []

    def mock_generator(req):
        attempts.append(req.config.temperature)
        if len(attempts) == 1:
            return "， ， ， ， ， ， ， ， ， ， "
        return '{"score": 92, "verdict": "PASS", "deductions": [], "expert_comments": "重试后正常输出"}'

    engine = UniversalJudgeEngine(MockAdapter(response_generator=mock_generator))
    report = engine.evaluate_dialogue(rule, scenario, dialogue)

    assert len(attempts) == 2
    assert attempts[1] > attempts[0]
    assert report.score == 92
    assert report.verdict == "PASS"
    steps = [d["step"] for d in report.debug_requests]
    assert "main_judge_attempt_1" in steps
    assert "main_judge" in steps
    failed_dbg = next(d for d in report.debug_requests if d["step"] == "main_judge_attempt_1")
    assert "， ，" in (failed_dbg.get("response_content") or "")
    assert "纠偏重试" in (next(d for d in report.debug_requests if d["step"] == "main_judge").get("label") or "")


def test_main_judge_retry_does_not_cool_down_high_temperature():
    """验证注册温度 >= 0.6 时重试不反向降温，且 evaluate_faq_accuracy 前置异常不报 UnboundLocalError"""
    from backend.app.schemas.chat import ModelConfig

    rule = _build_test_rule(mermaid_enabled=False)
    scenario = rule.scenarios[0]
    dialogue = [
        ChatMessage(role="assistant", content="您好，请问是张三本人吗？"),
        ChatMessage(role="user", content="是的"),
    ]

    attempts = []

    def mock_generator(req):
        attempts.append(req.config.temperature)
        if len(attempts) == 1:
            return "not json"
        return '{"score": 90, "verdict": "PASS", "deductions": [], "expert_comments": "ok"}'

    engine = UniversalJudgeEngine(
        MockAdapter(response_generator=mock_generator),
        default_config=ModelConfig(temperature=0.75, top_p=0.95, max_tokens=2048),
    )
    report = engine.evaluate_dialogue(rule, scenario, dialogue)
    assert attempts == [0.75, 0.75]
    assert report.score == 90

    # evaluate_faq_accuracy 在构造 req 前就抛出异常时，必须安全返回 [] 而非 UnboundLocalError
    dbg = []
    deductions = engine.evaluate_faq_accuracy(
        rule=rule,
        scenario=scenario,
        faq_round=None,  # type: ignore  # 故意传入 None 触发早期异常
        retrieved_candidates=[],
        dialogue=dialogue,
        debug_collector=dbg,
    )
    assert deductions == []
    assert dbg == []


def test_main_judge_empty_content_and_thinking_exhaustion_never_false_passes():
    """验证主质检遇到思考耗尽 max_tokens 导致正文为空时：重试降级 thinking_level，且双次为空绝不误判为 100 分 PASS"""
    from backend.app.schemas.benchmark import BenchmarkSample
    from backend.app.schemas.chat import ModelConfig, UnifiedChatResponse, TokenUsage, PerformanceMetrics
    from backend.app.services.flywheel_service import FlywheelPipelineService

    rule = _build_test_rule(mermaid_enabled=False)
    scenario = rule.scenarios[0]
    dialogue = [
        ChatMessage(role="assistant", content="您好，请问是张三本人吗？"),
        ChatMessage(role="user", content="是的"),
    ]

    captured_levels = []
    captured_max_tokens = []

    class ThinkingExhaustedAdapter(MockAdapter):
        def chat(self, request):
            captured_levels.append(request.config.thinking_level)
            captured_max_tokens.append(request.config.max_tokens)
            return UnifiedChatResponse(
                content="",
                thinking="思考过程耗尽了全部输出额度...",
                raw_output="",
                usage=TokenUsage(prompt_tokens=100, completion_tokens=10240, thinking_tokens=10240, answer_tokens=0, total_tokens=10340),
                metrics=PerformanceMetrics(ttft_ms=10.0, total_latency_ms=50.0, tokens_per_second=100.0),
            )

    cfg = ModelConfig(temperature=0.3, max_tokens=10240, thinking_enabled=True, thinking_level="HIGH")
    engine = UniversalJudgeEngine(ThinkingExhaustedAdapter(), default_config=cfg)
    report = engine.evaluate_dialogue(rule, scenario, dialogue)

    # 1. 重试应将 HIGH 跨级降为 LOW 并放大 max_tokens 至 16384，且生成可通知用户的 notices
    assert captured_levels == ["HIGH", "LOW"]
    assert captured_max_tokens == [10240, 16384]
    # 2. 绝不可把空正文当成 {} 从而误判为 100 分 PASS，统一返回 0 分 FAIL
    assert report.score == 0
    assert report.verdict == "FAIL"
    assert any("回复内容为空" in d for d in report.detected_defects)
    assert len(report.notices) == 1
    assert "HIGH 思考" in report.notices[0] and "降级为 LOW" in report.notices[0]
    assert "降级为 LOW" in report.expert_comments
    assert FlywheelPipelineService.is_gold_pass(report, rule.inspection_rules.pass_score_threshold) is False
    # 3. 两次 debug_requests 均应记录 error，且重试记录 downgrade_notice
    assert len(report.debug_requests) == 2
    assert report.debug_requests[0].get("error")
    assert report.debug_requests[1].get("error")
    assert report.debug_requests[1].get("downgrade_notice")
    assert "耗尽" in report.debug_requests[1]["error"]

    # 4. 常规 Benchmark 裁判解析失败同样统一返回 0.0 分 FAIL，不乱打 50/60 分
    bench_sample = BenchmarkSample(
        id="b_fail_01",
        messages=[ChatMessage(role="user", content="1+1=?")],
        reference="2",
        rubric="回答 2",
    )
    bench_res = engine.evaluate_benchmark_response(bench_sample, "2")
    assert bench_res["score"] == 0.0
    assert bench_res["verdict"] == "FAIL"
