# -*- coding: utf-8 -*-
"""
全平台提示词注册、持久化管理与激活模板解析底座 (Prompt Registry)
集中管理各引擎使用的内置默认提示词、占位符契约、静态示例变量与用户自定义版本。
模板生效优先级:
  单次调试覆盖 (template_override) > 规则级自定义模板 > 提示词管理中心激活模板 > 系统内置默认模板
"""
import json
import re
import threading
import time
from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session
from backend.app.models.base import PromptTemplateRecord, SessionLocal
from backend.app.schemas.rule import (
    DEFAULT_TRAJECTORY_EXTRACT_PROMPT_TEMPLATE,
    DEFAULT_JUDGE_PROMPT_TEMPLATE,
    DEFAULT_PLAIN_JUDGE_PROMPT_TEMPLATE,
    DEFAULT_FAQ_EXTRACT_PROMPT_TEMPLATE,
    DEFAULT_FAQ_JUDGE_PROMPT_TEMPLATE,
    DEFAULT_REMEDIATION_PROMPT_TEMPLATE,
    DEFAULT_CLIENT_SIMULATOR_PROMPT_TEMPLATE,
    DEFAULT_BENCHMARK_JUDGE_PROMPT_TEMPLATE,
    PROMPT_USER_SPLIT_MARKER,
    split_prompt_into_system_and_user,
)


BUILTIN_PROMPT_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "prompt_key": "trajectory_extract",
        "name": "SOP 拓扑轨迹检测与槽位抽取",
        "category": "拓扑与流程质检",
        "description": "Step A1：当规则开启 Mermaid 流程图时，逐轮识别坐席推进的主节点 (node_id) 与客户顺带透露的提前槽位 (user_slots)，供确定性拓扑校验器使用。",
        "engine_location": "backend/app/engine/judge.py:extract_trajectory_and_slots",
        "supports_rule_context": True,
        "default_content": DEFAULT_TRAJECTORY_EXTRACT_PROMPT_TEMPLATE,
        "placeholders": [
            {"name": "mermaid_sop", "desc": "当前规则的 Mermaid SOP 流程图源码", "required": True},
            {"name": "node_checkpoints_summary", "desc": "各节点 ID 与名称速查 JSON", "required": True},
            {"name": "dialogue", "desc": "按 [#轮次] 编号的纯对话记录（不含 system）", "required": True},
        ],
        "sample_vars": {
            "mermaid_sop": "flowchart TD\n  N1_1(\"1.1 订单身份核验\") --> N2_1(\"2.1 设备使用回访\") --> N4_1(\"4.1 服务结束语\")",
            "node_checkpoints_summary": '{\n  "N1_1": "订单身份核验",\n  "N2_1": "设备使用回访",\n  "N4_1": "服务结束语"\n}',
            "dialogue": "[#1] USER: 喂，哪位？\n[#2] ASSISTANT: 您好，请问是张女士本人吗？\n[#3] USER: 是的。\n[#4] ASSISTANT: 感谢反馈，祝您生活愉快，再见！",
        },
        "recommended_config": {
            "temperature": 0.0,
            "max_tokens": 4096,
            "thinking_enabled": False,
            "thinking_level": "NONE",
        },
    },
    {
        "prompt_key": "judge",
        "name": "白盒流程与红线质检 (Mermaid SOP)",
        "category": "拓扑与流程质检",
        "description": "Step A3：结合 Mermaid SOP、拓扑连通性元数据、节点检查点与全局红线，对多轮会话进行结构化扣分 (deductions) 与合规裁决。",
        "engine_location": "backend/app/engine/judge.py:build_judge_messages",
        "supports_rule_context": True,
        "default_content": DEFAULT_JUDGE_PROMPT_TEMPLATE,
        "placeholders": [
            {"name": "mermaid_sop", "desc": "Mermaid SOP 流程图源码", "required": True},
            {"name": "checkpoints", "desc": "各节点检查点与必含要素 JSON", "required": True},
            {"name": "redlines", "desc": "全局一票否决红线列表", "required": True},
            {"name": "custom_rubric", "desc": "附加评分准则说明", "required": True},
            {"name": "scenario_id", "desc": "客户剧本唯一标识", "required": True},
            {"name": "scenario_name", "desc": "客户剧本名称", "required": True},
            {"name": "topology_status", "desc": "拓扑校验连通结论", "required": True},
            {"name": "trajectory", "desc": "提取后的节点推进轨迹数组", "required": True},
            {"name": "topology_error", "desc": "拓扑断路异常诊断", "required": True},
            {"name": "trajectory_details", "desc": "逐轮轨迹与证据明细 JSON（可选扩展）", "required": False},
            {"name": "dialogue", "desc": "按 [#轮次] 编号的纯对话记录", "required": True},
        ],
        "sample_vars": {
            "mermaid_sop": "flowchart TD\n  N1_1(\"1.1 订单身份核验\") --> N2_1(\"2.1 设备使用回访\") --> N4_1(\"4.1 服务结束语\")",
            "checkpoints": '{\n  "N1_1": {"name": "订单身份核验", "required_elements": ["是否下单人", "工号"]}\n}',
            "redlines": "- 严禁未经核实泄露客户收货地址隐私\n- 严禁威胁恐吓客户",
            "custom_rubric": "- 态度温和、专业克制\n- 已交代槽位严禁重复反问",
            "scenario_id": "demo_scenario",
            "scenario_name": "张女士_智能硬件用户",
            "topology_status": "连通合规 (VALID)",
            "trajectory": '["N1_1", "N2_1", "N4_1"]',
            "topology_error": "无断路异常，状态转移连通合规",
            "trajectory_details": '[{"turn": 2, "node_id": "N1_1", "evidence": "核实下单人身份"}]',
            "dialogue": "[#1] USER: 喂，你好。\n[#2] ASSISTANT: 您好，星云智联售后客服专员，请问是张女士吗？",
        },
        "recommended_config": {
            "temperature": 0.1,
            "max_tokens": 10240,
            "thinking_enabled": True,
            "thinking_level": "HIGH",
        },
    },
    {
        "prompt_key": "judge_plain",
        "name": "纯规则与红线质检 (无 Mermaid)",
        "category": "拓扑与流程质检",
        "description": "当规则关闭 Mermaid 流程图时使用，直接依据及格门槛、全局红线与 Rubric 对对话服务规范打分。",
        "engine_location": "backend/app/engine/judge.py:build_plain_judge_messages",
        "supports_rule_context": True,
        "default_content": DEFAULT_PLAIN_JUDGE_PROMPT_TEMPLATE,
        "placeholders": [
            {"name": "pass_score_threshold", "desc": "质检及格分门槛（整型）", "required": True},
            {"name": "redlines", "desc": "全局高危红线列表", "required": True},
            {"name": "custom_rubric", "desc": "附加质检评分准则", "required": True},
            {"name": "scenario_id", "desc": "客户剧本唯一标识", "required": True},
            {"name": "scenario_name", "desc": "客户剧本名称", "required": True},
            {"name": "dialogue", "desc": "按 [#轮次] 编号的纯对话记录", "required": True},
        ],
        "sample_vars": {
            "pass_score_threshold": "85",
            "redlines": "- 严禁泄露客户隐私\n- 严禁态度恶劣或威胁恐吓",
            "custom_rubric": "- 回复专业严谨、温和礼貌\n- 无答非所问",
            "scenario_id": "demo_scenario",
            "scenario_name": "常规客户咨询",
            "dialogue": "[#1] USER: 我想问一下保修期怎么算。\n[#2] ASSISTANT: 您好，您可以在官方 App 订单详情中查看具体质保生效日期。",
        },
        "recommended_config": {
            "temperature": 0.1,
            "max_tokens": 10240,
            "thinking_enabled": True,
            "thinking_level": "HIGH",
        },
    },
    {
        "prompt_key": "faq_extract",
        "name": "FAQ 提问轮次检测与检索改写",
        "category": "FAQ 问答质检",
        "description": "Step B1：扫描对话识别客户主动插播的政策/费用/流程提问轮次，并结合上文消解代词生成知识库检索词 (rewritten_query)。",
        "engine_location": "backend/app/engine/judge.py:extract_faq_rounds_and_rewrite",
        "supports_rule_context": True,
        "default_content": DEFAULT_FAQ_EXTRACT_PROMPT_TEMPLATE,
        "placeholders": [
            {"name": "dialogue", "desc": "按 [#轮次] 编号的纯对话记录", "required": True},
            {"name": "fallback_policy", "desc": "兜底规范（兜底回复规则和特殊陈述回复触发faq规则）", "required": True},
            {"name": "scenario_id", "desc": "客户剧本唯一标识（可选上下文）", "required": False},
            {"name": "scenario_name", "desc": "客户剧本名称（可选上下文）", "required": False},
        ],
        "sample_vars": {
            "scenario_id": "demo_scenario",
            "scenario_name": "张女士_智能硬件用户",
            "fallback_policy": "若问题超出知识库范围，指引联系官方售后热线或 App 人工客服，严禁捏造。",
            "dialogue": "[#1] USER: 如果我七天内想退换货，收运费吗？\n[#2] ASSISTANT: 退换货均需客户自行承担运费。",
        },
        "recommended_config": {
            "temperature": 0.0,
            "max_tokens": 4096,
            "thinking_enabled": False,
            "thinking_level": "NONE",
        },
    },
    {
        "prompt_key": "faq_judge",
        "name": "专职 FAQ 准确度质检",
        "category": "FAQ 问答质检",
        "description": "Step B2：将抽出的客户 FAQ 提问轮次、坐席回答与 Top-K 混合检索召回的标准答案进行比对，核查事实一致性与超纲兜底规范。",
        "engine_location": "backend/app/engine/judge.py:evaluate_faq_accuracy",
        "supports_rule_context": True,
        "default_content": DEFAULT_FAQ_JUDGE_PROMPT_TEMPLATE,
        "placeholders": [
            {"name": "fallback_policy", "desc": "兜底规范（兜底回复规则和特殊陈述回复触发faq规则）", "required": True},
            {"name": "scenario_id", "desc": "客户剧本唯一标识", "required": True},
            {"name": "scenario_name", "desc": "客户剧本名称", "required": True},
            {"name": "user_turn", "desc": "客户提问轮次号（可选）", "required": False},
            {"name": "assistant_turn", "desc": "坐席应答轮次号（可选）", "required": False},
            {"name": "user_question", "desc": "客户原始提问内容（可选）", "required": False},
            {"name": "assistant_answer", "desc": "坐席实际回复内容（可选）", "required": False},
            {"name": "rewritten_query", "desc": "改写后的检索词（可选）", "required": False},
            {"name": "faq_dialogue_slice", "desc": "问答双轮上下文切片", "required": True},
            {"name": "candidate_faqs", "desc": "Top-K 召回的官方标准 FAQ 候选列表", "required": True},
        ],
        "sample_vars": {
            "fallback_policy": "若问题超出知识库范围，指引联系官方售后热线或 App 人工客服，严禁捏造。",
            "scenario_id": "demo_scenario",
            "scenario_name": "张女士_智能硬件用户",
            "user_turn": "5",
            "assistant_turn": "6",
            "user_question": "质量问题七天内退换货收运费吗？",
            "assistant_answer": "退换货均需买家自行承担往返运费哦。",
            "rewritten_query": "智能设备七天内质量问题退换货运费承担标准",
            "faq_dialogue_slice": "[#5] USER: 质量问题七天内退换货收运费吗？\n[#6] ASSISTANT: 退换货均需买家自行承担往返运费哦。",
            "candidate_faqs": "- [ID: FAQ_001] 标准问题: 七天无理由与质量问题退换货运费标准\n  官方标准答案口径: 因质量问题退换货，往返运费由商家承担。",
        },
        "recommended_config": {
            "temperature": 0.1,
            "max_tokens": 10240,
            "thinking_enabled": True,
            "thinking_level": "HIGH",
        },
    },
    {
        "prompt_key": "remediate",
        "name": "声明式双工具数据精修 (prune + replace)",
        "category": "数据精修与清洗",
        "description": "根据质检缺陷项与因果约束，开具成对剪枝 (prune) 与坐席话术微创替换 (replace) 处方单，严禁篡改客户原话。",
        "engine_location": "backend/app/engine/remediator.py:build_remediation_messages",
        "supports_rule_context": True,
        "default_content": DEFAULT_REMEDIATION_PROMPT_TEMPLATE,
        "placeholders": [
            {"name": "custom_instructions_block", "desc": "场景专属修复指导与防挂机要求", "required": True},
            {"name": "system_prompt", "desc": "坐席角色定位 System Prompt", "required": True},
            {"name": "mermaid_sop", "desc": "Mermaid SOP 流程图源码", "required": True},
            {"name": "scenario_id", "desc": "客户剧本唯一标识", "required": True},
            {"name": "scenario_name", "desc": "客户剧本名称", "required": True},
            {"name": "defects", "desc": "质检检出的缺陷与结构化扣分 JSON", "required": True},
            {"name": "dialogue", "desc": "按 [#轮次] 编号的原对话记录", "required": True},
        ],
        "sample_vars": {
            "custom_instructions_block": "严禁在未完成核心沟通前提前输出告别语；保持专业克制。",
            "system_prompt": "你是星云智联售后客服中心的专业客服专员。",
            "mermaid_sop": "flowchart TD\n  N1_1(\"1.1 订单身份核验\") --> N2_1(\"2.1 设备使用回访\")",
            "scenario_id": "demo_scenario",
            "scenario_name": "张女士_智能硬件用户",
            "defects": '["【语气违规】第4轮包含多余感叹号"]',
            "dialogue": "[#1] USER: 你好。\n[#2] ASSISTANT: 您好！太棒了！",
        },
        "recommended_config": {
            "temperature": 0.1,
            "max_tokens": 10240,
            "thinking_enabled": True,
            "thinking_level": "HIGH",
        },
    },
    {
        "prompt_key": "client_simulator",
        "name": "AI 模拟客户对弈人设驱动",
        "category": "模拟对弈",
        "description": "驱动模拟客户 Agent 的静态 System Prompt，锁定两极化人设极性、口语化表达及主动挂断机制，保障 Prefix Cache 稳定命中。",
        "engine_location": "backend/app/engine/simulator.py:_build_client_prompt",
        "supports_rule_context": True,
        "default_content": DEFAULT_CLIENT_SIMULATOR_PROMPT_TEMPLATE,
        "placeholders": [
            {"name": "persona_json", "desc": "客户画像 JSON 档案", "required": True},
            {"name": "scenario_instruction", "desc": "当前剧本目标与动机指示", "required": True},
        ],
        "sample_vars": {
            "persona_json": '{\n  "name": "张女士",\n  "device": "智能网关Pro",\n  "attitude": "平和配合"\n}',
            "scenario_instruction": "根据设备近期使用体验配合客服完成例行售后回访。",
        },
        "recommended_config": {
            "temperature": 0.4,
            "max_tokens": 256,
            "thinking_enabled": False,
            "thinking_level": "NONE",
        },
    },
    {
        "prompt_key": "benchmark_judge",
        "name": "常规评测 Rubric 裁判打分",
        "category": "常规评测",
        "description": "用于常规 Benchmark 数据集评测中，对照标准参考答案、Rubric 细则与正则约束对被测模型输出进行多维度裁判打分。",
        "engine_location": "backend/app/engine/judge.py:evaluate_benchmark_response",
        "supports_rule_context": False,
        "default_content": DEFAULT_BENCHMARK_JUDGE_PROMPT_TEMPLATE,
        "placeholders": [
            {"name": "category_display", "desc": "题目分类及子分类", "required": True},
            {"name": "difficulty", "desc": "题目难度等级", "required": True},
            {"name": "user_input", "desc": "标准测试集用户提问内容", "required": True},
            {"name": "reference", "desc": "标准参考答案", "required": True},
            {"name": "criteria", "desc": "评分细则 (Rubric)", "required": True},
            {"name": "constraints_desc", "desc": "客观正则/禁忌约束描述", "required": True},
            {"name": "model_answer", "desc": "被测模型实际回复", "required": True},
        ],
        "sample_vars": {
            "category_display": "逻辑推理 / 业务合规",
            "difficulty": "medium",
            "user_input": "客户询问智能设备签收7天内因质量问题退换货是否自费运费？",
            "reference": "因产品质量问题申请7天内退换货，往返运费均由商家承担，不收取额外手续费。",
            "criteria": "必须明确说明质量问题退换由商家承担运费，不得包含误导性收费表述。",
            "constraints_desc": '\n【客观正则/禁忌约束】: ["!售后专员", "商家承担|免运费"]',
            "model_answer": "商品因质量问题在7天内申请退换货，往返运费由商家承担，不会额外收取手续费。",
        },
        "recommended_config": {
            "temperature": 0.1,
            "max_tokens": 2048,
            "thinking_enabled": True,
            "thinking_level": "MEDIUM",
        },
    },
]

_BUILTIN_DEF_MAP: Dict[str, Dict[str, Any]] = {
    item["prompt_key"]: item for item in BUILTIN_PROMPT_DEFINITIONS
}

_CACHE_LOCK = threading.RLock()
_ACTIVE_PROMPT_CACHE: Dict[str, Optional[str]] = {}


def get_builtin_definition(prompt_key: str) -> Optional[Dict[str, Any]]:
    return _BUILTIN_DEF_MAP.get(prompt_key)


def _record_to_dict(rec: PromptTemplateRecord) -> Dict[str, Any]:
    defn = _BUILTIN_DEF_MAP.get(rec.prompt_key, {})
    try:
        placeholders = json.loads(rec.placeholders_json or "[]")
    except Exception:
        placeholders = defn.get("placeholders", [])
    try:
        sample_vars = json.loads(rec.sample_vars_json or "{}")
    except Exception:
        sample_vars = defn.get("sample_vars", {})

    return {
        "prompt_key": rec.prompt_key,
        "name": rec.name,
        "category": rec.category,
        "description": rec.description,
        "engine_location": rec.engine_location,
        "supports_rule_context": bool(defn.get("supports_rule_context", True)),
        "template_content": rec.template_content,
        "default_content": rec.default_content,
        "placeholders": placeholders,
        "sample_vars": sample_vars,
        "is_customized": bool(rec.is_customized),
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
    }


def sync_builtin_prompts(db: Session) -> None:
    """确保所有内置提示词都已入库；未自定义的条目自动随代码默认模板同步。"""
    now = int(time.time())
    changed = False
    for item in BUILTIN_PROMPT_DEFINITIONS:
        key = item["prompt_key"]
        rec = db.query(PromptTemplateRecord).filter(PromptTemplateRecord.prompt_key == key).first()
        placeholders_str = json.dumps(item.get("placeholders", []), ensure_ascii=False)
        sample_vars_str = json.dumps(item.get("sample_vars", {}), ensure_ascii=False)
        default_tpl = item["default_content"]

        if not rec:
            rec = PromptTemplateRecord(
                prompt_key=key,
                name=item["name"],
                category=item["category"],
                description=item["description"],
                engine_location=item["engine_location"],
                template_content=default_tpl,
                default_content=default_tpl,
                placeholders_json=placeholders_str,
                sample_vars_json=sample_vars_str,
                is_customized=False,
                created_at=now,
                updated_at=now,
            )
            db.add(rec)
            changed = True
        else:
            if (
                rec.name != item["name"]
                or rec.category != item["category"]
                or rec.description != item["description"]
                or rec.engine_location != item["engine_location"]
                or rec.default_content != default_tpl
                or rec.placeholders_json != placeholders_str
                or rec.sample_vars_json != sample_vars_str
            ):
                rec.name = item["name"]
                rec.category = item["category"]
                rec.description = item["description"]
                rec.engine_location = item["engine_location"]
                rec.default_content = default_tpl
                rec.placeholders_json = placeholders_str
                rec.sample_vars_json = sample_vars_str
                if not rec.is_customized:
                    rec.template_content = default_tpl
                rec.updated_at = now
                changed = True

        with _CACHE_LOCK:
            _ACTIVE_PROMPT_CACHE[key] = (
                rec.template_content
                if (rec.is_customized and rec.template_content and rec.template_content.strip())
                else None
            )

    if changed:
        db.commit()


def get_active_prompt_template(prompt_key: str, default_template: str) -> str:
    """引擎运行期读取当前生效的提示词模板（带内存缓存，避免批量任务频繁创建 SQLite Session）。"""
    with _CACHE_LOCK:
        if prompt_key in _ACTIVE_PROMPT_CACHE:
            cached = _ACTIVE_PROMPT_CACHE[prompt_key]
            return cached if cached else default_template

    db: Optional[Session] = None
    resolved: Optional[str] = None
    try:
        db = SessionLocal()
        rec = db.query(PromptTemplateRecord).filter(PromptTemplateRecord.prompt_key == prompt_key).first()
        if rec and rec.is_customized and rec.template_content and rec.template_content.strip():
            resolved = rec.template_content
        with _CACHE_LOCK:
            _ACTIVE_PROMPT_CACHE[prompt_key] = resolved
    except Exception:
        resolved = None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    return resolved if resolved else default_template


def list_prompt_templates(db: Session) -> List[Dict[str, Any]]:
    sync_builtin_prompts(db)
    order_map = {item["prompt_key"]: idx for idx, item in enumerate(BUILTIN_PROMPT_DEFINITIONS)}
    records = db.query(PromptTemplateRecord).all()
    records.sort(key=lambda r: order_map.get(r.prompt_key, 999))
    return [_record_to_dict(r) for r in records]


def get_prompt_template_detail(db: Session, prompt_key: str) -> Optional[Dict[str, Any]]:
    sync_builtin_prompts(db)
    rec = db.query(PromptTemplateRecord).filter(PromptTemplateRecord.prompt_key == prompt_key).first()
    if not rec:
        return None
    return _record_to_dict(rec)


def update_prompt_template(db: Session, prompt_key: str, template_content: str) -> Optional[Dict[str, Any]]:
    sync_builtin_prompts(db)
    rec = db.query(PromptTemplateRecord).filter(PromptTemplateRecord.prompt_key == prompt_key).first()
    if not rec:
        return None
    now = int(time.time())
    normalized = template_content if template_content is not None else ""
    rec.template_content = normalized
    rec.is_customized = normalized.strip() != (rec.default_content or "").strip()
    rec.updated_at = now
    db.commit()
    db.refresh(rec)
    with _CACHE_LOCK:
        _ACTIVE_PROMPT_CACHE[prompt_key] = (
            rec.template_content
            if (rec.is_customized and rec.template_content and rec.template_content.strip())
            else None
        )
    return _record_to_dict(rec)


def reset_prompt_template(db: Session, prompt_key: str) -> Optional[Dict[str, Any]]:
    sync_builtin_prompts(db)
    rec = db.query(PromptTemplateRecord).filter(PromptTemplateRecord.prompt_key == prompt_key).first()
    if not rec:
        return None
    now = int(time.time())
    rec.template_content = rec.default_content
    rec.is_customized = False
    rec.updated_at = now
    db.commit()
    db.refresh(rec)
    with _CACHE_LOCK:
        _ACTIVE_PROMPT_CACHE[prompt_key] = None
    return _record_to_dict(rec)


def validate_prompt_placeholders(prompt_key: str, template_content: str) -> List[str]:
    """检查模板中缺失的必需占位符名称列表（可选占位符不触发告警）。"""
    defn = _BUILTIN_DEF_MAP.get(prompt_key)
    if not defn:
        return []
    text = template_content or ""
    missing: List[str] = []
    for ph in defn.get("placeholders", []):
        if not ph.get("required", True):
            continue
        name = ph.get("name", "")
        if name and f"{{{name}}}" not in text:
            missing.append(name)
    return missing


def render_template_vars(template_str: str, variables: Dict[str, Any]) -> str:
    """安全替换 `{key}` 占位符，避免 Python str.format 对 JSON 花括号报错。"""
    rendered = template_str or ""
    for k, v in (variables or {}).items():
        rendered = rendered.replace(f"{{{k}}}", str(v if v is not None else ""))
    return rendered


def preview_prompt_static(
    db: Session,
    prompt_key: str,
    template_override: Optional[str] = None,
    custom_vars: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """纯静态占位符示例预览：不依赖业务引擎、规则库与模型适配器，仅验证模板占位符替换与 System/User 拆分。"""
    detail = get_prompt_template_detail(db, prompt_key)
    if not detail:
        return None

    tpl = (
        template_override
        if template_override is not None and template_override.strip()
        else detail["template_content"]
    )
    vars_used = dict(detail.get("sample_vars") or {})
    if custom_vars:
        vars_used.update(custom_vars)

    rendered = render_template_vars(tpl.strip(), vars_used)

    # 检测渲染后是否仍有疑似未替换的已知/未知占位符（排除 JSON 样例块）
    known_names = {ph["name"] for ph in detail.get("placeholders", [])}
    unresolved = sorted(
        {
            m.group(1)
            for m in re.finditer(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", rendered)
            if m.group(1) in known_names
        }
    )

    if prompt_key == "client_simulator":
        messages = [{"role": "system", "content": rendered}]
    elif prompt_key == "benchmark_judge" and PROMPT_USER_SPLIT_MARKER not in rendered:
        messages = [{"role": "user", "content": rendered}]
    else:
        sys_part, user_part = split_prompt_into_system_and_user(rendered)
        messages = [
            {"role": "system", "content": sys_part},
            {"role": "user", "content": user_part},
        ]

    return {
        "prompt_key": prompt_key,
        "assembled_prompt": "\n\n".join(m["content"] for m in messages),
        "assembled_messages": messages,
        "missing_placeholders": validate_prompt_placeholders(prompt_key, tpl),
        "unresolved_placeholders": unresolved,
        "sample_vars": vars_used,
    }
