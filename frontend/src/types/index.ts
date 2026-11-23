// ==========================================
// 统一模型与对话契约
// ==========================================

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
  thinking?: string;
}

export type ThinkingLevel = 'NONE' | 'LOW' | 'MEDIUM' | 'HIGH';

export type VertexThinkingMode =
  | 'auto'
  | 'gemini_native'
  | 'maas_reasoning_effort'
  | 'maas_chat_template'
  | 'maas_none'
  | 'none';

export interface ModelConfig {
  temperature: number;
  top_p: number;
  max_tokens: number;
  repetition_penalty: number;
  thinking_enabled: boolean;
  thinking_level: ThinkingLevel;
  vertex_thinking_mode?: VertexThinkingMode | null;
  extra_body?: Record<string, any>;
}

export interface RegisteredModel {
  id: string;
  name: string;
  provider_type: 'openai' | 'harmony' | 'vertex' | 'mock';
  api_url?: string;
  api_key?: string;
  model_name?: string;
  config: ModelConfig;
  created_at?: number;
}

export interface ModelCreateRequest {
  id: string;
  name: string;
  provider_type: string;
  api_url?: string;
  api_key?: string;
  model_name?: string;
  config?: ModelConfig;
}

export interface ModelUpdateRequest {
  name?: string;
  provider_type?: string;
  api_url?: string;
  api_key?: string;
  model_name?: string;
  config?: ModelConfig;
}

export interface ModelTestResponse {
  status: 'SUCCESS' | 'FAILED';
  reply?: string;
  latency_ms?: number;
  thinking?: string;
  vertex_thinking_mode?: VertexThinkingMode | string | null;
  probe_info?: Record<string, any> | null;
  error?: string;
}

// ==========================================
// 规则与 SOP 状态机契约
// ==========================================

export interface BotConfig {
  system_prompt: string;
  initiator: 'user' | 'assistant';
  max_turns: number;
  closing_keywords: string[];
}

export interface TopologySOPConfig {
  enabled: boolean;
  mermaid_graph: string;
  node_id_pattern?: string;
  entry_nodes: string[];
  terminal_nodes: string[];
  allow_self_loop: boolean;
  allow_slot_shortcut: boolean;
  stage_groups: string[][];
  trajectory_extract_prompt_template?: string;
}

export interface NodeCheckpoint {
  name?: string;
  required_elements: string[];
  forbidden_keywords?: string[];
  description?: string;
}

export interface InspectionRules {
  pass_score_threshold: number;
  global_redlines: string[];
  node_checkpoints: Record<string, NodeCheckpoint>;
  custom_rubric?: string;
  judge_prompt_template?: string;
}

export interface RemediationConfig {
  target_turn_range: [number, number];
  inject_empty_thinking: boolean;
  require_assistant_ending: boolean;
  strict_alternating_roles: boolean;
  freeze_user_input?: boolean;
  custom_instructions?: string;
  remediation_prompt_template?: string;
}

export interface PersonaDefinition {
  id: string;
  name: string;
  category: string;
  persona: Record<string, any>;
  instruction: string;
}

export interface FAQItem {
  id: string;
  question: string;
  answer: string;
  category?: string;
}

export interface EmbeddingConfig {
  provider_type: 'local_cpu' | 'remote_api' | 'disabled';
  local_model_name_or_path?: string;
  api_url?: string;
  api_key?: string;
  remote_model_name?: string;
}

export interface FAQInspectionConfig {
  enabled: boolean;
  retrieval_mode: 'hybrid' | 'vector_only' | 'lexical_only';
  knowledge_base: FAQItem[];
  top_k: number;
  fallback_policy: string;
  embedding_config: EmbeddingConfig;
  hybrid_weights: Record<string, number>;
  faq_extract_prompt_template?: string;
  faq_judge_prompt_template?: string;
}

export interface UniversalRuleSchema {
  rule_id: string;
  project_name: string;
  description?: string;
  bot_config: BotConfig;
  topology_sop: TopologySOPConfig;
  faq_inspection?: FAQInspectionConfig;
  inspection_rules: InspectionRules;
  remediation_config: RemediationConfig;
  scenarios: PersonaDefinition[];
}

export interface RuleSummary {
  rule_id: string;
  project_name: string;
  description?: string;
  created_at?: number;
  updated_at?: number;
}

// ==========================================
// 数据集与评测契约
// ==========================================

export interface DatasetRecord {
  id: string;
  name: string;
  file_path: string;
  sample_count: number;
  dataset_type: 'benchmark' | 'flywheel_gold';
  created_at?: number;
}

export interface DatasetImportRequest {
  dataset_id: string;
  dataset_type: 'benchmark' | 'flywheel_gold';
  mode?: 'overwrite' | 'append';
  mark_as_holdout?: boolean;
  content?: string;
  samples?: Record<string, any>[];
  source_file_path?: string;
}

export type BenchmarkExportScope =
  | 'sampled'
  | 'active_only'
  | 'cooldown_all'
  | 'skipped'
  | 'failed_only'
  | 'all';

export interface BenchmarkExportFilterRequest {
  sampling_config: SamplingFilterConfig;
  export_scope: BenchmarkExportScope;
  categories?: string[];
  difficulties?: string[];
  holdout_filter?: 'all' | 'holdout_only' | 'non_holdout_only';
  keyword?: string;
  include_tracker?: boolean;
  export_target: 'download' | 'save_dataset';
  target_dataset_id?: string;
  target_dataset_name?: string;
  save_mode?: 'overwrite' | 'append';
}

export interface BenchmarkExportPreviewResponse {
  dataset_id: string;
  total_samples: number;
  exported_count: number;
  export_scope: string;
  sampling_stats: SamplingPreviewResponse;
  preview_sample_ids: string[];
}

export interface BenchmarkDatasetFilterOptionsResponse {
  dataset_id: string;
  total_samples: number;
  holdout_count: number;
  non_holdout_count: number;
  categories: string[];
  difficulties: string[];
  model_lines: Array<{
    model_line: string;
    version_tags: string[];
    ledger_count: number;
  }>;
}

export interface BenchmarkFilteredExportResponse {
  message: string;
  dataset_id: string;
  filename?: string;
  total_samples: number;
  exported_count: number;
  export_scope: string;
  export_target: 'download' | 'save_dataset';
  sampling_stats: SamplingPreviewResponse;
  jsonl_content?: string;
  saved_dataset?: DatasetRecord;
}

export interface RubricConfig {
  must_contain?: string[];
  forbidden?: string[];
  regex_patterns?: string[];
  require_json?: boolean;
  judge_criteria?: string;
}

export interface SampleEvalTracker {
  passed_versions?: string[];
  consecutive_pass: number;
  miss_streak: number;
  target_interval: number;
  force_due?: boolean;
  last_content_hash?: string | null;
  last_status: 'UNSEEN' | 'PASS' | 'FAIL';
  last_score?: number | null;
  total_eval_count: number;
  last_eval_at?: string | null;
}

export interface SamplingFilterConfig {
  enabled: boolean;
  pass_threshold_x: number;
  interval_step_s: number;
  max_interval_n: number;
  min_pass_score?: number | null;
  holdout_filter?: 'all' | 'holdout_only' | 'non_holdout_only';
  model_line?: string | null;
  version_tag?: string | null;
  benchmark_model_id?: string | null;
  tracker_model_keys: string[];
  enable_group_regression_wakeup?: boolean;
}

export interface SamplingPreviewResponse {
  total_samples: number;
  sampled_samples: number;
  skipped_samples: number;
  saved_compute_ratio: number;
  active_pool_count: number;
  hard_cap_pool_count: number;
  regular_cooldown_count: number;
  cooldown_pool_total: number;
  holdout_count?: number;
  non_holdout_count?: number;
  sampled_holdout_count?: number;
  sampled_non_holdout_count?: number;
  skipped_holdout_count?: number;
  skipped_non_holdout_count?: number;
  holdout_filter?: 'all' | 'holdout_only' | 'non_holdout_only';
  is_rerun_same_version?: boolean;
  model_line?: string | null;
  version_tag?: string | null;
  notice: string;
}

export interface BenchmarkSample {
  id: string;
  category?: string;
  sub_category?: string;
  difficulty?: string;
  source?: string;
  max_new_tokens?: number;
  is_holdout?: boolean;
  group_key?: string;
  messages: ChatMessage[];
  reference?: string;
  rubric?: string | RubricConfig;
  regex_constraints?: string[];
  eval_tracker?: SampleEvalTracker;
}

export interface ModelEvalConfig {
  model_id: string;
  display_name?: string;
  provider_type: 'openai' | 'harmony' | 'vertex' | 'mock';
  api_url?: string;
  api_key?: string;
  model_name?: string;
  config: ModelConfig;
}

export interface BenchmarkRunRequest {
  dataset_path: string;
  model_ids?: string[];
  model_configs?: ModelEvalConfig[];
  judge_model_id?: string;
  workers?: number;
  max_tokens?: number;
  ignore_sample_max_tokens?: boolean;
  sampling_config?: SamplingFilterConfig;
  allow_unlabeled?: boolean;
}

export interface BenchmarkSummary {
  model_id: string;
  total_samples: number;
  dataset_total_samples?: number;
  skipped_mastered_samples?: number;
  passed_samples: number;
  failed_samples?: number;
  api_error_samples?: number;
  api_error_rate?: number;
  valid_samples?: number;
  pass_rate: number;
  estimated_full_pass_rate?: number | null;
  cooldown_tested_samples?: number;
  cooldown_passed_samples?: number;
  cooldown_failed_samples?: number;
  cooldown_retention_rate?: number | null;
  holdout_total_samples?: number;
  holdout_dataset_total_samples?: number;
  holdout_skipped_samples?: number;
  holdout_valid_samples?: number;
  holdout_passed_samples?: number;
  holdout_failed_samples?: number;
  holdout_api_error_samples?: number;
  holdout_pass_rate?: number | null;
  holdout_estimated_full_pass_rate?: number | null;
  holdout_avg_judge_score?: number | null;
  regular_total_samples?: number;
  regular_dataset_total_samples?: number;
  regular_skipped_samples?: number;
  regular_valid_samples?: number;
  regular_passed_samples?: number;
  regular_failed_samples?: number;
  regular_api_error_samples?: number;
  regular_pass_rate?: number | null;
  regular_estimated_full_pass_rate?: number | null;
  regular_avg_judge_score?: number | null;
  holdout_breakdown?: Record<string, any>;
  is_benchmark_model?: boolean;
  avg_latency_ms: number;
  avg_ttft_ms: number;
  avg_tps: number;
  thinking_token_ratio: number;
  objective_pass_rate: number;
  avg_judge_score: number;
  category_breakdown: Record<string, any>;
}

export interface BenchmarkTaskItem {
  id: string;
  status: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED';
  progress: number;
  created_at?: number;
  finished_at?: number;
  error_message?: string;
  dataset_path?: string;
  max_tokens?: number;
  ignore_sample_max_tokens?: boolean;
  dataset_total_samples?: number;
  sampled_samples?: number;
  skipped_mastered_samples?: number;
  sampling_config?: SamplingFilterConfig;
  progress_detail?: {
    completed: number;
    total: number;
    current_model: string;
    current_sample_id: string;
  };
  summaries: Record<string, BenchmarkSummary>;
  model_configs?: ModelEvalConfig[];
}

export interface SideBySideResultItem {
  reply?: string;
  content?: string;
  thinking: string;
  latency_ms?: number;
  ttft_ms?: number;
  tps?: number;
  thinking_tokens?: number;
  metrics?: {
    ttft_ms: number;
    total_latency_ms: number;
    tokens_per_second: number;
  };
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    thinking_tokens: number;
    answer_tokens: number;
    total_tokens: number;
  };
  objective_passed: boolean;
  objective_details: Record<string, any>;
  judge_score?: number;
  judge_verdict?: string;
  judge_feedback?: string;
  status?: 'PASS' | 'FAIL' | 'API_ERROR';
  error_message?: string;
}

export interface SideBySideSample {
  sample_id: string;
  category: string;
  sub_category?: string;
  difficulty?: string;
  source?: string;
  is_holdout?: boolean;
  max_new_tokens?: number;
  input_prompt?: any;
  messages?: ChatMessage[];
  reference?: string;
  rubric?: string | RubricConfig;
  regex_constraints?: string[];
  results?: Record<string, SideBySideResultItem>;
  models?: Record<string, SideBySideResultItem>;
}

// ==========================================
// 模拟对弈与数据飞轮契约
// ==========================================

export interface TrajectoryDetailItem {
  turn: number;
  node_id: string;
  user_slots?: string[];
  evidence?: string;
}

export interface DebugRequestEntry {
  step: string;
  label: string;
  model?: string;
  messages: Array<{ role: string; content: string }>;
  config?: Record<string, any>;
  thinking?: string;
  response_content?: string;
  raw_output?: string;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    thinking_tokens?: number;
    answer_tokens?: number;
    total_tokens?: number;
  } | null;
  latency_ms?: number | null;
  error?: string;
  downgrade_notice?: string;
}

export interface InspectionReport {
  score: number;
  verdict: 'PASS' | 'FAIL';
  detected_defects: string[];
  deductions?: Array<{ item?: string; points?: number; reason?: string }>;
  node_trajectory: string[];
  trajectory_details?: TrajectoryDetailItem[];
  topology_valid: boolean;
  topology_error?: string;
  flow_checkpoints: Record<string, any>;
  expert_comments: string;
  notices?: string[];
  request_messages?: Array<{ role: string; content: string }>;
  debug_requests?: DebugRequestEntry[];
}

export interface SimulationRunRequest {
  rule_id: string;
  target_model_id: string;
  client_model_id: string;
  judge_model_id: string;
  remediate_model_id?: string;
  scenario_ids?: string[];
  max_turns?: number;
  auto_remediate?: boolean;
}

export interface SimulationBattleResult {
  sample_id: string;
  scenario_id: string;
  scenario_name: string;
  status: 'PENDING' | 'GOLD_PASS' | 'REPAIRED_PASS' | 'DISCARDED';
  score?: number;
  total_turns: number;
  initial_eval: InspectionReport;
  verified_eval?: InspectionReport | null;
  remediation_debug?: DebugRequestEntry | null;
  dialogue: ChatMessage[];
  remediated?: ChatMessage[] | null;
}

export interface SimulationResponse {
  message: string;
  task_id: string;
  results: SimulationBattleResult[];
}

export type FlywheelStatus = 'PENDING' | 'GOLD_PASS' | 'REPAIRED_PASS' | 'DISCARDED' | 'MANUAL_APPROVED';

export interface FlywheelSampleRecord {
  id: string;
  task_id?: string;
  rule_id: string;
  scenario_id: string;
  status: FlywheelStatus;
  score: number;
  raw_messages: ChatMessage[];
  remediated_messages?: ChatMessage[] | null;
  benchmark_slices?: FlywheelBenchmarkSlice[];
  applied_actions?: Array<Record<string, any>>;
  initial_eval: InspectionReport;
  verified_eval?: InspectionReport | null;
  remediation_debug?: DebugRequestEntry | null;
  reviewer_comment?: string;
  exported_to_benchmark?: boolean;
  updated_at?: number;
}

export interface FlywheelBenchmarkSlice {
  slice_id: string;
  sample_id?: string;
  target_turn: number;
  defect_item: string;
  defect_reason: string;
  messages: ChatMessage[];
  reference: string;
  rubric: string;
  regex_constraints: string[];
  category?: string;
  sub_category?: string;
}

export interface UpdateBenchmarkSlicesRequest {
  slices: FlywheelBenchmarkSlice[];
  sync_to_dialogue?: boolean;
}

export interface ExportBenchmarkDatasetRequest {
  dataset_id: string;
  name: string;
  slice_ids?: string[];
  mode?: 'create' | 'append';
}

export interface BatchExportBenchmarkRequest {
  sample_ids: string[];
  dataset_id: string;
  name: string;
  mode?: 'create' | 'append';
  strategy?: 'all_defects' | 'primary_defect_only';
}

export interface SampleReviewUpdateRequest {
  status?: FlywheelStatus;
  remediated_messages?: ChatMessage[];
  reviewer_comment?: string;
}

export interface BatchFlywheelRunRequest {
  rule_id: string;
  input_file_path: string;
  gold_output_path?: string;
  discard_output_path?: string;
  judge_model_id: string;
  remediate_model_id?: string;
  workers?: number;
}

export interface ExportGoldDatasetRequest {
  rule_id: string;
  version_tag: string;
  include_manual_approved: boolean;
  inject_thinking: boolean;
  exclude_benchmark_exported?: boolean;
}

export interface BatchUpdateStatusRequest {
  sample_ids: string[];
  status: FlywheelStatus;
  reviewer_comment?: string;
}

export interface BatchDeleteSamplesRequest {
  sample_ids: string[];
}

export interface BatchRemediateRequest {
  sample_ids: string[];
  rule_id?: string;
  judge_model_id?: string;
  remediate_model_id?: string;
  run_initial_judge?: boolean;
  skip_if_gold_pass?: boolean;
  run_verified_judge?: boolean;
}

export interface BatchJudgeRequest {
  sample_ids: string[];
  rule_id: string;
  judge_model_id: string;
}

export interface BatchExportSelectedRequest {
  sample_ids: string[];
  version_tag: string;
  inject_thinking: boolean;
}

export interface DatasetInspectRequest {
  dataset_id?: string;
  dataset_path?: string;
  rule_id: string;
  judge_model_id: string;
  workers?: number;
  limit?: number;
  save_to_flywheel?: boolean;
}

export interface DatasetInspectDeductionAgg {
  item: string;
  count: number;
  total_points: number;
}

export interface DatasetInspectSummary {
  total_samples: number;
  completed_samples: number;
  pass_count: number;
  fail_count: number;
  error_count: number;
  valid_count: number;
  pass_rate: number;
  avg_score: number;
  min_score?: number | null;
  max_score?: number | null;
  topology_pass_rate: number;
  top_deductions: DatasetInspectDeductionAgg[];
}

export interface DatasetInspectSampleResult {
  sample_id: string;
  scenario_id: string;
  turn_count: number;
  score: number | null;
  verdict: 'PASS' | 'FAIL' | 'ERROR';
  topology_valid: boolean;
  topology_error?: string | null;
  node_trajectory: string[];
  deductions: Array<{ item?: string; points?: number; reason?: string; turn?: number }>;
  detected_defects: string[];
  expert_comments: string;
}

export interface DatasetInspectTaskReport {
  id: string;
  task_id: string;
  task_type: string;
  status: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED';
  progress: number;
  error_message?: string;
  created_at?: number;
  finished_at?: number;
  dataset_id?: string | null;
  dataset_path?: string | null;
  rule_id?: string | null;
  rule_name?: string | null;
  judge_model_id?: string | null;
  pass_score_threshold?: number | null;
  save_to_flywheel?: boolean;
  saved_sample_count?: number;
  progress_detail?: {
    completed: number;
    total: number;
    current_sample_id?: string | null;
  };
  report_file?: string;
  summary?: DatasetInspectSummary | null;
  samples: DatasetInspectSampleResult[];
}

// ==========================================
// 规则 Prompt 调试与实测契约
// ==========================================

export type RulePromptDebugType =
  | 'trajectory_extract'
  | 'judge'
  | 'judge_plain'
  | 'faq_extract'
  | 'faq_judge'
  | 'remediate'
  | 'client_simulator';

export interface DefaultTemplatesResponse {
  trajectory_extract_template?: string;
  judge_template: string;
  judge_plain_template?: string;
  faq_extract_template?: string;
  faq_judge_template?: string;
  remediation_template: string;
  client_simulator_template?: string;
}

export interface DebugPromptRequest {
  rule_id: string;
  prompt_type: RulePromptDebugType;
  template?: string;
  scenario_id?: string;
  dialogue?: ChatMessage[];
  defects?: string[];
  rule_override?: UniversalRuleSchema;
}

export interface DebugPromptResponse {
  rule_id: string;
  prompt_type: RulePromptDebugType;
  scenario_id: string;
  assembled_prompt: string;
  assembled_messages: ChatMessage[];
  sample_dialogue: ChatMessage[];
  recommended_config: {
    temperature: number;
    max_tokens: number;
    thinking_enabled: boolean;
    thinking_level: string;
  };
}

export interface TestPromptRequest {
  model_id: string;
  prompt_type: RulePromptDebugType;
  assembled_prompt: string;
  assembled_messages?: ChatMessage[];
  sample_dialogue?: ChatMessage[];
}

export interface TestPromptResponse {
  status: 'success' | 'error';
  latency_ms: number;
  raw_content: string;
  thinking?: string;
  error?: string;
  parsed_data?: any;
  effective_config?: Record<string, any>;
  simulation_preview?: {
    execution_report: {
      execution_log: string[];
      errors: string[];
      pruned_turns: number[];
      patched_turns: number[];
    };
    is_format_valid: boolean;
    format_message: string;
    remediated_turns: ChatMessage[];
  };
}

// ==========================================
// 常规评测裁判 Prompt 调试契约
// ==========================================

export interface DebugBenchmarkJudgeRequest {
  dataset_path?: string;
  sample_id?: string;
  sample?: BenchmarkSample;
  model_answer?: string;
  template_override?: string;
}

export interface DebugBenchmarkJudgeResponse {
  sample_id: string;
  sample: BenchmarkSample;
  model_answer: string;
  assembled_prompt: string;
  assembled_messages: ChatMessage[];
}

export interface TestBenchmarkJudgeRequest {
  model_id: string;
  assembled_prompt: string;
  assembled_messages?: ChatMessage[];
}

// ==========================================
// 全平台提示词管理中心契约 (全局模板资产治理)
// ==========================================

export interface PromptPlaceholder {
  name: string;
  desc: string;
  required?: boolean;
}

export interface PromptModuleItem {
  prompt_key: string;
  name: string;
  category: string;
  description: string;
  engine_location: string;
  supports_rule_context: boolean;
  template_content: string;
  default_content: string;
  placeholders: PromptPlaceholder[];
  sample_vars?: Record<string, any>;
  is_customized: boolean;
  missing_placeholders?: string[];
  created_at?: number;
  updated_at?: number;
}

export interface PromptStaticPreviewRequest {
  template_override?: string;
  custom_vars?: Record<string, any>;
}

export interface PromptStaticPreviewResponse {
  prompt_key: string;
  assembled_prompt: string;
  assembled_messages: ChatMessage[];
  missing_placeholders: string[];
  unresolved_placeholders: string[];
  sample_vars: Record<string, any>;
}
