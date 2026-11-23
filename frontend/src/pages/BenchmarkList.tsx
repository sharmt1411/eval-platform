import React, { useEffect, useState, useMemo } from 'react';
import { useNavigate, useLocation, useSearchParams } from 'react-router-dom';
import {
  Table,
  Card,
  Button,
  Tag,
  Space,
  Input,
  Select,
  Drawer,
  Form,
  InputNumber,
  Switch,
  Checkbox,
  Popconfirm,
  Tooltip,
  Badge,
  Progress,
  Typography,
  message,
  Divider,
  Row,
  Col,
  Statistic,
  Modal,
  Alert,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  ExperimentOutlined,
  EyeOutlined,
  PlusOutlined,
  ReloadOutlined,
  DeleteOutlined,
  CopyOutlined,
  SearchOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  SyncOutlined,
  SettingOutlined,
  ApartmentOutlined,
  ClockCircleOutlined,
  DatabaseOutlined,
  ThunderboltOutlined,
  ExportOutlined,
  FilterOutlined,
  InfoCircleOutlined,
  PlayCircleOutlined,
  DownloadOutlined,
  CodeOutlined,
} from '@ant-design/icons';
import { benchmarksApi, modelsApi, datasetsApi } from '../api';
import {
  copyTextSafely,
  exportPromptDebugJson,
  exportPromptDebugMarkdown,
} from '../utils/promptDebugExport';
import type {
  BenchmarkTaskItem,
  RegisteredModel,
  DatasetRecord,
  ModelEvalConfig,
  BenchmarkRunRequest,
  SamplingFilterConfig,
  SamplingPreviewResponse,
  DebugBenchmarkJudgeResponse,
  TestPromptResponse,
} from '../types';

const { Text, Paragraph } = Typography;

export const BenchmarkList: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();

  const [tasks, setTasks] = useState<BenchmarkTaskItem[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [searchKeyword, setSearchKeyword] = useState<string>('');
  const [statusFilter, setStatusFilter] = useState<string>('ALL');

  // 批量管理状态
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [batchDeleting, setBatchDeleting] = useState<boolean>(false);

  // 受控分页状态：解决每页条数切换无响应问题
  const [pagination, setPagination] = useState<{ current: number; pageSize: number }>({
    current: 1,
    pageSize: 10,
  });

  // 新建/发起测试抽屉状态
  const [drawerVisible, setDrawerVisible] = useState<boolean>(false);
  const [models, setModels] = useState<RegisteredModel[]>([]);
  const [datasets, setDatasets] = useState<DatasetRecord[]>([]);
  const [selectedModelIds, setSelectedModelIds] = useState<string[]>([]);
  const [overrideConfigs, setOverrideConfigs] = useState<Record<string, ModelEvalConfig>>({});
  const [submitting, setSubmitting] = useState<boolean>(false);

  // 单模型临时调参弹窗
  const [overrideModalVisible, setOverrideModalVisible] = useState<boolean>(false);
  const [overridingModelId, setOverridingModelId] = useState<string>('');

  const [runForm] = Form.useForm();
  const [overrideForm] = Form.useForm();
  const watchedMaxTokens = Form.useWatch('max_tokens', runForm) || 4096;

  // 抽样与阶梯降频预览状态
  const [samplingPreview, setSamplingPreview] = useState<SamplingPreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState<boolean>(false);
  const watchedSamplingEnabled = Form.useWatch('sampling_enabled', runForm) || false;
  const watchedHoldoutFilter = Form.useWatch('holdout_filter', runForm) || 'all';
  const watchedDatasetPath = Form.useWatch('dataset_path', runForm);
  const watchedThresholdX = Form.useWatch('pass_threshold_x', runForm);
  const watchedIntervalStepS = Form.useWatch('interval_step_s', runForm);
  const watchedMaxIntervalN = Form.useWatch('max_interval_n', runForm);
  const watchedModelLine = Form.useWatch('model_line', runForm);
  const watchedVersionTag = Form.useWatch('version_tag', runForm);
  const watchedBenchmarkModelId = Form.useWatch('benchmark_model_id', runForm);
  const watchedAllowUnlabeled = Form.useWatch('allow_unlabeled', runForm) || false;

  const selectedDatasetRecord = useMemo(
    () => datasets.find((d) => d.file_path === watchedDatasetPath),
    [datasets, watchedDatasetPath]
  );
  const isSelectedUnlabeledTrainingDataset = Boolean(
    selectedDatasetRecord && selectedDatasetRecord.dataset_type !== 'benchmark'
  );

  // 常规评测裁判 Prompt 调试弹窗状态
  const [judgeDebugVisible, setJudgeDebugVisible] = useState<boolean>(false);
  const [judgeDebugLoading, setJudgeDebugLoading] = useState<boolean>(false);
  const [judgeDebugDatasetPath, setJudgeDebugDatasetPath] = useState<string>('');
  const [judgeDebugAnswer, setJudgeDebugAnswer] = useState<string>('');
  const [judgeDebugData, setJudgeDebugData] = useState<DebugBenchmarkJudgeResponse | null>(null);
  const [judgeTestModelId, setJudgeTestModelId] = useState<string>('');
  const [judgeTestRunning, setJudgeTestRunning] = useState<boolean>(false);
  const [judgeTestResult, setJudgeTestResult] = useState<TestPromptResponse | null>(null);

  const benchmarkDatasets = useMemo(
    () => datasets.filter((d) => d.dataset_type === 'benchmark'),
    [datasets]
  );

  const judgeDebugDatasetOptions = useMemo(() => {
    const opts = benchmarkDatasets.map((d) => ({
      value: d.file_path,
      label: `${d.name} (${d.sample_count}条 · ${d.dataset_type})`,
    }));
    const normPaths = benchmarkDatasets.map((d) => d.file_path.replace(/\\/g, '/'));
    const presets = [
      {
        value: 'data/datasets/demo_benchmark.jsonl',
        label: '默认演示评测集 (data/datasets/demo_benchmark.jsonl)',
      },
      {
        value: 'data/datasets/general_eval_simple_test.jsonl',
        label: '30条通用能力简版基准测试集 (data/datasets/general_eval_simple_test.jsonl)',
      },
    ];
    for (const p of presets) {
      const alreadyRegistered = normPaths.some((np) => np === p.value || np.endsWith(`/${p.value}`));
      if (!alreadyRegistered || judgeDebugDatasetPath === p.value) {
        opts.push(p);
      }
    }
    return opts;
  }, [benchmarkDatasets, judgeDebugDatasetPath]);

  const handleAssembleJudgePrompt = async (customAnswer?: string, overrideDatasetPath?: string) => {
    const dsPath =
      overrideDatasetPath !== undefined
        ? overrideDatasetPath
        : judgeDebugDatasetPath || runForm.getFieldValue('dataset_path') || '';
    const answerText = customAnswer !== undefined ? customAnswer : judgeDebugAnswer;
    setJudgeDebugLoading(true);
    try {
      const res = await benchmarksApi.debugJudgePrompt({
        dataset_path: dsPath || undefined,
        model_answer: answerText.trim() ? answerText : undefined,
      });
      setJudgeDebugData(res);
      if (!answerText.trim()) {
        setJudgeDebugAnswer(res.model_answer || '');
      }
    } catch (e: any) {
      message.error(`组装评测裁判 Prompt 失败: ${e.message}`);
    } finally {
      setJudgeDebugLoading(false);
    }
  };

  const handleOpenJudgePromptDebug = async (preferredDatasetPath?: string) => {
    const selectedJudge = runForm.getFieldValue('judge_model_id');
    const dsPath =
      preferredDatasetPath !== undefined
        ? preferredDatasetPath
        : runForm.getFieldValue('dataset_path') ||
          benchmarkDatasets[0]?.file_path ||
          'data/datasets/demo_benchmark.jsonl';
    setJudgeDebugDatasetPath(dsPath);
    setJudgeTestModelId(selectedJudge || models[0]?.id || '');
    setJudgeTestResult(null);
    setJudgeDebugAnswer('');
    setJudgeDebugVisible(true);
    await handleAssembleJudgePrompt('', dsPath);
  };

  const handleTestJudgePrompt = async () => {
    if (!judgeDebugData?.assembled_prompt) {
      message.warning('请先组装评测裁判 Prompt');
      return;
    }
    if (!judgeTestModelId) {
      message.warning('请选择测试裁判模型');
      return;
    }
    setJudgeTestRunning(true);
    setJudgeTestResult(null);
    try {
      const res = await benchmarksApi.testJudgePrompt({
        model_id: judgeTestModelId,
        assembled_prompt: judgeDebugData.assembled_prompt,
        assembled_messages: judgeDebugData.assembled_messages,
      });
      setJudgeTestResult(res);
      if (res.status === 'success') {
        message.success(`裁判实测完成 (${res.latency_ms} ms)`);
      } else {
        message.error(`裁判调用异常: ${res.error || '未知错误'}`);
      }
    } catch (e: any) {
      message.error(`裁判实测请求失败: ${e.message}`);
    } finally {
      setJudgeTestRunning(false);
    }
  };

  const refreshSamplingPreview = async () => {
    const dsPath = runForm.getFieldValue('dataset_path');
    const enabled = runForm.getFieldValue('sampling_enabled');
    const hf = runForm.getFieldValue('holdout_filter') || 'all';
    if (!dsPath || (!enabled && hf === 'all')) {
      setSamplingPreview(null);
      return;
    }
    setPreviewLoading(true);
    try {
      const bmId = runForm.getFieldValue('benchmark_model_id') || null;
      const cfg: SamplingFilterConfig = {
        enabled: !!enabled,
        pass_threshold_x: runForm.getFieldValue('pass_threshold_x') ?? 3,
        interval_step_s: runForm.getFieldValue('interval_step_s') ?? 1,
        max_interval_n: runForm.getFieldValue('max_interval_n') ?? 5,
        min_pass_score: runForm.getFieldValue('min_pass_score') || null,
        holdout_filter: hf,
        model_line: runForm.getFieldValue('model_line') || null,
        version_tag: runForm.getFieldValue('version_tag') || null,
        benchmark_model_id: bmId,
        tracker_model_keys: bmId ? [bmId] : [],
      };
      const res = await benchmarksApi.previewSampling({
        dataset_path: dsPath,
        sampling_config: cfg,
      });
      setSamplingPreview(res);
    } catch (e) {
      console.error('抽样试算失败', e);
    } finally {
      setPreviewLoading(false);
    }
  };

  useEffect(() => {
    if (drawerVisible && (watchedSamplingEnabled || watchedHoldoutFilter !== 'all') && watchedDatasetPath) {
      const timer = setTimeout(() => {
        refreshSamplingPreview();
      }, 300);
      return () => clearTimeout(timer);
    } else if (!watchedSamplingEnabled && watchedHoldoutFilter === 'all') {
      setSamplingPreview(null);
    }
  }, [
    drawerVisible,
    watchedSamplingEnabled,
    watchedHoldoutFilter,
    watchedDatasetPath,
    watchedThresholdX,
    watchedIntervalStepS,
    watchedMaxIntervalN,
    watchedModelLine,
    watchedVersionTag,
    watchedBenchmarkModelId,
  ]);

  const handleResetDatasetTracker = async () => {
    const dsPath = runForm.getFieldValue('dataset_path');
    if (!dsPath) return;
    const mLine = runForm.getFieldValue('model_line') || undefined;
    try {
      const res = await benchmarksApi.resetTracker(dsPath, mLine);
      message.success(res.message);
      refreshSamplingPreview();
    } catch (e: any) {
      message.error(`重置战绩失败: ${e.message}`);
    }
  };

  // 加载评测任务列表
  const loadTasks = async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const data = await benchmarksApi.listTasks(100);
      setTasks(data);
    } catch (e: any) {
      if (!silent) message.error(`加载评测任务列表失败: ${e.message}`);
    } finally {
      if (!silent) setLoading(false);
    }
  };

  // 加载模型与数据集依赖
  const loadDependencies = async () => {
    try {
      const [mList, dList] = await Promise.all([modelsApi.list(), datasetsApi.list()]);
      setModels(mList);
      setDatasets(dList);
    } catch (e: any) {
      console.error('加载模型或数据集列表失败', e);
    }
  };

  useEffect(() => {
    loadTasks();
    loadDependencies();
  }, []);

  // 轮询机制：若有处于 RUNNING 或 PENDING 状态的任务，自动定时刷新
  useEffect(() => {
    const hasActiveTask = tasks.some((t) => t.status === 'RUNNING' || t.status === 'PENDING');
    if (hasActiveTask) {
      const timer = setInterval(() => {
        loadTasks(true);
      }, 2500);
      return () => clearInterval(timer);
    }
  }, [tasks]);

  // 从其他页面（例如数据资产中心一键流转启动评测）跳转进入时自动打开抽屉
  useEffect(() => {
    if (location.state?.datasetPath) {
      runForm.resetFields();
      setSelectedModelIds([]);
      setOverrideConfigs({});
      runForm.setFieldsValue({
        dataset_path: location.state.datasetPath,
        workers: 4,
        max_tokens: 4096,
        ignore_sample_max_tokens: false,
        sampling_enabled: false,
        holdout_filter: 'all',
        allow_unlabeled: false,
        pass_threshold_x: 3,
        interval_step_s: 1,
        max_interval_n: 5,
        min_pass_score: null,
        benchmark_model_id: null,
        model_line: null,
        version_tag: null,
      });
      setDrawerVisible(true);
    }
  }, [location.state]);

  // 从提示词管理中心直达（/benchmarks?debugJudge=1）时自动打开裁判 Prompt 组装与实测弹窗
  useEffect(() => {
    if (searchParams.get('debugJudge') === '1') {
      handleOpenJudgePromptDebug();
      setSearchParams({}, { replace: true });
    }
  }, [searchParams]);

  // 弹窗打开期间，待模型列表异步加载完成后自动兜底选中首个模型
  useEffect(() => {
    if (judgeDebugVisible && !judgeTestModelId && models.length > 0) {
      setJudgeTestModelId(models[0].id);
    }
  }, [judgeDebugVisible, judgeTestModelId, models]);

  // 打开新建抽屉
  const handleOpenCreateDrawer = () => {
    loadDependencies();
    runForm.resetFields();
    setSelectedModelIds([]);
    setOverrideConfigs({});
    runForm.setFieldsValue({
      dataset_path: 'data/datasets/demo_benchmark.jsonl',
      workers: 4,
      max_tokens: 4096,
      ignore_sample_max_tokens: false,
      sampling_enabled: false,
      allow_unlabeled: false,
      pass_threshold_x: 3,
      interval_step_s: 1,
      max_interval_n: 5,
      min_pass_score: null,
      benchmark_model_id: null,
      model_line: null,
      version_tag: null,
    });
    setDrawerVisible(true);
  };

  // 一键克隆任务配置重新评测
  const handleCloneTask = (task: BenchmarkTaskItem) => {
    loadDependencies();
    runForm.resetFields();
    const midSet: string[] = [];
    const overrides: Record<string, ModelEvalConfig> = {};

    if (task.model_configs && task.model_configs.length > 0) {
      task.model_configs.forEach((mc) => {
        midSet.push(mc.model_id);
        overrides[mc.model_id] = { ...mc, api_key: undefined };
      });
    } else if (task.summaries) {
      Object.keys(task.summaries).forEach((k) => {
        const matched = models.find((m) => m.name === k || m.id === k);
        if (matched) {
          midSet.push(matched.id);
        }
      });
    }

    setSelectedModelIds(Array.from(new Set(midSet)));
    setOverrideConfigs(overrides);

    const sCfg = task.sampling_config;
    runForm.setFieldsValue({
      dataset_path: task.dataset_path || 'data/datasets/demo_benchmark.jsonl',
      workers: 4,
      max_tokens: task.max_tokens || 4096,
      ignore_sample_max_tokens: !!task.ignore_sample_max_tokens,
      sampling_enabled: sCfg ? sCfg.enabled : false,
      holdout_filter: sCfg?.holdout_filter ?? 'all',
      allow_unlabeled: false,
      pass_threshold_x: sCfg?.pass_threshold_x ?? 3,
      interval_step_s: sCfg?.interval_step_s ?? 1,
      max_interval_n: sCfg?.max_interval_n ?? 5,
      min_pass_score: sCfg?.min_pass_score ?? null,
      benchmark_model_id: sCfg?.benchmark_model_id ?? sCfg?.tracker_model_keys?.[0] ?? null,
      model_line: sCfg?.model_line ?? null,
      version_tag: sCfg?.version_tag ?? null,
    });
    setDrawerVisible(true);
    message.info(`已克隆任务 [${task.id}] 的评测参数至发起面板`);
  };

  // 单个删除评测任务
  const handleDeleteTask = async (taskId: string) => {
    try {
      await benchmarksApi.deleteTask(taskId);
      message.success(`评测任务 [${taskId}] 已成功删除`);
      setSelectedRowKeys((prev) => prev.filter((k) => k !== taskId));
      loadTasks();
    } catch (e: any) {
      message.error(`删除评测任务失败: ${e.message}`);
    }
  };

  // 批量删除选定的评测任务
  const handleBatchDelete = async () => {
    if (selectedRowKeys.length === 0) return;
    setBatchDeleting(true);
    try {
      const res = await benchmarksApi.batchDeleteTasks(selectedRowKeys as string[]);
      message.success(`已成功批量删除 ${res.deleted_count} 个评测任务`);
      setSelectedRowKeys([]);
      await loadTasks();
    } catch (e: any) {
      message.error(`批量删除失败: ${e.message}`);
    } finally {
      setBatchDeleting(false);
    }
  };

  // 批量导出选中的任务汇总数据
  const handleBatchExport = () => {
    if (selectedRowKeys.length === 0) return;
    const selectedTasks = tasks.filter((t) => selectedRowKeys.includes(t.id));
    const dataStr =
      'data:text/json;charset=utf-8,' + encodeURIComponent(JSON.stringify(selectedTasks, null, 2));
    const downloadAnchor = document.createElement('a');
    downloadAnchor.setAttribute('href', dataStr);
    downloadAnchor.setAttribute('download', `benchmark_tasks_export_${Date.now()}.json`);
    document.body.appendChild(downloadAnchor);
    downloadAnchor.click();
    downloadAnchor.remove();
    message.success(`已成功导出 ${selectedTasks.length} 项评测汇总记录`);
  };

  // 打开单模型参数覆盖弹窗
  const openOverrideModal = (mid: string) => {
    setOverridingModelId(mid);
    const m = models.find((x) => x.id === mid);
    const hasOverride = !!overrideConfigs[mid];
    const existing = overrideConfigs[mid]?.config || m?.config;
    const globalMaxTokens = runForm.getFieldValue('max_tokens') || 4096;
    overrideForm.setFieldsValue({
      display_name: overrideConfigs[mid]?.display_name || `${m?.name || mid} (参数覆盖)`,
      temperature: existing?.temperature ?? 0.3,
      max_tokens: hasOverride ? (overrideConfigs[mid]?.config?.max_tokens ?? globalMaxTokens) : globalMaxTokens,
      thinking_enabled: existing?.thinking_enabled ?? false,
      thinking_level: existing?.thinking_level ?? 'NONE',
    });
    setOverrideModalVisible(true);
  };

  // 保存单模型覆盖配置
  const handleSaveOverride = async () => {
    try {
      const values = await overrideForm.validateFields();
      const m = models.find((x) => x.id === overridingModelId);
      const chosenMaxTokens = values.max_tokens ?? 4096;
      setOverrideConfigs({
        ...overrideConfigs,
        [overridingModelId]: {
          model_id: overridingModelId,
          display_name: values.display_name,
          provider_type: m?.provider_type || 'openai',
          api_url: m?.api_url,
          api_key: m?.api_key,
          model_name: m?.model_name || overridingModelId,
          config: {
            ...(m?.config || {
              temperature: 0.3,
              top_p: 0.9,
              repetition_penalty: 1.05,
            }),
            temperature: values.temperature,
            max_tokens: chosenMaxTokens,
            thinking_enabled: values.thinking_enabled,
            thinking_level: values.thinking_level,
          },
        },
      });
      setOverrideModalVisible(false);
      message.success(`已为模型 ${overridingModelId} 设定独立临时参数覆盖 (MaxTokens: ${chosenMaxTokens})`);
    } catch (e: any) {
      message.error(`保存覆盖配置失败: ${e.message}`);
    }
  };

  // 提交并启动异步评测
  const handleStartRun = async () => {
    try {
      const values = await runForm.validateFields();
      if (selectedModelIds.length === 0) {
        message.warning('请至少选择一个被测模型');
        return;
      }
      setSubmitting(true);

      const globalMaxTokens = values.max_tokens || 4096;

      const modelConfigsPayload: ModelEvalConfig[] = selectedModelIds.map((mid) => {
        if (overrideConfigs[mid]) {
          return overrideConfigs[mid];
        }
        const m = models.find((x) => x.id === mid);
        return {
          model_id: mid,
          display_name: m?.name || mid,
          provider_type: m?.provider_type || 'openai',
          api_url: m?.api_url,
          api_key: m?.api_key,
          model_name: m?.model_name || mid,
          config: {
            ...(m?.config || {
              temperature: 0.3,
              top_p: 0.9,
              repetition_penalty: 1.05,
              thinking_enabled: false,
              thinking_level: 'NONE',
            }),
            max_tokens: globalMaxTokens,
          },
        };
      });

      if (values.sampling_enabled && (values.benchmark_model_id || values.model_line?.trim() || values.version_tag?.trim())) {
        if (!values.benchmark_model_id) {
          message.warning('更新基准模型线战绩时，请在「参评基准模型 (单选)」中选择对应的参评模型');
          return;
        }
        if (!values.version_tag?.trim()) {
          message.warning('指定了战绩更新基准模型线时，请填写本次评测版本号 (例如 v2.1)');
          return;
        }
      }

      const bmId = values.benchmark_model_id || null;
      const bmModel = bmId ? models.find((x) => x.id === bmId) : undefined;
      const resolvedModelLine = values.model_line?.trim() || (bmModel ? bmModel.model_name || bmModel.id : bmId);

      const hfValue = values.holdout_filter || 'all';
      const payload: BenchmarkRunRequest = {
        dataset_path: values.dataset_path,
        model_configs: modelConfigsPayload,
        judge_model_id: values.judge_model_id || undefined,
        workers: values.workers || 4,
        max_tokens: globalMaxTokens,
        ignore_sample_max_tokens: !!values.ignore_sample_max_tokens,
        allow_unlabeled: !!values.allow_unlabeled,
        sampling_config:
          values.sampling_enabled || hfValue !== 'all'
            ? {
                enabled: !!values.sampling_enabled,
                pass_threshold_x: values.pass_threshold_x ?? 3,
                interval_step_s: values.interval_step_s ?? 1,
                max_interval_n: values.max_interval_n ?? 5,
                min_pass_score: values.min_pass_score ? Number(values.min_pass_score) : null,
                holdout_filter: hfValue,
                model_line: values.sampling_enabled ? resolvedModelLine : null,
                version_tag: values.sampling_enabled ? values.version_tag?.trim() || null : null,
                benchmark_model_id: values.sampling_enabled ? bmId : null,
                tracker_model_keys: values.sampling_enabled && bmId ? [bmId] : [],
              }
            : undefined,
      };

      const res = await benchmarksApi.run(payload);
      message.success(`评测任务已提交 (ID: ${res.task_id})，正在转入详情分析页...`);
      setDrawerVisible(false);
      await loadTasks();
      navigate(`/benchmarks/${res.task_id}`);
    } catch (e: any) {
      message.error(`提交失败: ${e.response?.data?.detail || e.message}`);
    } finally {
      setSubmitting(false);
    }
  };

  // 概览统计指标
  const statistics = useMemo(() => {
    const total = tasks.length;
    const running = tasks.filter((t) => t.status === 'RUNNING' || t.status === 'PENDING').length;
    const completed = tasks.filter((t) => t.status === 'COMPLETED').length;
    const failed = tasks.filter((t) => t.status === 'FAILED').length;
    return { total, running, completed, failed };
  }, [tasks]);

  // 搜索与状态筛选后的任务列表
  const filteredTasks = useMemo(() => {
    return tasks.filter((t) => {
      if (statusFilter !== 'ALL' && t.status !== statusFilter) return false;
      if (searchKeyword.trim()) {
        const kw = searchKeyword.trim().toLowerCase();
        const idMatch = t.id.toLowerCase().includes(kw);
        const dsMatch = t.dataset_path?.toLowerCase().includes(kw);
        const modelMatch = Object.keys(t.summaries || {}).some((m) => m.toLowerCase().includes(kw));
        if (!idMatch && !dsMatch && !modelMatch) return false;
      }
      return true;
    });
  }, [tasks, statusFilter, searchKeyword]);

  // 表格列定义
  const columns: ColumnsType<BenchmarkTaskItem> = [
    {
      title: '任务 ID / 创建时间',
      dataIndex: 'id',
      key: 'id',
      width: 170,
      render: (id: string, record) => (
        <div className="space-y-1">
          <div className="flex items-center gap-1.5">
            <span
              onClick={() => navigate(`/benchmarks/${id}`)}
              className="font-mono text-xs font-bold text-blue-600 hover:text-blue-800 cursor-pointer underline decoration-blue-300"
            >
              {id}
            </span>
          </div>
          <div className="text-3xs text-slate-400 font-mono">
            {record.created_at ? new Date(record.created_at * 1000).toLocaleString() : '—'}
          </div>
        </div>
      ),
    },
    {
      title: '基准数据集',
      dataIndex: 'dataset_path',
      key: 'dataset_path',
      width: 220,
      render: (path: string | undefined, record: BenchmarkTaskItem) => {
        const fileName = path ? path.split(/[\/\\]/).pop() : '默认基准测试集';
        return (
          <div>
            <div className="flex items-center gap-1.5">
              <DatabaseOutlined className="text-slate-400 text-xs" />
              <Tooltip title={path || 'data/datasets/demo_benchmark.jsonl'}>
                <span className="text-xs font-semibold text-slate-800 truncate block max-w-[190px]">
                  {fileName}
                </span>
              </Tooltip>
            </div>
            <span className="text-3xs text-slate-400 font-mono block truncate max-w-[190px]">
              {path || 'data/datasets/demo_benchmark.jsonl'}
            </span>
            {record.skipped_mastered_samples && record.skipped_mastered_samples > 0 ? (
              <div className="mt-1">
                <Tag color="purple" className="text-3xs font-mono m-0 py-0 px-1">
                  ⚡ 智能降频 (实测 {record.sampled_samples ?? '?'} / {record.dataset_total_samples ?? '?'} 题)
                </Tag>
              </div>
            ) : null}
          </div>
        );
      },
    },
    {
      title: '被测模型组合',
      dataIndex: 'summaries',
      key: 'models',
      width: 240,
      render: (_: any, record) => {
        const modelNames = Object.keys(record.summaries || {});
        if (modelNames.length === 0) {
          if (record.model_configs && record.model_configs.length > 0) {
            return (
              <div className="flex flex-wrap gap-1">
                {record.model_configs.map((mc, idx) => (
                  <Tag key={idx} color="default" className="text-3xs font-mono m-0">
                    {mc.display_name || mc.model_id}
                  </Tag>
                ))}
              </div>
            );
          }
          return <span className="text-xs text-slate-400">准备中...</span>;
        }
        return (
          <div className="flex flex-wrap gap-1">
            {modelNames.map((name) => (
              <Tag key={name} color="blue" className="text-3xs font-mono m-0">
                {name}
              </Tag>
            ))}
          </div>
        );
      },
    },
    {
      title: '运行状态 / 进度',
      dataIndex: 'status',
      key: 'status',
      width: 170,
      render: (status: string, record) => {
        if (status === 'RUNNING' || status === 'PENDING') {
          const pct = Math.round((record.progress || 0) * 100);
          return (
            <div className="space-y-1 w-36">
              <div className="flex items-center justify-between text-3xs">
                <span className="text-blue-600 font-semibold flex items-center gap-1">
                  <SyncOutlined spin /> 评测中 ({pct}%)
                </span>
                {record.progress_detail && (
                  <span className="font-mono text-slate-500">
                    {record.progress_detail.completed}/{record.progress_detail.total}
                  </span>
                )}
              </div>
              <Progress percent={pct} size="small" status="active" showInfo={false} />
            </div>
          );
        }
        if (status === 'COMPLETED') {
          return (
            <Tag color="green" icon={<CheckCircleOutlined />} className="text-xs">
              已完成
            </Tag>
          );
        }
        if (status === 'FAILED') {
          return (
            <Tooltip title={record.error_message || '执行过程出现异常'}>
              <Tag color="red" icon={<CloseCircleOutlined />} className="text-xs cursor-pointer">
                执行失败
              </Tag>
            </Tooltip>
          );
        }
        return <Tag>{status}</Tag>;
      },
    },
    {
      title: '模型通过率速览',
      key: 'pass_rate',
      width: 220,
      render: (_: any, record) => {
        const summaries = record.summaries || {};
        const entries = Object.entries(summaries);
        if (entries.length === 0) {
          return <span className="text-3xs text-slate-400">暂无聚合数据</span>;
        }
        return (
          <div className="space-y-1">
            {entries.map(([mName, s]: [string, any]) => {
              const rate = Math.round((s.pass_rate || 0) * 100);
              const hRate =
                s.holdout_pass_rate !== undefined && s.holdout_pass_rate !== null
                  ? Math.round(s.holdout_pass_rate * 100)
                  : null;
              const rRate =
                s.regular_pass_rate !== undefined && s.regular_pass_rate !== null
                  ? Math.round(s.regular_pass_rate * 100)
                  : null;
              const color =
                rate >= 80
                  ? 'text-emerald-700 bg-emerald-50 border-emerald-200'
                  : rate >= 60
                  ? 'text-blue-700 bg-blue-50 border-blue-200'
                  : 'text-orange-700 bg-orange-50 border-orange-200';
              return (
                <div
                  key={mName}
                  className={`text-3xs px-1.5 py-0.5 rounded border flex flex-col gap-0.5 ${color}`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono truncate max-w-[110px]" title={mName}>
                      {mName}
                    </span>
                    <div className="flex items-center gap-1">
                      <span className="font-bold">{rate}%</span>
                      {s.estimated_full_pass_rate !== undefined &&
                        s.estimated_full_pass_rate !== null &&
                        s.is_benchmark_model !== false &&
                        (record.skipped_mastered_samples ?? 0) > 0 && (
                          <span
                            className="text-3xs text-purple-700 bg-purple-50 px-1 rounded border border-purple-200"
                            title={`全集等效通过率 (按实测熟题保持率折算): ${Math.round(s.estimated_full_pass_rate * 100)}%`}
                          >
                            等效{Math.round(s.estimated_full_pass_rate * 100)}%
                          </span>
                        )}
                    </div>
                  </div>
                  {(hRate !== null || rRate !== null) && (
                    <div className="text-4xs text-slate-500 font-mono flex items-center justify-between border-t border-black/5 pt-0.5">
                      <span>留出: {hRate !== null ? `${hRate}%` : '—'}</span>
                      <span>普通: {rRate !== null ? `${rRate}%` : '—'}</span>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        );
      },
    },
    {
      title: '执行耗时',
      key: 'duration',
      width: 100,
      render: (_: any, record) => {
        if (!record.created_at || !record.finished_at) {
          return <span className="text-3xs text-slate-400 font-mono">—</span>;
        }
        const diffSec = Math.max(0, record.finished_at - record.created_at);
        if (diffSec < 60) {
          return <span className="text-xs font-mono text-slate-600">{diffSec}秒</span>;
        }
        const min = Math.floor(diffSec / 60);
        const sec = diffSec % 60;
        return <span className="text-xs font-mono text-slate-600">{min}分{sec}秒</span>;
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 190,
      fixed: 'right',
      render: (_: any, record) => (
        <Space size="small">
          <Button
            type="primary"
            size="small"
            icon={<EyeOutlined />}
            onClick={() => navigate(`/benchmarks/${record.id}`)}
          >
            查看对比报告
          </Button>
          <Tooltip title="一键克隆该评测任务参数配置重新运行">
            <Button
              size="small"
              icon={<CopyOutlined />}
              onClick={() => handleCloneTask(record)}
            />
          </Tooltip>
          <Popconfirm
            title="确定删除当前评测记录吗？"
            description="删除后将永久清理该任务元数据及结果产物文件。"
            onConfirm={() => handleDeleteTask(record.id)}
            okText="确认删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      {/* 顶部概览与新建入口 */}
      <div className="bg-white p-5 rounded-lg border border-slate-200 shadow-2xs">
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
          <div>
            <h1 className="text-lg font-bold text-slate-900 flex items-center gap-2 mb-1">
              <ExperimentOutlined className="text-blue-600" />
              常规评测任务中心
            </h1>
            <p className="text-xs text-slate-500 mb-0">
              集中管理多模型横向矩阵评测记录，发起新的基准测试，深度对比各模型在客观规则与裁判下的表现。
            </p>
          </div>

          <div className="flex items-center gap-2.5 flex-wrap">
            <Button icon={<CodeOutlined />} onClick={() => handleOpenJudgePromptDebug()}>
              调试裁判 Prompt
            </Button>
            <Button icon={<ReloadOutlined />} onClick={() => loadTasks()}>
              刷新列表
            </Button>
            <Button
              type="primary"
              size="large"
              icon={<PlusOutlined />}
              onClick={handleOpenCreateDrawer}
              className="bg-blue-600 hover:bg-blue-700 shadow-xs"
            >
              发起新评测任务
            </Button>
          </div>
        </div>

        {/* 概览统计指标四宫格 */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4 pt-4 border-t border-slate-100">
          <div className="bg-slate-50 p-3 rounded border border-slate-100">
            <span className="text-3xs text-slate-400 uppercase font-semibold">历史总评测任务</span>
            <div className="text-xl font-bold text-slate-800 font-mono mt-0.5">{statistics.total}</div>
          </div>
          <div className="bg-blue-50/60 p-3 rounded border border-blue-100">
            <span className="text-3xs text-blue-600 uppercase font-semibold">评测运行中</span>
            <div className="text-xl font-bold text-blue-700 font-mono mt-0.5 flex items-center gap-2">
              {statistics.running}
              {statistics.running > 0 && <SyncOutlined spin className="text-xs text-blue-500" />}
            </div>
          </div>
          <div className="bg-emerald-50/60 p-3 rounded border border-emerald-100">
            <span className="text-3xs text-emerald-600 uppercase font-semibold">已成功完成</span>
            <div className="text-xl font-bold text-emerald-700 font-mono mt-0.5">{statistics.completed}</div>
          </div>
          <div className="bg-red-50/60 p-3 rounded border border-red-100">
            <span className="text-3xs text-red-600 uppercase font-semibold">评测失败/异常</span>
            <div className="text-xl font-bold text-red-700 font-mono mt-0.5">{statistics.failed}</div>
          </div>
        </div>
      </div>

      {/* 批量操作工具条 (当有多选时展示) */}
      {selectedRowKeys.length > 0 && (
        <div className="bg-blue-50/90 border border-blue-200 p-3 rounded-lg flex items-center justify-between flex-wrap gap-2 shadow-2xs">
          <div className="flex items-center gap-2">
            <Badge count={selectedRowKeys.length} style={{ backgroundColor: '#2563eb' }} />
            <span className="text-xs font-semibold text-blue-900">
              已选中 <span className="font-mono text-sm">{selectedRowKeys.length}</span> 项评测任务记录
            </span>
          </div>

          <Space size="small">
            <Button size="small" icon={<ExportOutlined />} onClick={handleBatchExport}>
              导出选中汇总 (JSON)
            </Button>

            <Popconfirm
              title={`确定批量删除选中的 ${selectedRowKeys.length} 个评测任务吗？`}
              description="删除后无法恢复，关联的元数据及产物报告文件将被永久抹除。"
              onConfirm={handleBatchDelete}
              okText="确定删除"
              cancelText="取消"
              okButtonProps={{ danger: true, loading: batchDeleting }}
            >
              <Button size="small" danger icon={<DeleteOutlined />} loading={batchDeleting}>
                批量删除 ({selectedRowKeys.length})
              </Button>
            </Popconfirm>

            <Button size="small" onClick={() => setSelectedRowKeys([])}>
              取消选定
            </Button>
          </Space>
        </div>
      )}

      {/* 评测记录筛选与表格视图 */}
      <Card size="small" className="shadow-2xs border-slate-200">
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 mb-3">
          <div className="flex items-center gap-2.5 flex-wrap">
            <Input
              placeholder="搜索任务 ID / 数据集 / 模型名..."
              prefix={<SearchOutlined className="text-slate-400" />}
              allowClear
              value={searchKeyword}
              onChange={(e) => {
                setSearchKeyword(e.target.value);
                setPagination((prev) => ({ ...prev, current: 1 }));
              }}
              className="w-72"
            />
            <Select
              value={statusFilter}
              onChange={(val) => {
                setStatusFilter(val);
                setPagination((prev) => ({ ...prev, current: 1 }));
              }}
              className="w-36"
              options={[
                { value: 'ALL', label: '全部状态' },
                { value: 'RUNNING', label: '运行中' },
                { value: 'COMPLETED', label: '已完成' },
                { value: 'FAILED', label: '失败' },
              ]}
            />
          </div>

          <div className="text-xs text-slate-500 font-mono">
            共找到 <span className="font-bold text-slate-800">{filteredTasks.length}</span> 条评测记录
          </div>
        </div>

        <Table
          columns={columns}
          dataSource={filteredTasks}
          rowKey="id"
          loading={loading}
          rowSelection={{
            selectedRowKeys,
            onChange: (newSelectedKeys) => setSelectedRowKeys(newSelectedKeys),
            selections: [
              Table.SELECTION_ALL,
              Table.SELECTION_INVERT,
              Table.SELECTION_NONE,
              {
                key: 'select_all_data',
                text: `跨页全选所有 (${filteredTasks.length} 条)`,
                onSelect: () => setSelectedRowKeys(filteredTasks.map((t) => t.id)),
              },
            ],
          }}
          pagination={{
            current: pagination.current,
            pageSize: pagination.pageSize,
            showSizeChanger: true,
            pageSizeOptions: ['10', '20', '50', '100'],
            showQuickJumper: true,
            onChange: (page, pageSize) => {
              setPagination({ current: page, pageSize });
            },
            showTotal: (total, range) => `显示第 ${range[0]} - ${range[1]} 项，共 ${total} 条历史任务`,
          }}
          scroll={{ x: 1080 }}
          size="middle"
        />
      </Card>

      {/* 发起新评测任务专有抽屉 (Drawer) */}
      <Drawer
        title={
          <div className="flex items-center gap-2 text-slate-900 font-bold">
            <ExperimentOutlined className="text-blue-600" />
            发起多模型横向矩阵评测
          </div>
        }
        placement="right"
        width={760}
        onClose={() => setDrawerVisible(false)}
        open={drawerVisible}
        extra={
          <Space>
            <Button onClick={() => setDrawerVisible(false)}>取消</Button>
            <Button
              type="primary"
              loading={submitting}
              onClick={handleStartRun}
              className="bg-blue-600"
            >
              立即提交并开始评测
            </Button>
          </Space>
        }
      >
        <Form form={runForm} layout="vertical" className="space-y-4">
          {/* 数据集选择 */}
          <Form.Item
            name="dataset_path"
            label={<span className="font-semibold text-slate-800">1. 选择评测基准数据集</span>}
            rules={[{ required: true, message: '请选择评测基准数据集' }]}
            className="mb-2"
          >
            <Select
              showSearch
              placeholder="选择已登记数据集或输入路径"
              options={[
                ...datasets.map((d) => ({
                  value: d.file_path,
                  label:
                    d.dataset_type === 'benchmark'
                      ? `${d.name} (${d.sample_count}条 · 基准考题集)`
                      : `${d.name} (${d.sample_count}条 · ⚠️ ${d.dataset_type} 纯对话语料，建议改走规则质检)`,
                })),
                {
                  value: 'data/datasets/demo_benchmark.jsonl',
                  label: '默认演示评测集 (data/datasets/demo_benchmark.jsonl)',
                },
                {
                  value: 'data/datasets/general_eval_simple_test.jsonl',
                  label: '30条通用能力简版基准测试集 (data/datasets/general_eval_simple_test.jsonl)',
                },
              ]}
            />
          </Form.Item>

          {isSelectedUnlabeledTrainingDataset && (
            <Alert
              type="warning"
              showIcon
              className="mb-2"
              message="当前选择的是「纯对话训练语料」，而非包含答案标准/正则的基准考题集"
              description={
                <div className="space-y-2 text-xs">
                  <div>
                    常规评测仅对具备 <code className="font-mono">reference</code>、<code className="font-mono">rubric</code> 或{' '}
                    <code className="font-mono">regex_constraints</code> 的考题执行打分；纯对话语料直接评测会被后端拦截以防产生假阳性 100% PASS。
                  </div>
                  <div className="flex items-center justify-between flex-wrap gap-2 pt-1">
                    <Button
                      size="small"
                      type="primary"
                      onClick={() => {
                        setDrawerVisible(false);
                        navigate('/datasets');
                      }}
                    >
                      前往数据集中心执行「规则质检打分」
                    </Button>
                    <Form.Item
                      name="allow_unlabeled"
                      valuePropName="checked"
                      className="m-0"
                    >
                      <Switch
                        checkedChildren="已放行无标准集(仅测速)"
                        unCheckedChildren="默认拦截无标准集"
                      />
                    </Form.Item>
                  </div>
                  {watchedAllowUnlabeled && (
                    <div className="text-amber-700 font-medium">
                      已开启强行放行：本次仅用于模型连通性或生成吞吐测速，通过率仅供参考。
                    </div>
                  )}
                </div>
              }
            />
          )}

          {/* 智能阶梯降频与抽样策略配置卡片 */}
          <div className="p-3.5 bg-gradient-to-r from-purple-50/70 to-indigo-50/70 border border-purple-200 rounded-lg space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <div className="flex items-center gap-1.5 font-semibold text-slate-800 text-xs">
                  <FilterOutlined className="text-purple-600" />
                  <span>2. 测评集题型过滤与动态降频策略</span>
                </div>
                <span className="text-3xs text-slate-500 block mt-0.5">
                  留出题与普通题均支持学会降频、遗忘唤醒与评测过滤；评测报告将自动按留出题与普通题区分统计通过率。
                </span>
              </div>
              <Form.Item name="sampling_enabled" valuePropName="checked" className="m-0">
                <Switch checkedChildren="智能阶梯降频开启" unCheckedChildren="全量评测 (100%)" />
              </Form.Item>
            </div>

            <div className="pt-2 border-t border-purple-200/80">
              <Form.Item
                name="holdout_filter"
                label={<span className="text-3xs font-semibold text-slate-700">评测题型范围过滤</span>}
                className="m-0"
              >
                <Select
                  size="small"
                  options={[
                    { value: 'all', label: '全部题目参评（同时评测留出题与普通题，报告分别统计通过率）' },
                    { value: 'holdout_only', label: '🔒 仅评测留出题 (is_holdout=true)' },
                    { value: 'non_holdout_only', label: '📄 仅评测普通题 (is_holdout=false)' },
                  ]}
                />
              </Form.Item>
            </div>

            {watchedSamplingEnabled && (
              <div className="pt-2 border-t border-purple-200/80 space-y-3">
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 bg-white p-3 rounded border border-purple-100">
                  <Form.Item
                    name="pass_threshold_x"
                    label={<span className="text-3xs font-medium text-slate-700">降频门槛 (x 次)</span>}
                    tooltip="连续通过满 x 次后开始拉长复测间隔"
                    className="m-0"
                  >
                    <InputNumber min={1} max={20} className="w-full" size="small" />
                  </Form.Item>

                  <Form.Item
                    name="interval_step_s"
                    label={<span className="text-3xs font-medium text-slate-700">间隔步长 (s 轮)</span>}
                    tooltip="每多通过 1 次，复测间隔递增 s"
                    className="m-0"
                  >
                    <InputNumber min={1} max={10} className="w-full" size="small" />
                  </Form.Item>

                  <Form.Item
                    name="max_interval_n"
                    label={<span className="text-3xs font-medium text-slate-700">最大间隔上限 (N)</span>}
                    tooltip="封顶上限，最多隔 N-1 次不测，第 N 次必测"
                    className="m-0"
                  >
                    <InputNumber min={1} max={50} className="w-full" size="small" />
                  </Form.Item>

                  <Form.Item
                    name="min_pass_score"
                    label={<span className="text-3xs font-medium text-slate-700">最低分门槛</span>}
                    tooltip="选填。仅当裁判得分高于该分值才计入连续通过（留空则默认按 PASS 及格线）"
                    className="m-0"
                  >
                    <InputNumber min={0} max={100} placeholder="默认及格线" className="w-full" size="small" />
                  </Form.Item>
                </div>

                {/* 战绩更新基准模型线与版本控制 */}
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-semibold text-slate-700">
                      战绩归属基准模型线 (留空则进入只读摸底，零写入流水)
                    </span>
                    <span className="text-3xs text-slate-400">
                      {watchedBenchmarkModelId || watchedModelLine
                        ? `将入账模型线: ${watchedModelLine || watchedBenchmarkModelId}`
                        : '只读摸底模式 (不入账)'}
                    </span>
                  </div>

                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                    <Form.Item name="benchmark_model_id" label={<span className="text-3xs text-slate-600">参评基准模型 (单选)</span>} className="m-0">
                      <Select
                        allowClear
                        placeholder="留空为只读摸底"
                        onChange={(val) => {
                          if (val) {
                            const m = models.find((x) => x.id === val);
                            const curLine = (runForm.getFieldValue('model_line') || '').trim();
                            const isAutoLine = !curLine || models.some((x) => (x.model_name || x.id) === curLine || x.id === curLine);
                            if (isAutoLine) {
                              runForm.setFieldsValue({ model_line: m?.model_name || val });
                            }
                          }
                        }}
                        options={selectedModelIds.map((mid) => {
                          const m = models.find((x) => x.id === mid);
                          return { value: mid, label: `${m?.name || mid}` };
                        })}
                      />
                    </Form.Item>

                    <Form.Item name="model_line" label={<span className="text-3xs text-slate-600">模型线标识 (Model Line)</span>} className="m-0">
                      <Input placeholder="例如 qwen2.5-7b-sft" />
                    </Form.Item>

                    <Form.Item name="version_tag" label={<span className="text-3xs text-slate-600">版本号 (Version Tag)</span>} className="m-0">
                      <Input placeholder="例如 v2.1 (入账必填)" />
                    </Form.Item>
                  </div>

                  <Alert
                    type="info"
                    showIcon
                    className="text-3xs py-1 px-2.5"
                    message={
                      <span>
                        💡 <strong>独立流水账本</strong>：战绩保存在平台流水表（原始 .jsonl 保持纯净只读）。同模型线同版本号重跑将自动复用首次题单且不推进轮空计数；无论是留出题 (<code className="font-mono">is_holdout=true</code>) 还是普通题 (<code className="font-mono">is_holdout=false</code>)，均支持连续达标学会降频与复测失败遗忘唤醒。
                      </span>
                    }
                  />
                </div>
              </div>
            )}

            {(watchedSamplingEnabled || watchedHoldoutFilter !== 'all') && (
              <div className="p-2.5 bg-purple-50/80 rounded border border-purple-200">
                <div className="flex items-center justify-between mb-1.5">
                  <span className="text-3xs font-bold text-purple-900 flex items-center gap-1">
                    <ExperimentOutlined /> 本次预计评测样本分布与算力节省试算
                  </span>
                  <Space size="small">
                    <Button
                      size="small"
                      type="text"
                      icon={<ReloadOutlined spin={previewLoading} />}
                      onClick={refreshSamplingPreview}
                      className="text-3xs text-purple-700 h-6 px-1.5"
                    >
                      刷新试算
                    </Button>
                    <Popconfirm
                      title="确定重置该数据集在流水表中的复测战绩？"
                      description="重置后将作废对应流水记录，所有题目将回归全新状态 (c=0) 必测。"
                      onConfirm={handleResetDatasetTracker}
                      okText="确认重置"
                      cancelText="取消"
                    >
                      <Button size="small" danger type="link" className="text-3xs h-6 px-1">
                        🔄 重置战绩归零
                      </Button>
                    </Popconfirm>
                  </Space>
                </div>

                {samplingPreview ? (
                  <div className="space-y-1.5">
                    <div className="flex items-center justify-between text-xs font-semibold flex-wrap gap-1">
                      <span>
                        范围题量: <span className="font-mono">{samplingPreview.total_samples}</span> 题 ➔ 本次实测:{' '}
                        <span className="font-mono text-purple-700">{samplingPreview.sampled_samples}</span> 题
                      </span>
                      <div className="flex items-center gap-1">
                        {samplingPreview.is_rerun_same_version && (
                          <Tag color="cyan" className="m-0 font-semibold">
                            📌 命中同版本快照复用
                          </Tag>
                        )}
                        <Tag color="purple" className="m-0 font-bold">
                          节省 {samplingPreview.saved_compute_ratio}% 算力
                        </Tag>
                      </div>
                    </div>

                    <div className="flex items-center gap-1.5 flex-wrap text-3xs font-mono">
                      <Tag color="red" className="m-0">
                        🔴 必测攻坚: {samplingPreview.active_pool_count} 题
                      </Tag>
                      <Tag color="gold" className="m-0">
                        🟡 触顶必测: {samplingPreview.hard_cap_pool_count} 题
                      </Tag>
                      <Tag color="blue" className="m-0">
                        🟢 降频轮巡: {samplingPreview.regular_cooldown_count} 题
                      </Tag>
                      <Tag color="default" className="m-0">
                        💤 本轮休眠免测: {samplingPreview.skipped_samples} 题
                      </Tag>
                      <Tag color="geekblue" className="m-0">
                        🏷️ 留出题: {samplingPreview.sampled_holdout_count ?? samplingPreview.holdout_count ?? 0}/{samplingPreview.holdout_count ?? 0} ｜ 普通题: {samplingPreview.sampled_non_holdout_count ?? samplingPreview.non_holdout_count ?? 0}/{samplingPreview.non_holdout_count ?? 0}
                      </Tag>
                    </div>
                    {samplingPreview.notice && (
                      <div className="text-3xs text-purple-800">{samplingPreview.notice}</div>
                    )}
                  </div>
                ) : (
                  <div className="text-3xs text-purple-700">
                    {previewLoading ? '正在试算题目分布...' : '请选择数据集以查看评测样本与降频试算分布'}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* 对比模型选择与参数覆盖 */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <span className="font-semibold text-slate-800">3. 选择参与对比的模型 (支持开/关思考对比)</span>
              <span className="text-3xs text-slate-500">已选 {selectedModelIds.length} 个模型</span>
            </div>
            <div className="grid grid-cols-2 gap-3 p-3 bg-slate-50 rounded-lg border border-slate-200">
              {models.map((m) => {
                const isChecked = selectedModelIds.includes(m.id);
                const hasOverride = !!overrideConfigs[m.id];
                return (
                  <div
                    key={m.id}
                    className={`p-2.5 rounded border flex items-center justify-between transition-colors ${
                      isChecked ? 'bg-blue-50/80 border-blue-300 shadow-2xs' : 'bg-white border-slate-200'
                    }`}
                  >
                    <Checkbox
                      checked={isChecked}
                      onChange={(e) => {
                        if (e.target.checked) {
                          setSelectedModelIds([...selectedModelIds, m.id]);
                        } else {
                          setSelectedModelIds(selectedModelIds.filter((x) => x !== m.id));
                        }
                      }}
                    >
                      <span className="text-xs font-semibold block">{m.name}</span>
                      <div className="flex items-center gap-1.5 mt-0.5">
                        <span className="text-3xs text-slate-400 font-mono">{m.id}</span>
                        {hasOverride ? (
                          <Tag color="blue" className="text-3xs font-mono m-0 py-0 px-1">
                            MaxTokens: {overrideConfigs[m.id].config.max_tokens} (已单独调参)
                          </Tag>
                        ) : (
                          <span className="text-3xs text-blue-600 font-mono">
                            MaxTokens: {watchedMaxTokens} (统一生效)
                          </span>
                        )}
                      </div>
                    </Checkbox>

                    {isChecked && (
                      <Button
                        size="small"
                        type={hasOverride ? 'primary' : 'default'}
                        icon={<SettingOutlined />}
                        onClick={() => openOverrideModal(m.id)}
                      >
                        {hasOverride ? '已覆盖' : '临时调参'}
                      </Button>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {/* 生成长度与 Token 控制 */}
          <div className="p-3.5 bg-slate-50 border border-slate-200 rounded-lg space-y-2.5">
            <div>
              <span className="font-semibold text-slate-800 text-xs block">
                4. 生成长度与 Token 控制 (Max Output Tokens)
              </span>
              <span className="text-3xs text-slate-500">
                自定义被测模型输出长度限制，防止长文本被截断或模型无限生成。
              </span>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <Form.Item
                name="max_tokens"
                label={<span className="text-xs text-slate-700">统一最大生成 Token (Max Tokens)</span>}
                rules={[{ required: true, message: '请输入最大生成Token数' }]}
                className="mb-1"
              >
                <InputNumber min={1} max={32768} step={256} className="w-full" placeholder="默认 4096" />
              </Form.Item>

              <Form.Item
                name="ignore_sample_max_tokens"
                label={<span className="text-xs text-slate-700">覆盖并忽略样本级 max_new_tokens 限制</span>}
                valuePropName="checked"
                className="mb-1"
                tooltip="部分评测集样本中硬编码了 max_new_tokens（如 64 或 128）。开启后将忽略样本自带限制，强制使用本次配置的最大 Token（如 512），确保长回答不被强行截断。"
                extra={<span className="text-3xs text-slate-500 block mt-1">开启后强制使用本次配置的最大 Token，忽略样本自带上限</span>}
              >
                <Switch checkedChildren="强制覆盖生效" unCheckedChildren="遵从样本限制" />
              </Form.Item>
            </div>

            {/* 常用快捷预设值 */}
            <div className="flex items-center gap-1.5 flex-wrap pt-0.5 text-3xs text-slate-500">
              <span>快捷填入:</span>
              {[512, 1024, 2048, 4096, 8192, 16384].map((tokenVal) => (
                <Tag
                  key={tokenVal}
                  className="cursor-pointer hover:border-blue-400 hover:text-blue-600 transition-colors m-0 text-3xs font-mono"
                  onClick={() => runForm.setFieldsValue({ max_tokens: tokenVal })}
                >
                  {tokenVal}
                </Tag>
              ))}
            </div>
          </div>

          {/* 裁判模型与并发线程数 */}
          <div className="grid grid-cols-2 gap-4">
            <Form.Item
              name="judge_model_id"
              label={
                <div className="flex items-center gap-2">
                  <span className="font-semibold text-slate-800 text-xs">
                    5. LLM 裁判打分模型 (可选)
                  </span>
                  <Button
                    type="link"
                    size="small"
                    className="p-0 h-auto text-xs"
                    icon={<CodeOutlined />}
                    onClick={() => handleOpenJudgePromptDebug()}
                  >
                    调试裁判 Prompt
                  </Button>
                </div>
              }
            >
              <Select allowClear popupMatchSelectWidth={false} placeholder="如不指定则仅执行确定性客观规则校验">
                {models.map((m) => (
                  <Select.Option key={m.id} value={m.id}>
                    {m.name} ({m.id})
                  </Select.Option>
                ))}
              </Select>
            </Form.Item>

            <Form.Item
              name="workers"
              label={<span className="font-semibold text-slate-800 text-xs">并发评测线程数</span>}
            >
              <InputNumber min={1} max={16} className="w-full" />
            </Form.Item>
          </div>
        </Form>
      </Drawer>

      {/* 单模型临时调参弹窗 */}
      <Modal
        title={`临时参数覆盖: ${overridingModelId}`}
        open={overrideModalVisible}
        onCancel={() => setOverrideModalVisible(false)}
        onOk={handleSaveOverride}
        width={480}
        okText="确认覆盖"
        cancelText="取消"
      >
        <Form form={overrideForm} layout="vertical" className="mt-2">
          <Form.Item name="display_name" label="矩阵展示别名" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <div className="grid grid-cols-2 gap-3">
            <Form.Item name="temperature" label="Temperature">
              <InputNumber min={0} max={2} step={0.1} className="w-full" />
            </Form.Item>
            <Form.Item
              name="max_tokens"
              label="Max Tokens (最大生成Token)"
              rules={[{ required: true, message: '请输入Max Tokens' }]}
            >
              <InputNumber min={1} max={32768} step={256} className="w-full" />
            </Form.Item>
          </div>
          <div className="flex items-center gap-1.5 mb-3 flex-wrap">
            <span className="text-3xs text-slate-400">快捷预设:</span>
            {[512, 1024, 2048, 4096, 8192].map((tv) => (
              <Tag
                key={tv}
                className="cursor-pointer hover:border-blue-400 hover:text-blue-600 text-3xs font-mono m-0"
                onClick={() => overrideForm.setFieldsValue({ max_tokens: tv })}
              >
                {tv}
              </Tag>
            ))}
          </div>
          <Form.Item
            name="thinking_enabled"
            label="思考开关 (Thinking Enabled)"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item name="thinking_level" label="思考等级 (Thinking Level)">
            <Select>
              <Select.Option value="NONE">NONE</Select.Option>
              <Select.Option value="LOW">LOW</Select.Option>
              <Select.Option value="MEDIUM">MEDIUM</Select.Option>
              <Select.Option value="HIGH">HIGH</Select.Option>
            </Select>
          </Form.Item>
        </Form>
      </Modal>

      {/* 常规评测 Rubric 裁判 Prompt 调试弹窗 */}
      <Modal
        title={
          <div className="flex items-center gap-2">
            <CodeOutlined className="text-blue-600" />
            <span>常规评测 Rubric 裁判 Prompt 组装与实测</span>
            {judgeDebugData?.sample_id && (
              <Tag color="blue" className="font-mono">
                样本: {judgeDebugData.sample_id}
              </Tag>
            )}
          </div>
        }
        open={judgeDebugVisible}
        onCancel={() => setJudgeDebugVisible(false)}
        width={1080}
        footer={null}
        destroyOnClose
      >
        <div className="space-y-3 pt-1">
          <Alert
            type="info"
            showIcon
            message={
              <span className="text-xs">
                <strong>单次评测样本调试</strong>：当前读取「提示词管理中心」中 <code>benchmark_judge</code> 的全局激活模板，结合下方所选评测集首条真实样本与 Rubric 拼装试跑；<b>不会修改全局提示词或已有评测任务</b>。
              </span>
            }
          />
          <div className="p-3 bg-slate-50 rounded-lg border border-slate-200 space-y-2">
            <div className="flex items-center justify-between flex-wrap gap-2">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs font-semibold text-slate-700">样本来源评测集:</span>
                <Select
                  size="small"
                  allowClear
                  showSearch
                  popupMatchSelectWidth={false}
                  className="w-64"
                  placeholder="留空则使用内置示例样本"
                  value={judgeDebugDatasetPath || undefined}
                  onChange={(val) => {
                    const nextPath = val || '';
                    setJudgeDebugDatasetPath(nextPath);
                    setJudgeDebugAnswer('');
                    handleAssembleJudgePrompt('', nextPath);
                  }}
                  options={judgeDebugDatasetOptions}
                />
              </div>
              <Space wrap>
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={judgeDebugLoading}
                  onClick={() => handleAssembleJudgePrompt(judgeDebugAnswer, judgeDebugDatasetPath)}
                >
                  重新组装报文
                </Button>
                <Button
                  size="small"
                  icon={<CopyOutlined />}
                  disabled={!judgeDebugData?.assembled_messages?.length}
                  onClick={() => {
                    if (judgeDebugData?.assembled_messages?.length) {
                      copyTextSafely(
                        JSON.stringify(judgeDebugData.assembled_messages, null, 2),
                        '已复制裁判 Request Messages JSON'
                      );
                    }
                  }}
                >
                  复制 Messages
                </Button>
                <Button
                  size="small"
                  icon={<DownloadOutlined />}
                  disabled={!judgeDebugData?.assembled_prompt}
                  onClick={() => {
                    if (!judgeDebugData) return;
                    exportPromptDebugJson({
                      title: '常规评测 Rubric 裁判打分',
                      prompt_key: 'benchmark_judge',
                      assembled_messages: judgeDebugData.assembled_messages,
                      assembled_prompt: judgeDebugData.assembled_prompt,
                      test_result: judgeTestResult,
                    });
                    message.success('已导出裁判调试 JSON');
                  }}
                >
                  导出 JSON
                </Button>
                <Button
                  size="small"
                  icon={<DownloadOutlined />}
                  disabled={!judgeDebugData?.assembled_prompt}
                  onClick={() => {
                    if (!judgeDebugData) return;
                    exportPromptDebugMarkdown({
                      title: '常规评测 Rubric 裁判打分',
                      prompt_key: 'benchmark_judge',
                      assembled_messages: judgeDebugData.assembled_messages,
                      assembled_prompt: judgeDebugData.assembled_prompt,
                      test_result: judgeTestResult,
                    });
                    message.success('已导出裁判调试 Markdown');
                  }}
                >
                  导出 Markdown
                </Button>
              </Space>
            </div>
            <div className="text-xs font-semibold text-slate-600">
              被测模型模拟回答 (用于组装 {'{model_answer}'}):
            </div>
            <Input.TextArea
              rows={2}
              value={judgeDebugAnswer}
              onChange={(e) => setJudgeDebugAnswer(e.target.value)}
              placeholder="输入待测模型回复内容后点击重新组装..."
              className="font-mono text-xs"
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="flex flex-col h-[480px] bg-slate-900 rounded-lg border border-slate-700 overflow-hidden text-slate-100">
              <div className="px-3 py-2 bg-slate-800 border-b border-slate-700 flex items-center justify-between text-xs">
                <span className="font-semibold text-slate-200">组装后裁判 Request Messages</span>
                <Tag color="cyan" className="font-mono text-3xs m-0">
                  {judgeDebugData?.assembled_prompt?.length || 0} chars
                </Tag>
              </div>
              <div className="flex-1 overflow-auto p-3 space-y-2">
                {(judgeDebugData?.assembled_messages || []).map((m, idx) => (
                  <div key={idx} className="rounded border border-slate-700 bg-slate-950/60 overflow-hidden">
                    <div className="px-2.5 py-1 bg-slate-800/80 border-b border-slate-700">
                      <Tag color={m.role === 'system' ? 'purple' : 'blue'} className="font-mono text-3xs uppercase m-0">
                        {m.role}
                      </Tag>
                    </div>
                    <pre className="p-2.5 font-mono text-xs leading-5 whitespace-pre-wrap break-words text-slate-100 m-0">
                      {m.content}
                    </pre>
                  </div>
                ))}
              </div>
            </div>

            <div className="flex flex-col h-[480px] bg-white rounded-lg border border-slate-200 overflow-hidden">
              <div className="p-3 bg-slate-50 border-b border-slate-200 flex items-center justify-between gap-2">
                <Select
                  size="small"
                  value={judgeTestModelId}
                  onChange={setJudgeTestModelId}
                  popupMatchSelectWidth={false}
                  className="flex-1"
                  placeholder="选择裁判模型"
                >
                  {models.map((m) => (
                    <Select.Option key={m.id} value={m.id}>
                      {m.name} ({m.provider_type})
                    </Select.Option>
                  ))}
                </Select>
                <Button
                  type="primary"
                  size="small"
                  icon={<PlayCircleOutlined />}
                  loading={judgeTestRunning}
                  onClick={handleTestJudgePrompt}
                >
                  发送裁判实测
                </Button>
              </div>
              <div className="flex-1 overflow-auto p-3 space-y-2 text-xs">
                {judgeTestResult ? (
                  <>
                    <div className="flex items-center justify-between p-2 bg-slate-100 rounded border border-slate-200">
                      <Tag color={judgeTestResult.status === 'success' ? 'green' : 'red'}>
                        {judgeTestResult.status.toUpperCase()}
                      </Tag>
                      <span className="text-slate-600">耗时: {judgeTestResult.latency_ms} ms</span>
                    </div>
                    {judgeTestResult.parsed_data && (
                      <div className="p-2.5 bg-blue-50/60 rounded border border-blue-200 space-y-1">
                        <div className="font-semibold text-blue-950">
                          评分: {judgeTestResult.parsed_data.score ?? 0} · {judgeTestResult.parsed_data.verdict || 'FAIL'}
                        </div>
                        <div className="text-slate-700">{judgeTestResult.parsed_data.feedback}</div>
                      </div>
                    )}
                    <pre className="p-2 bg-slate-50 rounded border border-slate-200 font-mono text-3xs whitespace-pre-wrap m-0">
                      {judgeTestResult.raw_content}
                    </pre>
                  </>
                ) : (
                  <div className="py-20 text-center text-slate-400">
                    选择裁判模型并点击「发送裁判实测」查看打分效果
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      </Modal>
    </div>
  );
};
