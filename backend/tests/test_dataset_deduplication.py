# -*- coding: utf-8 -*-
"""
全平台数据集去重与防稀释全链路回归测试 (10大核心场景)
覆盖:
1. 同规则客户输入序列强去重与跨规则隔离;
2. 训练集复检 Upsert 保护 exported_to_benchmark 标记;
3. 裁剪/修复语料后双哈希命中与老库惰性回填;
4. SOP/FAQ 4种开关组合与无规则/无人设外部数据集退化;
5. 轨迹+FAQ融合签名区分 (s1->s2[faq1]->s3 vs s1->s2->s3[faq1] vs s1->s2->s3) 与自旋合并;
6. 80%混合对话 Step 2b 按坐席回复向量拆M个情景簇、不整体截断至K、同时折叠同质克隆;
7. 含外呼开场白及相同客户上下文的评测集去重收敛;
8. 簇内 Max-Min 客户输入多样性选优与 target_total_count 差异最大化补足;
9. 外部无标签老文件 append 局部精确轮次桶命中 + .dedup_cache.json 缓存复用与删除清理;
10. /flywheel/batch Upsert + 留出排除 + export-selected 默认拦截 + 导出评测集条件置位与元数据透传。
"""
import json
import os
import uuid
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.adapters.mock_adapter import MockAdapter
from backend.app.api import flywheel as flywheel_api_mod
from backend.app.api import simulation as simulation_api_mod
from backend.app.api.deps import upsert_flywheel_sample
from backend.app.config import settings
from backend.app.engine.deduplicator import DatasetDeduplicator
from backend.app.engine.faq_retriever import UniversalFAQRetriever
from backend.app.engine.judge import UniversalJudgeEngine
from backend.app.engine.slicer import ProgressiveContextSlicer
from backend.app.services.flywheel_service import FlywheelPipelineService
from backend.app.main import app
from backend.app.models import base as models_base_mod
from backend.app import models as models_pkg_mod
from backend.app.models.base import Base, DatasetRecord, FlywheelSampleRecord, RegisteredModel, get_db
from backend.app.schemas.chat import ChatMessage, ModelConfig
from backend.app.schemas.flywheel import DedupConfig
from backend.app.schemas.rule import (
    BotConfig,
    EmbeddingConfig,
    FAQInspectionConfig,
    FAQItem,
    NodeCheckpoint,
    PersonaDefinition,
    TopologySOPConfig,
    UniversalRuleSchema,
)


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    """隔离测试环境：独立临时 SQLite + 临时 data/{datasets,rules,runs} 目录"""
    datasets_dir = str(tmp_path / "datasets")
    rules_dir = str(tmp_path / "rules")
    runs_dir = str(tmp_path / "runs")
    os.makedirs(datasets_dir, exist_ok=True)
    os.makedirs(rules_dir, exist_ok=True)
    os.makedirs(runs_dir, exist_ok=True)

    monkeypatch.setattr(settings, "DATASETS_DIR", datasets_dir)
    monkeypatch.setattr(settings, "RULES_DIR", rules_dir)
    monkeypatch.setattr(settings, "RUNS_DIR", runs_dir)

    db_path = tmp_path / "dedup_test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(models_base_mod, "SessionLocal", TestingSession)
    monkeypatch.setattr(models_pkg_mod, "SessionLocal", TestingSession)
    monkeypatch.setattr(flywheel_api_mod, "SessionLocal", TestingSession)

    def _override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db

    # 预置 Mock 质检/修复模型与基础业务规则
    db = TestingSession()
    try:
        db.add(
            RegisteredModel(
                id="mock_judge_dedup",
                name="Mock质检裁判",
                provider_type="mock",
                model_name="mock-judge",
                config_json="{}",
            )
        )
        db.add(
            RegisteredModel(
                id="mock_remed_dedup",
                name="Mock修复模型",
                provider_type="mock",
                model_name="mock-remed",
                config_json="{}",
            )
        )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    yield {
        "client": client,
        "Session": TestingSession,
        "datasets_dir": datasets_dir,
        "rules_dir": rules_dir,
        "runs_dir": runs_dir,
    }
    app.dependency_overrides.clear()
    engine.dispose()


def _save_rule_via_api(client: TestClient, rule_id: str, extra_faq: bool = False) -> None:
    payload: Dict[str, Any] = {
        "rule_id": rule_id,
        "project_name": f"规则_{rule_id}",
        "description": "去重测试专用规则",
        "bot_config": {
            "system_prompt": "你是售后客服，请礼貌核实问题并解答。",
            "initiator": "user",
            "max_turns": 20,
        },
        "topology_sop": {
            "enabled": True,
            "mermaid_graph": "graph TD\n  s1[核实订单] --> s2[解答方案]\n  s2 --> s3[礼貌告别]",
            "entry_nodes": ["s1"],
            "terminal_nodes": ["s3"],
        },
        "inspection_rules": {
            "pass_score_threshold": 85,
            "global_redlines": ["严禁使用脏话或恶意推诿"],
            "node_checkpoints": {
                "s1": {
                    "name": "核实订单",
                    "required_elements": ["询问订单号"],
                    "forbidden_keywords": ["滚", "懒得管"],
                },
                "s2": {
                    "name": "解答方案",
                    "required_elements": ["给出明确指引"],
                    "forbidden_keywords": [],
                },
                "s3": {
                    "name": "礼貌告别",
                    "required_elements": ["礼貌结束"],
                    "forbidden_keywords": [],
                },
            },
        },
        "faq_inspection": {
            "enabled": extra_faq,
            "retrieval_mode": "lexical_only",
            "knowledge_base": (
                [
                    {
                        "id": "FAQ_RET_01",
                        "question": "怎么申请七天无理由退货？",
                        "answer": "请在订单详情页点击申请退货并填写物流单号，签收7天内免审核。",
                        "category": "退货政策",
                    }
                ]
                if extra_faq
                else []
            ),
        },
        "scenarios": [
            {
                "id": "p_refund",
                "name": "退货咨询客户",
                "category": "售后",
                "persona": {"background": "想申请退货", "tone": "急切"},
                "instruction": "咨询退货退款流程",
            }
        ],
    }
    res = client.post("/api/v1/rules/", json=payload)
    assert res.status_code == 200, res.text


def test_01_same_rule_user_seq_hard_dedup_and_cross_rule_isolation(isolated_env):
    """场景 1: 同规则下客户输入序列强去重（含标点/空格归一化）+ 跨规则完全隔离"""
    client: TestClient = isolated_env["client"]
    _save_rule_via_api(client, "rule_alpha")
    _save_rule_via_api(client, "rule_beta")

    # 直接校验归一化与序列哈希等价性
    m1 = [
        ChatMessage(role="assistant", content="您好，请问有什么可以帮您？"),
        ChatMessage(role="user", content="我想退货，怎么操作？！"),
        ChatMessage(role="assistant", content="请提供您的订单号。"),
        ChatMessage(role="user", content="订单号 20261124A"),
    ]
    m2 = [
        ChatMessage(role="assistant", content="欢迎联系星云售后。"),
        ChatMessage(role="user", content="我想退货 怎么操作"),
        ChatMessage(role="assistant", content="好的请报单号"),
        ChatMessage(role="user", content="订单号：20261124a "),
    ]
    assert DatasetDeduplicator.compute_user_sequence_hash(m1) == DatasetDeduplicator.compute_user_sequence_hash(m2)

    # 通过 /simulation/evaluate-dialogue 两次提交同规则同客户话术（不同标点与客服回复）
    r1 = client.post(
        "/api/v1/simulation/evaluate-dialogue",
        json={
            "rule_id": "rule_alpha",
            "scenario_id": "p_refund",
            "judge_model_id": "mock_judge_dedup",
            "save_as_sample": True,
            "dialogue": [m.model_dump() for m in m1],
        },
    )
    assert r1.status_code == 200, r1.text
    sid_1 = r1.json()["sample_id"]

    r2 = client.post(
        "/api/v1/simulation/evaluate-dialogue",
        json={
            "rule_id": "rule_alpha",
            "scenario_id": "p_refund",
            "judge_model_id": "mock_judge_dedup",
            "save_as_sample": True,
            "dialogue": [m.model_dump() for m in m2],
        },
    )
    assert r2.status_code == 200, r2.text
    sid_2 = r2.json()["sample_id"]
    assert sid_2 == sid_1

    # 切到另一个规则 rule_beta 提交相同客户序列 -> 必须独立保留，不得跨规则误杀
    r3 = client.post(
        "/api/v1/simulation/evaluate-dialogue",
        json={
            "rule_id": "rule_beta",
            "scenario_id": "p_refund",
            "judge_model_id": "mock_judge_dedup",
            "save_as_sample": True,
            "dialogue": [m.model_dump() for m in m1],
        },
    )
    assert r3.status_code == 200, r3.text
    sid_3 = r3.json()["sample_id"]
    assert sid_3 != sid_1

    alpha_list = client.get("/api/v1/flywheel/samples", params={"rule_id": "rule_alpha"}).json()
    beta_list = client.get("/api/v1/flywheel/samples", params={"rule_id": "rule_beta"}).json()
    assert len(alpha_list) == 1
    assert len(beta_list) == 1


def test_02_training_reinspect_upsert_preserves_exported_to_benchmark(isolated_env, monkeypatch):
    """场景 2: 新规则复检回写样本池走 Upsert，且严格保留 exported_to_benchmark=True"""
    client: TestClient = isolated_env["client"]
    Session = isolated_env["Session"]
    _save_rule_via_api(client, "rule_reinspect")

    dialogue = [
        {"role": "user", "content": "我要申请七天退货"},
        {"role": "assistant", "content": "好的，请提供订单号为您核实办理。"},
    ]
    # 预先写入一条已切题导出至评测集的样本
    db = Session()
    try:
        rec, created = upsert_flywheel_sample(
            db,
            sample_id="legacy_holdout_sample",
            task_id="old_task",
            rule_id="rule_reinspect",
            scenario_id="p_refund",
            status="REPAIRED_PASS",
            score=75,
            raw_messages=dialogue,
            remediated_messages=dialogue,
            commit=True,
        )
        assert created is True
        rec.exported_to_benchmark = True
        db.commit()
        assert rec.exported_to_benchmark is True
    finally:
        db.close()

    # 导入同客户话术纯对话集并通过 /flywheel/inspect-dataset(save_to_flywheel=True) 复检
    _orig_chat = MockAdapter.chat
    monkeypatch.setattr(
        MockAdapter,
        "chat",
        lambda self, req: _orig_chat(
            MockAdapter(
                response_generator=lambda _r: json.dumps(
                    {
                        "score": 96,
                        "verdict": "PASS",
                        "node_trajectory": ["s1", "s2"],
                        "trajectory_details": [
                            {"turn": 1, "node_id": "s1", "reason": "已核实"},
                        ],
                        "detected_defects": [],
                        "deductions": [],
                        "expert_comments": "新标准复检满分通过",
                    },
                    ensure_ascii=False,
                )
            ),
            req,
        ),
    )

    imp = client.post(
        "/api/v1/datasets/import",
        json={
            "dataset_id": "reinspect_conv_ds",
            "dataset_type": "flywheel_gold",
            "mode": "overwrite",
            "samples": [{"id": "conv_row_99", "scenario_id": "p_refund", "messages": dialogue}],
        },
    )
    assert imp.status_code == 200, imp.text

    insp = client.post(
        "/api/v1/flywheel/inspect-dataset",
        json={
            "dataset_id": "reinspect_conv_ds",
            "rule_id": "rule_reinspect",
            "judge_model_id": "mock_judge_dedup",
            "save_to_flywheel": True,
        },
    )
    assert insp.status_code == 200, insp.text

    rows = client.get("/api/v1/flywheel/samples", params={"rule_id": "rule_reinspect"}).json()
    assert len(rows) == 1
    assert rows[0]["id"] == "legacy_holdout_sample"
    assert rows[0]["exported_to_benchmark"] is True
    assert rows[0]["score"] == 96
    assert rows[0]["status"] == "GOLD_PASS"


def test_03_pruned_dialogue_double_hash_upsert_and_lazy_backfill(isolated_env):
    """场景 3: 修复/裁剪后语料双哈希命中与老库 raw_user_hash=NULL 惰性回填"""
    Session = isolated_env["Session"]
    db = Session()
    try:
        raw_full = [
            {"role": "user", "content": "查一下我的订单物流"},
            {"role": "assistant", "content": "请提供单号"},
            {"role": "user", "content": "算了不要了再见"},
            {"role": "assistant", "content": "再见"},
        ]
        remed_pruned = [
            {"role": "user", "content": "查一下我的订单物流"},
            {"role": "assistant", "content": "请提供订单号为您查询。"},
        ]
        row, created = upsert_flywheel_sample(
            db,
            sample_id="pruned_01",
            task_id="t1",
            rule_id="rule_prune",
            scenario_id="default",
            status="REPAIRED_PASS",
            score=90,
            raw_messages=raw_full,
            remediated_messages=remed_pruned,
            commit=True,
        )
        assert created is True
        assert row.raw_user_hash != row.remediated_user_hash

        # 用裁剪后的序列再次入库（不同 sample_id），命中 remediated_user_hash 原位更新
        row2, created2 = upsert_flywheel_sample(
            db,
            sample_id="pruned_reimported_02",
            task_id="t2",
            rule_id="rule_prune",
            scenario_id="default",
            status="GOLD_PASS",
            score=98,
            raw_messages=remed_pruned,
            commit=True,
        )
        assert created2 is False
        assert row2.id == "pruned_01"
        assert row2.score == 98

        # 构造一条遗留老样本（哈希列为 NULL），验证惰性回填匹配
        legacy_msgs = [{"role": "user", "content": "老样本历史提问"}, {"role": "assistant", "content": "旧回复"}]
        legacy_row = FlywheelSampleRecord(
            id="legacy_null_hash",
            task_id="t_old",
            rule_id="rule_prune",
            scenario_id="default",
            status="PENDING",
            score=60,
            raw_messages_json=json.dumps(legacy_msgs, ensure_ascii=False),
            raw_user_hash=None,
            remediated_user_hash=None,
        )
        db.add(legacy_row)
        db.commit()

        matched, created3 = upsert_flywheel_sample(
            db,
            sample_id="incoming_match_legacy",
            task_id="t_new",
            rule_id="rule_prune",
            scenario_id="default",
            status="GOLD_PASS",
            score=92,
            raw_messages=legacy_msgs,
            commit=True,
        )
        assert created3 is False
        assert matched.id == "legacy_null_hash"
        assert matched.raw_user_hash is not None
        assert db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.rule_id == "rule_prune").count() == 2
    finally:
        db.close()


def test_04_four_rule_switch_combos_and_external_degradation():
    """场景 4: SOP/FAQ 四种开关组合（含退化池路由）与无规则/无人设外部数据优雅降级"""
    # 1. SOP 开 + FAQ 开
    pk1, sig1 = DatasetDeduplicator.build_dialogue_trajectory_signature(
        rule_id="r_both",
        scenario_id="p_refund",
        trajectory_details=[{"turn": 1, "node_id": "s1"}, {"turn": 2, "node_id": "s2"}],
        faq_details=[{"assistant_turn": 2, "matched_faq_id": "FAQ_001", "is_ood": False}],
    )
    assert pk1 == "p_refund"
    assert sig1 == "s1->s2[FAQ_001]"

    # 2. SOP 开 + FAQ 关
    pk2, sig2 = DatasetDeduplicator.build_dialogue_trajectory_signature(
        rule_id="r_sop_only",
        scenario_id="p_refund",
        trajectory_details=[{"turn": 1, "node_id": "s1"}, {"turn": 2, "node_id": "s2"}],
        faq_details=[],
    )
    assert pk2 == "p_refund"
    assert sig2 == "s1->s2"

    # 3. SOP 关 + FAQ 开（命中有效 FAQ）
    pk3, sig3 = DatasetDeduplicator.build_dialogue_trajectory_signature(
        rule_id="r_faq_only",
        scenario_id="default",
        trajectory_details=[],
        faq_details=[
            {"assistant_turn": 1, "matched_faq_id": "FAQ_001", "is_ood": False},
            {"assistant_turn": 2, "matched_faq_id": "FAQ_OOD", "is_ood": True},
        ],
    )
    assert pk3 == ""
    assert sig3 == "FAQ_SEQ:[FAQ_001]"

    # 4. SOP 关 + FAQ 关 / 外部无规则无人设数据 -> 返回 None 优雅降级至局部 TURN 池
    deduper = DatasetDeduplicator()
    ext_rows = [
        {"id": "ext_1", "messages": [{"role": "user", "content": "开发票流程怎么走"}, {"role": "assistant", "content": "您好，在订单页申请电子发票。"}]},
        {"id": "ext_2", "messages": [{"role": "user", "content": "开发票流程怎么走！"}, {"role": "assistant", "content": "您可以在订单页点击开票。"}]},
        {"id": "ext_3", "messages": [{"role": "user", "content": "物流停滞好几天了"}, {"role": "assistant", "content": "为您联系快递公司加急派送。"}]},
    ]
    out_train, stats_train = deduper.deduplicate_training_rows(
        ext_rows, config=DedupConfig(enabled=True, cluster_quota=5)
    )
    assert len(out_train) == 2
    assert stats_train["exact_hash_dropped"] == 1
    assert stats_train["explicit_cluster_count"] == 0
    assert stats_train["implicit_cluster_count"] >= 1

    ext_eval = [
        {"id": "ev_1", "messages": [{"role": "user", "content": "怎么开发票？"}], "reference": "在订单页面点击申请发票"},
        {"id": "ev_2", "messages": [{"role": "user", "content": "怎么开发票"}], "reference": "在订单详情页开票"},
        {"id": "ev_3", "messages": [{"role": "user", "content": "快递丢件怎么赔？"}], "reference": "全额赔付或补发"},
    ]
    out_eval, stats_eval = deduper.deduplicate_benchmark_rows(
        ext_eval, config=DedupConfig(enabled=True, cluster_quota=5)
    )
    assert len(out_eval) == 2
    assert stats_eval["exact_hash_dropped"] == 1


def test_05_trajectory_signature_distinction_and_self_spin():
    """场景 5: s1->s2[faq1]->s3 vs s1->s2->s3[faq1] vs s1->s2->s3 必须成不同簇，自旋合并"""
    traj = [
        {"turn": 1, "node_id": "s1"},
        {"turn": 2, "node_id": "s2"},
        {"turn": 3, "node_id": "s3"},
    ]
    _, sig_at_s2 = DatasetDeduplicator.build_dialogue_trajectory_signature(
        "r", "p", trajectory_details=traj, faq_details=[{"assistant_turn": 2, "matched_faq_id": "faq1"}]
    )
    _, sig_at_s3 = DatasetDeduplicator.build_dialogue_trajectory_signature(
        "r", "p", trajectory_details=traj, faq_details=[{"assistant_turn": 3, "matched_faq_id": "faq1"}]
    )
    _, sig_none = DatasetDeduplicator.build_dialogue_trajectory_signature(
        "r", "p", trajectory_details=traj, faq_details=[]
    )
    assert sig_at_s2 == "s1->s2[faq1]->s3"
    assert sig_at_s3 == "s1->s2->s3[faq1]"
    assert sig_none == "s1->s2->s3"
    assert len({sig_at_s2, sig_at_s3, sig_none}) == 3

    # 连续相同节点自旋折叠并保序合并 FAQ
    spin_traj = [
        {"turn": 1, "node_id": "s1"},
        {"turn": 2, "node_id": "s2"},
        {"turn": 3, "node_id": "s2"},
        {"turn": 4, "node_id": "s3"},
    ]
    _, sig_spin = DatasetDeduplicator.build_dialogue_trajectory_signature(
        "r",
        "p",
        trajectory_details=spin_traj,
        faq_details=[{"assistant_turn": 3, "matched_faq_id": "faq_spin"}],
    )
    assert sig_spin == "s1->s2[faq_spin]->s3"

    # 验证真实 Judge 引擎对无扣分答对的 FAQ 轮次也输出 faq_details
    rule = UniversalRuleSchema(
        rule_id="r_judge_faq",
        project_name="FAQ测试",
        bot_config=BotConfig(system_prompt="你是客服"),
        topology_sop=TopologySOPConfig(enabled=False),
        faq_inspection=FAQInspectionConfig(
            enabled=True,
            retrieval_mode="lexical_only",
            knowledge_base=[
                FAQItem(
                    id="faq1",
                    question="怎么办理退款",
                    answer="进入订单详情点击申请退款即可原路返回。",
                )
            ],
        ),
    )

    def _faq_judge_gen(req):
        combined = "\n".join(m.content for m in req.messages)
        if "faq_rounds" in combined:
            return json.dumps(
                {
                    "faq_rounds": [
                        {
                            "user_turn": 1,
                            "assistant_turn": 2,
                            "raw_user_question": "怎么办理退款？",
                            "rewritten_query": "怎么办理退款",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "verdict": "PASS",
                "score": 100,
                "detected_defects": [],
                "deductions": [],
                "expert_comments": "回答准确无误",
            },
            ensure_ascii=False,
        )

    faq_judge_adapter = MockAdapter(response_generator=_faq_judge_gen)
    engine = UniversalJudgeEngine(faq_judge_adapter, ModelConfig(provider_type="mock", model_name="mock"))
    report = engine.evaluate_dialogue(
        rule,
        PersonaDefinition(
            id="p",
            name="客户",
            category="售后",
            persona={"background": "退货", "tone": "平和"},
            instruction="退款",
        ),
        [
            ChatMessage(role="user", content="怎么办理退款？"),
            ChatMessage(role="assistant", content="进入订单详情点击申请退款即可原路返回。"),
        ],
    )
    assert any(fd.get("matched_faq_id") == "faq1" for fd in report.faq_details)


def test_06_mixed_pool_step2b_splits_m_clusters_without_truncating_to_k():
    """场景 6: 80%未触发显式路径对话在 Step 2b 按坐席回复向量拆M个情景簇，保留 M×K 条而非截断为 K 条"""
    deduper = DatasetDeduplicator()
    rows: List[Dict[str, Any]] = []

    topics = [
        (
            "退货",
            "关于订单{code}号购买的服饰，支持签收后七天内无理由退换。",
            "您好，请您在手机端订单详情页面点击申请七天无理由退货并填写退回物流单号。",
        ),
        (
            "保修",
            "编号{code}的蓝牙耳机主板出现故障怎么走官方售后保修？",
            "您购买的智能硬件享受整机十二个月官方质保，请携带电子发票前往售后网点检修。",
        ),
        (
            "发票",
            "商品代码{code}采购完成后如何补开企业增值税专用电子发票？",
            "请在我的账单发票管理中心填写公司纳税人识别号及邮箱，系统将在三个工作日开票。",
        ),
    ]
    unique_tokens = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"]

    for t_idx, (label, u_tpl, a_text) in enumerate(topics):
        for i in range(10):
            # 刻意让用户问法略带不同修饰，避免 near_dup 提前合并；坐席回复高度同质以便 Step 2b 归为同一隐式簇
            u_text = u_tpl.format(code=f"{unique_tokens[i]}{t_idx}{i * 137 + 19}") + f" 第{i}批次核对"
            rows.append(
                {
                    "id": f"mix_{t_idx}_{i}",
                    "messages": [
                        {"role": "user", "content": u_text},
                        {"role": "assistant", "content": a_text},
                        {"role": "user", "content": f"明白了谢谢关于{label}第{i}条"},
                        {"role": "assistant", "content": a_text + " 祝您生活愉快。"},
                    ],
                    "_dedup_meta": {"rule_id": "rule_mix", "scenario_id": "default"},
                }
            )

    # 再加 4 条完全同质客户+客服克隆，验证克隆先被折叠
    for clone_i in range(4):
        rows.append(
            {
                "id": f"clone_{clone_i}",
                "messages": list(rows[0]["messages"]),
                "_dedup_meta": {"rule_id": "rule_mix", "scenario_id": "default"},
            }
        )

    out, stats = deduper.deduplicate_training_rows(
        rows,
        config=DedupConfig(enabled=True, cluster_quota=3, answer_sim_threshold=0.78, near_dup_input_threshold=0.95),
    )
    assert stats["exact_hash_dropped"] == 4
    assert stats["explicit_cluster_count"] == 0
    assert stats["implicit_cluster_count"] == 3
    assert len(out) == 9  # 3 个隐式场景簇 × 每簇 K=3，绝非整体截断到 3 条！


def test_07_benchmark_identical_context_including_outbound_greeting_dedup():
    """场景 7: 含外呼首轮客服开场白 + 相同客户序列的评测集题干去重收敛至 1 道"""
    deduper = DatasetDeduplicator()
    rows = [
        {
            "id": "sl_outbound_1",
            "messages": [
                {"role": "assistant", "content": "您好，这里是星云售后回访中心，请问有什么可以帮您？"},
                {"role": "user", "content": "我的快递三天没有更新物流了！"},
            ],
            "reference": "为您催促快递网点核实物流轨迹。",
            "metadata": {"rule_id": "r_out", "faq_id": "FAQ_LOG"},
        },
        {
            "id": "sl_outbound_2",
            "messages": [
                {"role": "assistant", "content": "你好，我是客服小美，请问遇到什么问题？"},
                {"role": "user", "content": "我的快递三天没有更新物流了"},
            ],
            "reference": "马上帮您查询快递单号并催派。",
            "metadata": {"rule_id": "r_out", "faq_id": "FAQ_LOG"},
        },
        {
            "id": "sl_multi_1",
            "messages": [
                {"role": "user", "content": "订单要修改收货地址"},
                {"role": "assistant", "content": "回复一"},
                {"role": "user", "content": "改到上海市浦东新区"},
            ],
            "reference": "已为您登记改地址。",
            "metadata": {"rule_id": "r_out", "node_id": "s2"},
        },
        {
            "id": "sl_multi_2",
            "messages": [
                {"role": "user", "content": "订单要修改收货地址"},
                {"role": "assistant", "content": "不同的历史坐席回复"},
                {"role": "user", "content": "改到上海市浦东新区！"},
            ],
            "reference": "为您重新下发修改地址工单。",
            "metadata": {"rule_id": "r_out", "node_id": "s2"},
        },
    ]
    out, stats = deduper.deduplicate_benchmark_rows(rows, config=DedupConfig(enabled=True, cluster_quota=5))
    assert stats["exact_hash_dropped"] == 2
    assert len(out) == 2


def test_08_maxmin_diversity_selection_and_target_total_backfill():
    """场景 8: 同簇内客户输入 Max-Min 多样性选优 + target_total_count 余量池差异最大化补足"""
    deduper = DatasetDeduplicator()
    rows = [
        {
            "id": "q_dup_1",
            "messages": [{"role": "user", "content": "我要开发票请发我邮箱"}],
            "reference": "订单页申请电子发票",
            "metadata": {"rule_id": "r_inv", "faq_id": "FAQ_INV"},
        },
        {
            "id": "q_dup_2",
            "messages": [{"role": "user", "content": "我要开发票请发我邮箱吧"}],
            "reference": "订单页申请电子发票",
            "metadata": {"rule_id": "r_inv", "faq_id": "FAQ_INV"},
        },
        {
            "id": "q_dup_3",
            "messages": [{"role": "user", "content": "我要开发票请发我邮箱谢谢"}],
            "reference": "订单页申请电子发票",
            "metadata": {"rule_id": "r_inv", "faq_id": "FAQ_INV"},
        },
        {
            "id": "q_orth_1",
            "messages": [{"role": "user", "content": "企业抬头与纳税人识别号写错了如何重开纸质专票"}],
            "reference": "联系人工作废原票并重开",
            "metadata": {"rule_id": "r_inv", "faq_id": "FAQ_INV"},
        },
        {
            "id": "q_orth_2",
            "messages": [{"role": "user", "content": "半年以前的旧历史购物记录还能补开增值税电子普票吗"}],
            "reference": "支持一年内订单补开电子发票",
            "metadata": {"rule_id": "r_inv", "faq_id": "FAQ_INV"},
        },
    ]

    # 限制簇配额 K=2，无 target_total_count: 种子(q_dup_1)后应优先选最远正交问法 q_orth_*，而非选 q_dup_2
    out_k2, stats_k2 = deduper.deduplicate_benchmark_rows(
        rows,
        config=DedupConfig(enabled=True, cluster_quota=2, near_dup_input_threshold=0.99),
    )
    sel_ids = {r["id"] for r in out_k2}
    assert len(sel_ids) == 2
    assert "q_dup_1" in sel_ids
    assert ("q_orth_1" in sel_ids or "q_orth_2" in sel_ids)
    assert stats_k2["quota_dropped"] == 3

    # 设置 target_total_count=4: 应从余量池按 Max-Min 补足至 4 条
    out_bf, stats_bf = deduper.deduplicate_benchmark_rows(
        rows,
        config=DedupConfig(enabled=True, cluster_quota=2, near_dup_input_threshold=0.99, target_total_count=4),
    )
    assert len(out_bf) == 4
    assert stats_bf["backfilled_count"] == 2


def test_09_append_unlabeled_local_bucket_only_and_dedup_cache_hit_and_delete(isolated_env):
    """场景 9: 无标签老文件 append 仅比对精确轮次局部老桶 + .dedup_cache.json 缓存复用 + 删除清理缓存"""
    client: TestClient = isolated_env["client"]
    datasets_dir = isolated_env["datasets_dir"]

    # 先覆盖导入外部无 metadata/cluster_key 的评测集（1条2轮题 + 1条3轮题）
    old_t2 = {
        "id": "old_t2",
        "messages": [
            {"role": "user", "content": "我要申请退货"},
            {"role": "assistant", "content": "请提供订单号"},
            {"role": "user", "content": "请问我的蓝牙耳机左侧一直没有声音了想申请办理官方退换货"},
        ],
        "reference": "核实订单号与物流信息后寄回官方检测中心质检",
    }
    old_t3 = {
        "id": "old_t3",
        "messages": [
            {"role": "user", "content": "查一下保修期"},
            {"role": "assistant", "content": "一年质保"},
            {"role": "user", "content": "电池鼓包免费更换吗"},
            {"role": "assistant", "content": "质保期内免费"},
            {"role": "user", "content": "好的怎么寄修"},
        ],
        "reference": "填写寄修工单",
    }
    r_init = client.post(
        "/api/v1/datasets/import",
        json={
            "dataset_id": "legacy_append_ds",
            "dataset_type": "benchmark",
            "mode": "overwrite",
            "samples": [old_t2, old_t3],
        },
    )
    assert r_init.status_code == 200, r_init.text

    # 第1次 append: 引入 1 条新题并触发 .dedup_cache.json 生成
    cand_first = {
        "id": "new_t2_unique",
        "messages": [
            {"role": "user", "content": "收款账户怎么解绑"},
            {"role": "assistant", "content": "进入钱包设置"},
            {"role": "user", "content": "解绑银行卡提示风控异常"},
        ],
        "reference": "请完成人脸核验后解绑",
    }
    r_app1 = client.post(
        "/api/v1/datasets/import",
        json={
            "dataset_id": "legacy_append_ds",
            "dataset_type": "benchmark",
            "mode": "append",
            "samples": [cand_first],
        },
    )
    assert r_app1.status_code == 200, r_app1.text
    cache_file = DatasetDeduplicator.get_cache_path(os.path.join(datasets_dir, "legacy_append_ds.jsonl"))
    assert os.path.exists(cache_file)

    # 第2次 append:
    # - cand_near_t2 与 old_t2 均为 2 轮且近似极高 -> 命中同精确轮次局部老桶被 near_dup 拦截
    # - cand_diff_turn_t1 与 old_t3 末尾文本完全相同但轮次 k=1 != 3 -> 仅比 k=1 桶而不被 k=3 误杀！
    # - cand_brand_new_t2 全部全新 -> 正常保留
    cand_near_t2 = {
        "id": "cand_near_t2",
        "messages": [
            {"role": "user", "content": "我要申请退货"},
            {"role": "assistant", "content": "好"},
            {"role": "user", "content": "请问我的蓝牙耳机左侧一直没有声音了想申请办理官方退换货呢"},
        ],
        "reference": "核实订单号与物流信息后寄回官方检测中心质检处理",
    }
    cand_diff_turn_t1 = {
        "id": "cand_diff_turn_t1",
        "messages": [
            {"role": "user", "content": "好的怎么寄修"},
        ],
        "reference": "点击售后申请寄修",
    }
    cand_brand_new_t2 = {
        "id": "cand_brand_new_t2",
        "messages": [
            {"role": "user", "content": "企业对公转账怎么操作"},
            {"role": "assistant", "content": "请查看收款账号"},
            {"role": "user", "content": "打款备注忘了填写订单号怎么办"},
        ],
        "reference": "提供转账水单核销",
    }
    r_app2 = client.post(
        "/api/v1/datasets/import",
        json={
            "dataset_id": "legacy_append_ds",
            "dataset_type": "benchmark",
            "mode": "append",
            "samples": [cand_near_t2, cand_diff_turn_t1, cand_brand_new_t2],
            "dedup_config": {"enabled": True, "near_dup_input_threshold": 0.90, "cluster_quota": 5},
        },
    )
    assert r_app2.status_code == 200, r_app2.text
    stats2 = r_app2.json()["dedup_stats"]
    assert stats2["cache_hits"] >= 3
    assert stats2["near_dup_dropped"] == 1
    assert stats2["kept_candidates"] == 2

    # 删除数据集时必须同步清理 .dedup_cache.json
    r_del = client.delete("/api/v1/datasets/legacy_append_ds")
    assert r_del.status_code == 200
    assert not os.path.exists(os.path.join(datasets_dir, "legacy_append_ds.jsonl"))
    assert not os.path.exists(cache_file)


def test_10_batch_upsert_holdout_exclusion_and_benchmark_export_flag(isolated_env, monkeypatch):
    """场景 10: /flywheel/batch Upsert + 留出排除 + export-selected 默认拦截 + 条件置位 exported_to_benchmark"""
    client: TestClient = isolated_env["client"]
    Session = isolated_env["Session"]
    datasets_dir = isolated_env["datasets_dir"]
    _save_rule_via_api(client, "rule_batch_10", extra_faq=True)

    # 让 MockAdapter 为质检/修复返回稳定结构化 JSON
    def _mock_chat(req):
        combined = "\n".join(m.content for m in req.messages)
        if "faq_rounds" in combined:
            if "退货" in combined:
                return json.dumps(
                    {
                        "faq_rounds": [
                            {
                                "user_turn": 1,
                                "assistant_turn": 2,
                                "raw_user_question": "我要办理七天无理由退货",
                                "rewritten_query": "怎么申请七天无理由退货",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            return json.dumps({"faq_rounds": []}, ensure_ascii=False)
        if "node_trajectory" in combined or "trajectory_details" in combined:
            return json.dumps(
                {
                    "node_trajectory": ["s1", "s2"],
                    "trajectory_details": [{"turn": 1, "node_id": "s1", "reason": "核实"}],
                },
                ensure_ascii=False,
            )
        if "<candidate_faqs>" in combined or "score" in combined or "评分" in combined or "质检" in combined:
            return json.dumps(
                {
                    "score": 95,
                    "verdict": "PASS",
                    "detected_defects": [],
                    "deductions": [],
                    "node_trajectory": ["s1", "s2"],
                    "trajectory_details": [{"turn": 1, "node_id": "s1", "reason": "核实"}],
                    "expert_comments": "合格通过",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "remediated_messages": [m.model_dump() for m in req.messages if m.role != "system"],
                "applied_actions": [],
            },
            ensure_ascii=False,
        )

    _orig_chat = MockAdapter.chat
    monkeypatch.setattr(
        MockAdapter,
        "chat",
        lambda self, req: _orig_chat(MockAdapter(response_generator=_mock_chat), req),
    )

    input_path = os.path.join(datasets_dir, "batch_raw_in.jsonl")
    raw_rows = [
        {
            "id": "bw_holdout_src",
            "scenario_id": "p_refund",
            "messages": [
                {"role": "user", "content": "我要办理七天无理由退货"},
                {"role": "assistant", "content": "好的，请提供订单号为您申请七天退货。"},
            ],
        },
        {
            "id": "bw_normal_src",
            "scenario_id": "p_refund",
            "messages": [
                {"role": "user", "content": "请问如何修改收货手机号"},
                {"role": "assistant", "content": "您可以在个人中心安全设置中修改绑定手机号。"},
            ],
        },
    ]
    with open(input_path, "w", encoding="utf-8") as f:
        for row in raw_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    gold_path = os.path.join(datasets_dir, "batch10_gold.jsonl")
    discard_path = os.path.join(datasets_dir, "batch10_discarded.jsonl")

    # 第一次跑 /flywheel/batch
    b1 = client.post(
        "/api/v1/flywheel/batch",
        json={
            "rule_id": "rule_batch_10",
            "input_file_path": input_path,
            "gold_output_path": gold_path,
            "discard_output_path": discard_path,
            "judge_model_id": "mock_judge_dedup",
            "remediate_model_id": "mock_remed_dedup",
            "workers": 1,
        },
    )
    assert b1.status_code == 200, b1.text
    samples_after_b1 = client.get("/api/v1/flywheel/samples", params={"rule_id": "rule_batch_10"}).json()
    assert len(samples_after_b1) == 2

    # 第二次跑 /flywheel/batch -> Upsert 不产生重复记录
    b2 = client.post(
        "/api/v1/flywheel/batch",
        json={
            "rule_id": "rule_batch_10",
            "input_file_path": input_path,
            "gold_output_path": gold_path,
            "discard_output_path": discard_path,
            "judge_model_id": "mock_judge_dedup",
            "remediate_model_id": "mock_remed_dedup",
            "workers": 1,
        },
    )
    assert b2.status_code == 200, b2.text
    assert len(client.get("/api/v1/flywheel/samples", params={"rule_id": "rule_batch_10"}).json()) == 2

    # 为 bw_holdout_src 写入 1 条评测切片并导出到评测集；同时再建 1 条同上下文的未标记样本 bw_dup_slice 验证未被保留切片时不误置位
    db = Session()
    try:
        s_hold = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == "bw_holdout_src").first()
        assert s_hold is not None
        hold_slice = [
            {
                "slice_id": "sl_hold_1",
                "sample_id": "bw_holdout_src",
                "target_turn": 2,
                "defect_item": "s1",
                "defect_reason": "核实订单",
                "messages": [{"role": "user", "content": "我要办理七天无理由退货"}],
                "reference": "请提供订单号为您办理七天退货。",
                "rubric": "核实订单后办理退货",
                "regex_constraints": [],
                "rule_id": "rule_batch_10",
                "node_id": "s1",
                "faq_id": "FAQ_RET_01",
                "is_faq_ood": False,
            }
        ]
        s_hold.benchmark_slices_json = json.dumps(hold_slice, ensure_ascii=False)

        s_dup = FlywheelSampleRecord(
            id="bw_dup_slice",
            task_id="t_dup",
            rule_id="rule_batch_10",
            scenario_id="p_refund",
            status="REPAIRED_PASS",
            score=90,
            raw_messages_json=json.dumps(
                [{"role": "user", "content": "我想咨询完全不同的话题"}, {"role": "assistant", "content": "好的"}],
                ensure_ascii=False,
            ),
            benchmark_slices_json=json.dumps(
                [
                    {
                        "slice_id": "sl_dup_1",
                        "sample_id": "bw_dup_slice",
                        "target_turn": 2,
                        "defect_item": "s1",
                        "defect_reason": "同题干重复",
                        "messages": [{"role": "user", "content": "我要办理七天无理由退货！"}],
                        "reference": "请提供单号",
                        "rubric": "核实订单",
                        "regex_constraints": [],
                        "rule_id": "rule_batch_10",
                        "node_id": "s1",
                        "faq_id": "FAQ_RET_01",
                        "is_faq_ood": False,
                    }
                ],
                ensure_ascii=False,
            ),
        )
        db.add(s_dup)
        db.commit()
    finally:
        db.close()

    # 单条导出评测集 -> metadata 携带 rule_id/node_id/faq_id 且 bw_holdout_src 置为 exported_to_benchmark=True
    exp_b = client.post(
        "/api/v1/flywheel/samples/bw_holdout_src/export-benchmark",
        json={"dataset_id": "bm_holdout_10", "name": "留出评测集", "mode": "create"},
    )
    assert exp_b.status_code == 200, exp_b.text
    assert exp_b.json()["sample_count"] == 1
    bm_rows = client.get("/api/v1/datasets/bm_holdout_10/samples").json()["samples"]
    assert bm_rows[0]["metadata"]["rule_id"] == "rule_batch_10"
    assert bm_rows[0]["metadata"]["faq_id"] == "FAQ_RET_01"

    # 批量追加导出含重复切片的 bw_dup_slice -> 切片全部被去重丢弃，bw_dup_slice 不得被误置 exported_to_benchmark=True！
    batch_bm = client.post(
        "/api/v1/flywheel/batch-export-benchmark",
        json={
            "sample_ids": ["bw_dup_slice"],
            "dataset_id": "bm_holdout_10",
            "name": "留出评测集",
            "mode": "append",
        },
    )
    assert batch_bm.status_code == 200, batch_bm.text
    assert batch_bm.json()["dedup_stats"]["exact_hash_dropped"] == 1

    db = Session()
    try:
        s_hold_db = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == "bw_holdout_src").first()
        s_dup_db = db.query(FlywheelSampleRecord).filter(FlywheelSampleRecord.id == "bw_dup_slice").first()
        assert s_hold_db.exported_to_benchmark is True
        assert not bool(s_dup_db.exported_to_benchmark)
    finally:
        db.close()

    # /flywheel/samples/export-selected 默认拦截已进评测集的同源样本；显式传入 exclude_benchmark_exported=False 才放行
    sel_blocked = client.post(
        "/api/v1/flywheel/samples/export-selected",
        json={"sample_ids": ["bw_holdout_src"], "version_tag": "try_leak"},
    )
    assert sel_blocked.status_code == 400

    sel_forced = client.post(
        "/api/v1/flywheel/samples/export-selected",
        json={
            "sample_ids": ["bw_holdout_src"],
            "version_tag": "allow_explicit",
            "exclude_benchmark_exported": False,
        },
    )
    assert sel_forced.status_code == 200, sel_forced.text
    assert sel_forced.json()["sample_count"] == 1

    # /flywheel/export 默认只导出非留出样本（bw_normal_src 与未导出成功的 bw_dup_slice），绝不导出 bw_holdout_src
    gold_exp = client.post(
        "/api/v1/flywheel/export",
        params={"rule_id": "rule_batch_10", "version_tag": "safe_train"},
    )
    assert gold_exp.status_code == 200, gold_exp.text
    assert gold_exp.json()["sample_count"] == 2
    with open(gold_exp.json()["file_path"], "r", encoding="utf-8") as gf:
        exported_user_texts = [
            " ".join(str(m.get("content") or "") for m in json.loads(ln)["messages"] if m.get("role") == "user")
            for ln in gf
            if ln.strip()
        ]
    assert not any("七天无理由退货" in t for t in exported_user_texts)
    assert any("修改收货手机号" in t for t in exported_user_texts)

    # 再次运行 /flywheel/batch (导出至新路径): 已导出评测集的 bw_holdout_src 必须被自动排除，仅 bw_normal_src 写入黄金训练集！
    gold_path_after = os.path.join(datasets_dir, "batch10_gold_after_holdout.jsonl")
    discard_path_after = os.path.join(datasets_dir, "batch10_discarded_after_holdout.jsonl")
    b3 = client.post(
        "/api/v1/flywheel/batch",
        json={
            "rule_id": "rule_batch_10",
            "input_file_path": input_path,
            "gold_output_path": gold_path_after,
            "discard_output_path": discard_path_after,
            "judge_model_id": "mock_judge_dedup",
            "remediate_model_id": "mock_remed_dedup",
            "workers": 1,
        },
    )
    assert b3.status_code == 200, b3.text
    with open(gold_path_after, "r", encoding="utf-8") as f:
        gold_lines = [json.loads(line) for line in f if line.strip()]
    assert len(gold_lines) == 1
    assert any("修改收货手机号" in str(m.get("content") or "") for m in gold_lines[0]["messages"])
    assert not any("七天无理由退货" in str(m.get("content") or "") for m in gold_lines[0]["messages"])


def test_11_hybrid_faq_ood_non_deduction_returns_faq_ood():
    """场景 11 (MAJOR 2): 默认 hybrid 模式下，超纲提问且合规兜底不扣分时绝不误判为 Top-1 FAQ，必须标记 FAQ_OOD"""
    kb = [
        FAQItem(
            id="FAQ_RET_01",
            category="退换货",
            question="七天无理由退换货与运费承担规则",
            answer="签收7天内支持无理由退换，质量问题往返运费由商家承担。",
        ),
        FAQItem(
            id="FAQ_WAR_02",
            category="质保",
            question="官方质保期及寄修服务说明",
            answer="智能设备主机享受1年质保，非人为损坏免费寄修。",
        ),
        FAQItem(
            id="FAQ_EXT_03",
            category="延保",
            question="延保服务生效时间与覆盖部件",
            answer="延保自原厂质保期满次日起生效，覆盖主板和电源模块。",
        ),
    ]
    faq_cfg = FAQInspectionConfig(
        enabled=True,
        retrieval_mode="hybrid",
        knowledge_base=kb,
        top_k=2,
        embedding_config=EmbeddingConfig(provider_type="local_cpu"),
    )
    retriever = UniversalFAQRetriever(faq_cfg)

    in_kb_res = retriever.search("请问七天质量问题退换货运费谁承担？", top_k=2)
    assert len(in_kb_res) > 0
    assert in_kb_res[0]["faq"].id == "FAQ_RET_01"
    assert in_kb_res[0]["lexical_score"] > 0.0
    assert in_kb_res[0]["raw_hit"] is True

    ood_res = retriever.search("你们实体门店在北京市朝阳区哪栋大厦周末营业吗？", top_k=2)
    assert len(ood_res) > 0
    # hybrid RRF 名次分恒正，但原始相关度 raw_hit 必须识别为未命中！
    assert ood_res[0]["score"] > 0.0
    assert ood_res[0]["raw_hit"] is False

    rule = UniversalRuleSchema(
        rule_id="r_hybrid_ood",
        project_name="混合检索超纲校验",
        bot_config=BotConfig(system_prompt="你是官方售后客服。"),
        topology_sop=TopologySOPConfig(enabled=False),
        faq_inspection=faq_cfg,
    )
    scenario = PersonaDefinition(id="p", name="客户", category="售后", instruction="超纲咨询")
    ood_dialogue = [
        ChatMessage(role="user", content="你们实体门店在北京市朝阳区哪栋大厦周末营业吗？"),
        ChatMessage(role="assistant", content="抱歉，暂未收录线下门店地址，建议联系官方客服热线或登录 App 查询。"),
    ]

    # A. 兼容老自定义模板：不返回顶层 matched_faq_id，仅返回 {"faq_deductions": []}
    def _legacy_ood_gen(req):
        combined = "\n".join(m.content for m in req.messages)
        if "faq_rounds" in combined:
            return json.dumps({
                "faq_rounds": [{
                    "user_turn": 1,
                    "assistant_turn": 2,
                    "raw_user_question": "你们实体门店在北京市朝阳区哪栋大厦周末营业吗？",
                    "rewritten_query": "实体门店在北京市朝阳区哪栋大厦周末营业吗",
                }]
            }, ensure_ascii=False)
        if "业务问答 (FAQ)" in combined or "<candidate_faqs>" in combined:
            return json.dumps({"faq_deductions": []}, ensure_ascii=False)
        return json.dumps({"score": 100, "verdict": "PASS", "deductions": [], "expert_comments": "合规兜底"}, ensure_ascii=False)

    legacy_engine = UniversalJudgeEngine(
        MockAdapter(response_generator=_legacy_ood_gen),
        ModelConfig(provider_type="mock", model_name="mock"),
    )
    rep_legacy = legacy_engine.evaluate_dialogue(rule, scenario, ood_dialogue)
    assert len(rep_legacy.faq_details) == 1
    assert rep_legacy.faq_details[0]["matched_faq_id"] == "FAQ_OOD"
    assert rep_legacy.faq_details[0]["is_ood"] is True

    # B. 新模板：显式输出顶层 "matched_faq_id": "FAQ_OOD" 且无扣分 -> 必须定为 FAQ_OOD
    def _explicit_ood_gen(req):
        combined = "\n".join(m.content for m in req.messages)
        if "faq_rounds" in combined:
            return json.dumps({
                "faq_rounds": [{
                    "user_turn": 1,
                    "assistant_turn": 2,
                    "raw_user_question": "请问你们支持支付宝花呗分期付款吗？",
                    "rewritten_query": "支付宝花呗分期付款政策",
                }]
            }, ensure_ascii=False)
        if "业务问答 (FAQ)" in combined or "<candidate_faqs>" in combined:
            return json.dumps({"matched_faq_id": "FAQ_OOD", "faq_deductions": []}, ensure_ascii=False)
        return json.dumps({"score": 100, "verdict": "PASS", "deductions": [], "expert_comments": "合规兜底"}, ensure_ascii=False)

    explicit_engine = UniversalJudgeEngine(
        MockAdapter(response_generator=_explicit_ood_gen),
        ModelConfig(provider_type="mock", model_name="mock"),
    )
    rep_explicit = explicit_engine.evaluate_dialogue(rule, scenario, ood_dialogue)
    assert len(rep_explicit.faq_details) == 1
    assert rep_explicit.faq_details[0]["matched_faq_id"] == "FAQ_OOD"
    assert rep_explicit.faq_details[0]["is_ood"] is True

    # C. 合法命中 KB 且无扣分：显式返回 FAQ_RET_01 -> 准确记为 FAQ_RET_01 & is_ood=False
    def _explicit_hit_gen(req):
        combined = "\n".join(m.content for m in req.messages)
        if "faq_rounds" in combined:
            return json.dumps({
                "faq_rounds": [{
                    "user_turn": 1,
                    "assistant_turn": 2,
                    "raw_user_question": "七天内质量问题退货运费谁出？",
                    "rewritten_query": "七天无理由退换货与质量问题运费承担规则",
                }]
            }, ensure_ascii=False)
        if "业务问答 (FAQ)" in combined or "<candidate_faqs>" in combined:
            return json.dumps({"matched_faq_id": "FAQ_RET_01", "faq_deductions": []}, ensure_ascii=False)
        return json.dumps({"score": 100, "verdict": "PASS", "deductions": [], "expert_comments": "准确解答"}, ensure_ascii=False)

    hit_engine = UniversalJudgeEngine(
        MockAdapter(response_generator=_explicit_hit_gen),
        ModelConfig(provider_type="mock", model_name="mock"),
    )
    rep_hit = hit_engine.evaluate_dialogue(
        rule,
        scenario,
        [
            ChatMessage(role="user", content="七天内质量问题退货运费谁出？"),
            ChatMessage(role="assistant", content="质量问题往返运费均由商家承担。"),
        ],
    )
    assert len(rep_hit.faq_details) == 1
    assert rep_hit.faq_details[0]["matched_faq_id"] == "FAQ_RET_01"
    assert rep_hit.faq_details[0]["is_ood"] is False


def test_12_concurrent_batch_workers4_and_db_error_holdout_barrier(isolated_env, monkeypatch):
    """场景 12 (MAJOR 1): workers=4 并发 /flywheel/batch 无 SQLite 竞态、保留留出隔离、且持久化异常绝不放行留出题"""
    client: TestClient = isolated_env["client"]
    Session = isolated_env["Session"]
    datasets_dir = isolated_env["datasets_dir"]
    _save_rule_via_api(client, "rule_conc_12")

    def _conc_mock_chat(req):
        combined = "\n".join(m.content for m in req.messages)
        if "node_trajectory" in combined or "trajectory_details" in combined:
            return json.dumps(
                {"node_trajectory": ["s1", "s2"], "trajectory_details": [{"turn": 1, "node_id": "s1"}]},
                ensure_ascii=False,
            )
        if "score" in combined or "评分" in combined or "质检" in combined:
            return json.dumps(
                {
                    "score": 96,
                    "verdict": "PASS",
                    "detected_defects": [],
                    "deductions": [],
                    "node_trajectory": ["s1", "s2"],
                    "trajectory_details": [{"turn": 1, "node_id": "s1"}],
                    "expert_comments": "通过",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"remediated_messages": [m.model_dump() for m in req.messages if m.role != "system"], "applied_actions": []},
            ensure_ascii=False,
        )

    _orig_chat = MockAdapter.chat
    monkeypatch.setattr(
        MockAdapter,
        "chat",
        lambda self, req: _orig_chat(MockAdapter(response_generator=_conc_mock_chat), req),
    )

    # 预置 1 条同客户序列的已导出留出源样本
    holdout_msgs = [
        {"role": "user", "content": "我要投诉你们延误发货十天了"},
        {"role": "assistant", "content": "非常抱歉，马上为您加急催发处理。"},
    ]
    db = Session()
    try:
        rec_h, _ = upsert_flywheel_sample(
            db,
            sample_id="pre_holdout_conc",
            task_id="old_t",
            rule_id="rule_conc_12",
            scenario_id="p_refund",
            status="GOLD_PASS",
            score=95,
            raw_messages=holdout_msgs,
            remediated_messages=holdout_msgs,
            commit=True,
        )
        rec_h.exported_to_benchmark = True
        db.commit()
    finally:
        db.close()

    # 8 条并发输入：2 条命中留出序列 + 3 条同客户序列普通对话 + 3 条不同客户序列普通对话
    batch_rows = [
        {"id": "c_h1", "scenario_id": "p_refund", "messages": holdout_msgs},
        {
            "id": "c_h2",
            "scenario_id": "p_refund",
            "messages": [
                {"role": "user", "content": "我要投诉你们延误发货十天了！"},
                {"role": "assistant", "content": "十分抱歉，立刻核实加急。"},
            ],
        },
        {
            "id": "c_dup1",
            "scenario_id": "p_refund",
            "messages": [{"role": "user", "content": "查一下快递签收记录"}, {"role": "assistant", "content": "已为您查到签收时间。"}],
        },
        {
            "id": "c_dup2",
            "scenario_id": "p_refund",
            "messages": [{"role": "user", "content": "查一下快递签收记录！"}, {"role": "assistant", "content": "您好，物流显示已签收。"}],
        },
        {
            "id": "c_dup3",
            "scenario_id": "p_refund",
            "messages": [{"role": "user", "content": "查一下快递签收记录"}, {"role": "assistant", "content": "快递已于昨日签收。"}],
        },
        {
            "id": "c_u1",
            "scenario_id": "p_refund",
            "messages": [{"role": "user", "content": "如何开具企业纸质发票"}, {"role": "assistant", "content": "请提供企业抬头与邮寄地址。"}],
        },
        {
            "id": "c_u2",
            "scenario_id": "p_refund",
            "messages": [{"role": "user", "content": "蓝牙连接总是断开怎么办"}, {"role": "assistant", "content": "请长按电源键十秒重置配对。"}],
        },
        {
            "id": "c_u3",
            "scenario_id": "p_refund",
            "messages": [{"role": "user", "content": "海外地址支持售后寄修吗"}, {"role": "assistant", "content": "目前仅支持中国大陆官方寄修网点。"}],
        },
    ]
    in_path = os.path.join(datasets_dir, "conc_batch_in.jsonl")
    gold_path = os.path.join(datasets_dir, "conc_batch_gold.jsonl")
    discard_path = os.path.join(datasets_dir, "conc_batch_discard.jsonl")
    with open(in_path, "w", encoding="utf-8") as f:
        for r in batch_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    resp = client.post(
        "/api/v1/flywheel/batch",
        json={
            "rule_id": "rule_conc_12",
            "input_file_path": in_path,
            "gold_output_path": gold_path,
            "discard_output_path": discard_path,
            "judge_model_id": "mock_judge_dedup",
            "remediate_model_id": "mock_remed_dedup",
            "workers": 4,
        },
    )
    assert resp.status_code == 200, resp.text

    # 验证 DB 零重复且留出标记依然锁定
    db_rows = client.get("/api/v1/flywheel/samples", params={"rule_id": "rule_conc_12"}).json()
    assert len(db_rows) == 5  # 1 holdout + 1 deduped(c_dup) + 3 distinct
    hold_rec = next(r for r in db_rows if r["id"] == "pre_holdout_conc")
    assert hold_rec["exported_to_benchmark"] is True

    # 验证导出黄金集：绝对不含留出对话，且同客户序列已去重（共 4 条）
    with open(gold_path, "r", encoding="utf-8") as gf:
        gold_out_rows = [json.loads(ln) for ln in gf if ln.strip()]
    assert len(gold_out_rows) == 4
    all_gold_user_txt = " ".join(
        str(m.get("content") or "")
        for r in gold_out_rows
        for m in r["messages"]
        if m.get("role") == "user"
    )
    assert "延误发货十天了" not in all_gold_user_txt

    # 直接压测异常兜底：on_record_done 写库抛错时，只读查出留出题必须排除，无法查库时必须阻断写入黄金集
    rule_obj = UniversalRuleSchema(
        rule_id="rule_conc_12",
        project_name="异常兜底测试",
        bot_config=BotConfig(system_prompt="你是客服"),
        topology_sop=TopologySOPConfig(enabled=False),
    )
    pipeline = FlywheelPipelineService(
        MockAdapter(response_generator=_conc_mock_chat),
        MockAdapter(response_generator=_conc_mock_chat),
    )
    err_gold_1 = os.path.join(datasets_dir, "err_barrier_1.jsonl")
    err_disc_1 = os.path.join(datasets_dir, "err_barrier_disc_1.jsonl")

    def _on_done_ro_blocks_holdout(f_rec, _raw):
        # 模拟写库失败后只读核查命中留出，返回 True 拦截
        return True

    st1 = pipeline.batch_process_dataset(
        rule=rule_obj,
        input_records=[{"id": "c_h1", "messages": holdout_msgs}],
        gold_output_path=err_gold_1,
        discard_output_path=err_disc_1,
        workers=2,
        on_record_done=_on_done_ro_blocks_holdout,
    )
    assert st1["excluded_holdout"] == 1
    assert st1["gold"] == 0
    assert not os.path.exists(err_gold_1)

    def _on_done_db_down(f_rec, _raw):
        raise RuntimeError("DB completely unavailable")

    st2 = pipeline.batch_process_dataset(
        rule=rule_obj,
        input_records=[{"id": "c_h1", "messages": holdout_msgs}],
        gold_output_path=err_gold_1,
        discard_output_path=err_disc_1,
        workers=2,
        on_record_done=_on_done_db_down,
    )
    assert st2["failed_persist"] == 1
    assert st2["gold"] == 0
    assert not os.path.exists(err_gold_1)


def test_13_same_id_append_backfill_count_and_encoder_fingerprint_and_stale_cache(isolated_env):
    """场景 13 (MINOR 4/5/6): 同 ID 不同客户序列追加计数修正 + 指纹 encoder_tag 隔离 + 关闭去重覆盖清理旧缓存"""
    client: TestClient = isolated_env["client"]
    Session = isolated_env["Session"]
    datasets_dir = isolated_env["datasets_dir"]

    # 1. 同维度(256)但不同 encoder_tag 的指纹必须不同，防止跨编码器错命中向量缓存
    sample_row = {"id": "fp_1", "messages": [{"role": "user", "content": "测试编码器指纹"}], "reference": "回复"}
    fp_hash = DatasetDeduplicator._row_content_fingerprint(sample_row, kind="benchmark", expected_dim=256, encoder_tag="hash256_v1")
    fp_cpu = DatasetDeduplicator._row_content_fingerprint(sample_row, kind="benchmark", expected_dim=256, encoder_tag="local_cpu:bge:256")
    assert fp_hash != fp_cpu

    # 2. append 模式同 ID 不同客户序列替换时，target_total_count 必须预扣被替换槽位并从余量池补足至目标总条数
    deduper = DatasetDeduplicator()
    existing = [
        {
            "id": "item_1",
            "messages": [{"role": "user", "content": "旧问题关于蓝牙耳机配对"}],
            "reference": "老答案",
            "metadata": {"rule_id": "r_rep", "node_id": "s1", "dedup_cluster_key": "r_rep::NODE::s1::ANS_0"},
        },
        {
            "id": "item_2",
            "messages": [{"role": "user", "content": "另一个老问题关于物流快递单号"}],
            "reference": "物流答案",
            "metadata": {"rule_id": "r_rep", "node_id": "s2", "dedup_cluster_key": "r_rep::NODE::s2::ANS_0"},
        },
    ]
    candidates = [
        {
            # 同 ID item_1，但换了全新客户输入序列 -> 应原地替换 existing[0]（占 0 个净增名额！）
            "id": "item_1",
            "messages": [{"role": "user", "content": "全新提问关于增值税电子发票开具流程"}],
            "reference": "全新发票答案A",
            "metadata": {"rule_id": "r_rep", "node_id": "s1"},
        },
        {
            # 同簇同配额被挤入余量池的全新 ID，因 item_1 不占净增名额，target_total_count=3 必须把它从余量池补回！
            "id": "item_3_new",
            "messages": [{"role": "user", "content": "顺便咨询企业对公转账账号怎么核销"}],
            "reference": "全新发票答案A",
            "metadata": {"rule_id": "r_rep", "node_id": "s1"},
        },
    ]
    final_rows, rep_stats = deduper.deduplicate_benchmark_rows(
        candidate_rows=candidates,
        existing_rows=existing,
        config=DedupConfig(enabled=True, cluster_quota=1, answer_sim_threshold=0.80, target_total_count=3),
    )
    assert len(final_rows) == 3
    assert rep_stats["replaced_existing_count"] == 1
    assert rep_stats["backfilled_count"] == 1
    assert {r["id"] for r in final_rows} == {"item_1", "item_2", "item_3_new"}

    # 3. 飞轮导出 mode="create" 且 enabled=False 时必须同步清理残留的 .dedup_cache.json
    _save_rule_via_api(client, "rule_stale_13")
    db = Session()
    try:
        upsert_flywheel_sample(
            db,
            sample_id="s_stale_1",
            task_id="t_s1",
            rule_id="rule_stale_13",
            scenario_id="p_refund",
            status="GOLD_PASS",
            score=95,
            raw_messages=[
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "您好，请问有什么可以帮您？"},
            ],
            remediated_messages=[
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "您好，请问有什么可以帮您？"},
            ],
            commit=True,
        )
    finally:
        db.close()

    # 先开启去重导出一次生成 .dedup_cache.json
    exp_on = client.post(
        "/api/v1/flywheel/export",
        params={"rule_id": "rule_stale_13", "version_tag": "v_stale", "mode": "create"},
        json={"mode": "create", "dedup_config": {"enabled": True}},
    )
    assert exp_on.status_code == 200, exp_on.text
    cache_path = DatasetDeduplicator.get_cache_path(exp_on.json()["file_path"])
    assert os.path.exists(cache_path)

    # 再用 mode="create" + enabled=False 覆盖导出 -> 必须清理旧缓存文件
    exp_off = client.post(
        "/api/v1/flywheel/export",
        params={"rule_id": "rule_stale_13", "version_tag": "v_stale", "mode": "create"},
        json={"mode": "create", "dedup_config": {"enabled": False}},
    )
    assert exp_off.status_code == 200, exp_off.text
    assert not os.path.exists(cache_path)
