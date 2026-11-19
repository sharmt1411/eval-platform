# -*- coding: utf-8 -*-
"""
规则中心 Prompt 模板默认值、调试组装与在线实测 API 回归测试
"""
import json
import pytest
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.config import settings
from backend.app.schemas.chat import ChatMessage

client = TestClient(app)
TEST_RULE_ID = "demo_after_sales_dbg"


@pytest.fixture(autouse=True)
def setup_temp_rule_and_dataset(tmp_path, monkeypatch):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    demo_rule = {
        "rule_id": TEST_RULE_ID,
        "project_name": "星云智联售后回访SOP",
        "bot_config": {"system_prompt": "你是星云智联售后客服专员。", "max_turns": 60},
        "topology_sop": {
            "enabled": True,
            "mermaid_graph": "flowchart TD\n  N1_1(\"1.1 身份核验\") --> N2_1(\"2.1 订单确认\") --> N4_1(\"4.1 结束语\")",
            "node_id_pattern": "N[1-8]_\\d+",
            "entry_nodes": ["N1_1"],
            "terminal_nodes": ["N4_1"],
            "allow_self_loop": True,
            "allow_slot_shortcut": True,
            "allow_customer_hangup_early_exit": True,
            "require_terminal_node": False,
        },
        "faq_inspection": {
            "enabled": True,
            "retrieval_mode": "hybrid",
            "knowledge_base": [
                {
                    "id": "FAQ_001",
                    "category": "售后政策",
                    "question": "7天内退换货运费标准",
                    "answer": "质量问题退换货运费由星云智联全额承担。",
                }
            ],
            "top_k": 3,
            "fallback_policy": "若超出知识库范围，指引拨打官方售后热线。",
        },
        "inspection_rules": {"pass_score_threshold": 85, "global_redlines": ["严禁辱骂客户"]},
        "remediation_config": {"enabled": True, "freeze_user_input": True},
        "scenarios": [{"id": "sc_1", "name": "常规测试场景", "instruction": "配合完成回访"}],
    }
    (rules_dir / f"{TEST_RULE_ID}.json").write_text(
        json.dumps(demo_rule, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    monkeypatch.setattr(settings, "RULES_DIR", str(rules_dir))

    ds_file = tmp_path / "demo_bench.jsonl"
    ds_file.write_text(
        json.dumps(
            {
                "id": "gen_004",
                "category": "通用百科常识",
                "sub_category": "计算机网络",
                "difficulty": "easy",
                "messages": [{"role": "user", "content": "HTTP状态码 404 和 502 分别代表什么含义？"}],
                "reference": "404 表示未找到资源，502 表示网关错误。",
                "rubric": "准确说明 404 和 502。",
                "regex_constraints": ["未找到", "网关"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"rule_id": TEST_RULE_ID, "dataset_path": str(ds_file)}


def test_rule_default_templates_api():
    """测试获取系统内置标准质检与修复模板"""
    resp = client.get("/api/v1/rules/templates/defaults")
    assert resp.status_code == 200
    data = resp.json()
    assert "judge_template" in data
    assert "judge_plain_template" in data
    assert "trajectory_extract_template" in data
    assert "faq_extract_template" in data
    assert "faq_judge_template" in data
    assert "remediation_template" in data
    assert "client_simulator_template" in data
    assert "node_id" in data["trajectory_extract_template"]
    assert "faq_rounds" in data["faq_extract_template"]
    # 验证修复模板中包含 prune 和 replace 双工具说明与 Markdown/XML 排版
    assert "replace" in data["remediation_template"]
    assert "prune" in data["remediation_template"]
    assert "<sop_graph>" in data["remediation_template"]
    assert "<dialogue>" in data["remediation_template"]
    assert "客户言论神圣不可篡改" in data["remediation_template"]


def test_rule_debug_prompt_api(monkeypatch):
    """测试调试组装提示词 (根据数据与规则组装完整 Prompt)"""
    for mod in ("judge", "remediator", "simulator"):
        monkeypatch.setattr(
            f"backend.app.engine.{mod}.get_active_prompt_template",
            lambda _k, default: default,
        )
    # 1. 质检提示词组装
    resp_judge = client.post("/api/v1/rules/debug-prompt", json={
        "rule_id": TEST_RULE_ID,
        "prompt_type": "judge",
    })
    assert resp_judge.status_code == 200
    judge_data = resp_judge.json()
    assert "assembled_prompt" in judge_data
    assert "SOP" in judge_data["assembled_prompt"]
    assert len(judge_data["assembled_messages"]) == 2
    assert judge_data["assembled_messages"][0]["role"] == "system"
    assert judge_data["assembled_messages"][1]["role"] == "user"
    assert "<dialogue>" in judge_data["assembled_messages"][1]["content"]
    assert len(judge_data["sample_dialogue"]) > 0

    # 2. 修复提示词组装
    resp_remed = client.post("/api/v1/rules/debug-prompt", json={
        "rule_id": TEST_RULE_ID,
        "prompt_type": "remediate",
    })
    assert resp_remed.status_code == 200
    remed_data = resp_remed.json()
    assert len(remed_data["assembled_messages"]) == 2
    assert remed_data["assembled_messages"][0]["role"] == "system"
    assert remed_data["assembled_messages"][1]["role"] == "user"
    assert "<dialogue>" in remed_data["assembled_messages"][1]["content"]
    assert "assembled_prompt" in remed_data
    assert "replace" in remed_data["assembled_prompt"]
    assert "prune" in remed_data["assembled_prompt"]
    assert "<sop_graph>" in remed_data["assembled_prompt"]
    assert "<dialogue>" in remed_data["assembled_prompt"]
    assert "<defects>" in remed_data["assembled_prompt"]
    # 严格验证：修复提示词已剔除各节点质检要点与必含要素 (checkpoints)
    assert "<checkpoints>" not in remed_data["assembled_prompt"]
    assert "【各节点质检要点与必含要素】" not in remed_data["assembled_prompt"]

    # 3. 拓扑轨迹检测提示词组装 (trajectory_extract)
    resp_traj = client.post("/api/v1/rules/debug-prompt", json={
        "rule_id": TEST_RULE_ID,
        "prompt_type": "trajectory_extract",
    })
    assert resp_traj.status_code == 200
    traj_data = resp_traj.json()
    assert len(traj_data["assembled_messages"]) == 2
    assert traj_data["assembled_messages"][0]["role"] == "system"
    assert traj_data["assembled_messages"][1]["role"] == "user"
    assert "N1_1" in traj_data["assembled_messages"][0]["content"]
    assert "<dialogue>" in traj_data["assembled_messages"][1]["content"]

    # 4. FAQ 提问轮次抽取与改写提示词组装 (faq_extract) 含兜底规范注入
    default_faq_ext_tpl = client.get("/api/v1/rules/templates/defaults").json()["faq_extract_template"]
    resp_faq_ext = client.post("/api/v1/rules/debug-prompt", json={
        "rule_id": TEST_RULE_ID,
        "prompt_type": "faq_extract",
        "template": default_faq_ext_tpl,
    })
    assert resp_faq_ext.status_code == 200
    faq_ext_data = resp_faq_ext.json()
    assert len(faq_ext_data["assembled_messages"]) == 2
    assert "rewritten_query" in faq_ext_data["assembled_messages"][0]["content"]
    assert "<fallback_policy>" in faq_ext_data["assembled_messages"][0]["content"]
    assert "<dialogue>" in faq_ext_data["assembled_messages"][1]["content"]

    base_rule_faq = client.get(f"/api/v1/rules/{TEST_RULE_ID}").json()
    base_rule_faq["faq_inspection"]["fallback_policy"] = "第一行：兜底回复规则\n第二行：特殊陈述回复触发faq规则"
    resp_faq_ext_override = client.post("/api/v1/rules/debug-prompt", json={
        "rule_id": TEST_RULE_ID,
        "prompt_type": "faq_extract",
        "template": default_faq_ext_tpl,
        "rule_override": base_rule_faq,
    })
    assert resp_faq_ext_override.status_code == 200
    assert "第二行：特殊陈述回复触发faq规则" in resp_faq_ext_override.json()["assembled_messages"][0]["content"]

    # 5. FAQ 准确度质检提示词组装 (faq_judge)
    resp_faq_judge = client.post("/api/v1/rules/debug-prompt", json={
        "rule_id": TEST_RULE_ID,
        "prompt_type": "faq_judge",
    })
    assert resp_faq_judge.status_code == 200
    faq_judge_data = resp_faq_judge.json()
    assert len(faq_judge_data["assembled_messages"]) == 2
    assert "<candidate_faqs>" in faq_judge_data["assembled_messages"][1]["content"]
    assert "<faq_dialogue_slice>" in faq_judge_data["assembled_messages"][1]["content"]

    # 6. 纯规则质检提示词组装 (judge_plain) 与 客户模拟提示词组装 (client_simulator)
    resp_plain = client.post("/api/v1/rules/debug-prompt", json={
        "rule_id": TEST_RULE_ID,
        "prompt_type": "judge_plain",
    })
    assert resp_plain.status_code == 200
    assert "业务对话规则与红线质检任务" in resp_plain.json()["assembled_messages"][0]["content"]

    resp_sim = client.post("/api/v1/rules/debug-prompt", json={
        "rule_id": TEST_RULE_ID,
        "prompt_type": "client_simulator",
    })
    assert resp_sim.status_code == 200
    assert "客户扮演核心准则" in resp_sim.json()["assembled_messages"][0]["content"]

    # 7. 验证 rule_override 草稿实时生效 & 不再硬编码注入 N1_1~N4_1 虚假轨迹
    base_rule = client.get(f"/api/v1/rules/{TEST_RULE_ID}").json()
    base_rule["topology_sop"]["mermaid_graph"] = "flowchart TD\n  CUSTOM_START --> CUSTOM_END"
    base_rule["topology_sop"]["entry_nodes"] = ["CUSTOM_START"]
    base_rule["topology_sop"]["terminal_nodes"] = ["CUSTOM_END"]
    base_rule["inspection_rules"]["global_redlines"] = ["草稿红线_X99"]
    resp_draft = client.post("/api/v1/rules/debug-prompt", json={
        "rule_id": TEST_RULE_ID,
        "prompt_type": "judge",
        "rule_override": base_rule,
    })
    assert resp_draft.status_code == 200
    draft_data = resp_draft.json()
    draft_prompt = draft_data["assembled_prompt"]
    assert "草稿红线_X99" in draft_prompt
    assert "CUSTOM_START" in draft_prompt
    assert "N1_1" not in draft_data["assembled_messages"][1]["content"]

    # 8. 向后兼容：历史自定义 judge 模板若仍包含 {sop_dynamics_block}，应自动替换为动力学规则而非残留占位符
    resp_legacy_judge = client.post("/api/v1/rules/debug-prompt", json={
        "rule_id": TEST_RULE_ID,
        "prompt_type": "judge",
        "template": "自定义头部\n{sop_dynamics_block}\n<!-- USER_INPUT_START -->\n{dialogue}",
    })
    assert resp_legacy_judge.status_code == 200
    legacy_sys = resp_legacy_judge.json()["assembled_messages"][0]["content"]
    assert "{sop_dynamics_block}" not in legacy_sys
    assert "Slot Shortcut" in legacy_sys


def test_rule_test_prompt_api_with_mock(setup_temp_rule_and_dataset):
    """测试在线测试组装好的 Prompt"""
    # 注册一个临时 mock 模型
    model_id = "test-prompt-mock-llm"
    client.delete(f"/api/v1/models/{model_id}")
    client.post("/api/v1/models/", json={
        "id": model_id,
        "name": "Prompt测试专用Mock",
        "provider_type": "mock",
        "api_url": "http://127.0.0.1:8000/v1",
        "api_key": "sk-mock",
        "model_name": "mock-llm",
    })

    # 先组装修复提示词
    resp_debug = client.post("/api/v1/rules/debug-prompt", json={
        "rule_id": TEST_RULE_ID,
        "prompt_type": "remediate",
    })
    assembled = resp_debug.json()["assembled_prompt"]

    # 发起实测
    resp_test = client.post("/api/v1/rules/test-prompt", json={
        "model_id": model_id,
        "prompt_type": "remediate",
        "assembled_prompt": assembled,
    })
    assert resp_test.status_code == 200
    test_res = resp_test.json()
    assert test_res["status"] == "success"
    assert "latency_ms" in test_res
    assert "raw_content" in test_res

    # 测试 faq_judge 在线实测走 judge JSON 解析分支而非 remediate actions
    resp_faq = client.post("/api/v1/rules/test-prompt", json={
        "model_id": model_id,
        "prompt_type": "faq_judge",
        "assembled_prompt": '{"faq_deductions": []}',
    })
    assert resp_faq.status_code == 200
    faq_res = resp_faq.json()
    assert faq_res["status"] == "success"
    assert isinstance(faq_res.get("parsed_data"), dict)
    assert "actions" not in faq_res["parsed_data"]

    # 测试 trajectory_extract 与 faq_extract 在线实测解析分支
    resp_traj_test = client.post("/api/v1/rules/test-prompt", json={
        "model_id": model_id,
        "prompt_type": "trajectory_extract",
        "assembled_prompt": '{"trajectory": [{"turn": 2, "node_id": "N1_1", "user_slots": [], "evidence": "核实本人"}]}',
    })
    assert resp_traj_test.status_code == 200
    assert resp_traj_test.json()["status"] == "success"

    resp_faq_ext_test = client.post("/api/v1/rules/test-prompt", json={
        "model_id": model_id,
        "prompt_type": "faq_extract",
        "assembled_prompt": '{"faq_rounds": [{"user_turn": 5, "assistant_turn": 6, "raw_user_question": "退换货收费吗", "rewritten_query": "7天内退换货运费"}]}',
    })
    assert resp_faq_ext_test.status_code == 200
    assert resp_faq_ext_test.json()["status"] == "success"

    # 测试常规评测裁判 Prompt 在基准模块组装与实测
    dbg_bench = client.post("/api/v1/benchmarks/debug-judge-prompt", json={
        "dataset_path": setup_temp_rule_and_dataset["dataset_path"],
        "model_answer": "404 是资源未找到，502 是网关错误。",
    })
    assert dbg_bench.status_code == 200
    dbg_bench_data = dbg_bench.json()
    assert "404 是资源未找到" in dbg_bench_data["assembled_prompt"]

    from unittest.mock import patch
    from backend.app.adapters.mock_adapter import MockAdapter
    from backend.app.schemas.chat import UnifiedChatResponse
    with patch.object(
        MockAdapter,
        "chat",
        return_value=UnifiedChatResponse(
            content='{"score": 95, "verdict": "PASS", "feedback": "准确"}',
            model_name="mock-llm",
            status="SUCCESS",
        ),
    ):
        tst_bench = client.post("/api/v1/benchmarks/test-judge-prompt", json={
            "model_id": model_id,
            "assembled_prompt": dbg_bench_data["assembled_prompt"],
            "assembled_messages": dbg_bench_data["assembled_messages"],
        })
    assert tst_bench.status_code == 200
    assert tst_bench.json()["status"] == "success"
    assert tst_bench.json()["parsed_data"]["score"] == 95.0

    # 测试 /test-prompt 继承已注册模型的温度与思考参数
    custom_model_id = "test-prompt-cfg-inherit"
    client.delete(f"/api/v1/models/{custom_model_id}")
    client.post("/api/v1/models/", json={
        "id": custom_model_id,
        "name": "配置继承Mock",
        "provider_type": "mock",
        "api_url": "http://127.0.0.1:8000/v1",
        "api_key": "sk-mock",
        "model_name": "mock-llm",
        "config": {
            "temperature": 0.42,
            "top_p": 0.95,
            "max_tokens": 2048,
            "thinking_enabled": False,
            "thinking_level": "NONE",
            "vertex_thinking_mode": "maas_chat_template",
        },
    })
    captured_req = {}

    class SpyMockAdapter(MockAdapter):
        def chat(self, req):
            captured_req["config"] = req.config
            return super().chat(req)

    with patch("backend.app.api.rules.AdapterFactory.create", return_value=SpyMockAdapter()):
        resp_cfg = client.post("/api/v1/rules/test-prompt", json={
            "model_id": custom_model_id,
            "prompt_type": "judge",
            "assembled_prompt": '{"score": 99, "verdict": "PASS"}',
        })
    assert resp_cfg.status_code == 200
    assert captured_req["config"].temperature == 0.42
    assert captured_req["config"].top_p == 0.95
    assert captured_req["config"].max_tokens == 10240
    assert captured_req["config"].thinking_enabled is False
    assert captured_req["config"].vertex_thinking_mode == "maas_chat_template"

    # 清理
    client.delete(f"/api/v1/models/{model_id}")
    client.delete(f"/api/v1/models/{custom_model_id}")
