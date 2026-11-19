# -*- coding: utf-8 -*-
"""
数据飞轮：重新质检 (Re-Judge) 与重新修复 (Re-Remediate) 全链路回归测试
"""
import json
import pytest
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.config import settings
from backend.app.models import SessionLocal, FlywheelSampleRecord, RegisteredModel
from backend.app.schemas.chat import ChatMessage
from backend.app.engine.remediator import UniversalDataRemediator
from backend.app.schemas.rule import UniversalRuleSchema, PersonaDefinition
from backend.app.schemas.flywheel import InspectionReport

client = TestClient(app)
TEST_RULE_ID = "demo_after_sales_template"


@pytest.fixture(autouse=True)
def setup_mock_models(tmp_path, monkeypatch):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    demo_rule = {
        "rule_id": TEST_RULE_ID,
        "project_name": "星云智联售后回访SOP",
        "bot_config": {"system_prompt": "你是星云智联专业售后客服。", "max_turns": 60},
        "topology_sop": {
            "enabled": True,
            "mermaid_graph": "flowchart TD\n  N1_1(\"1.1 是本人？\") --> N2_1(\"2.1 订单确认\") --> N4_1(\"4.1 结束语\")",
            "node_id_pattern": "N[1-8]_\\d+",
            "entry_nodes": ["N1_1"],
            "terminal_nodes": ["N4_1"],
            "allow_self_loop": True,
            "allow_slot_shortcut": True,
            "allow_customer_hangup_early_exit": True,
            "require_terminal_node": False,
        },
        "inspection_rules": {"pass_score_threshold": 85, "global_redlines": ["严禁辱骂客户"]},
        "remediation_config": {"enabled": True, "freeze_user_input": True},
        "scenarios": [
            {"id": "case_01_standard_after_sales", "name": "常规客户场景", "instruction": "配合完成回访"},
            {"id": "manual_playground_test", "name": "自由对话测试", "instruction": "配合测试"},
        ],
    }
    (rules_dir / f"{TEST_RULE_ID}.json").write_text(
        json.dumps(demo_rule, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    monkeypatch.setattr(settings, "RULES_DIR", str(rules_dir))

    db = SessionLocal()
    # 注入 mock-judge 与 mock-remediate 模型
    for mid in ["mock-judge", "mock-remediate"]:
        existing = db.query(RegisteredModel).filter(RegisteredModel.id == mid).first()
        if not existing:
            db.add(
                RegisteredModel(
                    id=mid,
                    name=mid,
                    provider_type="mock",
                    model_name=mid,
                    api_url="http://mock.local",
                    config_json="{}",
                )
            )
    db.commit()
    db.close()


def test_remediation_prompt_anti_hanging_and_custom_instructions():
    """验证数据修复 Prompt 包含防提前挂机禁令与场景专属规则指导"""
    remediator = UniversalDataRemediator(remediate_adapter=None)

    rule = UniversalRuleSchema(
        rule_id="test_rule",
        project_name="测试规则",
        bot_config={
            "system_prompt": "你是星云智联专业售后客服。",
            "max_turns": 40,
        },
        remediation_config={
            "custom_instructions": "严禁提前挂机，补充质保条件说明",
        },
    )

    sc = PersonaDefinition(id="test_sc", name="测试场景", instruction="保持配合")
    raw_msgs = [
        ChatMessage(role="user", content="你好，干嘛？"),
        ChatMessage(role="assistant", content="您好，我是星云智联售后客服。"),
    ]
    report = InspectionReport(
        score=60,
        verdict="FAIL",
        detected_defects=["未宣讲质保条件"],
        node_trajectory=[],
        topology_valid=True,
    )

    prompt = remediator._build_remediation_prompt(rule, sc, raw_msgs, report)
    # 验证最高数据治理铁律 (客户言论不可篡改、基于原始序号)
    assert "客户言论神圣不可篡改" in prompt
    assert "基于原始序号声明" in prompt
    # 验证规则中配置的自定义业务指导被成功注入 XML 标签中
    assert "严禁提前挂机，补充质保条件说明" in prompt
    assert "<custom_business_rules>" in prompt
    assert "星云智联专业售后客服" in prompt


def test_batch_judge_and_single_judge_flow():
    """验证批量重新质检与单样本重新质检 API 闭环"""
    db = SessionLocal()
    sample_id = "test_rejudge_sample_001"
    # 清理旧数据
    db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).delete()

    dialogue = [
        {"role": "system", "content": "你是星云智联售后客服专员。"},
        {"role": "user", "content": "你好，干嘛？"},
        {"role": "assistant", "content": "您好，我是星云智联售后客服专员。"},
    ]

    record = FlywheelSampleRecord(
        id=sample_id,
        task_id="test_task",
        rule_id=TEST_RULE_ID,
        scenario_id="case_01_standard_after_sales",
        status="DISCARDED",  # 假设旧规则下被淘汰
        score=40,
        raw_messages_json=json.dumps(dialogue, ensure_ascii=False),
        remediated_messages_json=None,
        initial_eval_json=json.dumps({"score": 40, "verdict": "FAIL", "detected_defects": ["旧规则误判"]}, ensure_ascii=False),
        verified_eval_json="{}",
        updated_at=1000,
    )
    db.add(record)
    db.commit()
    db.close()

    # 1. 调用批量重新质检接口
    resp = client.post(
        "/api/v1/flywheel/samples/batch-judge",
        json={
            "sample_ids": [sample_id],
            "rule_id": TEST_RULE_ID,
            "judge_model_id": "mock-judge",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["succeeded"] == 1
    assert data["results"][0]["sample_id"] == sample_id

    # 2. 调用单样本重新质检接口
    resp_single = client.post(
        f"/api/v1/flywheel/samples/{sample_id}/judge",
        json={
            "rule_id": TEST_RULE_ID,
            "judge_model_id": "mock-judge",
        },
    )
    assert resp_single.status_code == 200, resp_single.text
    data_single = resp_single.json()
    assert data_single["sample_id"] == sample_id
    assert "initial_eval" in data_single


def test_batch_remediate_with_custom_rule():
    """验证批量重新修复支持指定 rule_id 重新执行纠偏"""
    db = SessionLocal()
    sample_id = "test_remediate_sample_002"
    db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).delete()

    dialogue = [
        {"role": "system", "content": "你是星云智联售后客服专员。"},
        {"role": "user", "content": "你好，干嘛？"},
        {"role": "assistant", "content": "您好，我是星云智联售后客服专员。"},
    ]

    record = FlywheelSampleRecord(
        id=sample_id,
        task_id="test_task",
        rule_id=TEST_RULE_ID,
        scenario_id="case_01_standard_after_sales",
        status="PENDING",
        score=50,
        raw_messages_json=json.dumps(dialogue, ensure_ascii=False),
        remediated_messages_json=None,
        initial_eval_json=json.dumps({"score": 50, "verdict": "FAIL", "detected_defects": ["缺少宣讲"]}, ensure_ascii=False),
        verified_eval_json="{}",
        updated_at=1000,
    )
    db.add(record)
    db.commit()
    db.close()

    resp = client.post(
        "/api/v1/flywheel/samples/batch-remediate",
        json={
            "sample_ids": [sample_id],
            "rule_id": TEST_RULE_ID,
            "judge_model_id": "mock-judge",
            "remediate_model_id": "mock-remediate",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["succeeded"] == 1
    assert data["results"][0]["sample_id"] == sample_id

    # 验证数据库已刷新 initial_eval_json / applied_actions_json / benchmark_slices_json
    db = SessionLocal()
    refreshed = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).first()
    assert refreshed is not None
    assert json.loads(refreshed.initial_eval_json).get("score") is not None
    assert refreshed.applied_actions_json is not None
    assert refreshed.benchmark_slices_json is not None

    # 清理测试数据
    db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id.in_([sample_id, "test_rejudge_sample_001"])).delete()
    db.commit()
    db.close()


def test_flywheel_pipeline_single_remediation_discards_on_second_fail():
    """验证 FlywheelPipelineService 严格执行单次修复 + 单次复检，复检不通过直接标记 DISCARDED"""
    from backend.app.adapters.mock_adapter import MockAdapter
    from backend.app.schemas.chat import UnifiedChatRequest
    from backend.app.schemas.rule import BotConfig
    from backend.app.services.flywheel_service import FlywheelPipelineService

    calls = {"judge": 0, "remediate": 0}

    def judge_gen(req: UnifiedChatRequest) -> str:
        calls["judge"] += 1
        return json.dumps({
            "score": 45,
            "verdict": "FAIL",
            "detected_defects": ["缺少规范核身与运费告知"],
            "expert_comments": "仍未达到及格要求",
        }, ensure_ascii=False)

    def remediate_gen(req: UnifiedChatRequest) -> str:
        calls["remediate"] += 1
        return json.dumps([
            {"tool": "replace", "turn": 2, "text": "您好，这里是客服专员。", "reason": "尝试微创修复"}
        ], ensure_ascii=False)

    rule = UniversalRuleSchema(
        rule_id="test_single_pass_rule",
        project_name="单次修复校验规则",
        bot_config=BotConfig(system_prompt="你是客服专员"),
        scenarios=[PersonaDefinition(id="sc1", name="场景1", instruction="配合测试")],
    )
    service = FlywheelPipelineService(
        judge_adapter=MockAdapter(response_generator=judge_gen),
        remediate_adapter=MockAdapter(response_generator=remediate_gen),
    )
    raw_msgs = [
        ChatMessage(role="system", content="你是客服专员"),
        ChatMessage(role="user", content="你好"),
        ChatMessage(role="assistant", content="喂啥事"),
    ]

    record = service.process_single_dialogue_record(
        rule=rule,
        scenario=rule.scenarios[0],
        raw_messages=raw_msgs,
        sample_id="single_pass_discard_001",
    )

    assert record.status == "DISCARDED"
    assert calls["remediate"] == 1
    assert calls["judge"] == 2


def test_persist_simulation_sample_persists_actions_slices_and_discards_on_second_fail():
    """验证模拟对弈委托 FlywheelPipelineService（复用 precomputed_initial_eval）正确执行单次修复、切题与落库"""
    from backend.app.adapters.mock_adapter import MockAdapter
    from backend.app.api.simulation import _persist_flywheel_record
    from backend.app.services.flywheel_service import FlywheelPipelineService
    from backend.app.schemas.chat import UnifiedChatRequest
    from backend.app.schemas.rule import BotConfig

    calls = {"judge": 0, "remediate": 0}

    def judge_gen(req: UnifiedChatRequest) -> str:
        calls["judge"] += 1
        return json.dumps({
            "score": 50,
            "verdict": "FAIL",
            "detected_defects": ["未规范核身"],
            "expert_comments": "复检仍未达标",
        }, ensure_ascii=False)

    def remediate_gen(req: UnifiedChatRequest) -> str:
        calls["remediate"] += 1
        return json.dumps({
            "thought": "补充身份核实",
            "actions": [
                {"tool": "replace", "turn": 2, "text": "您好，请核实是否张先生本人。", "reason": "补充核身"}
            ],
        }, ensure_ascii=False)

    rule = UniversalRuleSchema(
        rule_id="test_persist_sim_rule",
        project_name="对弈落库切题校验规则",
        bot_config=BotConfig(system_prompt="你是客服专员"),
        scenarios=[PersonaDefinition(id="sc_sim_01", name="模拟人设1", instruction="配合")],
    )
    dialogue = [
        ChatMessage(role="user", content="你好"),
        ChatMessage(role="assistant", content="喂"),
    ]
    initial_eval = InspectionReport(
        score=55,
        verdict="FAIL",
        detected_defects=["缺少核身"],
        deductions=[{"turn": 2, "item": "身份核实", "points": 45, "reason": "缺少核身"}],
    )

    task_id = "test_sim_persist_task"
    sample_id = f"{task_id}_sc_sim_01"
    db = SessionLocal()
    db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).delete()
    db.commit()

    try:
        remediate_adapter = MockAdapter(response_generator=remediate_gen)
        judge_adapter = MockAdapter(response_generator=judge_gen)
        pipeline = FlywheelPipelineService(
            judge_adapter=judge_adapter,
            remediate_adapter=remediate_adapter,
        )
        record = pipeline.process_single_dialogue_record(
            rule=rule,
            scenario=rule.scenarios[0],
            raw_messages=dialogue,
            sample_id=sample_id,
            precomputed_initial_eval=initial_eval,
            auto_remediate=True,
        )
        payload = _persist_flywheel_record(
            db=db,
            task_id=task_id,
            rule_id=rule.rule_id,
            scenario=rule.scenarios[0],
            raw_dialogue=dialogue,
            total_turns=2,
            record=record,
        )
        assert payload["status"] == "DISCARDED"
        assert calls["remediate"] == 1
        # 复用了 precomputed_initial_eval，仅二次质检调用 1 次 judge
        assert calls["judge"] == 1

        saved = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).first()
        assert saved is not None
        assert saved.status == "DISCARDED"
        raw_saved = json.loads(saved.raw_messages_json or "[]")
        assert raw_saved[0]["role"] == "system"
        actions = json.loads(saved.applied_actions_json or "[]")
        slices = json.loads(saved.benchmark_slices_json or "[]")
        assert len(actions) == 1 and actions[0]["turn"] == 2
        assert len(slices) == 1 and slices[0]["reference"] == "您好，请核实是否张先生本人。"
    finally:
        db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).delete()
        db.commit()
        db.close()


def test_pipeline_auto_remediate_false_and_evaluate_dialogue_pending_status():
    """验证 auto_remediate=False 时未达标落为 PENDING 且补齐首轮 system，并验证 /simulation/evaluate-dialogue 语义对齐"""
    from backend.app.adapters.mock_adapter import MockAdapter
    from backend.app.services.flywheel_service import FlywheelPipelineService
    from backend.app.schemas.chat import UnifiedChatRequest
    from backend.app.schemas.rule import BotConfig

    calls = {"judge": 0, "remediate": 0}

    def fail_judge(req: UnifiedChatRequest) -> str:
        calls["judge"] += 1
        return json.dumps({"score": 40, "verdict": "FAIL", "detected_defects": ["缺少核身"]}, ensure_ascii=False)

    def fail_remed(req: UnifiedChatRequest) -> str:
        calls["remediate"] += 1
        return "[]"

    rule = UniversalRuleSchema(
        rule_id="test_pending_rule",
        project_name="免修Pending校验规则",
        bot_config=BotConfig(system_prompt="你是专业客服"),
        scenarios=[PersonaDefinition(id="sc_pending", name="场景P", instruction="测试")],
    )
    pipeline = FlywheelPipelineService(
        judge_adapter=MockAdapter(response_generator=fail_judge),
        remediate_adapter=MockAdapter(response_generator=fail_remed),
    )
    raw_msgs = [
        ChatMessage(role="user", content="喂"),
        ChatMessage(role="assistant", content="啥事"),
    ]
    precomputed = InspectionReport(score=50, verdict="FAIL", detected_defects=["态度敷衍"])

    rec = pipeline.process_single_dialogue_record(
        rule=rule,
        scenario=rule.scenarios[0],
        raw_messages=raw_msgs,
        sample_id="pending_test_001",
        precomputed_initial_eval=precomputed,
        auto_remediate=False,
    )
    assert rec.status == "PENDING"
    assert rec.raw_messages[0].role == "system"
    assert rec.raw_messages[0].content == "你是专业客服"
    assert rec.remediated_messages is None
    assert calls["judge"] == 0
    assert calls["remediate"] == 0

    # 验证 /api/v1/simulation/evaluate-dialogue 失败入库为 PENDING（非 DISCARDED）
    resp = client.post(
        "/api/v1/simulation/evaluate-dialogue",
        json={
            "rule_id": TEST_RULE_ID,
            "scenario_id": "manual_playground_test",
            "dialogue": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "随便"},
            ],
            "judge_model_id": "mock-judge",
            "save_as_sample": True,
        },
    )
    assert resp.status_code == 200, resp.text
    eval_data = resp.json()
    saved_id = eval_data["sample_id"]
    assert saved_id is not None

    db = SessionLocal()
    try:
        row = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == saved_id).first()
        assert row is not None
        if row.status != "GOLD_PASS":
            assert row.status == "PENDING", f"仅初检未达标应落为 PENDING，实际: {row.status}"
        raw_saved = json.loads(row.raw_messages_json or "[]")
        assert raw_saved[0]["role"] == "system"
    finally:
        db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == saved_id).delete()
        db.commit()
        db.close()


def test_topology_metadata_only_does_not_trigger_remediation_or_slicing():
    """以裁判裁决为准：仅有【拓扑检测元数据】提示且裁判未扣分/未报缺陷时，应直接 GOLD_PASS 免修不切题，同时保留缺陷提示展示"""
    from backend.app.adapters.mock_adapter import MockAdapter
    from backend.app.services.flywheel_service import FlywheelPipelineService

    calls = {"judge": 0, "remediate": 0}

    def judge_gen(_req):
        calls["judge"] += 1
        return "{}"

    def rem_gen(_req):
        calls["remediate"] += 1
        return json.dumps({
            "actions": [{"tool": "replace", "turn": 2, "text": "不应该被调用", "reason": "test"}]
        }, ensure_ascii=False)

    pipeline = FlywheelPipelineService(
        judge_adapter=MockAdapter(response_generator=judge_gen),
        remediate_adapter=MockAdapter(response_generator=rem_gen),
    )

    rule = UniversalRuleSchema(
        rule_id="test_topo_meta_gold",
        project_name="测试规则",
        bot_config={"system_prompt": "你是专业客服", "max_turns": 20},
        inspection_rules={"pass_score_threshold": 85},
    )
    sc = PersonaDefinition(id="sc_1", name="崩溃客户", instruction="哭诉")
    raw_msgs = [
        ChatMessage(role="user", content="我厂着火了"),
        ChatMessage(role="assistant", content="非常理解您的难处，先别急"),
    ]

    # 场景 1: 裁判给 100 分 PASS 且 deductions=[]，仅代码附带【拓扑检测元数据】
    topo_only_eval = InspectionReport(
        score=100,
        verdict="PASS",
        deductions=[],
        detected_defects=["【拓扑检测元数据】【拓扑断路/跨分支窜流】从节点 'N3_1' 无法到达节点 'N2_3'"],
        topology_valid=False,
        topology_error="【拓扑断路/跨分支窜流】从节点 'N3_1' 无法到达节点 'N2_3'",
    )
    assert FlywheelPipelineService.is_gold_pass(topo_only_eval, 85) is True

    rec = pipeline.process_single_dialogue_record(
        rule=rule,
        scenario=sc,
        raw_messages=raw_msgs,
        sample_id="test_topo_meta_only_01",
        precomputed_initial_eval=topo_only_eval,
        auto_remediate=True,
    )
    assert rec.status == "GOLD_PASS"
    assert rec.benchmark_slices == []
    assert calls["judge"] == 0
    assert calls["remediate"] == 0
    assert rec.initial_eval is not None
    assert any("【拓扑检测元数据】" in d for d in rec.initial_eval.detected_defects)

    # 场景 2: 裁判给出了实质缺陷（即便 PASS 95 分且同时带拓扑元数据），仍须进入修复
    judge_defect_eval = InspectionReport(
        score=95,
        verdict="PASS",
        deductions=[{"turn": 2, "item": "语气生硬", "points": 5, "reason": "缺少安抚"}],
        detected_defects=[
            "[语气生硬] -5分: 缺少安抚",
            "【拓扑检测元数据】【拓扑断路】...",
        ],
        topology_valid=False,
    )
    assert FlywheelPipelineService.is_gold_pass(judge_defect_eval, 85) is False

    # 场景 3: 非 Mermaid 模式无 deductions，但裁判自行输出了 detected_defects，仍须进入修复
    non_mermaid_defect_eval = InspectionReport(
        score=90,
        verdict="PASS",
        deductions=[],
        detected_defects=["坐席态度生硬，未礼貌告别"],
        topology_valid=True,
    )
    assert FlywheelPipelineService.is_gold_pass(non_mermaid_defect_eval, 85) is False


def test_judge_and_remediation_capture_request_messages_and_thinking():
    """验证质检与修复均记录完整的 request_messages、config 与 thinking 快照并透出"""
    from backend.app.adapters.mock_adapter import MockAdapter
    from backend.app.services.flywheel_service import FlywheelPipelineService

    def judge_gen(req):
        all_text = "\n".join(m.content for m in req.messages)
        if "抽取分析铁律" in all_text:
            return json.dumps({
                "trajectory": [{"turn": 1, "node_id": "N1_1", "user_slots": [], "evidence": "问候"}]
            }, ensure_ascii=False)
        return json.dumps({
            "score": 70,
            "verdict": "FAIL",
            "deductions": [{"turn": 2, "item": "话术超长", "points": 30, "reason": "超过5句话"}],
            "expert_comments": "需要精简",
        }, ensure_ascii=False)

    def rem_gen(_req):
        return json.dumps({
            "thought": "精简第2轮",
            "actions": [{"tool": "replace", "turn": 2, "text": "您好，请问是本人吗？", "reason": "精简为1句话"}],
        }, ensure_ascii=False)

    pipeline = FlywheelPipelineService(
        judge_adapter=MockAdapter(response_generator=judge_gen, model_name="mock-judge-debug"),
        remediate_adapter=MockAdapter(response_generator=rem_gen, model_name="mock-remediate-debug"),
    )

    rule = UniversalRuleSchema(
        rule_id="test_req_debug",
        project_name="测试调试报文",
        bot_config={"system_prompt": "你是专业客服", "max_turns": 20},
        inspection_rules={"pass_score_threshold": 85, "custom_rubric": "- 单轮不超过5句话"},
    )
    sc = PersonaDefinition(id="sc_debug", name="测试场景", instruction="正常配合")
    raw_msgs = [
        ChatMessage(role="user", content="喂"),
        ChatMessage(role="assistant", content="你好，这里是非常长的一段开场白。第一句。第二句。第三句。第四句。第五句。第六句。"),
    ]

    rec = pipeline.process_single_dialogue_record(
        rule=rule,
        scenario=sc,
        raw_messages=raw_msgs,
        sample_id="test_req_debug_01",
        auto_remediate=True,
    )

    assert rec.initial_eval is not None
    assert len(rec.initial_eval.request_messages) == 2
    assert rec.initial_eval.request_messages[0]["role"] == "system"
    assert rec.initial_eval.request_messages[1]["role"] == "user"
    assert "单轮不超过5句话" in rec.initial_eval.request_messages[0]["content"]
    assert "开场白" in rec.initial_eval.request_messages[1]["content"]
    assert any(d["step"] == "main_judge" for d in rec.initial_eval.debug_requests)
    main_dbg = next(d for d in rec.initial_eval.debug_requests if d["step"] == "main_judge")
    assert main_dbg["config"]["thinking_enabled"] is True
    assert main_dbg["config"]["thinking_level"] == "HIGH"

    assert rec.remediation_debug is not None
    assert rec.remediation_debug["step"] == "remediate"
    assert rec.remediation_debug["model"] == "mock-remediate-debug"
    assert len(rec.remediation_debug["messages"]) == 2
    assert rec.remediation_debug["messages"][0]["role"] == "system"
    assert rec.remediation_debug["messages"][1]["role"] == "user"
    assert "话术超长" in rec.remediation_debug["messages"][1]["content"]
    assert "声明式修复处方单" in rec.remediation_debug["messages"][0]["content"]
    assert rec.remediation_debug["config"]["thinking_enabled"] is True
    assert rec.verified_eval is not None
    assert len(rec.verified_eval.request_messages) == 2


def test_single_remediate_options_skip_judge_and_no_auto_gold_pass_by_default():
    """验证页面重新修复默认：1) 不重跑初检直接用当前结果；2) 即使满分也不跳过修复直接通过；3) 修复后默认不自动二次质检"""
    from unittest.mock import patch
    from backend.app.adapters.mock_adapter import MockAdapter

    db = SessionLocal()
    sample_id = "test_manual_remediate_opts_001"
    db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).delete()
    dialogue = [
        {"role": "system", "content": "你是星云智联售后客服专员。"},
        {"role": "user", "content": "喂你好"},
        {"role": "assistant", "content": "您好，我是客服专员。"},
    ]
    # 0. 验证若只有【拓扑检测元数据】且无 deductions，或质检异常截断时，必须拦截并提示没有异常可以修复
    topo_only_record = FlywheelSampleRecord(
        id=sample_id,
        task_id="test_task_opts",
        rule_id=TEST_RULE_ID,
        scenario_id="case_01_standard_after_sales",
        status="PENDING",
        score=100,
        raw_messages_json=json.dumps(dialogue, ensure_ascii=False),
        remediated_messages_json=None,
        initial_eval_json=json.dumps({
            "score": 100,
            "verdict": "PASS",
            "deductions": [],
            "detected_defects": ["【拓扑检测元数据】【拓扑断路/跨分支窜流】从节点 'N3_1' 无法到达节点 'N2_3'"],
        }, ensure_ascii=False),
        verified_eval_json="{}",
        updated_at=1000,
    )
    db.add(topo_only_record)
    db.commit()
    db.close()

    call_log = {"judge": 0, "remediate": 0}

    def fake_get_adapter(db_sess, model_id, fallback_mock_name="mock"):
        if model_id == "mock-remediate" or fallback_mock_name == "mock-remediate":
            def rem_gen(_req):
                call_log["remediate"] += 1
                return json.dumps({
                    "actions": [
                        {"tool": "replace", "turn": 2, "text": "您好，我是星云智联售后专员(编号A)，请问是本人吗？", "reason": "补充核身"}
                    ]
                }, ensure_ascii=False)
            return MockAdapter(response_generator=rem_gen, model_name="mock-remediate"), None

        def judge_gen(_req):
            call_log["judge"] += 1
            return json.dumps({"score": 95, "verdict": "PASS", "deductions": []}, ensure_ascii=False)
        return MockAdapter(response_generator=judge_gen, model_name="mock-judge"), None

    try:
        with patch("backend.app.api.flywheel.get_adapter_and_config_by_model_id", side_effect=fake_get_adapter):
            resp_topo_only = client.post(
                f"/api/v1/flywheel/samples/{sample_id}/remediate",
                json={"rule_id": TEST_RULE_ID, "remediate_model_id": "mock-remediate"},
            )
            assert resp_topo_only.status_code == 400
            assert "没有异常可以修复" in resp_topo_only.json()["detail"]
            assert call_log["remediate"] == 0

            # 0b. 质检异常截断（main_judge 正文为空或含 error）时同样拦截，禁止送入修复
            db = SessionLocal()
            rec_row = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).first()
            rec_row.initial_eval_json = json.dumps({
                "score": 0,
                "verdict": "FAIL",
                "deductions": [],
                "detected_defects": ["模型未按标准 JSON 输出质检报告 (原始回复: 回复内容为空)"],
                "debug_requests": [{"step": "main_judge", "label": "主流程质检", "messages": [], "response_content": "", "error": "思考耗尽"}],
            }, ensure_ascii=False)
            db.commit()
            db.close()

            resp_truncated = client.post(
                f"/api/v1/flywheel/samples/{sample_id}/remediate",
                json={"rule_id": TEST_RULE_ID, "remediate_model_id": "mock-remediate"},
            )
            assert resp_truncated.status_code == 400
            assert "异常或输出被截断" in resp_truncated.json()["detail"]
            assert call_log["remediate"] == 0

            # 0c. 注入包含真实扣分项 (deductions) 的评估结果（即便当前 score=100 PASS）
            db = SessionLocal()
            rec_row = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).first()
            rec_row.initial_eval_json = json.dumps({
                "score": 100,
                "verdict": "PASS",
                "deductions": [{"turn": 2, "item": "核身规范", "points": 0, "reason": "建议补充标准工号"}],
                "detected_defects": ["[核身规范] -0分: 建议补充标准工号"],
            }, ensure_ascii=False)
            db.commit()
            db.close()

            # 1. 默认调用：不重跑初检、满分不跳过、修复后不自动二次质检
            resp1 = client.post(
                f"/api/v1/flywheel/samples/{sample_id}/remediate",
                json={
                    "rule_id": TEST_RULE_ID,
                    "remediate_model_id": "mock-remediate",
                },
            )
            assert resp1.status_code == 200, resp1.text
            d1 = resp1.json()
            assert call_log["judge"] == 0, "默认不应调用质检模型（既不重跑初检，也不自动二次验收）"
            assert call_log["remediate"] == 1, "即使当前为 100 分，默认也不直接通过，必须执行修复"
            assert d1["status"] == "PENDING"
            assert d1["verified_eval"] is None
            assert "编号A" in d1["remediated_messages"][2]["content"]
            assert d1["remediation_debug"] is not None

            # 2. 开启 run_verified_judge=True（仍不重跑初检）：复用当前初检，修复后直接二次质检验收
            call_log["judge"] = 0
            call_log["remediate"] = 0
            resp2 = client.post(
                f"/api/v1/flywheel/samples/{sample_id}/remediate",
                json={
                    "rule_id": TEST_RULE_ID,
                    "remediate_model_id": "mock-remediate",
                    "judge_model_id": "mock-judge",
                    "run_initial_judge": False,
                    "skip_if_gold_pass": False,
                    "run_verified_judge": True,
                },
            )
            assert resp2.status_code == 200, resp2.text
            d2 = resp2.json()
            assert call_log["remediate"] == 1
            assert call_log["judge"] >= 1
            assert d2["status"] == "REPAIRED_PASS"
            assert d2["verified_eval"] is not None
            assert d2["verified_eval"]["score"] == 95

            # 3. 开启 run_initial_judge=True + skip_if_gold_pass=True：初检满分直通 GOLD_PASS 跳过修复
            call_log["judge"] = 0
            call_log["remediate"] = 0
            resp3 = client.post(
                f"/api/v1/flywheel/samples/{sample_id}/remediate",
                json={
                    "rule_id": TEST_RULE_ID,
                    "remediate_model_id": "mock-remediate",
                    "judge_model_id": "mock-judge",
                    "run_initial_judge": True,
                    "skip_if_gold_pass": True,
                    "run_verified_judge": False,
                },
            )
            assert resp3.status_code == 200, resp3.text
            d3 = resp3.json()
            assert call_log["judge"] >= 1
            assert call_log["remediate"] == 0
            assert d3["status"] == "GOLD_PASS"
    finally:
        db = SessionLocal()
        db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).delete()
        db.commit()
        db.close()
