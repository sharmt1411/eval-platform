import axios from 'axios';
import type {
  RegisteredModel,
  ModelCreateRequest,
  ModelUpdateRequest,
  ModelTestResponse,
  RuleSummary,
  UniversalRuleSchema,
  DatasetRecord,
  DatasetImportRequest,
  BenchmarkSample,
  BenchmarkRunRequest,
  BenchmarkTaskItem,
  SamplingFilterConfig,
  SamplingPreviewResponse,
  SideBySideSample,
  SimulationRunRequest,
  SimulationResponse,
  FlywheelSampleRecord,
  SampleReviewUpdateRequest,
  BatchFlywheelRunRequest,
  ExportGoldDatasetRequest,
  BatchUpdateStatusRequest,
  BatchDeleteSamplesRequest,
  BatchRemediateRequest,
  BatchJudgeRequest,
  BatchExportSelectedRequest,
  FlywheelBenchmarkSlice,
  UpdateBenchmarkSlicesRequest,
  ExportBenchmarkDatasetRequest,
  BatchExportBenchmarkRequest,
  DefaultTemplatesResponse,
  DebugPromptRequest,
  DebugPromptResponse,
  TestPromptRequest,
  TestPromptResponse,
  DebugBenchmarkJudgeRequest,
  DebugBenchmarkJudgeResponse,
  TestBenchmarkJudgeRequest,
  PromptModuleItem,
  PromptStaticPreviewRequest,
  PromptStaticPreviewResponse,
  DatasetInspectRequest,
  DatasetInspectTaskReport,
} from '../types';

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 300000, // 5 分钟超时，满足大模型评测与对弈
  headers: {
    'Content-Type': 'application/json',
  },
});

export const modelsApi = {
  list: async () => {
    const res = await api.get<RegisteredModel[]>('/models/');
    return res.data;
  },
  create: async (data: ModelCreateRequest) => {
    const res = await api.post<{
      message: string;
      id: string;
      vertex_thinking_mode?: string | null;
      probe_info?: Record<string, any> | null;
    }>('/models/', data);
    return res.data;
  },
  update: async (modelId: string, data: ModelUpdateRequest) => {
    const res = await api.put<{
      message: string;
      id: string;
      vertex_thinking_mode?: string | null;
      probe_info?: Record<string, any> | null;
    }>(`/models/${modelId}`, data);
    return res.data;
  },
  delete: async (modelId: string) => {
    const res = await api.delete<{ message: string }>(`/models/${modelId}`);
    return res.data;
  },
  test: async (modelId: string) => {
    const res = await api.post<ModelTestResponse>(`/models/${modelId}/test`);
    return res.data;
  },
  chat: async (modelId: string, data: { messages: any[]; system_prompt?: string; config?: any }) => {
    const res = await api.post<{
      status: string;
      reply: string;
      thinking: string;
      latency_ms: number;
      usage?: any;
    }>(`/models/${modelId}/chat`, data);
    return res.data;
  },
};

export const rulesApi = {
  list: async () => {
    const res = await api.get<RuleSummary[]>('/rules/');
    return res.data;
  },
  get: async (ruleId: string) => {
    const res = await api.get<UniversalRuleSchema>(`/rules/${ruleId}`);
    return res.data;
  },
  save: async (data: UniversalRuleSchema) => {
    const res = await api.post<{ message: string; rule_id: string }>('/rules/', data);
    return res.data;
  },
  getDefaultTemplates: async () => {
    const res = await api.get<DefaultTemplatesResponse>('/rules/templates/defaults');
    return res.data;
  },
  debugPrompt: async (data: DebugPromptRequest) => {
    const res = await api.post<DebugPromptResponse>('/rules/debug-prompt', data);
    return res.data;
  },
  testPrompt: async (data: TestPromptRequest) => {
    const res = await api.post<TestPromptResponse>('/rules/test-prompt', data);
    return res.data;
  },
};

export const datasetsApi = {
  list: async () => {
    const res = await api.get<DatasetRecord[]>('/datasets/');
    return res.data;
  },
  preview: async (datasetId: string, limit = 20) => {
    const res = await api.get<{ dataset_id: string; samples: BenchmarkSample[] }>(
      `/datasets/${datasetId}/samples`,
      { params: { limit } }
    );
    return res.data;
  },
  importDataset: async (data: DatasetImportRequest) => {
    const res = await api.post<{
      message: string;
      imported_count: number;
      dataset: DatasetRecord;
    }>('/datasets/import', data);
    return res.data;
  },
  deleteDataset: async (datasetId: string) => {
    const res = await api.delete<{ message: string }>(`/datasets/${datasetId}`);
    return res.data;
  },
  getFilterOptions: async (datasetId: string) => {
    const res = await api.get<import('../types').BenchmarkDatasetFilterOptionsResponse>(
      `/datasets/${datasetId}/filter-options`
    );
    return res.data;
  },
  previewExport: async (
    datasetId: string,
    data: import('../types').BenchmarkExportFilterRequest
  ) => {
    const res = await api.post<import('../types').BenchmarkExportPreviewResponse>(
      `/datasets/${datasetId}/export/preview`,
      data
    );
    return res.data;
  },
  exportFiltered: async (
    datasetId: string,
    data: import('../types').BenchmarkExportFilterRequest
  ) => {
    const res = await api.post<import('../types').BenchmarkFilteredExportResponse>(
      `/datasets/${datasetId}/export`,
      data
    );
    return res.data;
  },
};

export const benchmarksApi = {
  listTasks: async (limit = 50) => {
    const res = await api.get<BenchmarkTaskItem[]>('/benchmarks/tasks', { params: { limit } });
    return res.data;
  },
  run: async (data: BenchmarkRunRequest) => {
    const res = await api.post<{ message: string; task_id: string }>('/benchmarks/run', data);
    return res.data;
  },
  getTask: async (taskId: string) => {
    const res = await api.get<{
      id: string;
      task_type: string;
      status: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED';
      progress: number;
      error_message?: string;
      created_at?: number;
      finished_at?: number;
      result: {
        summaries?: Record<string, any>;
        result_file?: string;
        max_tokens?: number;
        ignore_sample_max_tokens?: boolean;
        dataset_total_samples?: number;
        sampled_samples?: number;
        skipped_mastered_samples?: number;
        sampling_config?: SamplingFilterConfig;
        ledger_id?: string;
        ledger_revoked?: boolean;
        is_rerun_same_version?: boolean;
        progress_detail?: any;
      };
    }>(`/benchmarks/tasks/${taskId}`);
    return res.data;
  },
  getSideBySide: async (taskId: string) => {
    const res = await api.get<Record<string, SideBySideSample>>(`/benchmarks/tasks/${taskId}/side_by_side`);
    return res.data;
  },
  deleteTask: async (taskId: string) => {
    const res = await api.delete<{ message: string; task_id: string }>(`/benchmarks/tasks/${taskId}`);
    return res.data;
  },
  batchDeleteTasks: async (taskIds: string[]) => {
    const res = await api.post<{ message: string; deleted_count: number }>(
      '/benchmarks/tasks/batch-delete',
      { task_ids: taskIds }
    );
    return res.data;
  },
  previewSampling: async (data: { dataset_path: string; sampling_config: SamplingFilterConfig }) => {
    const res = await api.post<SamplingPreviewResponse>('/benchmarks/preview-sampling', data);
    return res.data;
  },
  resetTracker: async (dataset_path: string, model_line?: string) => {
    const res = await api.post<{ message: string; reset_count: number }>('/benchmarks/reset-tracker', {
      dataset_path,
      model_line: model_line || undefined,
    });
    return res.data;
  },
  revokeLedger: async (taskId: string) => {
    const res = await api.post<{ message: string; task_id: string; revoked_count: number }>(
      `/benchmarks/tasks/${taskId}/revoke-ledger`
    );
    return res.data;
  },
  debugJudgePrompt: async (data: DebugBenchmarkJudgeRequest) => {
    const res = await api.post<DebugBenchmarkJudgeResponse>('/benchmarks/debug-judge-prompt', data);
    return res.data;
  },
  testJudgePrompt: async (data: TestBenchmarkJudgeRequest) => {
    const res = await api.post<TestPromptResponse>('/benchmarks/test-judge-prompt', data);
    return res.data;
  },
};

export const simulationApi = {
  simulate: async (data: SimulationRunRequest) => {
    const res = await api.post<SimulationResponse>('/simulation/simulate', data);
    return res.data;
  },
  simulateStream: async (
    data: SimulationRunRequest,
    onEvent: (event: string, payload: any) => void,
    signal?: AbortSignal
  ) => {
    const response = await fetch('/api/v1/simulation/simulate/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(data),
      signal,
    });

    if (!response.ok) {
      const errText = await response.text();
      throw new Error(`对弈请求失败 (${response.status}): ${errText}`);
    }

    const reader = response.body?.getReader();
    if (!reader) {
      throw new Error('当前浏览器不支持流式读取');
    }

    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed.startsWith('data: ')) {
          try {
            const parsed = JSON.parse(trimmed.slice(6));
            if (parsed.event) {
              onEvent(parsed.event, parsed.data);
            }
          } catch (e) {
            console.warn('解析 SSE 消息异常:', trimmed, e);
          }
        }
      }
    }
  },
  listRecentBattles: async (params?: { rule_id?: string; status?: string; limit?: number }) => {
    const res = await api.get<any[]>('/simulation/battles/recent', { params });
    return res.data;
  },
  clearBattles: async (ruleId?: string) => {
    const res = await api.delete<{ message: string; deleted_count: number }>(
      '/simulation/battles/all',
      { params: { rule_id: ruleId } }
    );
    return res.data;
  },
  evaluateDialogue: async (data: {
    rule_id: string;
    scenario_id?: string;
    dialogue: any[];
    judge_model_id: string;
    save_as_sample?: boolean;
  }) => {
    const res = await api.post<{
      status: string;
      sample_id?: string;
      sample_status?: string;
      eval_report: any;
      total_turns: number;
    }>('/simulation/evaluate-dialogue', data);
    return res.data;
  },
};

export const flywheelApi = {
  batchDeleteSamples: async (sampleIds: string[]) => {
    const res = await api.post<{ message: string; deleted_count: number }>(
      '/flywheel/samples/batch-delete',
      { sample_ids: sampleIds }
    );
    return res.data;
  },
  deleteSample: async (sampleId: string) => {
    const res = await api.delete<{ message: string; sample_id: string }>(
      `/flywheel/samples/${sampleId}`
    );
    return res.data;
  },
  batch: async (data: BatchFlywheelRunRequest) => {
    const res = await api.post<{ message: string; task_id: string }>('/flywheel/batch', data);
    return res.data;
  },
  listSamples: async (params?: { task_id?: string; status?: string; rule_id?: string; limit?: number }) => {
    const res = await api.get<FlywheelSampleRecord[]>('/flywheel/samples', { params });
    return res.data;
  },
  updateSample: async (sampleId: string, data: SampleReviewUpdateRequest) => {
    const res = await api.put<{ message: string; id: string; status: string }>(
      `/flywheel/samples/${sampleId}`,
      data
    );
    return res.data;
  },
  remediateSample: async (
    sampleId: string,
    params?: {
      rule_id?: string;
      judge_model_id?: string;
      remediate_model_id?: string;
      run_initial_judge?: boolean;
      skip_if_gold_pass?: boolean;
      run_verified_judge?: boolean;
    },
    signal?: AbortSignal
  ) => {
    const res = await api.post<{
      message: string;
      sample_id: string;
      rule_id?: string;
      status: string;
      score: number;
      remediated_messages?: any[];
      initial_eval?: any;
      verified_eval: any;
      remediation_debug?: any;
      benchmark_slices?: any[];
      notices?: string[];
      skipped_remediation?: boolean;
      skip_reason?: string;
    }>(`/flywheel/samples/${sampleId}/remediate`, params, { signal });
    return res.data;
  },
  judgeSample: async (
    sampleId: string,
    data: { rule_id: string; judge_model_id: string; target_data?: 'raw' | 'remediated' },
    signal?: AbortSignal
  ) => {
    const res = await api.post<{
      message: string;
      sample_id: string;
      rule_id?: string;
      status: string;
      score: number;
      target_data?: 'raw' | 'remediated';
      initial_eval?: any;
      verified_eval?: any;
      notices?: string[];
    }>(`/flywheel/samples/${sampleId}/judge`, data, { signal });
    return res.data;
  },
  exportGold: async (params: ExportGoldDatasetRequest) => {
    const res = await api.post<{ message: string; file_path: string; sample_count: number }>(
      '/flywheel/export',
      null,
      { params }
    );
    return res.data;
  },
  batchUpdateStatus: async (data: BatchUpdateStatusRequest) => {
    const res = await api.post<{ message: string; updated_count: number; status: string }>(
      '/flywheel/samples/batch-status',
      data
    );
    return res.data;
  },
  batchRemediateSamples: async (data: BatchRemediateRequest) => {
    const res = await api.post<{
      message: string;
      total: number;
      succeeded: number;
      results: any[];
      notices?: string[];
    }>('/flywheel/samples/batch-remediate', data);
    return res.data;
  },
  batchJudgeSamples: async (data: BatchJudgeRequest) => {
    const res = await api.post<{
      message: string;
      total: number;
      succeeded: number;
      results: any[];
      notices?: string[];
    }>('/flywheel/samples/batch-judge', data);
    return res.data;
  },
  exportSelectedSamples: async (data: BatchExportSelectedRequest) => {
    const res = await api.post<{
      message: string;
      file_path: string;
      sample_count: number;
    }>('/flywheel/samples/export-selected', data);
    return res.data;
  },
  getSlices: async (sampleId: string) => {
    const res = await api.get<{
      sample_id: string;
      slices: FlywheelBenchmarkSlice[];
      source: string;
    }>(`/flywheel/samples/${sampleId}/slices`);
    return res.data;
  },
  updateSlices: async (sampleId: string, data: UpdateBenchmarkSlicesRequest) => {
    const res = await api.put<{
      message: string;
      slice_count: number;
    }>(`/flywheel/samples/${sampleId}/slices`, data);
    return res.data;
  },
  exportBenchmark: async (sampleId: string, data: ExportBenchmarkDatasetRequest) => {
    const res = await api.post<{
      message: string;
      dataset_id: string;
      file_path: string;
      sample_count: number;
    }>(`/flywheel/samples/${sampleId}/export-benchmark`, data);
    return res.data;
  },
  batchExportBenchmark: async (data: BatchExportBenchmarkRequest) => {
    const res = await api.post<{
      message: string;
      dataset_id: string;
      file_path: string;
      sample_count: number;
    }>('/flywheel/batch-export-benchmark', data);
    return res.data;
  },
  inspectDataset: async (data: DatasetInspectRequest) => {
    const res = await api.post<{
      message: string;
      task_id: string;
      dataset_id?: string | null;
      total_samples: number;
    }>('/flywheel/inspect-dataset', data);
    return res.data;
  },
  getInspectTask: async (taskId: string) => {
    const res = await api.get<DatasetInspectTaskReport>(`/flywheel/inspect-tasks/${taskId}`);
    return res.data;
  },
};

export const promptsApi = {
  list: async () => {
    const res = await api.get<PromptModuleItem[]>('/prompts/');
    return res.data;
  },
  get: async (promptKey: string) => {
    const res = await api.get<PromptModuleItem>(`/prompts/${promptKey}`);
    return res.data;
  },
  update: async (promptKey: string, templateContent: string) => {
    const res = await api.put<{ message: string; item: PromptModuleItem }>(`/prompts/${promptKey}`, {
      template_content: templateContent,
    });
    return res.data;
  },
  reset: async (promptKey: string) => {
    const res = await api.post<{ message: string; item: PromptModuleItem }>(`/prompts/${promptKey}/reset`);
    return res.data;
  },
  preview: async (promptKey: string, data: PromptStaticPreviewRequest) => {
    const res = await api.post<PromptStaticPreviewResponse>(`/prompts/${promptKey}/preview`, data);
    return res.data;
  },
};

export default api;
