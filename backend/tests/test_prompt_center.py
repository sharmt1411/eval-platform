# -*- coding: utf-8 -*-
"""
提示词管理中心 (/api/v1/prompts) 全链路回归测试:
1. 自动初始化并列出全平台 8 大在用提示词模块;
2. 在线修改提示词并验证缺失占位符检测、内存缓存即时刷新与引擎实时生效;
3. 恢复系统默认模板并验证优先级 (规则自定义 > 提示词中心 > 系统默认);
4. 纯静态占位符示例预览 (/preview) 与关闭 Mermaid 时不泄漏 Mermaid 模板的回归验证。
"""
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.models import init_db
from backend.app.schemas import ChatMessage, PersonaDefinition, UniversalRuleSchema, BotConfig
from backend.app.engine.judge import UniversalJudgeEngine
from backend.app.engine.simulator import UniversalSimulator

client = TestClient(app)


def test_prompt_center_crud_and_engine_runtime_propagation():
    init_db()
    resp = client.get("/api/v1/prompts/")
    assert resp.status_code == 200
    items = resp.json()
    keys = {it["prompt_key"] for it in items}
    expected_keys = {
        "trajectory_extract",
        "judge",
        "judge_plain",
        "faq_extract",
        "faq_judge",
        "remediate",
        "client_simulator",
        "benchmark_judge",
    }
    assert expected_keys.issubset(keys), f"缺少提示词模块: {expected_keys - keys}"

    # 默认状态下所有模板的 missing_placeholders 必须为空（含可选占位符校验）
    for it in items:
        assert it["missing_placeholders"] == [], f"{it['prompt_key']} 存在误报缺失占位符: {it['missing_placeholders']}"

    detail_before = client.get("/api/v1/prompts/client_simulator").json()
    orig_tpl = detail_before["default_content"]
    custom_sim_tpl = (
        "【提示词中心定制标记_SIM_V2】\n人设:{persona_json}\n目标:{scenario_instruction}"
    )
    try:
        upd_resp = client.put(
            "/api/v1/prompts/client_simulator",
            json={"template_content": custom_sim_tpl},
        )
        assert upd_resp.status_code == 200
        upd_item = upd_resp.json()["item"]
        assert upd_item["is_customized"] is True
        assert upd_item["missing_placeholders"] == []

        sim = UniversalSimulator(adapter=None)  # type: ignore
        built = sim._build_client_prompt(
            PersonaDefinition(
                id="p1",
                name="测试人设",
                persona={"age": 30},
                instruction="询问售后",
            )
        )
        assert "【提示词中心定制标记_SIM_V2】" in built

        # 缺失必需占位符检测
        warn_resp = client.put(
            "/api/v1/prompts/client_simulator",
            json={"template_content": "仅保留纯文本无占位符"},
        )
        assert warn_resp.status_code == 200
        assert set(warn_resp.json()["item"]["missing_placeholders"]) == {
            "persona_json",
            "scenario_instruction",
        }
    finally:
        rst_resp = client.post("/api/v1/prompts/client_simulator/reset")
        assert rst_resp.status_code == 200
        assert rst_resp.json()["item"]["is_customized"] is False
        assert rst_resp.json()["item"]["template_content"] == orig_tpl


def test_prompt_center_precedence_and_plain_judge_isolation():
    init_db()
    custom_traj_tpl = (
        "【提示词中心自定义轨迹抽取】\n{mermaid_sop}\n{node_checkpoints_summary}\n<!-- USER_INPUT_START -->\n{dialogue}"
    )
    try:
        client.put(
            "/api/v1/prompts/trajectory_extract",
            json={"template_content": custom_traj_tpl},
        )
        rule = UniversalRuleSchema(
            rule_id="r_test",
            project_name="测试",
            bot_config=BotConfig(system_prompt="客服"),
        )
        rule.topology_sop.enabled = True
        rule.topology_sop.mermaid_graph = "flowchart TD\n  N1 --> N2"
        dlg = [ChatMessage(role="assistant", content="你好"), ChatMessage(role="user", content="你好")]

        judge = UniversalJudgeEngine(judge_adapter=None)  # type: ignore
        msgs_from_center = judge.build_trajectory_extract_messages(rule, dlg)
        assert "【提示词中心自定义轨迹抽取】" in msgs_from_center[0].content

        # 规则级模板优先级高于提示词中心
        rule.topology_sop.trajectory_extract_prompt_template = (
            "【规则级专属轨迹模板】\n{mermaid_sop}\n{node_checkpoints_summary}\n<!-- USER_INPUT_START -->\n{dialogue}"
        )
        msgs_from_rule = judge.build_trajectory_extract_messages(rule, dlg)
        assert "【规则级专属轨迹模板】" in msgs_from_rule[0].content
        assert "【提示词中心自定义轨迹抽取】" not in msgs_from_rule[0].content

        # 回归验证：关闭 Mermaid 时，即使规则里残留了带 {mermaid_sop} 的 judge_prompt_template，也绝不泄漏到纯规则分支
        rule.topology_sop.enabled = False
        rule.inspection_rules.judge_prompt_template = "Mermaid专属模板 {mermaid_sop} {checkpoints} {trajectory}"
        plain_msgs = judge.build_judge_messages(
            rule=rule,
            scenario=PersonaDefinition(id="s1", name="场景1"),
            dialogue=dlg,
        )
        joined_plain = "\n".join(m.content for m in plain_msgs)
        assert "{mermaid_sop}" not in joined_plain
        assert "{checkpoints}" not in joined_plain
        assert "{trajectory}" not in joined_plain
        assert "业务对话规则与红线质检任务" in plain_msgs[0].content
    finally:
        client.post("/api/v1/prompts/trajectory_extract/reset")


def test_prompt_center_static_preview_all_modules():
    init_db()
    all_keys = [
        "trajectory_extract",
        "judge",
        "judge_plain",
        "faq_extract",
        "faq_judge",
        "remediate",
        "client_simulator",
        "benchmark_judge",
    ]
    for key in all_keys:
        resp = client.post(f"/api/v1/prompts/{key}/preview", json={})
        assert resp.status_code == 200, f"{key} static preview 失败: {resp.text}"
        data = resp.json()
        assert data["prompt_key"] == key
        assert len(data["assembled_messages"]) >= 1
        assert data["assembled_prompt"].strip() != ""
        assert data["unresolved_placeholders"] == [], f"{key} 存在未替换占位符: {data['unresolved_placeholders']}"
