# -*- coding: utf-8 -*-
"""
通用规则与 SOP 状态机配置契约
"""
import re
from typing import List, Dict, Any, Optional, Literal, Tuple
from pydantic import BaseModel, Field


class BotConfig(BaseModel):
    system_prompt: str
    initiator: Literal["user", "assistant"] = "user"
    max_turns: int = 40
    closing_keywords: List[str] = Field(
        default_factory=lambda: ["祝您生活愉快，再见", "祝您生意兴隆，再见", "再见！"]
    )


class TopologySOPConfig(BaseModel):
    enabled: bool = True
    mermaid_graph: str = ""
    node_id_pattern: str = r"[A-Za-z0-9_]+"
    entry_nodes: List[str] = Field(default_factory=list)
    terminal_nodes: List[str] = Field(default_factory=list)
    allow_self_loop: bool = True
    allow_slot_shortcut: bool = True
    allow_customer_hangup_early_exit: bool = True  # 允许客户主动中断/挂机时在当前节点合法终止
    require_terminal_node: bool = False  # 是否刚性要求必须推进至法定终结节点 (默认False，防止客户挂机被误判为拓扑断路)
    stage_groups: List[List[str]] = Field(default_factory=list)
    trajectory_extract_prompt_template: Optional[str] = ""  # 可选的拓扑轨迹抽取提示词模板 (留空使用系统/提示词中心激活模板)


class NodeCheckpoint(BaseModel):
    name: Optional[str] = ""
    required_elements: List[str] = Field(default_factory=list)
    forbidden_keywords: List[str] = Field(default_factory=list)
    description: Optional[str] = None


class InspectionRules(BaseModel):
    pass_score_threshold: int = 85
    global_redlines: List[str] = Field(default_factory=list)
    node_checkpoints: Dict[str, NodeCheckpoint] = Field(default_factory=dict)
    custom_rubric: Optional[str] = None
    judge_prompt_template: Optional[str] = ""  # 可选的完整质检提示词模板 (留空使用系统默认模板)


class RemediationConfig(BaseModel):
    target_turn_range: List[int] = Field(default_factory=lambda: [30, 80])
    inject_empty_thinking: bool = False
    require_assistant_ending: bool = True
    strict_alternating_roles: bool = True
    freeze_user_input: bool = True  # 最高铁律: 默认绝对冻结用户输入，仅靶向修复客服回复
    custom_instructions: Optional[str] = ""  # 针对特定业务场景的自定义修复指导 (由业务规则自行补充，如挂机规范/要素宣讲)
    remediation_prompt_template: Optional[str] = ""  # 可选的完整自定义修复提示词模板


class PersonaDefinition(BaseModel):
    id: str
    name: str
    category: str = "默认分类"
    persona: Dict[str, Any] = Field(default_factory=dict)
    instruction: str = ""


class FAQItem(BaseModel):
    id: str = Field(..., description="FAQ 唯一标识，例如 FAQ_001")
    question: str = Field(..., description="标准问题及常见相似问法")
    answer: str = Field(..., description="官方唯一标准答案口径")
    category: Optional[str] = "默认分类"


class EmbeddingConfig(BaseModel):
    provider_type: Literal["local_cpu", "remote_api", "disabled"] = "local_cpu"
    local_model_name_or_path: Optional[str] = "BAAI/bge-small-zh-v1.5"
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    remote_model_name: Optional[str] = "text-embedding-3-small"


class FAQInspectionConfig(BaseModel):
    enabled: bool = False
    retrieval_mode: Literal["hybrid", "vector_only", "lexical_only"] = "hybrid"
    knowledge_base: List[FAQItem] = Field(default_factory=list)
    top_k: int = 3
    fallback_policy: str = "若问题超出知识库范围，必须委婉说明并指引联系官方售后热线或登录官方 App 查询，严禁凭空捏造政策。"
    embedding_config: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    hybrid_weights: Dict[str, float] = Field(
        default_factory=lambda: {"lexical": 0.4, "dense": 0.6}
    )
    faq_extract_prompt_template: Optional[str] = ""
    faq_judge_prompt_template: Optional[str] = ""


class UniversalRuleSchema(BaseModel):
    rule_id: str
    project_name: str
    description: Optional[str] = ""
    bot_config: BotConfig
    topology_sop: TopologySOPConfig = Field(default_factory=TopologySOPConfig)
    faq_inspection: FAQInspectionConfig = Field(default_factory=FAQInspectionConfig)
    inspection_rules: InspectionRules = Field(default_factory=InspectionRules)
    remediation_config: RemediationConfig = Field(default_factory=RemediationConfig)
    scenarios: List[PersonaDefinition] = Field(default_factory=list)


PROMPT_USER_SPLIT_MARKER = "<!-- USER_INPUT_START -->"


def split_prompt_into_system_and_user(
    rendered_prompt: str,
    fallback_user_prompt: str = "请严格遵照系统指令中的规则与输出格式要求完成任务。",
) -> Tuple[str, str]:
    """将完整渲染后的提示词拆分为 (system_instruction, user_input) 两段。
    优先按 PROMPT_USER_SPLIT_MARKER 精确拆分；兼容历史未带标记的自定义模板，自动剥离动态对话/元数据块至 user。
    """
    text = (rendered_prompt or "").strip()
    if not text:
        return "", fallback_user_prompt

    if PROMPT_USER_SPLIT_MARKER in text:
        sys_part, user_part = text.split(PROMPT_USER_SPLIT_MARKER, 1)
        return sys_part.strip(), (user_part.strip() or fallback_user_prompt)

    dynamic_patterns = [
        r"##\s*(?:评测)?场景上下文[\s\S]*?(?=\n##\s+|\n<|\Z)",
        r"(?:##\s*流程图拓扑连通性[^\n]*\n)?<topology_metadata>[\s\S]*?</topology_metadata>",
        r"(?:##\s*质检法官检出的缺陷项[^\n]*\n)?<defects>[\s\S]*?</defects>",
        r"(?:##\s*(?:被测实际对话记录|原对话记录)[^\n]*\n)?<dialogue>[\s\S]*?</dialogue>",
        r"(?:##\s*待核查的问答轮次[^\n]*\n)?<faq_dialogue_slice>[\s\S]*?</faq_dialogue_slice>",
        r"(?:##\s*检索召回的官方标准[^\n]*\n)?<candidate_faqs>[\s\S]*?</candidate_faqs>",
    ]
    user_chunks: List[str] = []
    sys_remaining = text
    for pat in dynamic_patterns:
        for m in re.finditer(pat, sys_remaining):
            chunk = m.group(0).strip()
            if chunk:
                user_chunks.append(chunk)
        sys_remaining = re.sub(pat, "", sys_remaining)

    sys_part = re.sub(r"\n{3,}", "\n\n", sys_remaining).strip()
    user_part = "\n\n".join(user_chunks).strip() or fallback_user_prompt
    if not sys_part:
        return fallback_user_prompt, text
    return sys_part, user_part

# 系统内置流程图状态机流转与豁免规则 (向后兼容历史 {sop_dynamics_block} 占位符，并直接内联至默认质检模板)
DEFAULT_SOP_DYNAMICS_BLOCK = """## 流程图状态机流转与豁免规则 (SOP Dynamics & Exemptions)
本场景启用了 Mermaid 状态机考核，审查坐席推进逻辑时必须严格遵照以下五项动力学准则：

1. **允许已获槽位顺承跳步 (Slot Shortcut)**: 
   若客户在之前的回答中已主动抢答或透露了后续某一节点所需的事实（如一句话同时交代了设备型号与安装环境），坐席顺承推进至下一未调研节点属于【合规高效推进】，**严禁判为遗漏该节点或违规跳步**！
2. **严禁要求对已获事实机械二次确认 (No Redundant Re-confirmation)**:
   当客户已在先前回合中主动交代了某项业务事实（例如客户在回答使用场景时顺带说明了“配件均已齐全”），坐席直接顺承提问下一项指标，**绝对严禁判为“节点缺失”或批评“未在配件核验节点进行二次确认”**！不进行多余冗余确认是极高服务质量的体现，避免死板复读。
3. **客户挂机意愿最高与合法中断豁免 (Customer Hangup Exemption)**:
   真实外呼业务中，客户拥有最高主导权。**当客户明确表达要开会、在开车、在忙、有急事或主动要求挂机时，坐席遵从客户意愿礼貌收尾、顺应挂机属于规范服务！绝对严禁批评坐席“未适当挽留”、“未能完成后续调研”、“节点未闭环”并以此为由判定不及格！**
4. **允许节点内自旋与 FAQ 解答 (Self-Loop Retention)**: 
   当客户提出质疑、顾虑、投诉或业务政策咨询时，坐席在当前业务节点进行安抚、解释或政策解答（在当前节点停留 1~2 轮），属于【合规服务交互】，**严禁判为流程停滞或拖沓**！
5. **刚性禁止拓扑断路与逆流乱窜 (Strict Topology Path)**: 
   坐席提问必须严格沿着流程图中的有向连通路径单向推进，绝对严禁无边跨越分支、逆流倒退或在互斥分支间无序乱跳。"""

# 系统内置默认质检 Prompt 骨架模板 (System 静态规则指令 + User 动态待测输入)
DEFAULT_JUDGE_PROMPT_TEMPLATE = (
    """# 业务对话白盒质检任务

你是一名严谨客观的业务对话质检专家。
请根据业务流程图 (Mermaid SOP)、各环节质检检查点、已客观提取的会话推进轨迹与全局红线禁忌，对用户消息中提供的实际对话记录进行白盒质检打分。

## 业务流程图 (SOP Graph)
<sop_graph>
```mermaid
{mermaid_sop}
```
</sop_graph>

"""
    + DEFAULT_SOP_DYNAMICS_BLOCK
    + """

## 各节点质检要点与必含要素 (Checkpoints)
<checkpoints>
{checkpoints}
</checkpoints>

## 全局一票否决红线禁忌 (Redlines)
<redlines>
{redlines}
</redlines>

## 评分标准与细则 (Rubric)
<rubric>
{custom_rubric}
</rubric>

## 质检打分与判定铁律
1. **基准分 100 分与扣分量规 (Scoring Rubric)**:
   - 会话初始基准分为 100 分。若整体流程顺畅合规、未触碰红线且无实质硬伤，必须给出 95~100 分高分，**绝对严禁凭空随性压分**！
   - 每一处扣分必须有明确的客观缺陷支撑：
     * 触碰全局红线：单项扣 40~50 分（一票否决为 FAIL）；
     * 核心要素严重遗漏或不可挽回的逆流乱窜：单项扣 10~15 分；
     * 违反 `<rubric>` 明确要求的约束（如单轮回复句数/长度限制等）或轻微冗余反问、生硬机械、语气瑕疵：单项扣 2~5 分；
     * **合规豁免项（客户主动要求挂机、客户提前抢答槽位顺承跳步）：严格扣 0 分！绝对严禁因未执行后续未激活节点而扣减所谓的“完整度分”！**
2. **结构化扣分列表铁律 (Strict Deductions Only)**:
   - `deductions` 数组是唯一的扣分依据源。**严格只填入真正导致扣分的实质性缺陷**，每项明确指出扣分节点/扣分项、具体坐席轮次序号 turn、扣减分值及具体原因；
   - 每一项扣分必须精准指定对应的坐席轮次序号 `turn` (整型数值，如 8，严格对应对话中的 `[#轮次编号]`)；
   - 凡是属于已合规豁免、或经判定“不视为缺陷”、“不视为遗漏”、“可接受”的分析说明，**绝对严禁写入 `deductions` 数组中**（可以在 `expert_comments` 综合点评中进行背景阐述）；
   - 若本次会话整体合格、无实质性违规扣分项，`deductions` **必须输出为空数组 `[]`**，`score` 必须输出为 100！
3. **拓扑客观元数据参考与大模型自主裁决**:
   - 用户输入中的拓扑连通性检测结果为系统图论分析生成的客观元数据事实，仅供你在打分时参考，轨迹图生成本身不加入代码级打分！
   - 所有的扣分幅度与 PASS/FAIL 结论全权由你作为专业法官综合判断裁决。若检测到严重拓扑断路或无连通关系的乱窜，由你根据对业务破坏程度自主决定扣分；若出现合理跳步、客户主动挂机或提前交代槽位，严禁盲目判为不及格！
4. **节点达成判断**: 重点审查各已发生节点中，坐席是否合规询问、客户是否给出有效应答(包括否定/为0/没有，已构成闭环)；
5. **客户主动挂机与信息顺承免责**:
   - 当客户明确表达要开会、在忙或主动要求挂机时，坐席礼貌结语顺应挂机属于规范服务，绝对严禁批评坐席“未适当挽留”或以“后续节点未调研/未闭环”为由扣分！
   - 若客户已在前序轮次中主动透露了某项业务事实（如设备型号或配件状态），坐席顺承跳过当前槽位属于高质量高效推进，绝对严禁判定为“节点缺失”或要求坐席机械二次确认！
6. **输出格式要求**: 请严格输出纯 JSON 对象，格式如下 (不要包含额外 markdown 包裹):
```json
{
  "score": 97,
  "verdict": "PASS",
  "deductions": [
    {
      "turn": 8,
      "item": "扣分节点/扣分项",
      "points": 3,
      "reason": "具体扣分依据与原因"
    }
  ],
  "expert_comments": "专家综合业务点评 (简述服务亮点、合规表现及改进建议，2~3句话)"
}
```
**特别注意**: 若本次会话无实质扣分项，`deductions` 必须输出为空数组 `[]`，`score` 必须输出为 100！

<!-- USER_INPUT_START -->
## 评测场景上下文
- 场景标识: {scenario_id}
- 场景名称: {scenario_name}

## 流程图拓扑连通性客观检测元数据 (Topology Inspection Metadata)
<topology_metadata>
- 拓扑连通状态: {topology_status}
- 提取推进轨迹: {trajectory}
- 拓扑异常诊断: {topology_error}
</topology_metadata>

## 被测实际对话记录 (Dialogue)
<dialogue>
{dialogue}
</dialogue>
"""
)

# 系统内置默认基于双工具 (prune + replace) 的声明式修复 Prompt 骨架模板
DEFAULT_REMEDIATION_PROMPT_TEMPLATE = """# 业务大模型微调 (SFT) 数据精修任务

你是一名顶级对话数据精修专家。以下是一段在业务模拟对弈中产生合规或时序缺陷的多轮对话记录。
请根据标准业务流程图与质检法官检出的缺陷项，使用平台赋予你的两项核心对话编辑工具开具【声明式修复处方单】，将原对话精修为高质量黄金样本。

## 平台赋权对话编辑工具箱 (DIALOGUE SURGICAL TOOLS)
你拥有且仅拥有以下两项工具，操作序号必须严格对应原对话的原始 [#轮次编号]：

1. **话术替换工具: replace**
   - 作用: 就地替换指定轮次的坐席 (assistant) 话术。
   - 铁律约束: 仅能动 assistant 轮次！**绝对严禁指定替换 user 轮次**！
   - 适用场景: 纠正生硬/违规语气、去除夸张感叹号、补齐工号/说明、修正漏问槽位。
   - 语法: {"tool": "replace", "turn": 4, "text": "张女士您好，主要是做个例行售后使用回访...", "reason": "补齐工号与例行回访说明"}

2. **问答剪枝工具: prune**
   - 作用: 剪除任意位置的冗余问答轮次，或删除结尾单出的客户发言。
   - 适用位置: **不限制从哪里剪枝**！无论是开场多余客套寒暄、中间跑题/重复反问/信息冗余、还是后序多余拉扯，只要需要精简，均可在对话的任意位置自由执行剪枝。
   - 铁律约束: 
     ① **必须保证成对**！每次 prune 必须包含相等数量的 user 与 assistant（如成对删除 1 对 [5, 6] 或 2 对 [5, 6, 7, 8]），严禁不成对删除破坏问答平衡；
     ② 唯一单轮特例: 允许单删末尾单个未被客服回复的 user 轮次（以保证 assistant 收尾）。
   - 语法: 
     - 剪枝 1 对: {"tool": "prune", "turns": [5, 6], "reason": "成对剪除多余客套/重复反问/跑题拉扯"}
     - 剪枝多对: {"tool": "prune", "turns": [5, 6, 7, 8], "reason": "批量剪除连续两对冗余问答"}

## 最高数据治理铁律
1. **客户言论神圣不可篡改**: 
   你没有任何修改 user 内容的工具！真实客户的原话代表生产环境真实输入分布，绝对不可被大模型篡改。若因重复提问产生冗余，只能使用 prune 成对剪除！
2. **基于原始序号声明**: 
   无论你开具的处方单先后顺序如何，所有 turn 和 turns 编号必须始终严格对应原对话中给出的原始 [#序号]！系统后台会自动按原始序号精准执行并保持时序连贯。
3. **【严禁因果断裂与答非所问 (ANTI-CAUSAL-BREAK)】**:
   当你打算使用 replace 修改某轮坐席话术时，**必须向下检查紧随其后的下一轮客户发言！**
   若你将当前轮改成了提问新问题，而下一轮真实客户的原话依然在回答旧问题，这就产生了严重的“答非所问（因果崩塌）”！
   **此时绝对严禁单纯修改坐席话术！必须使用 prune 工具成对剪除由旧坐席重复提问与客户被动复读构成的冗余问答对（例如 prune [12, 13]），使前文直接顺承后续正常对话！**
4. **【FAQ 缺陷的三段式合成修复铁律 (Answer ➔ Bridge ➔ Resume)】**:
   当缺陷项中指明了官方 FAQ 标准口径 (包含 matched_faq 或 reference_answer) 时，坐席在该轮 replace 中必须在单轮内合成三段式回复：
   - **第一段 (Answer 精准解答)**: 严格依照 reference_answer 官方标准口径解答客户插播的问题（事实零篡改，费用/时效/政策 100% 严谨）；
   - **第二段 (Bridge 自然过渡)**: 使用“跟您解释清楚后，回到刚才跟您核实的事项……”等过渡衔接语；
   - **第三段 (Resume 拉回主线)**: 立即抛出当前所处业务节点的提问，确保无缝咬合下一轮真实客户原话回答，严防流程走丢或答非所问！

<custom_business_rules>
{custom_instructions_block}
</custom_business_rules>

## 坐席角色定位 (System Prompt)
{system_prompt}

## 业务流程图 (SOP Graph)
<sop_graph>
```mermaid
{mermaid_sop}
```
</sop_graph>

## 输出格式要求
请严格输出纯 JSON 对象，格式如下:
```json
{
  "thought": "简短分析：原会话存在哪些问题，客户抢答了什么，哪些轮次应剪枝，哪些坐席轮次需替换文本...",
  "actions": [
    {
      "tool": "prune",
      "turns": [3, 4],
      "reason": "消除因客户抢答导致的坐席重复提问与客户复读"
    },
    {
      "tool": "replace",
      "turn": 6,
      "text": "修正后的客服合规话术...",
      "reason": "规范语气与业务合规"
    }
  ]
}
```

<!-- USER_INPUT_START -->
## 评测场景上下文
- 场景标识: {scenario_id}
- 场景名称: {scenario_name}

## 质检法官检出的缺陷项 (Defects - 本次必须纠正此处错误)
<defects>
{defects}
</defects>

## 原对话记录 (Dialogue)
<dialogue>
{dialogue}
</dialogue>
"""

# 系统内置默认专职 FAQ 问答质检 Prompt 骨架模板 (System 静态指令 + User 动态待查问答)
DEFAULT_FAQ_JUDGE_PROMPT_TEMPLATE = """# 业务问答 (FAQ) 准确度专职质检任务

你是一名严谨客观的业务政策与服务规范质检专家。
在人机对话中，客户可能会插播提问业务政策、费用、售后保障或操作流程。请结合检索到的官方标准政策知识库与超纲安全兜底要求，对坐席的应答准确度进行专职白盒质检打分。

## 兜底规范 (Fallback Policy)
<fallback_policy>
{fallback_policy}
</fallback_policy>

## 质检核查要点
1. **事实一致性 (Policy Consistency)**:
   - 核查坐席回答中的具体要素（金额、时效、费用标准、退换方式、服务条件）是否与官方标准知识 100% 一致；
   - 严禁凭空捏造政策、擅自许诺或出现误导性答复。
2. **兜底规范与特殊陈述核查 (Fallback Policy & Special Triggers)**:
   - 若检索出的候选 FAQ 均与客户问题不匹配（属知识库未覆盖的超纲问题），或触发了 `<fallback_policy>` 中约定的特殊陈述回复触发规则，核查坐席是否严格执行了兜底规范；
   - 严禁坐席对未收录业务信口开河。
3. **扣分规范**:
   - 若坐席回答准确合规或合法兜底，输出 0 处扣分 (`faq_deductions: []`)；
   - 若存在政策口径错误、事实捏造或超纲未兜底，视严重程度单项扣 10~25 分；
   - 每一条扣分必须指明对应 [#turn] 坐席回答轮次号、命中的 matched_faq_id、具体扣分原因 reason，并附带官方唯一正确口径 reference_answer。

## 输出格式要求
请严格输出纯 JSON 对象，格式如下:
```json
{
  "matched_faq_id": "FAQ_001",
  "faq_deductions": [
    {
      "turn": 14,
      "item": "FAQ问答准确度 [FAQ_001: 七天退换货政策]",
      "points": 15,
      "reason": "客户询问质量问题退换运费承担方，官方口径由商家承担往返运费，坐席错误回答需买家自费",
      "matched_faq_id": "FAQ_001",
      "reference_answer": "因产品质量问题申请7天内退换货，往返运费由商家承担，不收取额外手续费。"
    }
  ]
}
```
**特别注意**:
- 若坐席问答完全准确合规，"faq_deductions" 必须输出为空数组 `[]`；
- 无扣分时也请在顶层返回 `"matched_faq_id"`：若问题确实对应候选库中某条 FAQ 且坐席回答准确，填入对应 FAQ 的 ID（如 `"FAQ_001"`）；若客户问题属于知识库未覆盖的超纲问题且坐席已按兜底规范合规回应，请填入 `"FAQ_OOD"`！

<!-- USER_INPUT_START -->
## 场景上下文
- 场景标识: {scenario_id}
- 场景名称: {scenario_name}

## 待核查的问答轮次 (FAQ Rounds)
<faq_dialogue_slice>
{faq_dialogue_slice}
</faq_dialogue_slice>

## 检索召回的官方标准 FAQ 候选库 (Retrieved Candidates)
<candidate_faqs>
{candidate_faqs}
</candidate_faqs>
"""

# 系统内置默认 SOP 拓扑轨迹检测与槽位抽取 Prompt 骨架模板
DEFAULT_TRAJECTORY_EXTRACT_PROMPT_TEMPLATE = """你是一名专业的业务对话状态与流程分析师。
请根据以下 Mermaid 业务流程图与节点定义，逐轮分析人机对话中坐席 (assistant) 推进的业务节点轨迹，以及客户 (user) 顺带透露的提前槽位。

【业务流程图 (Mermaid SOP)】:
```mermaid
{mermaid_sop}
```

【已知节点名称速查】:
{node_checkpoints_summary}

【抽取分析铁律】:
1. 轨迹严格针对坐席 (assistant): 坐席每一轮提问或业务推进，严格只对应流程图中的【一个当前主节点】(node_id)；若仅为开场问候或结束道别，映射到开场/结束节点；
2. 客户 (user) 回复可能涉及其他节点数据: 若客户一句话同时顺带回答了后续节点所需的信息，将顺带涉及的节点ID记入 user_slots 数组；
3. 提取简单证据 (evidence): 用一句话说明坐席该轮具体发起/确认了什么；
4. 轮次编号 (turn): 必须严格对应下方对话记录中的 `[#轮次编号]` 整型数字（例如 `[#2] ASSISTANT` 对应 `"turn": 2`）。

请严格输出纯 JSON 对象，格式如下:
{
  "trajectory": [
    {
      "turn": 2,
      "node_id": "N1_1",
      "user_slots": [],
      "evidence": "核实下单人身份"
    },
    {
      "turn": 16,
      "node_id": "N2_3",
      "user_slots": [],
      "evidence": "核对配件齐全情况"
    }
  ]
}

<!-- USER_INPUT_START -->
## 被测实际对话记录 (Dialogue)
<dialogue>
{dialogue}
</dialogue>
"""

# 系统内置默认 FAQ 提问轮次抽取与检索改写 Prompt 骨架模板
DEFAULT_FAQ_EXTRACT_PROMPT_TEMPLATE = """你是一名专业的客服对话问答与意图分析师。
请仔细扫描用户提供的人机对话，识别出【真实客户 (USER) 主动插播询问政策、费用、售后规则、服务网点或业务办理流程的非主线问答轮次】，以及【命中下方兜底规范或特殊陈述触发规则的轮次】。
针对每一个命中轮次，提取原始发言并在结合上下文消解代词后，生成用于知识库精准检索的标准检索词 (rewritten_query)。

## 兜底规范与特殊触发规则 (Fallback Policy)
<fallback_policy>
{fallback_policy}
</fallback_policy>

【提取铁律】:
1. 提取真实客户提出业务政策疑问的轮次 (如询问七天退换货怎么算运费、质保期多久、如何预约上门维修、延保覆盖范围等)；
2. 若客户发言触发了 `<fallback_policy>` 中约定的兜底回复规则或特殊陈述触发 FAQ 规则（如超纲政策咨询、特殊异议/陈述触发特定答复等），必须一并提取为待核查轮次，并在 `rewritten_query` 中提炼核心诉求或触发场景关键词；
3. 若客户只是顺从回答坐席的主线业务提问（如“是我本人”、“设备运行正常”、“配件齐全”、“先不聊了在开会”）且未触发上述兜底/特殊规则，绝对不属于 FAQ 提问，严禁提取！
4. 若整通对话完全没有命中任何 FAQ 政策疑问或兜底触发条件，请严格输出纯 JSON: {"faq_rounds": []}；
5. rewritten_query: 必须结合上下文消解指代（如客户说“那质量问题退货呢”，改写为“智能设备七天内质量问题退换货运费承担规则”）。

请严格输出纯 JSON 对象，格式如下:
{
  "faq_rounds": [
    {
      "user_turn": 5,
      "assistant_turn": 6,
      "raw_user_question": "客户原始提问语句",
      "rewritten_query": "消解代词后的标准业务检索词"
    }
  ]
}

<!-- USER_INPUT_START -->
## 被测实际对话记录 (Dialogue)
<dialogue>
{dialogue}
</dialogue>
"""

# 系统内置默认纯规则与红线质检 Prompt 骨架模板 (未启用 Mermaid 流程图时使用)
DEFAULT_PLAIN_JUDGE_PROMPT_TEMPLATE = """# 业务对话规则与红线质检任务

你是一名严谨客观的业务对话质检专家。请根据质检标准、全局红线禁忌及评分细则，对用户提供的人机实际对弈记录进行质检打分。

- 及格分门槛: {pass_score_threshold} 分

## 全局一票否决红线禁忌 (Redlines)
<redlines>
{redlines}
</redlines>

## 质检标准与细则 (Rubric)
<rubric>
{custom_rubric}
</rubric>

## 质检判定准则
1. 检查坐席是否有触碰全局红线、态度恶劣或答非所问；
2. 对照质检标准与细则，评估坐席话术专业性与服务规范；
3. 请严格输出纯 JSON 对象:
```json
{
  "score": 90,
  "verdict": "PASS",
  "detected_defects": ["缺陷描述1", "缺陷描述2"],
  "flow_checkpoints": {},
  "expert_comments": "专家综合点评"
}
```

<!-- USER_INPUT_START -->
## 评测场景上下文
- 场景标识: {scenario_id}
- 场景名称: {scenario_name}

## 被测实际对话记录 (Dialogue)
<dialogue>
{dialogue}
</dialogue>
"""

# 系统内置默认 AI 模拟客户对弈人设驱动 System Prompt 模板
DEFAULT_CLIENT_SIMULATOR_PROMPT_TEMPLATE = """你现在正在参与一场针对业务对话机器人的实战模拟测试。
你必须严格扮演指定的【真实客户角色】（受访用户），对方是正在与你电话沟通的【业务坐席机器人】。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚨【客户扮演核心准则】🚨
1. 【绝不暴露 AI 身份】: 你是一个有血有肉、有真实痛点或现实防备的真实人类，说话必须完全口语化、接地气，禁止书面腔和 AI 解释；
2. 【严格秉承人设极性】:
   - 若人设为【极度抗辩/敏感】: 必须对来电目的、工号合法性字字计较，态度敌对、动辄扬言向平台或消协投诉；
   - 若人设为【极简冷漠回避】: 绝不多说废话，多用单字/短句（“哦”、“没空”、“以后再说”、“挂了”），态度消极推诿但绝不给具体配合；
   - 若人设为【专业规则套话】: 熟悉服务规范，故意引诱机器人说出不合规或越权承诺的话术，以套取违规证据；
   - 若人设为【焦虑遇障诉求】: 遇到明显的产品使用或服务阻碍，情绪焦急烦躁，如实反映核心痛点；
   - 若人设为【温和配合】: 如实交代使用情况与真实需求，积极配合沟通解决方案；
3. 【主动挂断机制】: 若你的耐心完全耗尽、或受到不当威胁、或对方已礼貌道别收口，请在回复末尾附带标定 `[挂断电话]`。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【客户角色背景与档案】:
{persona_json}

【当前场景目标与动机指示】:
{scenario_instruction}
"""

# 系统内置默认常规 Benchmark 评测 Rubric 裁判打分 Prompt 模板
DEFAULT_BENCHMARK_JUDGE_PROMPT_TEMPLATE = """你是一名严谨客观的大模型能力评测裁判。请根据提供的用户问题、标准参考答案及评分细则，对被测模型的回复进行多维度打分。

【题目分类】: {category_display} (难度: {difficulty})
【用户提问】:
{user_input}

【标准参考答案 (Reference)】:
{reference}

【评分细则 (Rubric)】:
{criteria}{constraints_desc}

【被测模型实际回复】:
{model_answer}

请严格按以下 JSON 格式输出评判结果:
{
  "score": 85,
  "verdict": "PASS",
  "feedback": "简短评价优缺点及扣分依据"
}
"""

