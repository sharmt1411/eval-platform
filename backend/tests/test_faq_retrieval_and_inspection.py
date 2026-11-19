# -*- coding: utf-8 -*-
"""
FAQ 检索质检与修复全链路测试
测试维度:
1. FAQRetriever 词法 BM25、稠密向量与混合 RRF 检索
2. 入口双开关守门: FAQ 未启用或无知识库时零开销跳过
3. Step B1: FAQ 提问抽取与 Query Rewrite
4. Step B2: 专职 FAQ 质检法官与带 #turn/reference_answer 的扣分单汇合
5. Remediator: 包含 FAQ 官方标准口径的缺陷项透传与三段式修复声明
"""
import json
import pytest
from backend.app.adapters.mock_adapter import MockAdapter
from backend.app.engine.faq_retriever import (
    UniversalFAQRetriever,
    BM25Retriever,
    tokenize_chinese_lexical,
)
from backend.app.engine.judge import UniversalJudgeEngine
from backend.app.engine.remediator import UniversalDataRemediator
from backend.app.schemas.chat import ChatMessage, UnifiedChatRequest
from backend.app.schemas.rule import (
    UniversalRuleSchema,
    BotConfig,
    TopologySOPConfig,
    InspectionRules,
    FAQInspectionConfig,
    FAQItem,
    EmbeddingConfig,
    PersonaDefinition,
)
from backend.app.schemas.flywheel import InspectionReport


def _create_sample_faq_rule(faq_enabled: bool = True) -> UniversalRuleSchema:
    kb = [
        FAQItem(
            id="FAQ_001",
            category="退换货政策",
            question="七天无理由退换货与运费承担规则",
            answer="星云智联智能设备支持7天内未拆封无理由退换，因产品质量问题退换货往返运费由商家承担。",
        ),
        FAQItem(
            id="FAQ_002",
            category="售后保障",
            question="设备质保期与上门维修服务说明",
            answer="主机享受1年官方质保，质保期内非人为损坏可免费预约工程师上门检修或寄修。",
        ),
        FAQItem(
            id="FAQ_003",
            category="延保政策",
            question="延保服务生效时间与覆盖范围",
            answer="延保服务自原厂质保期满次日起生效，覆盖主板、电源及核心传感器等主要部件。",
        ),
    ]

    return UniversalRuleSchema(
        rule_id="test_faq_rule",
        project_name="FAQ质检与修复测试规则",
        bot_config=BotConfig(system_prompt="星云智联售后回访专员"),
        topology_sop=TopologySOPConfig(
            enabled=True,
            mermaid_graph="""flowchart TD
    N1_1("1.1 是本人？") --> N2_1("2.1 使用状态回访")
    N2_1 --> N2_2("2.2 配件核对")
    N2_2 --> N2_3("2.3 使用环境确认")
    N2_3 --> N4_1("4.1 结束语")
""",
            entry_nodes=["N1_1"],
            terminal_nodes=["N4_1"],
        ),
        faq_inspection=FAQInspectionConfig(
            enabled=faq_enabled,
            retrieval_mode="hybrid",
            knowledge_base=kb,
            top_k=2,
            fallback_policy="若超出知识库范围，指引联系官方售后热线，严禁捏造。",
            embedding_config=EmbeddingConfig(provider_type="local_cpu"),
            hybrid_weights={"lexical": 0.4, "dense": 0.6},
        ),
        inspection_rules=InspectionRules(
            pass_score_threshold=85,
            global_redlines=["严禁辱骂客户"],
        ),
        scenarios=[PersonaDefinition(id="sc1", name="测试场景", instruction="配合测试")],
    )


def test_faq_retriever_bm25_and_hybrid():
    """测试 FAQ 检索器: BM25 词法与混合 RRF 检索精准度"""
    rule = _create_sample_faq_rule(faq_enabled=True)
    retriever = UniversalFAQRetriever(rule.faq_inspection)

    # 1. 词法关键词强匹配 (退换货 / 运费)
    query_1 = "请问七天内质量问题退换货收不收运费？"
    results_1 = retriever.search(query_1, top_k=2)
    assert len(results_1) > 0
    assert results_1[0]["faq"].id == "FAQ_001"
    assert "商家承担" in results_1[0]["faq"].answer

    # 2. 延保覆盖范围测试
    query_2 = "你们这个延保服务什么时候生效，覆盖哪些部件？"
    results_2 = retriever.search(query_2, top_k=2)
    assert len(results_2) > 0
    assert results_2[0]["faq"].id == "FAQ_003"
    assert "主板" in results_2[0]["faq"].answer


def test_faq_inspection_zero_cost_gate():
    """测试入口双开关守门: FAQ 质检未开启时零开销跳过"""
    rule_disabled = _create_sample_faq_rule(faq_enabled=False)

    call_count = {"count": 0}

    def counting_generator(req: UnifiedChatRequest) -> str:
        call_count["count"] += 1
        return json.dumps({"score": 95, "verdict": "PASS", "deductions": []})

    adapter = MockAdapter(response_generator=counting_generator)
    judge = UniversalJudgeEngine(adapter)

    dialogue = [
        ChatMessage(role="user", content="你好"),
        ChatMessage(role="assistant", content="您好，星云智联售后回访，请问是张女士吗？"),
        ChatMessage(role="user", content="是我，顺便问下七天质量问题退换收运费吗？"),
        ChatMessage(role="assistant", content="需要自费运费，那请问设备配件齐全吗？"),
        ChatMessage(role="user", content="齐全，再见"),
        ChatMessage(role="assistant", content="祝您生活愉快，再见！"),
    ]

    # 直接调用 extract_faq_rounds_and_rewrite 应该立即返回 []，不调用模型
    extracted = judge.extract_faq_rounds_and_rewrite(rule_disabled, dialogue)
    assert extracted == []
    assert call_count["count"] == 0


def test_dual_track_inspection_and_deductions_merge():
    """测试双轨并行质检 (流程图轨 + FAQ 轨) 并在代码层统一汇合扣分项"""
    rule = _create_sample_faq_rule(faq_enabled=True)

    def mock_judge_responses(req: UnifiedChatRequest) -> str:
        prompt = req.messages[0].content
        # Step A1: 抽取 Mermaid 轨迹
        if "抽取 Mermaid 节点轨迹" in prompt or "推进的业务节点轨迹" in prompt:
            return json.dumps({
                "trajectory": [
                    {"turn": 2, "node_id": "N1_1", "evidence": "核实身份"},
                    {"turn": 4, "node_id": "N2_2", "evidence": "询问配件核对"},
                    {"turn": 6, "node_id": "N4_1", "evidence": "结束语"},
                ]
            })
        # Step B1: 抽取 FAQ 提问轮次与改写
        elif "识别出【真实客户 (USER) 主动插播询问政策" in prompt:
            return json.dumps({
                "faq_rounds": [
                    {
                        "user_turn": 3,
                        "assistant_turn": 4,
                        "raw_user_question": "顺便问下质量问题退换货收运费吗？",
                        "rewritten_query": "七天无理由退换货与质量问题运费承担规则",
                    }
                ]
            })
        # Step B2: 专职 FAQ 质检法官打分
        elif "业务问答 (FAQ) 准确度专职质检任务" in prompt:
            return json.dumps({
                "faq_deductions": [
                    {
                        "turn": 4,
                        "item": "FAQ问答准确度 [FAQ_001: 七天退换货政策]",
                        "points": 15,
                        "reason": "客户询问质量问题退换是否收运费，官方规定由商家承担往返运费，坐席错误回答需买家自费",
                        "matched_faq_id": "FAQ_001",
                        "reference_answer": "星云智联智能设备支持7天内未拆封无理由退换，因产品质量问题退换货往返运费由商家承担。",
                    }
                ]
            })
        # Step A3: 流程与红线质检主法官
        else:
            return json.dumps({
                "score": 90,
                "verdict": "PASS",
                "deductions": [
                    {
                        "turn": 4,
                        "item": "跳步预警",
                        "points": 5,
                        "reason": "未询问设备运行状况直接进入配件核对",
                    }
                ],
                "expert_comments": "流程推进基本完整。",
            })

    adapter = MockAdapter(response_generator=mock_judge_responses)
    judge = UniversalJudgeEngine(adapter)

    dialogue = [
        ChatMessage(role="user", content="你好"),
        ChatMessage(role="assistant", content="您好，我是星云智联售后回访专员，请问是下单人张女士吗？"),
        ChatMessage(role="user", content="是我，顺便问下质量问题退换货收运费吗？"),
        ChatMessage(role="assistant", content="需买家自费运费，那请问随箱配件齐全吗？"),
        ChatMessage(role="user", content="齐全，没别的事先挂了"),
        ChatMessage(role="assistant", content="祝您生活愉快，再见！"),
    ]

    report: InspectionReport = judge.evaluate_dialogue(rule, rule.scenarios[0], dialogue)

    # 验证双轨扣分汇合: flow扣5分 + faq扣15分 = 总扣20分 -> 总分 80 分
    assert len(report.deductions) == 2
    assert report.score == 80
    assert report.verdict == "FAIL"  # 80 < pass_threshold(85)

    # 验证 FAQ deduction 携带官方标准答案透传给下游修复器
    faq_ded = next((d for d in report.deductions if d.get("matched_faq_id") == "FAQ_001"), None)
    assert faq_ded is not None
    assert faq_ded["turn"] == 4
    assert "商家承担" in faq_ded["reference_answer"]


def test_remediator_with_faq_three_stage_synthesis(monkeypatch):
    """测试修复器: 透传 FAQ reference_answer 并执行声明式三段式修复"""
    monkeypatch.setattr(
        "backend.app.engine.remediator.get_active_prompt_template",
        lambda _k, default: default,
    )
    rule = _create_sample_faq_rule(faq_enabled=True)

    dialogue = [
        ChatMessage(role="system", content="星云智联售后回访规范"),
        ChatMessage(role="user", content="你好"),
        ChatMessage(role="assistant", content="您好，星云智联回访，请问是张女士吗？"),
        ChatMessage(role="user", content="是我，顺便问下质量问题退换货收运费吗？"),
        ChatMessage(role="assistant", content="需要买家自费，那请问设备主要放在家里使用还是办公室使用呢？"),
        ChatMessage(role="user", content="主要是家里用"),
        ChatMessage(role="assistant", content="了解，祝您生活愉快，再见！"),
    ]

    eval_report = InspectionReport(
        score=75,
        verdict="FAIL",
        detected_defects=["[FAQ问答准确度] -15分: 质量问题退换货错误回答需买家自费"],
        deductions=[
            {
                "turn": 4,
                "item": "FAQ问答准确度 [FAQ_001: 七天退换货政策]",
                "points": 15,
                "reason": "客户询问质量问题退换是否收运费，官方由商家承担，坐席错误回答需买家自费",
                "matched_faq_id": "FAQ_001",
                "reference_answer": "星云智联智能设备支持7天内未拆封无理由退换，因产品质量问题退换货往返运费由商家承担。",
            }
        ],
    )

    remediator = UniversalDataRemediator(remediate_adapter=None)

    # 1. 验证 build_remediation_prompt 将结构化 reference_answer 注入了 Prompt
    prompt = remediator.build_remediation_prompt(rule, rule.scenarios[0], dialogue, eval_report)
    assert "FAQ_001" in prompt
    assert "三段式合成修复铁律" in prompt
    assert "往返运费由商家承担" in prompt

    # 2. 模拟大模型输出的三段式修复处方单 (Answer ➔ Bridge ➔ Resume)
    actions = [
        {
            "tool": "replace",
            "turn": 4,
            "text": "张女士您好，星云智联智能设备支持7天内未拆封无理由退换，因产品质量问题退换货往返运费由商家承担。跟您解释清楚后，也回到刚才跟您核实的事项，请问咱们设备主要是放在家里使用还是办公室使用呢？",
            "reason": "按照官方口径精准解答质量问题免运费(Answer)，自然过渡(Bridge)，并拉回使用场景调研主线(Resume)",
        }
    ]

    remediated_msgs, report = remediator.apply_remediation_actions(dialogue, actions)
    assert report["patched_turns"] == [4]
    assert len(report["errors"]) == 0

    # 校验最终 messages 格式
    ok, finalized, err = remediator.validate_and_sanitize_format(remediated_msgs)
    assert ok is True
    # 验证用户话术 0 篡改
    user_turns = [m.content for m in finalized if m.role == "user"]
    assert user_turns == ["你好", "是我，顺便问下质量问题退换货收运费吗？", "主要是家里用"]
    # 验证修复后的话术融合了 FAQ 答案与业务主线提问
    turn_4_asst = finalized[4]
    assert "商家承担" in turn_4_asst.content
    assert "家里使用还是办公室使用" in turn_4_asst.content


def test_non_mermaid_mode_preserves_base_score_and_detected_defects():
    """测试非 Mermaid 模式 + FAQ 双轨扣分汇总：保留主裁判基础扣分与 detected_defects"""
    rule = _create_sample_faq_rule(faq_enabled=True)
    rule.topology_sop.enabled = False

    def mock_judge_responses(req: UnifiedChatRequest) -> str:
        prompt = req.messages[0].content
        if "识别出【真实客户 (USER) 主动插播询问政策" in prompt:
            return json.dumps({
                "faq_rounds": [
                    {
                        "user_turn": 3,
                        "assistant_turn": 4,
                        "raw_user_question": "顺便问下质量问题退换货收运费吗？",
                        "rewritten_query": "七天质量问题退换货运费承担标准",
                    }
                ]
            })
        elif "业务问答 (FAQ) 准确度专职质检任务" in prompt:
            return json.dumps({
                "faq_deductions": [
                    {
                        "turn": 4,
                        "item": "FAQ问答准确度 [FAQ_001: 七天退换货政策]",
                        "points": 15,
                        "reason": "错误告知需买家自费运费",
                        "matched_faq_id": "FAQ_001",
                    }
                ]
            })
        else:
            # 非 Mermaid 通用质检仅输出 score 与 detected_defects（无 deductions 列表）
            return json.dumps({
                "score": 50,
                "verdict": "FAIL",
                "detected_defects": ["坐席态度生硬，未礼貌告别"],
                "expert_comments": "存在服务态度严重缺陷",
            })

    judge = UniversalJudgeEngine(MockAdapter(response_generator=mock_judge_responses))
    dialogue = [
        ChatMessage(role="user", content="你好"),
        ChatMessage(role="assistant", content="干嘛"),
        ChatMessage(role="user", content="质量问题退换收运费吗？"),
        ChatMessage(role="assistant", content="自己掏运费"),
    ]

    report = judge.evaluate_dialogue(rule, rule.scenarios[0], dialogue)
    # 主裁判扣 50 分 (100 - 50) + FAQ 扣 15 分 => 35 分 FAIL，不能虚高为 85 分
    assert report.score == 35
    assert report.verdict == "FAIL"
    assert any("坐席态度生硬" in d for d in report.detected_defects)
    assert any("FAQ问答准确度" in d for d in report.detected_defects)


def test_non_mermaid_float_score_and_redline_fail_preserved():
    """测试非 Mermaid 模式返回浮点字符串分数与红线 FAIL 时，叠加轻微 FAQ 扣分不抛异常且保留 FAIL 判决"""
    rule = _create_sample_faq_rule(faq_enabled=True)
    rule.topology_sop.enabled = False
    rule.inspection_rules.pass_score_threshold = 85

    def mock_judge_responses(req: UnifiedChatRequest) -> str:
        prompt = req.messages[0].content
        if "识别出【真实客户 (USER) 主动插播询问政策" in prompt:
            return json.dumps({
                "faq_rounds": [
                    {
                        "user_turn": 3,
                        "assistant_turn": 4,
                        "raw_user_question": "质量问题退换收运费吗？",
                        "rewritten_query": "七天质量问题退换货运费承担标准",
                    }
                ]
            })
        elif "业务问答 (FAQ) 准确度专职质检任务" in prompt:
            return json.dumps({
                "faq_deductions": [
                    {
                        "turn": 4,
                        "item": "FAQ问答准确度 [FAQ_001: 七天退换货政策]",
                        "points": 2,
                        "reason": "口径表述略不严谨",
                        "matched_faq_id": "FAQ_001",
                    }
                ]
            })
        else:
            return json.dumps({
                "score": "90.0",
                "verdict": "FAIL",
                "detected_defects": ["触碰全局红线: 私自违规承诺额外赔付"],
                "expert_comments": "存在红线一票否决",
            })

    judge = UniversalJudgeEngine(MockAdapter(response_generator=mock_judge_responses))
    dialogue = [
        ChatMessage(role="user", content="你好"),
        ChatMessage(role="assistant", content="您好，给您额外赔付现金"),
        ChatMessage(role="user", content="质量问题退换收运费吗？"),
        ChatMessage(role="assistant", content="一般不收"),
    ]

    report = judge.evaluate_dialogue(rule, rule.scenarios[0], dialogue)
    assert report.score == 88
    assert report.verdict == "FAIL"
    assert any("私自违规承诺额外赔付" in d for d in report.detected_defects)
