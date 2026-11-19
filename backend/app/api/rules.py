# -*- coding: utf-8 -*-
"""
场景规则配置管理 API
"""
import os
import json
import time
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from backend.app.config import settings
from backend.app.models import get_db, RuleConfigRecord, RegisteredModel
from backend.app.api.deps import load_rule_schema
from backend.app.schemas import (
    UniversalRuleSchema,
    ChatMessage,
    ModelConfig,
    UnifiedChatRequest,
    PersonaDefinition,
    InspectionReport,
    FAQItem,
    DEFAULT_TRAJECTORY_EXTRACT_PROMPT_TEMPLATE,
    DEFAULT_JUDGE_PROMPT_TEMPLATE,
    DEFAULT_PLAIN_JUDGE_PROMPT_TEMPLATE,
    DEFAULT_REMEDIATION_PROMPT_TEMPLATE,
    DEFAULT_FAQ_EXTRACT_PROMPT_TEMPLATE,
    DEFAULT_FAQ_JUDGE_PROMPT_TEMPLATE,
    DEFAULT_CLIENT_SIMULATOR_PROMPT_TEMPLATE,
)
from backend.app.adapters.factory import AdapterFactory
from backend.app.engine.judge import UniversalJudgeEngine
from backend.app.engine.faq_retriever import UniversalFAQRetriever
from backend.app.engine.prompt_registry import get_builtin_definition
from backend.app.engine.remediator import UniversalDataRemediator
from backend.app.engine.simulator import UniversalSimulator
from backend.app.engine.topology import UniversalTopologyValidator

router = APIRouter(prefix="/rules", tags=["场景规则配置"])


class DebugPromptRequest(BaseModel):
    rule_id: str
    prompt_type: str = "judge"  # "trajectory_extract" | "judge" | "judge_plain" | "faq_extract" | "faq_judge" | "remediate" | "client_simulator"
    template: Optional[str] = None
    scenario_id: Optional[str] = None
    dialogue: Optional[List[ChatMessage]] = None
    defects: Optional[List[str]] = None
    rule_override: Optional[UniversalRuleSchema] = None


class TestPromptRequest(BaseModel):
    model_id: str
    prompt_type: str = "judge"  # "trajectory_extract" | "judge" | "judge_plain" | "faq_extract" | "faq_judge" | "remediate" | "client_simulator"
    assembled_prompt: str
    assembled_messages: Optional[List[Dict[str, str]]] = None
    sample_dialogue: Optional[List[ChatMessage]] = None


def _build_demo_trajectory_from_rule(rule: UniversalRuleSchema) -> List[str]:
    """根据当前规则的真实 Mermaid 拓扑图推导一段合法示范轨迹，避免硬编码固定节点轨迹污染自定义流程图。"""
    validator = UniversalTopologyValidator(rule.topology_sop)
    if not validator.all_nodes:
        return []
    start = next(iter(rule.topology_sop.entry_nodes or []), None)
    if not start or start not in validator.all_nodes:
        start = sorted(validator.all_nodes)[0]
    trail: List[str] = [start]
    cur = start
    visited = {start}
    for _ in range(5):
        nxts = [n for n in sorted(validator.adj.get(cur, set())) if n not in visited]
        if not nxts:
            break
        cur = nxts[0]
        visited.add(cur)
        trail.append(cur)
    return trail


# 标准典型示范对话 (用于开箱即用的 Prompt 组装与实测预览)
DEFAULT_DEMO_DIALOGUE = [
    ChatMessage(role="user", content="你好，哪位？"),
    ChatMessage(role="assistant", content="您好，我是星云智联售后客服中心的回访专员，请问是张女士吗？"),
    ChatMessage(role="user", content="是我，什么事？"),
    ChatMessage(role="assistant", content="张女士您好，主要是对您近期签收的智能网关设备做一次例行使用回访。请问设备目前安装联网是否顺利呢？"),
    ChatMessage(role="user", content="已经连上网了，挺稳定的"),
    ChatMessage(role="assistant", content="好的，请问您主要是在家庭客厅使用还是用于办公环境覆盖呢？"),
    ChatMessage(role="user", content="我们放家里客厅用的，配合了三个门窗传感器，信号一直挺稳的。"),
    ChatMessage(role="assistant", content="运行稳定就好！请问随箱配件和保修凭证是否都齐全呢？"),
    ChatMessage(role="user", content="都齐全的，没什么问题"),
    ChatMessage(role="assistant", content="好的，请问您对本次配送安装和客服体验还有其他建议吗？"),
    ChatMessage(role="user", content="暂时没有其他建议了。"),
    ChatMessage(role="assistant", content="了解，非常感谢张女士的配合，祝您生活愉快，再见！"),
]

# 含客户 FAQ 插播提问的示范对话 (用于 FAQ 提问检测与准确度质检演示)
DEFAULT_FAQ_DEMO_DIALOGUE: List[ChatMessage] = [
    ChatMessage(role="user", content="你好，哪位？"),
    ChatMessage(role="assistant", content="您好，我是星云智联售后客服中心的回访专员，请问是张女士吗？"),
    ChatMessage(role="user", content="是我，什么事？"),
    ChatMessage(role="assistant", content="张女士您好，主要是对您近期签收的智能网关设备做一次例行使用回访。请问设备目前安装使用是否顺利呢？"),
    ChatMessage(role="user", content="如果我七天内想申请质量问题退换货，往返运费是谁承担呀？"),
    ChatMessage(role="assistant", content="张女士，咱们所有退换货都需要买家自己承担全部运费哦。"),
    ChatMessage(role="user", content="哦这样啊，那设备目前用着暂时还可以。"),
    ChatMessage(role="assistant", content="好的，非常感谢您的配合，祝您生活愉快，再见！"),
]


@router.get("/")
def list_rules(db: Session = Depends(get_db)):
    # 优先同步 data/rules 目录下的 json 模板到数据库
    if os.path.exists(settings.RULES_DIR):
        for fname in os.listdir(settings.RULES_DIR):
            if fname.endswith(".json"):
                fpath = os.path.join(settings.RULES_DIR, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    rid = data.get("rule_id", fname.replace(".json", ""))
                    existing = db.query(RuleConfigRecord).filter(RuleConfigRecord.rule_id == rid).first()
                    if not existing:
                        rec = RuleConfigRecord(
                            rule_id=rid,
                            project_name=data.get("project_name", rid),
                            description=data.get("description", ""),
                            content_json=json.dumps(data, ensure_ascii=False),
                        )
                        db.add(rec)
                        db.commit()
                except Exception:
                    pass

    records = db.query(RuleConfigRecord).all()
    return [
        {
            "rule_id": r.rule_id,
            "project_name": r.project_name,
            "description": r.description,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }
        for r in records
    ]


@router.get("/{rule_id}")
def get_rule_detail(rule_id: str, db: Session = Depends(get_db)):
    rec = db.query(RuleConfigRecord).filter(RuleConfigRecord.rule_id == rule_id).first()
    if not rec:
        # 尝试从文件读
        fpath = os.path.join(settings.RULES_DIR, f"{rule_id}.json")
        if os.path.exists(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                return json.load(f)
        raise HTTPException(status_code=404, detail="未找到该场景规则配置")
    return json.loads(rec.content_json)


@router.post("/")
def save_rule_config(payload: UniversalRuleSchema, db: Session = Depends(get_db)):
    rec = db.query(RuleConfigRecord).filter(RuleConfigRecord.rule_id == payload.rule_id).first()
    content_str = json.dumps(payload.model_dump(), ensure_ascii=False)
    now = int(time.time())

    if rec:
        rec.project_name = payload.project_name
        rec.description = payload.description or ""
        rec.content_json = content_str
        rec.updated_at = now
    else:
        rec = RuleConfigRecord(
            rule_id=payload.rule_id,
            project_name=payload.project_name,
            description=payload.description or "",
            content_json=content_str,
            created_at=now,
            updated_at=now,
        )
        db.add(rec)
    db.commit()

    # 同步美化写入本地 rules 文件库 (保持缩进以利于 git 版本控制与审阅)
    file_content_str = json.dumps(payload.model_dump(), ensure_ascii=False, indent=2)
    fpath = os.path.join(settings.RULES_DIR, f"{payload.rule_id}.json")
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(file_content_str)

    return {"message": "规则配置保存成功", "rule_id": payload.rule_id}


@router.get("/templates/defaults")
def get_default_prompt_templates():
    """获取系统内置的标准轨迹检测、质检、FAQ 检测/质检、修复及对弈模拟 Prompt 模板"""
    return {
        "trajectory_extract_template": DEFAULT_TRAJECTORY_EXTRACT_PROMPT_TEMPLATE,
        "judge_template": DEFAULT_JUDGE_PROMPT_TEMPLATE,
        "judge_plain_template": DEFAULT_PLAIN_JUDGE_PROMPT_TEMPLATE,
        "faq_extract_template": DEFAULT_FAQ_EXTRACT_PROMPT_TEMPLATE,
        "faq_judge_template": DEFAULT_FAQ_JUDGE_PROMPT_TEMPLATE,
        "remediation_template": DEFAULT_REMEDIATION_PROMPT_TEMPLATE,
        "client_simulator_template": DEFAULT_CLIENT_SIMULATOR_PROMPT_TEMPLATE,
    }


@router.post("/debug-prompt")
def debug_assemble_prompt(payload: DebugPromptRequest, db: Session = Depends(get_db)):
    """
    调试组装提示词：优先使用前端传入的实时规则草稿 (rule_override)，次之读取已保存规则，
    将模板中的占位符完全拼接替换，供业务/开发人员查看准备发给 LLM 的真实请求体。
    """
    # 1. 获取规则（支持前端实时草稿覆盖，保障未保存字段即时进入拼装）
    rule = payload.rule_override if payload.rule_override is not None else load_rule_schema(db, payload.rule_id)

    # 2. 获取测试场景画像
    scenario = None
    if payload.scenario_id:
        scenario = next((s for s in rule.scenarios if s.id == payload.scenario_id), None)
    if not scenario and rule.scenarios:
        scenario = rule.scenarios[0]
    if not scenario:
        scenario = PersonaDefinition(
            id="demo_scenario",
            name="张女士_智能硬件用户",
            category="智能设备售后",
            persona={"device": "智能网关Pro", "attitude": "平和配合"},
        )

    # 3. 对话数据
    if payload.dialogue and len(payload.dialogue) > 0:
        dialogue: List[ChatMessage] = payload.dialogue
    elif payload.prompt_type in ("faq_extract", "faq_judge"):
        dialogue = DEFAULT_FAQ_DEMO_DIALOGUE
    else:
        dialogue = DEFAULT_DEMO_DIALOGUE

    judge_engine = UniversalJudgeEngine(judge_adapter=None)  # type: ignore
    assembled_prompt = ""
    assembled_msg_objs: List[ChatMessage] = []
    defn = get_builtin_definition(payload.prompt_type) or {}
    recommended_config: Dict[str, Any] = dict(
        defn.get(
            "recommended_config",
            {
                "temperature": 0.1,
                "max_tokens": 10240,
                "thinking_enabled": True,
                "thinking_level": "HIGH",
            },
        )
    )

    if payload.prompt_type == "trajectory_extract":
        assembled_msg_objs = judge_engine.build_trajectory_extract_messages(
            rule=rule,
            dialogue=dialogue,
            template_override=payload.template,
        )
        assembled_prompt = "\n\n".join(m.content for m in assembled_msg_objs)
    elif payload.prompt_type == "judge_plain":
        assembled_msg_objs = judge_engine.build_plain_judge_messages(
            rule=rule,
            scenario=scenario,
            dialogue=dialogue,
            template_override=payload.template,
        )
        assembled_prompt = "\n\n".join(m.content for m in assembled_msg_objs)
    elif payload.prompt_type == "judge":
        has_mermaid = bool(rule.topology_sop.enabled and rule.topology_sop.mermaid_graph.strip())
        if has_mermaid:
            demo_traj = _build_demo_trajectory_from_rule(rule)
            cp_map = rule.inspection_rules.node_checkpoints or {}
            demo_details = [
                {
                    "turn": (idx + 1) * 2,
                    "node_id": nid,
                    "evidence": (cp_map[nid].name if nid in cp_map and cp_map[nid].name else f"推进节点 {nid}"),
                }
                for idx, nid in enumerate(demo_traj)
            ]
            assembled_msg_objs = judge_engine.build_judge_messages(
                rule=rule,
                scenario=scenario,
                dialogue=dialogue,
                confirmed_trajectory=demo_traj,
                trajectory_details=demo_details,
                topology_valid=True,
                template_override=payload.template,
            )
        else:
            assembled_msg_objs = judge_engine.build_judge_messages(
                rule=rule,
                scenario=scenario,
                dialogue=dialogue,
                confirmed_trajectory=None,
                trajectory_details=None,
                topology_valid=None,
                template_override=payload.template,
            )
        assembled_prompt = "\n\n".join(m.content for m in assembled_msg_objs)
    elif payload.prompt_type == "faq_extract":
        assembled_msg_objs = judge_engine.build_faq_extract_messages(
            rule=rule,
            dialogue=dialogue,
            scenario=scenario,
            template_override=payload.template,
        )
        assembled_prompt = "\n\n".join(m.content for m in assembled_msg_objs)
    elif payload.prompt_type == "faq_judge":
        faq_round: Dict[str, Any] = {
            "user_turn": 5,
            "assistant_turn": 6,
            "raw_user_question": "如果我七天内想申请质量问题退换货，往返运费是谁承担呀？",
            "raw_assistant_answer": "张女士，咱们所有退换货都需要买家自己承担全部运费哦。",
            "rewritten_query": "智能设备七天内质量问题退换货往返运费承担规则",
        }
        non_sys_turns = [m for m in dialogue if m.role != "system"]
        for idx, msg in enumerate(non_sys_turns):
            if msg.role == "user" and ("？" in msg.content or "?" in msg.content or "运费" in msg.content or "退换" in msg.content):
                next_a = non_sys_turns[idx + 1] if idx + 1 < len(non_sys_turns) and non_sys_turns[idx + 1].role == "assistant" else None
                faq_round = {
                    "user_turn": idx + 1,
                    "assistant_turn": (idx + 2) if next_a else idx + 1,
                    "raw_user_question": msg.content,
                    "raw_assistant_answer": next_a.content if next_a else "",
                    "rewritten_query": msg.content,
                }
                break

        retrieved_candidates: List[Dict[str, Any]] = []
        if getattr(rule, "faq_inspection", None) and rule.faq_inspection.knowledge_base:
            try:
                retriever = UniversalFAQRetriever(rule.faq_inspection)
                retrieved_candidates = retriever.search(
                    faq_round["rewritten_query"], top_k=rule.faq_inspection.top_k or 3
                )
            except Exception:
                retrieved_candidates = []
            if not retrieved_candidates:
                for it in rule.faq_inspection.knowledge_base[:3]:
                    retrieved_candidates.append(
                        {"faq": it, "score": 0.92, "retrieval_source": "kb_preview"}
                    )
        else:
            retrieved_candidates = [
                {
                    "faq": FAQItem(
                        id="FAQ_001",
                        category="退换货政策",
                        question="七天无理由退换货与质量问题运费承担标准",
                        answer="未拆封激活商品支持签收起7天内无理由退换；因商品质量问题退换货，往返运费由商家承担。",
                    ),
                    "score": 0.92,
                    "retrieval_source": "hybrid_rrf",
                }
            ]

        assembled_msg_objs = judge_engine.build_faq_judge_messages(
            rule=rule,
            scenario=scenario,
            faq_round=faq_round,
            retrieved_candidates=retrieved_candidates,
            dialogue=dialogue,
            template_override=payload.template,
        )
        assembled_prompt = "\n\n".join(m.content for m in assembled_msg_objs)
    elif payload.prompt_type == "client_simulator":
        sim = UniversalSimulator(adapter=None)  # type: ignore
        sys_content = sim._build_client_prompt(scenario, template_override=payload.template)
        first_bot_line = next(
            (m.content for m in dialogue if m.role == "assistant"),
            "您好，请问是本人吗？",
        )
        assembled_msg_objs = [
            ChatMessage(role="system", content=sys_content),
            ChatMessage(role="user", content=first_bot_line),
        ]
        assembled_prompt = "\n\n".join(m.content for m in assembled_msg_objs)
    else:
        defects = payload.defects or [
            "【语气违规】坐席在第8轮评价'产品确实不错！'带感叹号过于随意，不符合严谨克制规范",
            "【逻辑抢跑/倒置】客户已在第7轮回答了设备型号与故障现象，坐席在第10轮重复追问使用问题",
        ]
        eval_report = InspectionReport(
            score=65,
            verdict="FAIL",
            detected_defects=defects,
            node_trajectory=[],
            topology_valid=True,
            flow_checkpoints={},
            expert_comments="出现重复提问与语气轻浮违规",
        )
        remediator = UniversalDataRemediator(remediate_adapter=None)
        assembled_msg_objs = remediator.build_remediation_messages(
            rule=rule,
            scenario=scenario,
            raw_dialogue=dialogue,
            eval_report=eval_report,
            template_override=payload.template,
        )
        assembled_prompt = "\n\n".join(m.content for m in assembled_msg_objs)

    return {
        "rule_id": rule.rule_id,
        "prompt_type": payload.prompt_type,
        "scenario_id": scenario.id,
        "assembled_prompt": assembled_prompt,
        "assembled_messages": [
            {"role": m.role, "content": m.content} for m in assembled_msg_objs
        ],
        "sample_dialogue": [{"role": m.role, "content": m.content} for m in dialogue],
        "recommended_config": recommended_config,
    }


@router.post("/test-prompt")
def test_assembled_prompt(payload: TestPromptRequest, db: Session = Depends(get_db)):
    """
    在线测试组装好的 Prompt：调用指定已注册模型，实时获取大模型输出与结构化解析结果。
    """
    model_rec = db.query(RegisteredModel).filter(RegisteredModel.id == payload.model_id).first()
    if not model_rec:
        raise HTTPException(status_code=404, detail=f"未找到已注册模型 '{payload.model_id}'")

    model_cfg_dict = json.loads(model_rec.config_json or "{}")
    adapter = AdapterFactory.create(
        provider_type=model_rec.provider_type,
        model_name=model_rec.model_name or model_rec.id,
        api_url=model_rec.api_url,
        api_key=model_rec.api_key,
        **model_cfg_dict,
    )
    registered_cfg = ModelConfig(**model_cfg_dict)

    test_msgs = (
        [
            ChatMessage(role=m.get("role", "user"), content=m.get("content", ""))
            for m in payload.assembled_messages
            if m.get("content")
        ]
        if payload.assembled_messages
        else [ChatMessage(role="user", content=payload.assembled_prompt)]
    )

    defn = get_builtin_definition(payload.prompt_type) or {}
    min_tokens = int(defn.get("recommended_config", {}).get("max_tokens", 10240))
    req_cfg = ModelConfig(
        temperature=registered_cfg.temperature,
        top_p=registered_cfg.top_p,
        max_tokens=max(registered_cfg.max_tokens or 0, min_tokens),
        repetition_penalty=registered_cfg.repetition_penalty,
        thinking_enabled=registered_cfg.thinking_enabled,
        thinking_level=registered_cfg.thinking_level,
        vertex_thinking_mode=registered_cfg.vertex_thinking_mode,
        extra_body=dict(registered_cfg.extra_body or {}),
    )

    req = UnifiedChatRequest(
        model_id=payload.model_id,
        messages=test_msgs,
        config=req_cfg,
    )

    t0 = time.time()
    try:
        resp = adapter.chat(req)
        latency_ms = int((time.time() - t0) * 1000)
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        return {
            "status": "error",
            "latency_ms": latency_ms,
            "error": str(e),
            "raw_content": "",
            "thinking": "",
            "parsed_data": None,
            "effective_config": req_cfg.model_dump(),
        }

    raw_content = resp.content or ""
    thinking = resp.thinking or ""
    parsed_data = None
    simulation_preview = None

    if payload.prompt_type in ("trajectory_extract", "faq_extract"):
        judge_engine = UniversalJudgeEngine(adapter)
        parsed_data = judge_engine._try_parse_json_dict(raw_content)
        if parsed_data is None:
            parsed_data = {
                "parse_error": "模型未返回合法 JSON 对象",
                "trajectory": [] if payload.prompt_type == "trajectory_extract" else None,
                "faq_rounds": [] if payload.prompt_type == "faq_extract" else None,
            }
    elif payload.prompt_type in ("judge", "judge_plain", "faq_judge"):
        judge_engine = UniversalJudgeEngine(adapter)
        parsed_data = judge_engine._extract_json_from_response(raw_content)
    elif payload.prompt_type == "client_simulator":
        parsed_data = {"reply": raw_content}
    else:
        remediator = UniversalDataRemediator(adapter)
        actions = remediator._extract_actions_from_response(raw_content)
        parsed_data = {"actions": actions}
        sample_dlg = payload.sample_dialogue or DEFAULT_DEMO_DIALOGUE
        if actions:
            remediated_msgs, report = remediator.apply_remediation_actions(sample_dlg, actions)
            ok, finalized, err = remediator.validate_and_sanitize_format(remediated_msgs)
            final_list: List[ChatMessage] = finalized if ok else remediated_msgs
            simulation_preview = {
                "execution_report": report,
                "is_format_valid": ok,
                "format_message": err,
                "remediated_turns": [{"role": m.role, "content": m.content} for m in final_list],
            }

    return {
        "status": "success",
        "latency_ms": latency_ms,
        "raw_content": raw_content,
        "thinking": thinking,
        "parsed_data": parsed_data,
        "simulation_preview": simulation_preview,
        "effective_config": req_cfg.model_dump(),
    }
