# -*- coding: utf-8 -*-
"""
测评切题引擎与基准评测集生成全链路回归测试 (Test Benchmark Slicer)
"""
import os
import json
import pytest
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.config import settings
from backend.app.schemas.chat import ChatMessage
from backend.app.schemas.rule import UniversalRuleSchema, PersonaDefinition
from backend.app.schemas.benchmark import BenchmarkSample
from backend.app.schemas.flywheel import FlywheelBenchmarkSlice, UpdateBenchmarkSlicesRequest
from backend.app.engine.slicer import ProgressiveContextSlicer
from backend.app.models.base import SessionLocal, FlywheelSampleRecord, DatasetRecord, init_db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    init_db()
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    demo_rule = {
        "rule_id": "demo_after_sales_template",
        "project_name": "星云智联智能设备售后回访SOP",
        "bot_config": {"system_prompt": "你是业务坐席。", "max_turns": 60},
        "inspection_rules": {"pass_score_threshold": 85},
        "scenarios": [
            {"id": "s1_normal", "name": "常规客户", "instruction": "正常咨询"},
            {"id": "sc_kw", "name": "违禁词测试", "instruction": "测试"},
        ],
    }
    (rules_dir / "demo_after_sales_template.json").write_text(
        json.dumps(demo_rule, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    monkeypatch.setattr(settings, "RULES_DIR", str(rules_dir))


def test_progressive_slicer_with_prune_and_replace():
    """测试在带有 prune 剪枝与 replace 替换下的不可变序号切题与累积上文防位移"""
    # 模拟一通 8 轮外呼对话 (第 1 轮是 assistant 主动开场)
    raw_dialogue = [
        ChatMessage(role="system", content="你是专业的售后客服。"),
        ChatMessage(role="assistant", content="张女士您好，我是星云智联客服专员。"),             # orig_turn 1
        ChatMessage(role="user", content="你好，有什么事？"),                                  # orig_turn 2
        ChatMessage(role="assistant", content="请问您今天有空吗？天气真不错啊！"),                 # orig_turn 3 (冗余客套)
        ChatMessage(role="user", content="挺好的，你快说正事。"),                              # orig_turn 4 (冗余客套)
        ChatMessage(role="assistant", content="主要是对您近期购买的智能设备做使用回访。"),           # orig_turn 5
        ChatMessage(role="user", content="我知道，但我最近遇到点联网问题。"),                        # orig_turn 6
        ChatMessage(role="assistant", content="这是你自己网络问题，我们不管！"),                   # orig_turn 7 (违规敷衍)
        ChatMessage(role="user", content="你怎么态度这么差？！"),                              # orig_turn 8
    ]

    # 处方单:
    # 1. prune 剪除 3, 4 轮冗余客套
    # 2. replace 替换第 7 轮违规话术
    actions = [
        {"tool": "prune", "turns": [3, 4], "reason": "剪除多余客套寒暄"},
        {
            "tool": "replace",
            "turn": 7,
            "text": "理解您的不便，请问设备具体出现了什么报错提示呢？我这边帮您登记排查并安排售后工程师跟进。",
            "reason": "纠正违规推诿语气，按业务SOP开展故障排查并安抚",
        },
    ]

    # 质检扣分项
    deductions = [
        {
            "turn": 7,
            "item": "N2_2 故障原因排查与语气合规",
            "points": 15,
            "reason": "坐席推诿拒绝服务，触碰红线且未排查设备具体故障",
        }
    ]

    slices = ProgressiveContextSlicer.slice_dialogue(
        sample_id="sample_test_001",
        raw_dialogue=raw_dialogue,
        actions=actions,
        deductions=deductions,
    )

    # 验证仅生成 1 道针对第 7 轮的切题
    assert len(slices) == 1
    sl = slices[0]
    assert sl.slice_id == "slice_sample_test_001_turn7"
    assert sl.target_turn == 7
    assert "故障原因排查与语气合规" in sl.defect_item
    assert "推诿拒绝服务" in sl.defect_reason
    assert sl.reference == "理解您的不便，请问设备具体出现了什么报错提示呢？我这边帮您登记排查并安排售后工程师跟进。"

    # 验证切题上下文 messages:
    # 1. 第 3, 4 轮由于被 prune，绝不出现在 messages 中！
    # 2. 最后一轮严格停在第 6 轮 user 客户发言！
    context_msgs = sl.messages
    assert context_msgs[0].role == "system"
    assert any("智能设备做使用回访" in m.content for m in context_msgs)
    assert not any("天气真不错" in m.content for m in context_msgs), "已剪枝的第3轮绝不能出现在上文中"
    assert context_msgs[-1].role == "user", "考题上下文必须严格以 user 结尾收尾"
    assert "联网问题" in context_msgs[-1].content


def test_first_turn_outbound_slice_defense():
    """测试外呼第 1 轮开场被扣分时的自动补位与防崩校验"""
    raw_dialogue = [
        ChatMessage(role="system", content="你是外呼客服。"),
        ChatMessage(role="assistant", content="喂，你是谁啊？"),  # orig_turn 1 (开场不合规)
        ChatMessage(role="user", content="我是张三。"),           # orig_turn 2
    ]

    actions = [
        {"tool": "replace", "turn": 1, "text": "张先生您好，我是星云智联客服专员，请问是张三先生本人吗？", "reason": "规范开场播报与身份核验"}
    ]
    deductions = [
        {"turn": 1, "item": "N1_1 开场播报", "points": 10, "reason": "未报工号与品牌名称"}
    ]

    slices = ProgressiveContextSlicer.slice_dialogue(
        sample_id="sample_first_turn",
        raw_dialogue=raw_dialogue,
        actions=actions,
        deductions=deductions,
    )

    assert len(slices) == 1
    sl = slices[0]
    assert sl.target_turn == 1
    assert sl.reference == "张先生您好，我是星云智联客服专员，请问是张三先生本人吗？"
    # 上下文必须包含 user 触发语，且以 user 结尾，绝不能只有 system 或为空
    assert sl.messages[-1].role == "user"
    assert "[系统电话已接通" in sl.messages[-1].content


def test_unpatched_defect_does_not_become_fake_reference():
    """测试未被成功 replace 修复的扣分项不会被错误切出为错题标答"""
    raw_dialogue = [
        ChatMessage(role="system", content="系统提示。"),
        ChatMessage(role="assistant", content="这是违规错误话术。"),
        ChatMessage(role="user", content="客户回答。"),
    ]
    # 无任何 replace action
    actions = []
    deductions = [{"turn": 1, "item": "违规", "points": 20, "reason": "严重违规"}]

    slices = ProgressiveContextSlicer.slice_dialogue(
        sample_id="sample_no_replace",
        raw_dialogue=raw_dialogue,
        actions=actions,
        deductions=deductions,
    )
    # 坚决不切题，杜绝将错误话术当作 reference
    assert len(slices) == 0


def test_api_slices_crud_and_two_way_sync():
    """测试 API 路由获取切片、修改切片以及双向原子回写多轮会话"""
    db = SessionLocal()
    sample_id = "test_slice_api_sample_01"

    # 清理历史旧数据
    db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).delete()
    db.commit()

    raw_msgs = [
        {"role": "system", "content": "你是业务坐席。"},
        {"role": "user", "content": "设备质保期是多久？"},
        {"role": "assistant", "content": "这个我不知道，你自己看APP。"}, # turn 2 (待修复)
    ]
    remed_msgs = [
        {"role": "system", "content": "你是业务坐席。"},
        {"role": "user", "content": "设备质保期是多久？"},
        {"role": "assistant", "content": "主机享受 1 年官方质保，非人为损坏可免费寄修。"},
    ]
    actions = [
        {"tool": "replace", "turn": 2, "text": "主机享受 1 年官方质保，非人为损坏可免费寄修。", "reason": "准确解答FAQ质保政策"}
    ]
    initial_eval = {
        "score": 50,
        "verdict": "FAIL",
        "deductions": [{"turn": 2, "item": "FAQ 质保咨询", "points": 50, "reason": "客服未解答质保期"}]
    }

    sample = FlywheelSampleRecord(
        id=sample_id,
        rule_id="demo_after_sales_template",
        scenario_id="s1_normal",
        status="REPAIRED_PASS",
        score=95,
        raw_messages_json=json.dumps(raw_msgs, ensure_ascii=False),
        remediated_messages_json=json.dumps(remed_msgs, ensure_ascii=False),
        applied_actions_json=json.dumps(actions, ensure_ascii=False),
        initial_eval_json=json.dumps(initial_eval, ensure_ascii=False),
        benchmark_slices_json="[]",
    )
    db.add(sample)
    db.commit()
    db.close()

    # 1. 测试 GET /api/v1/flywheel/samples/{id}/slices 即时切片生成
    res = client.get(f"/api/v1/flywheel/samples/{sample_id}/slices")
    assert res.status_code == 200
    data = res.json()
    assert data["sample_id"] == sample_id
    assert len(data["slices"]) == 1
    slice_item = data["slices"][0]
    assert slice_item["target_turn"] == 2
    assert "1 年官方质保" in slice_item["reference"]

    # 2. 测试 PUT /api/v1/flywheel/samples/{id}/slices 修改切片并双向回写
    slice_item["reference"] = "人工精修优化：主机享受 1 年官方质保，支持免费上门检修。"
    update_payload = {
        "slices": [slice_item],
        "sync_to_dialogue": True,
    }
    res_put = client.put(f"/api/v1/flywheel/samples/{sample_id}/slices", json=update_payload)
    assert res_put.status_code == 200

    # 检查数据库中的 remediated_messages_json 是否已原子化同步更新
    db = SessionLocal()
    updated_sample = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).first()
    remed_after_sync = json.loads(updated_sample.remediated_messages_json)
    assert any("人工精修优化" in m["content"] for m in remed_after_sync), "切片人工微调必须双向同步回写至完整多轮对话"

    # 3. 测试 POST /api/v1/flywheel/samples/{id}/export-benchmark 单样本导出
    export_req = {
        "dataset_id": "test_exported_eval_single",
        "name": "测试单样本评测集",
        "mode": "create",
    }
    res_exp = client.post(f"/api/v1/flywheel/samples/{sample_id}/export-benchmark", json=export_req)
    assert res_exp.status_code == 200
    exp_data = res_exp.json()
    assert exp_data["dataset_id"] == "test_exported_eval_single"
    assert exp_data["sample_count"] == 1
    assert os.path.exists(exp_data["file_path"])

    # 验证导出的 .jsonl 文件内容符合 BenchmarkSample 规范
    with open(exp_data["file_path"], "r", encoding="utf-8") as f:
        line = f.readline().strip()
        sample_dict = json.loads(line)
        bench_obj = BenchmarkSample(**sample_dict)
        assert bench_obj.id == slice_item["slice_id"]
        assert "人工精修优化" in bench_obj.reference
        assert bench_obj.messages[-1].role == "user"

    # 4. 测试 POST /api/v1/flywheel/batch-export-benchmark 批量合并导出
    batch_req = {
        "sample_ids": [sample_id],
        "dataset_id": "test_exported_eval_batch",
        "name": "测试批量评测集",
        "mode": "create",
        "strategy": "all_defects",
    }
    res_batch = client.post("/api/v1/flywheel/batch-export-benchmark", json=batch_req)
    assert res_batch.status_code == 200
    batch_data = res_batch.json()
    assert batch_data["sample_count"] == 1

    # 验证 DatasetRecord 已在数据库中注册
    ds_rec = db.query(DatasetRecord).filter(DatasetRecord.id == "test_exported_eval_batch").first()
    assert ds_rec is not None
    assert ds_rec.dataset_type == "benchmark"

    # 清理测试数据
    db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).delete()
    db.query(DatasetRecord).filter(DatasetRecord.id.in_(["test_exported_eval_single", "test_exported_eval_batch"])).delete()
    db.commit()
    db.close()
    if os.path.exists(exp_data["file_path"]):
        os.remove(exp_data["file_path"])
    batch_file = os.path.join("data", "datasets", "test_exported_eval_batch.jsonl")
    if os.path.exists(batch_file):
        os.remove(batch_file)


def test_two_way_sync_replays_prune_and_checkpoint_forbidden_keywords():
    """验证 NodeCheckpoint.forbidden_keywords 继承与双向回写重放 prune 及多条 replace"""
    from backend.app.schemas.rule import BotConfig, InspectionRules, NodeCheckpoint

    rule = UniversalRuleSchema(
        rule_id="test_checkpoint_kw_rule",
        project_name="检查点违禁词与双向同步测试",
        bot_config=BotConfig(system_prompt="你是业务坐席。"),
        inspection_rules=InspectionRules(
            node_checkpoints={
                "N1_1": NodeCheckpoint(
                    name="身份核实",
                    required_elements=["本人"],
                    forbidden_keywords=["违禁词"],
                )
            }
        ),
        scenarios=[PersonaDefinition(id="sc_kw", name="测试人设", instruction="测试")],
    )
    assert rule.inspection_rules.node_checkpoints["N1_1"].forbidden_keywords == ["违禁词"]

    raw_msgs = [
        {"role": "system", "content": "你是业务坐席。"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "冗余寒暄一"},
        {"role": "user", "content": "哦"},
        {"role": "assistant", "content": "请问您是哪位"},
        {"role": "user", "content": "我是张三，质保期多久"},
        {"role": "assistant", "content": "违禁词，不知道"},
    ]
    actions = [
        {"tool": "prune", "turns": [2, 3], "reason": "剪除多余寒暄"},
        {"tool": "replace", "turn": 4, "text": "请问是张先生本人吗？", "reason": "补充核身", "target_node": "N1_1"},
        {"tool": "replace", "turn": 6, "text": "主机享受 1 年官方质保。", "reason": "准确解答质保"},
    ]
    remed_msgs = [
        {"role": "system", "content": "你是业务坐席。"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "请问是张先生本人吗？"},
        {"role": "user", "content": "我是张三，质保期多久"},
        {"role": "assistant", "content": "主机享受 1 年官方质保。"},
    ]
    initial_eval = {
        "score": 60,
        "verdict": "FAIL",
        "deductions": [
            {"turn": 4, "item": "N1_1 身份核实", "points": 20, "reason": "未确认本人"},
            {"turn": 6, "item": "质保解答", "points": 20, "reason": "答错且含违禁词"},
        ],
    }

    db = SessionLocal()
    sample_id = "test_two_way_sync_prune_sample"
    db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).delete()
    db.commit()

    db.add(
        FlywheelSampleRecord(
            id=sample_id,
            rule_id="demo_after_sales_template",
            scenario_id="sc_kw",
            status="REPAIRED_PASS",
            score=95,
            raw_messages_json=json.dumps(raw_msgs, ensure_ascii=False),
            remediated_messages_json=json.dumps(remed_msgs, ensure_ascii=False),
            applied_actions_json=json.dumps(actions, ensure_ascii=False),
            initial_eval_json=json.dumps(initial_eval, ensure_ascii=False),
            benchmark_slices_json="[]",
        )
    )
    db.commit()
    db.close()

    # 验证 NodeCheckpoint.forbidden_keywords 透传进入切片 regex_constraints
    slices = ProgressiveContextSlicer.slice_dialogue(
        sample_id=sample_id,
        raw_dialogue=[ChatMessage(**m) for m in raw_msgs],
        actions=actions,
        deductions=initial_eval["deductions"],
        rule=rule,
        scenario=rule.scenarios[0],
        remediated_dialogue=[ChatMessage(**m) for m in remed_msgs],
    )
    assert len(slices) == 2
    assert "!违禁词" in slices[0].regex_constraints

    # 只修改第 2 条切片(原 turn 6)并触发双向回写
    slice_dicts = [s.model_dump() for s in slices]
    slice_dicts[1]["reference"] = "人工终审版：主机享受 1 年官方质保（含免费寄修）。"
    res_put = client.put(
        f"/api/v1/flywheel/samples/{sample_id}/slices",
        json={"slices": slice_dicts, "sync_to_dialogue": True},
    )
    assert res_put.status_code == 200

    db = SessionLocal()
    updated = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).first()
    remed_after = json.loads(updated.remediated_messages_json)
    contents = [m["content"] for m in remed_after]
    # turn 2-3 被成功剪枝保持消失，turn 4 保持第一次 replace，turn 6 为人工终审版
    assert "冗余寒暄一" not in contents
    assert "请问是张先生本人吗？" in contents
    assert "人工终审版：主机享受 1 年官方质保（含免费寄修）。" in contents

    # 验证 applied_actions_json 也已同步更新为人工终审话术
    actions_after = json.loads(updated.applied_actions_json)
    replace_turn6 = next(a for a in actions_after if a.get("turn") == 6)
    assert replace_turn6["text"] == "人工终审版：主机享受 1 年官方质保（含免费寄修）。"

    # 验证动作别名 prune_turn_pair / patch_assistant 也可通过统一回放引擎无损重放
    updated.applied_actions_json = json.dumps([
        {"action": "prune_turn_pair", "turn_indices": [2, 3], "reason": "别名剪枝"},
        {"action": "patch_assistant", "turn_index": 4, "assistant_text": "别名核身话术", "reason": "别名替换"},
        {"tool": "replace", "turn": 6, "text": "旧文本", "reason": "待覆盖"},
    ], ensure_ascii=False)
    db.commit()
    db.close()

    slice_dicts[1]["reference"] = "别名重放后终审文本"
    res_alias = client.put(
        f"/api/v1/flywheel/samples/{sample_id}/slices",
        json={"slices": [slice_dicts[1]], "sync_to_dialogue": True},
    )
    assert res_alias.status_code == 200

    db = SessionLocal()
    updated_alias = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).first()
    alias_contents = [m["content"] for m in json.loads(updated_alias.remediated_messages_json)]
    assert "冗余寒暄一" not in alias_contents
    assert "别名核身话术" in alias_contents
    assert "别名重放后终审文本" in alias_contents

    db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == sample_id).delete()
    db.commit()
    db.close()
