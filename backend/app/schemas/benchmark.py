# -*- coding: utf-8 -*-
"""
常规数据集评测数据契约
"""
from typing import List, Dict, Any, Optional, Literal, Union
from pydantic import BaseModel, Field
from backend.app.schemas.chat import ChatMessage, ModelConfig, UnifiedChatResponse


class SampleEvalTracker(BaseModel):
    """单条评测样本的复测战绩追踪器 (由流水表聚合派生)"""
    passed_versions: List[str] = Field(default_factory=list, description="当前连续通过周期内已达标的不重复 version_tag 集合")
    consecutive_pass: int = Field(0, description="连续达标通过的不同版本数 (c)")
    miss_streak: int = Field(0, description="连续轮空未测次数 (m)")
    target_interval: int = Field(1, description="当前目标复测间隔 (I)")
    force_due: bool = Field(False, description="是否被同组退化连坐唤醒下轮必测")
    last_content_hash: Optional[str] = Field(None, description="最近一次实测时单题内容指纹")
    last_status: Literal["UNSEEN", "PASS", "FAIL"] = "UNSEEN"
    last_score: Optional[float] = None
    total_eval_count: int = 0
    last_eval_at: Optional[str] = None


class SamplingFilterConfig(BaseModel):
    """评测抽样与动态降频过滤配置"""
    enabled: bool = Field(False, description="是否开启智能阶梯降频抽样 (False则为全量评测)")
    pass_threshold_x: int = Field(3, ge=1, le=20, description="连续通过 x 个不同版本后开始降频")
    interval_step_s: int = Field(1, ge=1, le=10, description="每次通过后复测间隔递增步长 s")
    max_interval_n: int = Field(5, ge=1, le=50, description="最大评测间隔上限 N (第 N 次必测)")
    min_pass_score: Optional[float] = Field(
        None, ge=0.0, le=100.0, description="计入连续通过的最低裁判分数线 (None表示默认按PASS及格线)"
    )
    holdout_filter: Literal["all", "holdout_only", "non_holdout_only"] = Field(
        "all", description="评测题型过滤: all(全部题目) / holdout_only(仅留出题) / non_holdout_only(仅普通题)"
    )
    model_line: Optional[str] = Field(
        None, description="基准模型线标识 (例如 qwen2.5-7b-postloan；为空且未指定基准模型时表示只读摸底不入账)"
    )
    version_tag: Optional[str] = Field(
        None, description="本次评测模型版本号 (例如 v2.1；指定基准模型线更新战绩时必填)"
    )
    benchmark_model_id: Optional[str] = Field(
        None, description="参评模型列表中对应基准模型线的模型 ID 或展示名"
    )
    tracker_model_keys: List[str] = Field(
        default_factory=list,
        description="兼容旧版字段：用于更新战绩的基准模型标识列表；为空且 model_line 为空时表示只读摸底",
    )
    enable_group_regression_wakeup: bool = Field(
        False, description="是否开启显式 group_key 同组退化连坐唤醒（默认关闭）"
    )


class SamplingPreviewResponse(BaseModel):
    """抽样试算与预览响应"""
    total_samples: int = Field(0, description="当前题型过滤范围内总题数")
    sampled_samples: int = Field(0, description="本次预计抽测题数")
    skipped_samples: int = Field(0, description="本次休眠免测熟题数")
    saved_compute_ratio: float = Field(0.0, description="预计节省算力百分比 (0~100)")
    active_pool_count: int = Field(0, description="必测攻坚池题数 (I_i == 1)")
    hard_cap_pool_count: int = Field(0, description="封顶到期必测题数 (m + 1 >= N)")
    regular_cooldown_count: int = Field(0, description="常规降频轮巡抽中题数")
    cooldown_pool_total: int = Field(0, description="降频总池容量")
    holdout_count: int = Field(0, description="留出题总数量 (is_holdout=True)")
    non_holdout_count: int = Field(0, description="普通题总数量 (is_holdout=False)")
    sampled_holdout_count: int = Field(0, description="本次实测留出题数量")
    sampled_non_holdout_count: int = Field(0, description="本次实测普通题数量")
    skipped_holdout_count: int = Field(0, description="本次休眠留出熟题数量")
    skipped_non_holdout_count: int = Field(0, description="本次休眠普通熟题数量")
    holdout_filter: Literal["all", "holdout_only", "non_holdout_only"] = "all"
    is_rerun_same_version: bool = Field(False, description="是否命中同模型线同版本题单快照复用")
    model_line: Optional[str] = None
    version_tag: Optional[str] = None
    notice: str = Field("", description="业务提示说明")


class ResetTrackerRequest(BaseModel):
    """重置数据集战绩请求"""
    dataset_path: str
    model_line: Optional[str] = Field(None, description="可选指定模型线；为空时重置该数据集下全部模型线战绩")


class ResetTrackerResponse(BaseModel):
    """重置数据集战绩响应"""
    message: str
    reset_count: int


class BenchmarkExportFilterRequest(BaseModel):
    """评测集抽样与条件过滤导出请求"""
    sampling_config: SamplingFilterConfig = Field(
        default_factory=lambda: SamplingFilterConfig(enabled=True),
        description="复测抽样与动态降频策略配置",
    )
    export_scope: Literal[
        "sampled",
        "active_only",
        "cooldown_all",
        "skipped",
        "failed_only",
        "all",
    ] = Field(
        "sampled",
        description="导出范围: sampled(本轮抽样命中) / active_only(必测攻坚池) / cooldown_all(全部熟题池) / skipped(本轮休眠免测) / failed_only(最近一次失败) / all(全量)",
    )
    categories: List[str] = Field(default_factory=list, description="按分类过滤 (为空表示不限)")
    difficulties: List[str] = Field(default_factory=list, description="按难度过滤 (为空表示不限)")
    holdout_filter: Literal["all", "holdout_only", "non_holdout_only"] = Field(
        "all", description="留出题属性过滤"
    )
    keyword: Optional[str] = Field(None, description="按 ID / 对话文本 / 参考答案关键词过滤")
    include_tracker: bool = Field(
        False, description="是否在导出的 JSONL 中附带当前模型线的 eval_tracker 战绩快照"
    )
    export_target: Literal["download", "save_dataset"] = Field(
        "download", description="导出目标: download(返回JSONL下载) / save_dataset(另存为平台新评测集)"
    )
    target_dataset_id: Optional[str] = Field(None, description="另存为新评测集时的目标 ID")
    target_dataset_name: Optional[str] = Field(None, description="另存为新评测集时的显示名称")
    save_mode: Literal["overwrite", "append"] = Field("overwrite", description="另存为新评测集时的写入模式")


class BenchmarkExportPreviewResponse(BaseModel):
    """评测集过滤导出试算预览响应"""
    dataset_id: str
    total_samples: int
    exported_count: int
    export_scope: str
    sampling_stats: SamplingPreviewResponse
    preview_sample_ids: List[str] = Field(default_factory=list)


class BenchmarkDatasetFilterOptionsResponse(BaseModel):
    """评测集可用过滤维度与历史基准模型线选项"""
    dataset_id: str
    total_samples: int
    holdout_count: int
    non_holdout_count: int
    categories: List[str] = Field(default_factory=list)
    difficulties: List[str] = Field(default_factory=list)
    model_lines: List[Dict[str, Any]] = Field(default_factory=list)


class RubricConfig(BaseModel):
    must_contain: List[str] = Field(default_factory=list)
    forbidden: List[str] = Field(default_factory=list)
    regex_patterns: List[str] = Field(default_factory=list)
    require_json: bool = False
    judge_criteria: Optional[str] = None


class BenchmarkSample(BaseModel):
    id: str
    category: str = "默认场景"
    sub_category: Optional[str] = None
    difficulty: Optional[str] = None
    source: Optional[str] = None
    max_new_tokens: Optional[int] = None
    is_holdout: bool = Field(False, description="是否为留出评测题 (True=留出题, False=普通题)")
    group_key: Optional[str] = Field(None, description="可选显式业务分组键 (如节点ID/FAQ编号)")
    messages: List[ChatMessage]
    reference: Optional[str] = None
    rubric: Optional[Union[str, RubricConfig]] = None
    regex_constraints: List[str] = Field(default_factory=list)
    eval_tracker: Optional[SampleEvalTracker] = None


class ModelEvalConfig(BaseModel):
    model_id: str
    display_name: Optional[str] = None
    provider_type: Literal["openai", "harmony", "vertex", "mock"] = "openai"
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    model_name: Optional[str] = None
    config: ModelConfig = Field(default_factory=ModelConfig)


class BenchmarkRunRequest(BaseModel):
    dataset_path: str
    model_ids: Optional[List[str]] = Field(default_factory=list)
    model_configs: Optional[List[ModelEvalConfig]] = None
    judge_model_id: Optional[str] = None
    workers: int = 4
    max_tokens: Optional[int] = Field(None, description="统一最大生成Token，若指定将应用为各模型的默认上限")
    ignore_sample_max_tokens: bool = Field(False, description="是否忽略样本自带的max_new_tokens限制")
    sampling_config: Optional[SamplingFilterConfig] = Field(
        None, description="智能阶梯降频抽样与评测题型过滤配置，None 时为全量评测"
    )
    allow_unlabeled: bool = Field(
        False,
        description="是否允许无 reference/rubric/regex_constraints 的纯对话集跳过校验（仅用于连通性或生成速度测试）",
    )


class SampleEvalResult(BaseModel):
    sample_id: str
    category: str
    sub_category: Optional[str] = None
    difficulty: Optional[str] = None
    source: Optional[str] = None
    is_holdout: bool = False
    model_id: str
    source_model_id: Optional[str] = None
    response: UnifiedChatResponse
    objective_passed: bool
    objective_details: Dict[str, Any] = Field(default_factory=dict)
    judge_score: Optional[float] = None
    judge_verdict: Optional[str] = None
    judge_feedback: Optional[str] = None
    status: Literal["PASS", "FAIL", "API_ERROR"] = "FAIL"
    error_message: Optional[str] = None


class BenchmarkSummary(BaseModel):
    model_id: str
    total_samples: int = 0
    dataset_total_samples: int = 0
    skipped_mastered_samples: int = 0
    passed_samples: int = 0
    failed_samples: int = 0
    api_error_samples: int = 0
    api_error_rate: float = 0.0
    valid_samples: int = 0
    pass_rate: float = 0.0
    estimated_full_pass_rate: Optional[float] = None
    cooldown_tested_samples: int = 0
    cooldown_passed_samples: int = 0
    cooldown_failed_samples: int = 0
    cooldown_retention_rate: Optional[float] = None
    # 留出题 (is_holdout=True) 独立通过率与统计
    holdout_total_samples: int = 0
    holdout_dataset_total_samples: int = 0
    holdout_skipped_samples: int = 0
    holdout_valid_samples: int = 0
    holdout_passed_samples: int = 0
    holdout_failed_samples: int = 0
    holdout_api_error_samples: int = 0
    holdout_pass_rate: Optional[float] = None
    holdout_estimated_full_pass_rate: Optional[float] = None
    holdout_avg_judge_score: Optional[float] = None
    # 普通题 (is_holdout=False) 独立通过率与统计
    regular_total_samples: int = 0
    regular_dataset_total_samples: int = 0
    regular_skipped_samples: int = 0
    regular_valid_samples: int = 0
    regular_passed_samples: int = 0
    regular_failed_samples: int = 0
    regular_api_error_samples: int = 0
    regular_pass_rate: Optional[float] = None
    regular_estimated_full_pass_rate: Optional[float] = None
    regular_avg_judge_score: Optional[float] = None
    holdout_breakdown: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    is_benchmark_model: bool = False
    avg_latency_ms: float = 0.0
    avg_ttft_ms: float = 0.0
    avg_tps: float = 0.0
    thinking_token_ratio: float = 0.0
    objective_pass_rate: float = 0.0
    avg_judge_score: float = 0.0
    category_breakdown: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
