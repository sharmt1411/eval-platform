# -*- coding: utf-8 -*-
"""
模拟对弈与数据飞轮流转契约
"""
from typing import List, Dict, Any, Optional, Literal
from pydantic import BaseModel, Field
from backend.app.schemas.chat import ChatMessage


class InspectionReport(BaseModel):
    score: int
    verdict: Literal["PASS", "FAIL"]
    detected_defects: List[str] = Field(default_factory=list)
    deductions: List[Dict[str, Any]] = Field(default_factory=list)  # 扣分明细与依据说明 [{item, points, reason}]
    node_trajectory: List[str] = Field(default_factory=list)
    trajectory_details: List[Dict[str, Any]] = Field(default_factory=list)
    topology_valid: bool = True
    topology_error: Optional[str] = None
    flow_checkpoints: Dict[str, Any] = Field(default_factory=dict)
    expert_comments: str = ""
    notices: List[str] = Field(default_factory=list)
    request_messages: List[Dict[str, Any]] = Field(default_factory=list)
    debug_requests: List[Dict[str, Any]] = Field(default_factory=list)


class FlywheelBenchmarkSlice(BaseModel):
    """飞轮生成的基准评测切片契约"""
    slice_id: str                          # 切片唯一ID (例如 slice_sample001_turn8)
    sample_id: Optional[str] = None        # 所属飞轮样本 ID
    target_turn: int                       # 靶向原始轮次序号 (orig_turn)
    defect_item: str                       # 质检扣分项/节点名称
    defect_reason: str                     # 质检扣分具体原因
    messages: List[ChatMessage]            # 状态过滤后的累积纯净上文 (严格以 user 结尾)
    reference: str                         # 修复后的实际回复文本 (真实黄金答案，支持在线微调)
    rubric: str                            # 评分细则 (格式: item: reason，支持在线编辑)
    regex_constraints: List[str] = Field(default_factory=list) # 正反向正则 (支持自动继承与人工微调)
    category: Optional[str] = "飞轮实战质检" # 考题分类
    sub_category: Optional[str] = None     # 子分类 (如节点ID)


class FlywheelRecord(BaseModel):
    id: str
    rule_id: str
    scenario_id: str
    status: Literal["PENDING", "GOLD_PASS", "REPAIRED_PASS", "DISCARDED"] = "PENDING"
    initial_eval: Optional[InspectionReport] = None
    verified_eval: Optional[InspectionReport] = None
    raw_messages: List[ChatMessage] = Field(default_factory=list)
    remediated_messages: Optional[List[ChatMessage]] = None
    applied_actions: List[Dict[str, Any]] = Field(default_factory=list)
    benchmark_slices: List[FlywheelBenchmarkSlice] = Field(default_factory=list)
    remediation_debug: Optional[Dict[str, Any]] = None
    notices: List[str] = Field(default_factory=list)
    skipped_remediation: bool = False
    skip_reason: Optional[str] = None
    reviewer_comment: Optional[str] = None
    created_at: int = 0
    updated_at: int = 0


class SimulationRunRequest(BaseModel):
    rule_id: str
    target_model_id: str
    client_model_id: str
    judge_model_id: str
    remediate_model_id: Optional[str] = None
    scenario_ids: List[str] = Field(default_factory=lambda: ["all"])
    max_turns: int = 60
    auto_remediate: bool = True


class BatchUpdateStatusRequest(BaseModel):
    sample_ids: List[str]
    status: Literal["PENDING", "GOLD_PASS", "REPAIRED_PASS", "DISCARDED", "MANUAL_APPROVED"]
    reviewer_comment: Optional[str] = None


class BatchDeleteSamplesRequest(BaseModel):
    sample_ids: List[str]


class BatchRemediateRequest(BaseModel):
    sample_ids: List[str] = Field(..., min_length=1)
    rule_id: Optional[str] = None
    judge_model_id: Optional[str] = None
    remediate_model_id: Optional[str] = None
    run_initial_judge: bool = False
    skip_if_gold_pass: bool = False
    run_verified_judge: bool = False


class BatchJudgeRequest(BaseModel):
    sample_ids: List[str] = Field(..., min_length=1)
    rule_id: str
    judge_model_id: str


class SingleJudgeRequest(BaseModel):
    rule_id: str
    judge_model_id: str
    target_data: Literal["raw", "remediated"] = "raw"  # 质检目标: raw=原实战会话(重新初检), remediated=当前修复后语料(二次质检验收)


class SingleRemediateRequest(BaseModel):
    rule_id: Optional[str] = None
    judge_model_id: Optional[str] = None
    remediate_model_id: Optional[str] = None
    run_initial_judge: bool = False
    skip_if_gold_pass: bool = False
    run_verified_judge: bool = False


class BatchExportSelectedRequest(BaseModel):
    sample_ids: List[str]
    version_tag: str = "v1"
    inject_thinking: bool = False


class UpdateBenchmarkSlicesRequest(BaseModel):
    slices: List[FlywheelBenchmarkSlice]
    sync_to_dialogue: bool = True          # 是否原子化同步回写 remediated_messages 的对应坐席轮次


class ExportBenchmarkDatasetRequest(BaseModel):
    dataset_id: str
    name: str
    slice_ids: Optional[List[str]] = None
    mode: Literal["create", "append"] = "create"


class BatchExportBenchmarkRequest(BaseModel):
    sample_ids: List[str]
    dataset_id: str
    name: str
    mode: Literal["create", "append"] = "create"
    strategy: Literal["all_defects", "primary_defect_only"] = "all_defects"


class BatchFlywheelRunRequest(BaseModel):
    rule_id: str
    judge_model_id: str
    remediate_model_id: Optional[str] = None
    input_file_path: str
    gold_output_path: Optional[str] = None
    discard_output_path: Optional[str] = None

