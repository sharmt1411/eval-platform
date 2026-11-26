# -*- coding: utf-8 -*-
"""
API 与引擎全链路回归测试套件:
覆盖模型增删改查及连通性、Benchmark 克隆配置与评测删除/API异常隔离、对弈入库/流式SSE对弈/样本删除、批量治理任务。
"""
import os
import sys
import json
import time
import pathlib
import tempfile

# 将项目根目录加入 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.config import settings
from backend.app.models import init_db

init_db()
client = TestClient(app)


def test_full_regression(tmp_path: pathlib.Path, monkeypatch):
    print("🚀 开始执行 API 与引擎全链路回归测试...")

    # 在 tmp_path 下创建纯虚构的通用测试规则与评测集，不依赖本地 data/ 资产
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    test_rule_id = "demo_after_sales_template"
    demo_rule = {
        "rule_id": test_rule_id,
        "project_name": "星云智联智能设备售后回访SOP",
        "bot_config": {
            "system_prompt": "你是星云智联售后客服中心的专业客服专员。",
            "initiator": "user",
            "max_turns": 60,
            "closing_keywords": ["再见"],
        },
        "topology_sop": {
            "enabled": True,
            "mermaid_graph": "flowchart TD\n  N1_1(\"1.1 是下单人吗？\") --> N2_1(\"2.1 使用状态回访\") --> N4_1(\"4.1 结束语\")",
            "node_id_pattern": "N[1-4]_\\d+",
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
            {
                "id": "case_01_standard_relief",
                "name": "常规客户场景",
                "persona": {"posture": "平和配合"},
                "instruction": "配合完成常规回访。",
            }
        ],
    }
    (rules_dir / f"{test_rule_id}.json").write_text(
        json.dumps(demo_rule, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    monkeypatch.setattr(settings, "RULES_DIR", str(rules_dir))

    tmp_bench_file = tmp_path / "demo_bench.jsonl"
    tmp_bench_rows = [
        {
            "id": "bench_001",
            "category": "通用问答",
            "sub_category": "政策说明",
            "difficulty": "easy",
            "max_new_tokens": 128,
            "messages": [{"role": "user", "content": "请问智能设备支持七天无理由退货吗？"}],
            "reference": "支持七天无理由退换货，质量问题免运费。",
            "rubric": "说明七天退换规则即可。",
            "regex_constraints": ["Mock回复"],
        },
        {
            "id": "gen_004",
            "category": "通用百科常识",
            "sub_category": "计算机网络",
            "difficulty": "easy",
            "max_new_tokens": 64,
            "messages": [{"role": "user", "content": "HTTP 404 代表什么？"}],
            "reference": "未找到资源。",
            "rubric": "说明 404 为未找到资源。",
            "regex_constraints": ["Mock回复"],
        },
    ]
    tmp_bench_file.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in tmp_bench_rows) + "\n",
        encoding="utf-8",
    )
    tmp_bench_path = str(tmp_bench_file)

    # 1. 测试模型注册
    test_model_id = "test-mock-dual-config"
    client.delete(f"/api/v1/models/{test_model_id}")

    reg_payload = {
        "id": test_model_id,
        "name": "测试Mock模型",
        "provider_type": "mock",
        "api_url": "http://127.0.0.1:8000/v1",
        "api_key": "sk-test123456",
        "model_name": "mock-llm-v1",
        "config": {
            "temperature": 0.7,
            "max_tokens": 128,
            "thinking_enabled": True,
            "thinking_level": "LOW",
        },
    }
    r = client.post("/api/v1/models/", json=reg_payload)
    assert r.status_code == 200, f"模型注册失败: {r.text}"
    print("✅ 1. 模型注册接口测试通过")

    # 2. 测试 PUT /api/v1/models/{id} 更新模型
    upd_payload = {
        "name": "测试Mock模型-已更新",
        "config": {
            "temperature": 0.2,
            "thinking_enabled": False,
        },
    }
    r_put = client.put(f"/api/v1/models/{test_model_id}", json=upd_payload)
    assert r_put.status_code == 200, f"模型更新失败: {r_put.text}"

    # 验证更新是否生效
    r_list = client.get("/api/v1/models/")
    m_found = next((m for m in r_list.json() if m["id"] == test_model_id), None)
    assert m_found is not None
    assert m_found["name"] == "测试Mock模型-已更新"
    assert m_found["config"]["temperature"] == 0.2
    assert m_found["config"]["thinking_enabled"] is False
    print("✅ 2. PUT /api/v1/models/{id} 更新模型接口测试通过")

    # 3. 测试 POST /api/v1/models/{id}/test 连通性测试 (保持 thinking 设置)
    r_test = client.post(f"/api/v1/models/{test_model_id}/test")
    assert r_test.status_code == 200
    assert r_test.json()["status"] == "SUCCESS"
    print("✅ 3. POST /api/v1/models/{id}/test 连通性测试通过")

    # 4. 测试 Benchmark 对比克隆配置 (同模型不同思考开关 + 凭证自动回填 + 矩阵防覆盖 + 步进落盘)
    bench_payload = {
        "dataset_path": tmp_bench_path,
        "model_configs": [
            {
                "model_id": test_model_id,
                "display_name": "Mock模型 (思考关)",
                "config": {"thinking_enabled": False},
            },
            {
                "model_id": test_model_id,
                "display_name": "Mock模型 (思考开)",
                "config": {"thinking_enabled": True},
            },
        ],
        "workers": 2,
    }
    r_bench = client.post("/api/v1/benchmarks/run", json=bench_payload)
    assert r_bench.status_code == 200, f"提交评测失败: {r_bench.text}"
    bench_task_id = r_bench.json()["task_id"]
    print(f"   提交评测任务成功, task_id={bench_task_id}")

    # 等待后台任务完成
    max_wait = 20
    for _ in range(max_wait):
        r_stat = client.get(f"/api/v1/benchmarks/tasks/{bench_task_id}")
        stat = r_stat.json()["status"]
        if stat in ("COMPLETED", "FAILED"):
            break
        time.sleep(0.5)

    r_final = client.get(f"/api/v1/benchmarks/tasks/{bench_task_id}")
    task_info = r_final.json()
    assert task_info["status"] == "COMPLETED", f"评测任务未成功: {task_info.get('error_message')}"

    summaries = task_info["result"]["summaries"]
    assert "Mock模型 (思考关)" in summaries, f"缺少克隆配置1: {list(summaries.keys())}"
    assert "Mock模型 (思考开)" in summaries, f"缺少克隆配置2: {list(summaries.keys())}"

    # 检验 Side-by-Side 数据可读性与 status 字段
    r_sbs = client.get(f"/api/v1/benchmarks/tasks/{bench_task_id}/side_by_side")
    assert r_sbs.status_code == 200
    sbs_data = r_sbs.json()
    first_sid = list(sbs_data.keys())[0]
    first_m_res = sbs_data[first_sid]["results"]["Mock模型 (思考关)"]
    assert "status" in first_m_res
    print(f"✅ 4. Benchmark 克隆配置评测与 Side-by-Side 验证通过: status={first_m_res['status']}")

    # 5. 测试 DELETE /api/v1/benchmarks/tasks/{id} 删除任务与产物文件
    r_del_bench = client.delete(f"/api/v1/benchmarks/tasks/{bench_task_id}")
    assert r_del_bench.status_code == 200, f"删除任务失败: {r_del_bench.text}"
    r_del_check = client.get(f"/api/v1/benchmarks/tasks/{bench_task_id}")
    assert r_del_check.status_code == 404, "删除后查询该任务必须返回 404"
    print("✅ 5. DELETE /api/v1/benchmarks/tasks/{id} 删除任务接口验证通过")

    # 5.1 测试 POST /api/v1/benchmarks/tasks/batch-delete 批量删除评测任务
    # 创建2个轻量临时任务用于测试批量删除
    t1_res = client.post("/api/v1/benchmarks/run", json={
        "dataset_path": tmp_bench_path,
        "model_ids": [test_model_id],
        "workers": 1,
    })
    t2_res = client.post("/api/v1/benchmarks/run", json={
        "dataset_path": tmp_bench_path,
        "model_ids": [test_model_id],
        "workers": 1,
    })
    t1_id = t1_res.json()["task_id"]
    t2_id = t2_res.json()["task_id"]
    r_batch_del = client.post("/api/v1/benchmarks/tasks/batch-delete", json={"task_ids": [t1_id, t2_id]})
    assert r_batch_del.status_code == 200
    assert r_batch_del.json()["deleted_count"] == 2
    assert client.get(f"/api/v1/benchmarks/tasks/{t1_id}").status_code == 404
    assert client.get(f"/api/v1/benchmarks/tasks/{t2_id}").status_code == 404
    print("✅ 5.1 POST /api/v1/benchmarks/tasks/batch-delete 批量删除接口验证通过")

    # 6. 测试底层 API 异常独立隔离 (不混淆为模型业务规则未通过，重试5次)
    from backend.app.schemas import BenchmarkSample, ModelEvalConfig, ChatMessage
    from backend.app.adapters.mock_adapter import MockAdapter
    from backend.app.engine.benchmark_runner import BenchmarkRunner

    class FailingAdapter(MockAdapter):
        def chat(self, req):
            raise ConnectionError("502 Bad Gateway: 模拟网络超时或上游熔断")

    sample_api_err = BenchmarkSample(
        id="sample_err_check",
        messages=[ChatMessage(role="user", content="ping")],
        regex_constraints=["pong"]
    )
    fail_cfg = ModelEvalConfig(model_id="fail-model", provider_type="mock")
    b_runner = BenchmarkRunner()
    fail_res = b_runner.evaluate_sample_single_model(sample_api_err, fail_cfg, FailingAdapter("fail-model"))
    assert fail_res.status == "API_ERROR", f"期望状态为 API_ERROR, 实际为: {fail_res.status}"
    assert "502 Bad Gateway" in (fail_res.error_message or "")
    assert fail_res.judge_verdict == "API_ERROR"
    # 确认 API 异常并未执行正向正则扣分
    assert "regex_missing" not in fail_res.objective_details
    print("✅ 6. API 异常独立隔离逻辑与状态标记验证通过")

    # 7. 测试 POST /api/v1/simulation/simulate 模拟对弈 (精准指定 scenario_ids 场景过滤)
    sim_payload = {
        "rule_id": test_rule_id,
        "target_model_id": test_model_id,
        "client_model_id": test_model_id,
        "judge_model_id": test_model_id,
        "scenario_ids": ["case_01_standard_relief"],
        "max_turns": 4,
    }
    r_sim = client.post("/api/v1/simulation/simulate", json=sim_payload)
    assert r_sim.status_code == 200, f"模拟对弈失败: {r_sim.text}"
    sim_data = r_sim.json()
    assert len(sim_data["results"]) == 1, f"期望精准执行1个指定场景，实际返回了: {len(sim_data['results'])}"
    first_res = sim_data["results"][0]
    assert first_res["scenario_id"] == "case_01_standard_relief"
    sample_id = first_res["sample_id"]
    assert sample_id is not None
    assert len(first_res["dialogue"]) > 0
    # 确认旧 /api/v1/flywheel/simulate 已彻底剥离 (返回 404/405)
    r_old_sim = client.post("/api/v1/flywheel/simulate", json=sim_payload)
    assert r_old_sim.status_code in (404, 405), f"旧路由应已移除，实际: {r_old_sim.status_code}"
    print(f"✅ 7. POST /api/v1/simulation/simulate 指定场景对弈成功，生成 sample_id={sample_id}")

    # 8. 测试 POST /api/v1/simulation/simulate/stream 实时流式对弈接口 (SSE) & /battles/recent
    stream_payload = {
        "rule_id": test_rule_id,
        "target_model_id": test_model_id,
        "client_model_id": test_model_id,
        "judge_model_id": test_model_id,
        "scenario_ids": ["case_01_standard_relief"],
        "max_turns": 3,
        "auto_remediate": False,
    }
    stream_sample_id = None
    with client.stream("POST", "/api/v1/simulation/simulate/stream", json=stream_payload) as stream_resp:
        assert stream_resp.status_code == 200
        events_received = []
        for line in stream_resp.iter_lines():
            if line.startswith("data: "):
                data_str = line[6:]
                ev = json.loads(data_str)
                events_received.append(ev.get("event"))
                if ev.get("event") == "scenario_done":
                    stream_sample_id = ev.get("data", {}).get("sample_id")
        assert "scenario_start" in events_received, f"未收到 scenario_start 事件: {events_received}"
        assert "turn" in events_received, f"未收到 turn 实时轮次事件: {events_received}"
        assert "scenario_done" in events_received, f"未收到 scenario_done 事件: {events_received}"
        assert "finish" in events_received, f"未收到 finish 事件: {events_received}"

    r_recent = client.get(f"/api/v1/simulation/battles/recent?rule_id={test_rule_id}&limit=10")
    assert r_recent.status_code == 200, f"查询最近对弈失败: {r_recent.text}"
    assert any(item["sample_id"] == sample_id for item in r_recent.json()), "recent battles 应包含刚生成的样本"
    if stream_sample_id:
        client.delete(f"/api/v1/flywheel/samples/{stream_sample_id}")
    print(f"✅ 8. POST /api/v1/simulation/simulate/stream 实时流式 SSE 测试通过，收到事件: {set(events_received)}")

    # 9. 测试 DELETE /api/v1/flywheel/samples/{sample_id} 样本删除接口
    r_del = client.delete(f"/api/v1/flywheel/samples/{sample_id}")
    assert r_del.status_code == 200
    r_check = client.get(f"/api/v1/flywheel/samples?task_id={sim_data['task_id']}")
    assert not any(s["id"] == sample_id for s in r_check.json()), "已删除的样本不应存在"
    print(f"✅ 9. DELETE /api/v1/flywheel/samples/{sample_id} 样本记录删除接口测试通过")
    # 10. 测试 POST /api/v1/flywheel/batch 批量治理任务提交
    batch_payload = {
        "rule_id": test_rule_id,
        "input_file_path": tmp_bench_path,
        "judge_model_id": test_model_id,
        "workers": 2,
    }
    r_batch = client.post("/api/v1/flywheel/batch", json=batch_payload)
    assert r_batch.status_code == 200, f"批量任务提交失败: {r_batch.text}"
    batch_task_id = r_batch.json()["task_id"]
    assert batch_task_id.startswith("fly_")
    print(f"✅ 10. POST /api/v1/flywheel/batch 批量治理任务提交成功: {batch_task_id}")

    # 11. 测试 POST /api/v1/datasets/import 外部评测集导入与新版字段/正则正反向约束校验
    from backend.app.engine.benchmark_runner import check_objective_rules

    sample_gen_004 = {
        "id": "gen_004",
        "category": "通用百科常识",
        "sub_category": "计算机网络",
        "difficulty": "easy",
        "source": "MMLU计算机基础",
        "max_new_tokens": 64,
        "messages": [
            {"role": "user", "content": "请简要解释 HTTP 状态码 404 和 502 分别代表什么含义？"}
        ],
        "reference": "404 Not Found 表示客户端请求的资源在服务器上未找到；502 Bad Gateway 表示作为网关或代理角色的服务器从上游服务器收到了无效响应。",
        "rubric": "准确说明 404 为未找到资源/Not Found，502 为网关错误/Bad Gateway。",
        "regex_constraints": ["未找到|不存在|Not Found", "网关|上游|Bad Gateway", "!星云智联|专员A|售后回访"],
    }
    import_payload = {
        "dataset_id": "test_ext_bench",
        "samples": [sample_gen_004],
    }
    r_imp = client.post("/api/v1/datasets/import", json=import_payload)
    assert r_imp.status_code == 200

    r_prev = client.get("/api/v1/datasets/test_ext_bench/samples")
    assert r_prev.status_code == 200
    prev_samples = r_prev.json()["samples"]
    assert len(prev_samples) == 1
    assert prev_samples[0]["id"] == "gen_004"
    assert prev_samples[0]["sub_category"] == "计算机网络"
    assert prev_samples[0]["difficulty"] == "easy"
    assert prev_samples[0]["source"] == "MMLU计算机基础"
    assert prev_samples[0]["max_new_tokens"] == 64
    assert prev_samples[0]["rubric"] == "准确说明 404 为未找到资源/Not Found，502 为网关错误/Bad Gateway。"
    assert prev_samples[0]["regex_constraints"] == ["未找到|不存在|Not Found", "网关|上游|Bad Gateway", "!星云智联|专员A|售后回访"]

    # 验证客观正则算子：合规回答 vs 灾难性遗忘身份串台回答
    ok_res = check_objective_rules(
        "404表示未找到资源，502表示错误的网关响应。",
        rubric=sample_gen_004["rubric"],
        regex_constraints=sample_gen_004["regex_constraints"],
    )
    assert ok_res["passed"] is True, f"期望合规回答通过客观正则校验: {ok_res}"

    bad_res = check_objective_rules(
        "您好，星云智联专员A为您服务：404表示未找到资源，502表示网关错误。",
        rubric=sample_gen_004["rubric"],
        regex_constraints=sample_gen_004["regex_constraints"],
    )
    assert bad_res["passed"] is False, "触发 !星云智联|专员A|售后回访 反选禁含正则时必须判为不通过"
    assert len(bad_res["details"].get("regex_violated", [])) > 0

    # 清理测试导入的数据集
    r_del_ds = client.delete("/api/v1/datasets/test_ext_bench")
    assert r_del_ds.status_code == 200
    print("✅ 11. 外部评测集导入及新版格式 (regex_constraints 正/反向约束) 测试通过")


    # 12. 测试数据飞轮复核台批量管理功能 (批量状态流转 / 批量补考 / 批量导出 / 批量删除)
    from backend.app.models import SessionLocal, FlywheelSampleRecord
    db_session = SessionLocal()
    sample_id_1 = f"batch_test_01_{int(time.time())}"
    sample_id_2 = f"batch_test_02_{int(time.time())}"

    # 预置 2 条待复核样本
    rec1 = FlywheelSampleRecord(
        id=sample_id_1,
        task_id="test_task_batch",
        rule_id=test_rule_id,
        scenario_id="case_01_standard_relief",
        status="DISCARDED",
        score=50,
        raw_messages_json=json.dumps([
            {"role": "system", "content": "售后回访机器人"},
            {"role": "assistant", "content": "您好，星云智联致电，请问是张三先生吗？"},
            {"role": "user", "content": "是的，怎么了？"}
        ], ensure_ascii=False),
        initial_eval_json=json.dumps({
            "score": 50,
            "verdict": "FAIL",
            "deductions": [{"turn": 2, "item": "话术规范", "points": 50, "reason": "需补充标准身份核实"}],
            "detected_defects": ["[话术规范] -50分: 需补充标准身份核实"],
        }, ensure_ascii=False),
        updated_at=int(time.time())
    )
    rec2 = FlywheelSampleRecord(
        id=sample_id_2,
        task_id="test_task_batch",
        rule_id=test_rule_id,
        scenario_id="case_01_standard_relief",
        status="DISCARDED",
        score=45,
        raw_messages_json=json.dumps([
            {"role": "system", "content": "售后回访机器人"},
            {"role": "assistant", "content": "您好，星云智联致电，请问是李四女士吗？"},
            {"role": "user", "content": "我正在忙，有什么事快说。"}
        ], ensure_ascii=False),
        initial_eval_json=json.dumps({
            "score": 45,
            "verdict": "FAIL",
            "deductions": [{"turn": 2, "item": "话术规范", "points": 55, "reason": "需补充标准身份核实"}],
            "detected_defects": ["[话术规范] -55分: 需补充标准身份核实"],
        }, ensure_ascii=False),
        updated_at=int(time.time())
    )
    db_session.add_all([rec1, rec2])
    db_session.commit()
    db_session.close()

    # a. 测试批量更新状态为 MANUAL_APPROVED
    r_batch_status = client.post("/api/v1/flywheel/samples/batch-status", json={
        "sample_ids": [sample_id_1, sample_id_2],
        "status": "MANUAL_APPROVED",
        "reviewer_comment": "批量人工审核放行"
    })
    assert r_batch_status.status_code == 200, f"批量更新状态失败: {r_batch_status.text}"
    assert r_batch_status.json()["updated_count"] == 2

    # 校验 DB 确认状态已变更
    r_chk1 = client.get(f"/api/v1/flywheel/samples?task_id=test_task_batch")
    assert r_chk1.status_code == 200
    for s in r_chk1.json():
        if s["id"] in [sample_id_1, sample_id_2]:
            assert s["status"] == "MANUAL_APPROVED"
            assert s["reviewer_comment"] == "批量人工审核放行"

    # b. 测试批量定点补考（默认不开启二次验收，修复后落为 PENDING，待人工或二检确认）
    r_batch_remed = client.post("/api/v1/flywheel/samples/batch-remediate", json={
        "sample_ids": [sample_id_1, sample_id_2],
        "judge_model_id": test_model_id,
        "remediate_model_id": test_model_id
    })
    assert r_batch_remed.status_code == 200, f"批量定点补考失败: {r_batch_remed.text}"
    assert r_batch_remed.json()["total"] == 2
    assert r_batch_remed.json()["succeeded"] == 2

    # c. 验证 PENDING 状态不可直接导出训练集，需经人工复核放行后导出选定样本
    r_batch_exp_blocked = client.post("/api/v1/flywheel/samples/export-selected", json={
        "sample_ids": [sample_id_1, sample_id_2],
        "version_tag": "test_batch_blocked",
        "inject_thinking": False
    })
    assert r_batch_exp_blocked.status_code == 400, f"PENDING 样本不应直接导出: {r_batch_exp_blocked.text}"

    r_reapprove = client.post("/api/v1/flywheel/samples/batch-status", json={
        "sample_ids": [sample_id_1, sample_id_2],
        "status": "MANUAL_APPROVED",
        "active_version": "remediated",
        "reviewer_comment": "复核补考修复结果通过"
    })
    assert r_reapprove.status_code == 200, f"人工复核放行失败: {r_reapprove.text}"

    r_batch_exp = client.post("/api/v1/flywheel/samples/export-selected", json={
        "sample_ids": [sample_id_1, sample_id_2],
        "version_tag": "test_batch",
        "inject_thinking": False
    })
    assert r_batch_exp.status_code == 200, f"批量导出失败: {r_batch_exp.text}"
    exp_data = r_batch_exp.json()
    assert exp_data["sample_count"] == 2
    assert os.path.exists(exp_data["file_path"])
    # 清理临时导出的文件
    try:
        os.remove(exp_data["file_path"])
    except Exception:
        pass

    # d. 测试批量删除样本
    r_batch_del = client.post("/api/v1/flywheel/samples/batch-delete", json={
        "sample_ids": [sample_id_1, sample_id_2]
    })
    assert r_batch_del.status_code == 200, f"批量删除失败: {r_batch_del.text}"
    assert r_batch_del.json()["deleted_count"] == 2

    # 再次查询验证已被彻底移除
    r_chk2 = client.get(f"/api/v1/flywheel/samples?task_id=test_task_batch")
    assert len(r_chk2.json()) == 0
    print("✅ 12. 数据飞轮复核台批量管理功能 (状态流转/批量补考/选定导出/批量删除) 测试通过")

    # 清理测试模型
    client.delete(f"/api/v1/models/{test_model_id}")
    print("🎉 全部 12 项回归测试 100% 顺利通过！")

if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        class _DummyMP:
            def setattr(self, obj, name, val):
                setattr(obj, name, val)
        test_full_regression(pathlib.Path(td), _DummyMP())
