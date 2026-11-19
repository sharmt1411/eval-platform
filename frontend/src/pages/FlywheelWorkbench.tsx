import React, { useEffect, useRef, useState } from 'react';
import {
  Table,
  Button,
  Tag,
  Space,
  Select,
  Radio,
  Drawer,
  Modal,
  Form,
  Input,
  InputNumber,
  Switch,
  Typography,
  message,
  notification,
  Popconfirm,
  Badge,
  Spin,
  Dropdown,
  Alert,
  Tooltip,
  Tabs,
  Collapse,
  Card,
} from 'antd';
import {
  SyncOutlined,
  ExportOutlined,
  CheckOutlined,
  StopOutlined,
  ReloadOutlined,
  CodeOutlined,
  AppstoreOutlined,
  ExperimentOutlined,
  DeleteOutlined,
  DownOutlined,
  CheckCircleOutlined,
  SafetyOutlined,
  AuditOutlined,
  InfoCircleOutlined,
  RocketOutlined,
  PlusOutlined,
  SaveOutlined,
} from '@ant-design/icons';
import { useSearchParams } from 'react-router-dom';
import { flywheelApi, rulesApi, modelsApi } from '../api';
import { TurnDiffViewer } from '../components/TurnDiffViewer';
import { MonacoDiffModal } from '../components/MonacoDiffModal';
import type {
  FlywheelSampleRecord,
  RuleSummary,
  RegisteredModel,
  FlywheelStatus,
  ChatMessage,
  FlywheelBenchmarkSlice,
} from '../types';

const { Text } = Typography;

const FLYWHEEL_LAST_JUDGE_MODEL_KEY = 'eval_platform_flywheel_last_judge_model_id';
const FLYWHEEL_LAST_REMEDIATE_MODEL_KEY = 'eval_platform_flywheel_last_remediate_model_id';

export const FlywheelWorkbench: React.FC = () => {
  const [searchParams] = useSearchParams();
  const [samples, setSamples] = useState<FlywheelSampleRecord[]>([]);
  const [rules, setRules] = useState<RuleSummary[]>([]);
  const [models, setModels] = useState<RegisteredModel[]>([]);
  const [loading, setLoading] = useState<boolean>(false);

  // 筛选状态
  const [statusFilter, setStatusFilter] = useState<string>('ALL');
  const [selectedRuleFilter, setSelectedRuleFilter] = useState<string>('ALL');

  // 多选批量管理状态
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [flywheelPagination, setFlywheelPagination] = useState<{ current: number; pageSize: number }>({
    current: 1,
    pageSize: 15,
  });
  const [batchActionLoading, setBatchActionLoading] = useState<boolean>(false);

  // 批量状态更新模态框
  const [batchStatusModalVisible, setBatchStatusModalVisible] = useState<boolean>(false);
  const [targetBatchStatus, setTargetBatchStatus] = useState<FlywheelStatus>('GOLD_PASS');
  const [batchStatusForm] = Form.useForm();

  // 批量定点补考模态框
  const [batchRemediateModalVisible, setBatchRemediateModalVisible] = useState<boolean>(false);
  const [batchRemediateForm] = Form.useForm();

  // 批量重新质检模态框 (按新标准/法官模型重检)
  const [batchJudgeModalVisible, setBatchJudgeModalVisible] = useState<boolean>(false);
  const [batchJudgeForm] = Form.useForm();

  // 批量导出选定样本模态框
  const [batchExportSelectedModalVisible, setBatchExportSelectedModalVisible] = useState<boolean>(false);
  const [batchExportSelectedForm] = Form.useForm();

  // 批量导出评测集模态框
  const [batchExportBenchmarkModalVisible, setBatchExportBenchmarkModalVisible] = useState<boolean>(false);
  const [batchExportBenchmarkForm] = Form.useForm();

  // 复核抽屉与当前样本 (单条)
  const [reviewDrawerVisible, setReviewDrawerVisible] = useState<boolean>(false);
  const [currentSample, setCurrentSample] = useState<FlywheelSampleRecord | null>(null);
  const [remediatedMessages, setRemediatedMessages] = useState<ChatMessage[]>([]);
  const [reviewerComment, setReviewerComment] = useState<string>('');

  // 测评切题状态 (抽屉内 Tab 2)
  const [currentSlices, setCurrentSlices] = useState<FlywheelBenchmarkSlice[]>([]);
  const [slicesLoading, setSlicesLoading] = useState<boolean>(false);
  const [sliceSaving, setSliceSaving] = useState<boolean>(false);
  const [activeDrawerTab, setActiveDrawerTab] = useState<string>('diff');
  const [exportSingleBenchmarkVisible, setExportSingleBenchmarkVisible] = useState<boolean>(false);
  const [singleExportForm] = Form.useForm();

  // 单条重新质检模态框 (抽屉内)
  const [singleJudgeModalVisible, setSingleJudgeModalVisible] = useState<boolean>(false);
  const [singleJudgeLoading, setSingleJudgeLoading] = useState<boolean>(false);
  const [singleJudgeForm] = Form.useForm();
  const singleJudgeAbortRef = useRef<AbortController | null>(null);

  // 单条数据重新修复模型配置模态框
  const [remediateModalVisible, setRemediateModalVisible] = useState<boolean>(false);
  const [remediateLoading, setRemediateLoading] = useState<boolean>(false);
  const [remediateForm] = Form.useForm();
  const remediateAbortRef = useRef<AbortController | null>(null);
  const watchSingleRunInitial = Form.useWatch('run_initial_judge', remediateForm);
  const watchSingleRunVerified = Form.useWatch('run_verified_judge', remediateForm);
  const watchBatchRunInitial = Form.useWatch('run_initial_judge', batchRemediateForm);
  const watchBatchRunVerified = Form.useWatch('run_verified_judge', batchRemediateForm);

  // Monaco Diff 模态框
  const [monacoModalVisible, setMonacoModalVisible] = useState<boolean>(false);

  // 全量导出模态框 (原全规则导出)
  const [exportModalVisible, setExportModalVisible] = useState<boolean>(false);
  const [exportLoading, setExportLoading] = useState<boolean>(false);
  const [exportForm] = Form.useForm();

  // 批量存量语料治理模态框 (原离线任务)
  const [batchModalVisible, setBatchModalVisible] = useState<boolean>(false);
  const [batchForm] = Form.useForm();

  const persistFlywheelModels = (judgeModelId?: string, remediateModelId?: string) => {
    try {
      if (judgeModelId) {
        localStorage.setItem(FLYWHEEL_LAST_JUDGE_MODEL_KEY, judgeModelId);
      }
      if (remediateModelId) {
        localStorage.setItem(FLYWHEEL_LAST_REMEDIATE_MODEL_KEY, remediateModelId);
      }
    } catch {
      // ignore storage errors
    }
  };

  const resolvePreferredModelId = (
    kind: 'judge' | 'remediate',
    sample?: FlywheelSampleRecord | null
  ): string => {
    const validIds = new Set(models.map((m) => m.id));
    const storageKey =
      kind === 'judge' ? FLYWHEEL_LAST_JUDGE_MODEL_KEY : FLYWHEEL_LAST_REMEDIATE_MODEL_KEY;

    // 1. 优先沿用复核台最近一次测试提交保留的模型选择
    try {
      const savedId = localStorage.getItem(storageKey);
      if (savedId && validIds.has(savedId)) {
        return savedId;
      }
    } catch {
      // ignore
    }

    // 2. 尝试匹配当前样本历史质检/修复快照中记录的模型
    if (sample) {
      const candidateEntries =
        kind === 'judge'
          ? [
              ...(sample.verified_eval?.debug_requests || []),
              ...(sample.initial_eval?.debug_requests || []),
            ]
          : sample.remediation_debug
          ? [sample.remediation_debug]
          : [];
      const mainEntry =
        candidateEntries.find((e) => e.step === 'main_judge' || e.step === 'remediate') ||
        candidateEntries[0];
      if (mainEntry?.model) {
        const byId = models.find((m) => m.id === mainEntry.model);
        if (byId) return byId.id;
        const entryThinking = mainEntry.config?.thinking_enabled;
        const byNameAndThinking = models.find(
          (m) =>
            m.model_name === mainEntry.model &&
            (entryThinking === undefined ||
              Boolean(m.config?.thinking_enabled) === Boolean(entryThinking))
        );
        if (byNameAndThinking) return byNameAndThinking.id;
        const byName = models.find((m) => m.model_name === mainEntry.model);
        if (byName) return byName.id;
      }
    }

    // 3. 回退读取模拟对弈台当前选中的预设方案模型
    try {
      const lastPresetId = localStorage.getItem('eval_platform_last_selected_preset_id');
      const presetsRaw = localStorage.getItem('eval_platform_model_presets');
      if (lastPresetId && presetsRaw) {
        const parsed = JSON.parse(presetsRaw);
        const preset = Array.isArray(parsed)
          ? parsed.find((p: any) => p?.id === lastPresetId)
          : null;
        const presetModelId =
          kind === 'judge'
            ? preset?.judge_model_id
            : preset?.remediate_model_id || preset?.judge_model_id;
        if (presetModelId && validIds.has(presetModelId)) {
          return presetModelId;
        }
      }
    } catch {
      // ignore
    }

    return models[0]?.id || '';
  };

  const loadData = async () => {
    setLoading(true);
    try {
      const [rList, mList, sList] = await Promise.all([
        rulesApi.list(),
        modelsApi.list(),
        flywheelApi.listSamples({
          status: statusFilter === 'ALL' ? undefined : statusFilter,
          rule_id: selectedRuleFilter === 'ALL' ? undefined : selectedRuleFilter,
          limit: 300,
        }),
      ]);
      setRules(rList);
      setModels(mList);
      setSamples(sList);
    } catch (e: any) {
      message.error(`加载飞轮样本失败: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setSelectedRowKeys([]);
    loadData();
  }, [statusFilter, selectedRuleFilter]);

  // 支持从其他页面携带 rule_id / sample_id 路由跳转并自动打开抽屉
  useEffect(() => {
    const ruleParam = searchParams.get('rule_id');
    const sampleParam = searchParams.get('sample_id');
    if (ruleParam && ruleParam !== selectedRuleFilter) {
      setSelectedRuleFilter(ruleParam);
    }
    if (sampleParam && samples.length > 0) {
      const target = samples.find((s) => s.id === sampleParam);
      if (target) {
        openReviewDrawer(target);
      }
    }
  }, [searchParams, samples]);

  const loadSampleSlices = async (sampleId: string) => {
    setSlicesLoading(true);
    try {
      const res = await flywheelApi.getSlices(sampleId);
      setCurrentSlices(res.slices || []);
    } catch (e: any) {
      console.warn('获取测评切片失败:', e);
      setCurrentSlices([]);
    } finally {
      setSlicesLoading(false);
    }
  };

  const openReviewDrawer = (sample: FlywheelSampleRecord) => {
    setCurrentSample(sample);
    setRemediatedMessages(sample.remediated_messages || sample.raw_messages || []);
    setReviewerComment(sample.reviewer_comment || '');
    setActiveDrawerTab('diff');
    setReviewDrawerVisible(true);
    loadSampleSlices(sample.id);
  };

  const handleSaveSlices = async () => {
    if (!currentSample) return;
    setSliceSaving(true);
    try {
      await flywheelApi.updateSlices(currentSample.id, {
        slices: currentSlices,
        sync_to_dialogue: true,
      });
      message.success('测评切片已保存，并已双向同步更新至完整多轮会话！');
      const list = await flywheelApi.listSamples({ limit: 200 });
      setSamples(list);
      const updated = list.find((s) => s.id === currentSample.id);
      if (updated) {
        setCurrentSample(updated);
        if (updated.remediated_messages) {
          setRemediatedMessages(updated.remediated_messages);
        }
      }
    } catch (e: any) {
      message.error(`保存切片失败: ${e.message}`);
    } finally {
      setSliceSaving(false);
    }
  };

  const handleSingleExportBenchmark = async () => {
    if (!currentSample) return;
    try {
      const values = await singleExportForm.validateFields();
      const res = await flywheelApi.exportBenchmark(currentSample.id, {
        dataset_id: values.dataset_id,
        name: values.name,
        mode: values.mode,
      });
      message.success(`评测集 [${res.dataset_id}] 导出成功，共 ${res.sample_count} 道切题，已在数据资产中心注册！`);
      setExportSingleBenchmarkVisible(false);
    } catch (e: any) {
      if (e.errorFields) return;
      message.error(`导出评测集失败: ${e.response?.data?.detail || e.message}`);
    }
  };

  const handleBatchExportBenchmarkSubmit = async () => {
    try {
      const values = await batchExportBenchmarkForm.validateFields();
      setBatchActionLoading(true);
      const res = await flywheelApi.batchExportBenchmark({
        sample_ids: selectedRowKeys as string[],
        dataset_id: values.dataset_id,
        name: values.name,
        mode: values.mode,
        strategy: values.strategy,
      });
      message.success(`批量导出成功！共导出 ${res.sample_count} 道考题至基准评测集 [${res.dataset_id}]`);
      setBatchExportBenchmarkModalVisible(false);
      setSelectedRowKeys([]);
    } catch (e: any) {
      if (e.errorFields) return;
      message.error(`批量导出失败: ${e.response?.data?.detail || e.message}`);
    } finally {
      setBatchActionLoading(false);
    }
  };

  const handleUpdateStatus = async (status: FlywheelStatus) => {
    if (!currentSample) return;
    try {
      await flywheelApi.updateSample(currentSample.id, {
        status,
        remediated_messages: remediatedMessages,
        reviewer_comment: reviewerComment,
      });
      message.success(`样本已更新为 ${status}`);
      setReviewDrawerVisible(false);
      loadData();
    } catch (e: any) {
      message.error(`更新失败: ${e.message}`);
    }
  };

  const handleCancelRemediate = () => {
    if (remediateAbortRef.current) {
      remediateAbortRef.current.abort();
      remediateAbortRef.current = null;
      message.info({ content: '已取消数据重新修复', key: 'remediate', duration: 2 });
    }
    setRemediateLoading(false);
    setRemediateModalVisible(false);
  };

  const notifyDowngradeNotices = (notices?: string[]) => {
    if (!notices || notices.length === 0) return;
    notification.warning({
      message: '模型思考深度已自动降级重试',
      description: notices.join('\n'),
      duration: 8,
    });
  };

  const checkRemediableEval = (
    evalRep?: FlywheelSampleRecord['initial_eval'] | null
  ): { remediable: boolean; reason: string } => {
    if (!evalRep || (evalRep.score === undefined && !evalRep.verdict)) {
      return {
        remediable: false,
        reason: '当前样本尚未执行质检，暂无异常诊断可修复，请先重新质检或开启「修复前重新执行质检」。',
      };
    }
    const substantiveDefects = (evalRep.detected_defects || [])
      .map((d) => String(d).trim())
      .filter((d) => d && !d.startsWith('【拓扑检测元数据】'));
    const hasDeductions = Array.isArray(evalRep.deductions) && evalRep.deductions.length > 0;
    const judgeErrorPrefixes = [
      '模型未按标准 JSON 输出质检报告',
      '质检裁判模型调用异常',
      '模型返回的 JSON 缺少',
    ];
    const hasMainJudgeError = (evalRep.debug_requests || []).some(
      (dbg) =>
        dbg?.step === 'main_judge' &&
        (Boolean(dbg?.error) ||
          (!String(dbg?.response_content || '').trim() &&
            !String(dbg?.raw_output || '').trim()))
    );
    const hasErrorDefect = substantiveDefects.some((d) =>
      judgeErrorPrefixes.some((p) => d.startsWith(p))
    );
    if (hasMainJudgeError || hasErrorDefect) {
      return {
        remediable: false,
        reason:
          '当前质检结果异常或输出被截断，未产生有效缺陷诊断，无法直接修复；请先重新质检（或开启「修复前重新执行质检」）。',
      };
    }
    if (!hasDeductions && substantiveDefects.length === 0) {
      const hasOnlyTopo = (evalRep.detected_defects || []).some((d) =>
        String(d).trim().startsWith('【拓扑检测元数据】')
      );
      return {
        remediable: false,
        reason: hasOnlyTopo
          ? '没有异常可以修复：当前质检无扣分项 (deductions) 且无实质业务缺陷（【拓扑检测元数据】仅供参考，不作为修复依据）。'
          : '没有异常可以修复：当前质检未检出任何扣分项 (deductions) 或实质业务缺陷。',
      };
    }
    return { remediable: true, reason: '' };
  };

  const singleEvalCheck = checkRemediableEval(currentSample?.initial_eval);

  const handleRemediateSubmit = async () => {
    if (!currentSample) return;
    let controller: AbortController | null = null;
    try {
      const values = await remediateForm.validateFields();
      const runInitial = Boolean(values.run_initial_judge);
      const skipIfGoldPass = Boolean(values.skip_if_gold_pass);
      const runVerified = Boolean(values.run_verified_judge);
      if (!runInitial) {
        const chk = checkRemediableEval(currentSample.initial_eval);
        if (!chk.remediable) {
          message.warning({ content: chk.reason, key: 'remediate', duration: 5 });
          return;
        }
      }
      persistFlywheelModels(values.judge_model_id, values.remediate_model_id);
      controller = new AbortController();
      remediateAbortRef.current = controller;
      setRemediateLoading(true);
      message.loading({
        content: runInitial
          ? '正在先重新执行质检，再依据最新诊断修复话术...'
          : runVerified
          ? '正在调用修复模型重塑客服话术并执行二次验收...'
          : '正在直接使用当前质检结果调用修复模型重塑话术...',
        key: 'remediate',
        duration: 0,
      });
      const res = await flywheelApi.remediateSample(
        currentSample.id,
        {
          rule_id: values.rule_id || currentSample.rule_id,
          judge_model_id: runInitial || runVerified ? values.judge_model_id : undefined,
          remediate_model_id: values.remediate_model_id,
          run_initial_judge: runInitial,
          skip_if_gold_pass: skipIfGoldPass,
          run_verified_judge: runVerified,
        },
        controller.signal
      );
      if (controller.signal.aborted) return;
      if (res.skipped_remediation) {
        message.warning({
          content: res.skip_reason || res.message || '没有异常可以修复',
          key: 'remediate',
          duration: 5,
        });
      } else {
        message.success({
          content: `数据重新修复完成: 当前得分 ${res.score} · 状态 ${res.status}`,
          key: 'remediate',
          duration: 3,
        });
      }
      notifyDowngradeNotices(res.notices);
      setRemediateModalVisible(false);

      // 原地更新抽屉内样本数据，保留抽屉让用户即时查阅修复效果
      const updatedSample: FlywheelSampleRecord = {
        ...currentSample,
        rule_id: res.rule_id || currentSample.rule_id,
        status: res.status as FlywheelStatus,
        score: res.score,
        remediated_messages: res.remediated_messages || currentSample.remediated_messages,
        initial_eval: res.initial_eval || currentSample.initial_eval,
        verified_eval: res.verified_eval ?? null,
        remediation_debug: res.status === 'GOLD_PASS' ? null : (res.remediation_debug ?? null),
      };
      setCurrentSample(updatedSample);
      if (res.remediated_messages) {
        setRemediatedMessages(res.remediated_messages);
      }
      if (Array.isArray(res.benchmark_slices)) {
        setCurrentSlices(res.benchmark_slices);
      }
      loadData();
    } catch (e: any) {
      if (e?.name === 'CanceledError' || e?.name === 'AbortError' || e?.code === 'ERR_CANCELED') {
        return;
      }
      const errDetail = e?.response?.data?.detail || e.message;
      message.error({ content: `数据重新修复失败: ${errDetail}`, key: 'remediate', duration: 5 });
    } finally {
      if (controller && remediateAbortRef.current === controller) {
        remediateAbortRef.current = null;
      }
      setRemediateLoading(false);
    }
  };

  const handleCancelSingleJudge = () => {
    if (singleJudgeAbortRef.current) {
      singleJudgeAbortRef.current.abort();
      singleJudgeAbortRef.current = null;
      message.info({ content: '已取消重新质检', key: 'judge', duration: 2 });
    }
    setSingleJudgeLoading(false);
    setSingleJudgeModalVisible(false);
  };

  const handleSingleJudgeSubmit = async () => {
    if (!currentSample) return;
    let controller: AbortController | null = null;
    try {
      const values = await singleJudgeForm.validateFields();
      persistFlywheelModels(values.judge_model_id, undefined);
      const targetData = values.target_data || 'raw';
      controller = new AbortController();
      singleJudgeAbortRef.current = controller;
      setSingleJudgeLoading(true);
      message.loading({
        content: `正在对【${targetData === 'remediated' ? '当前修复语料' : '原实战会话'}】执行白盒质检 (只检不修)...`,
        key: 'judge',
        duration: 0,
      });
      const res = await flywheelApi.judgeSample(
        currentSample.id,
        {
          rule_id: values.rule_id,
          judge_model_id: values.judge_model_id,
          target_data: targetData,
        },
        controller.signal
      );
      if (controller.signal.aborted) return;
      message.success({
        content: `${res.message}: 得分 ${res.score} · 状态 ${res.status}`,
        key: 'judge',
        duration: 4,
      });
      notifyDowngradeNotices(res.notices);
      setSingleJudgeModalVisible(false);

      // 原地刷新抽屉内的诊断报告与得分，保留抽屉让用户立即核验
      const updatedSample: FlywheelSampleRecord = {
        ...currentSample,
        rule_id: res.rule_id || values.rule_id,
        status: res.status as FlywheelStatus,
        score: res.score,
        initial_eval: targetData === 'raw' && res.initial_eval ? res.initial_eval : currentSample.initial_eval,
        verified_eval: targetData === 'remediated' && res.verified_eval ? res.verified_eval : currentSample.verified_eval,
      };
      setCurrentSample(updatedSample);
      loadData();
    } catch (e: any) {
      if (e?.name === 'CanceledError' || e?.name === 'AbortError' || e?.code === 'ERR_CANCELED') {
        return;
      }
      message.error({ content: `重新质检失败: ${e.message}`, key: 'judge', duration: 4 });
    } finally {
      if (controller && singleJudgeAbortRef.current === controller) {
        singleJudgeAbortRef.current = null;
      }
      setSingleJudgeLoading(false);
    }
  };

  const handleExportSubmit = async () => {
    try {
      const values = await exportForm.validateFields();
      setExportLoading(true);
      const res = await flywheelApi.exportGold({
        rule_id: values.rule_id,
        version_tag: values.version_tag || 'v1.0',
        include_manual_approved: values.include_manual_approved ?? true,
        inject_thinking: values.inject_thinking ?? false,
        exclude_benchmark_exported: values.exclude_benchmark_exported ?? true,
      });
      message.success(`成功导出 ${res.sample_count} 条达标黄金语料至 ${res.file_path}`);
      setExportModalVisible(false);
    } catch (e: any) {
      message.error(`导出失败: ${e.message}`);
    } finally {
      setExportLoading(false);
    }
  };

  const handleBatchSubmit = async () => {
    try {
      const values = await batchForm.validateFields();
      persistFlywheelModels(values.judge_model_id, values.remediate_model_id);
      await flywheelApi.batch({
        rule_id: values.rule_id,
        input_file_path: values.input_file_path,
        judge_model_id: values.judge_model_id,
        remediate_model_id: values.remediate_model_id || undefined,
        workers: values.workers || 4,
      });
      message.success('存量数据集批量飞轮治理任务已提交');
      setBatchModalVisible(false);
    } catch (e: any) {
      message.error(`提交失败: ${e.message}`);
    }
  };

  // ========================================================
  // 批量管理操作处理器
  // ========================================================

  const openBatchStatusModal = (status: FlywheelStatus) => {
    setTargetBatchStatus(status);
    batchStatusForm.resetFields();
    setBatchStatusModalVisible(true);
  };

  const handleBatchStatusSubmit = async () => {
    if (selectedRowKeys.length === 0) return;
    try {
      const values = await batchStatusForm.validateFields();
      setBatchActionLoading(true);
      const res = await flywheelApi.batchUpdateStatus({
        sample_ids: selectedRowKeys as string[],
        status: targetBatchStatus,
        reviewer_comment: values.reviewer_comment || undefined,
      });
      message.success(res.message);
      setBatchStatusModalVisible(false);
      setSelectedRowKeys([]);
      loadData();
    } catch (e: any) {
      message.error(`批量修改状态失败: ${e.message}`);
    } finally {
      setBatchActionLoading(false);
    }
  };

  const handleBatchDeleteSubmit = async () => {
    if (selectedRowKeys.length === 0) return;
    try {
      setBatchActionLoading(true);
      const res = await flywheelApi.batchDeleteSamples(selectedRowKeys as string[]);
      message.success(res.message);
      setSelectedRowKeys([]);
      loadData();
    } catch (e: any) {
      message.error(`批量删除失败: ${e.message}`);
    } finally {
      setBatchActionLoading(false);
    }
  };

  const openBatchRemediateModal = () => {
    batchRemediateForm.resetFields();
    const defaultRule = selectedRuleFilter !== 'ALL' ? selectedRuleFilter : (rules[0]?.rule_id || '');
    batchRemediateForm.setFieldsValue({
      rule_id: defaultRule,
      judge_model_id: resolvePreferredModelId('judge'),
      remediate_model_id: resolvePreferredModelId('remediate'),
      run_initial_judge: false,
      skip_if_gold_pass: false,
      run_verified_judge: false,
    });
    setBatchRemediateModalVisible(true);
  };

  const handleBatchRemediateSubmit = async () => {
    if (selectedRowKeys.length === 0) return;
    try {
      const values = await batchRemediateForm.validateFields();
      const runInitial = Boolean(values.run_initial_judge);
      const skipIfGoldPass = Boolean(values.skip_if_gold_pass);
      const runVerified = Boolean(values.run_verified_judge);
      persistFlywheelModels(values.judge_model_id, values.remediate_model_id);
      setBatchActionLoading(true);
      message.loading({
        content: `正在对选中的 ${selectedRowKeys.length} 条样本执行批量修复...`,
        key: 'batch_remediate',
      });
      const res = await flywheelApi.batchRemediateSamples({
        sample_ids: selectedRowKeys as string[],
        rule_id: values.rule_id,
        judge_model_id: runInitial || runVerified ? values.judge_model_id : undefined,
        remediate_model_id: values.remediate_model_id,
        run_initial_judge: runInitial,
        skip_if_gold_pass: skipIfGoldPass,
        run_verified_judge: runVerified,
      });
      const skippedCount = (res.results || []).filter((r: any) => r?.skipped).length;
      if (res.succeeded === 0 && skippedCount > 0) {
        const firstErr = (res.results || []).find((r: any) => r?.error)?.error || '所选样本均无异常可修复或质检结果异常';
        message.warning({
          content: `批量修复已跳过 ${skippedCount} 条（${firstErr}）`,
          key: 'batch_remediate',
          duration: 5,
        });
      } else {
        message.success({
          content:
            skippedCount > 0
              ? `批量修复完成: 共 ${res.total} 条，修复 ${res.succeeded} 条，跳过无异常/质检异常 ${skippedCount} 条`
              : `批量修复完成: 共 ${res.total} 条，成功完成 ${res.succeeded} 条`,
          key: 'batch_remediate',
          duration: 4,
        });
      }
      notifyDowngradeNotices(res.notices);
      setBatchRemediateModalVisible(false);
      setSelectedRowKeys([]);
      loadData();
    } catch (e: any) {
      message.error({
        content: `批量补考失败: ${e.message}`,
        key: 'batch_remediate',
      });
    } finally {
      setBatchActionLoading(false);
    }
  };

  const openBatchJudgeModal = () => {
    batchJudgeForm.resetFields();
    const defaultRule = selectedRuleFilter !== 'ALL' ? selectedRuleFilter : (rules[0]?.rule_id || '');
    batchJudgeForm.setFieldsValue({
      rule_id: defaultRule,
      judge_model_id: resolvePreferredModelId('judge'),
    });
    setBatchJudgeModalVisible(true);
  };

  const handleBatchJudgeSubmit = async () => {
    if (selectedRowKeys.length === 0) return;
    try {
      const values = await batchJudgeForm.validateFields();
      persistFlywheelModels(values.judge_model_id, undefined);
      setBatchActionLoading(true);
      message.loading({
        content: `正在对选中的 ${selectedRowKeys.length} 条样本使用新标准重新质检...`,
        key: 'batch_judge',
      });
      const res = await flywheelApi.batchJudgeSamples({
        sample_ids: selectedRowKeys as string[],
        rule_id: values.rule_id,
        judge_model_id: values.judge_model_id,
      });
      message.success({
        content: `批量重新质检完成: 共 ${res.total} 条，成功达标/通过 ${res.succeeded} 条`,
        key: 'batch_judge',
        duration: 4,
      });
      notifyDowngradeNotices(res.notices);
      setBatchJudgeModalVisible(false);
      setSelectedRowKeys([]);
      loadData();
    } catch (e: any) {
      message.error({
        content: `批量重新质检失败: ${e.message}`,
        key: 'batch_judge',
      });
    } finally {
      setBatchActionLoading(false);
    }
  };

  const openBatchExportSelectedModal = () => {
    batchExportSelectedForm.resetFields();
    batchExportSelectedForm.setFieldsValue({
      version_tag: 'selected-v1.0',
      inject_thinking: false,
    });
    setBatchExportSelectedModalVisible(true);
  };

  const handleBatchExportSelectedSubmit = async () => {
    if (selectedRowKeys.length === 0) return;
    try {
      const values = await batchExportSelectedForm.validateFields();
      setBatchActionLoading(true);
      const res = await flywheelApi.exportSelectedSamples({
        sample_ids: selectedRowKeys as string[],
        version_tag: values.version_tag || 'selected',
        inject_thinking: values.inject_thinking ?? false,
      });
      message.success(`成功导出 ${res.sample_count} 条选中样本至: ${res.file_path}`);
      setBatchExportSelectedModalVisible(false);
      setSelectedRowKeys([]);
    } catch (e: any) {
      message.error(`批量导出失败: ${e.message}`);
    } finally {
      setBatchActionLoading(false);
    }
  };

  const getStatusTag = (status: string) => {
    switch (status) {
      case 'GOLD_PASS':
        return <Tag color="green">及格直通 (GOLD_PASS)</Tag>;
      case 'REPAIRED_PASS':
        return <Tag color="blue">修复达标 (REPAIRED_PASS)</Tag>;
      case 'MANUAL_APPROVED':
        return <Tag color="purple">人工放行 (MANUAL_APPROVED)</Tag>;
      case 'DISCARDED':
        return <Tag color="red">淘汰隔离 (DISCARDED)</Tag>;
      case 'PENDING':
      default:
        return <Tag color="orange">待复核 (PENDING)</Tag>;
    }
  };

  return (
    <div className="space-y-4">
      {/* 顶部操作与筛选栏 */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 bg-white p-4 rounded-lg border border-slate-200 shadow-2xs">
        <div className="flex items-center gap-3 flex-wrap">
          <span className="text-sm font-medium text-slate-700">状态过滤:</span>
          <Radio.Group
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            size="small"
            buttonStyle="solid"
          >
            <Radio.Button value="ALL">全部</Radio.Button>
            <Radio.Button value="PENDING">待复核</Radio.Button>
            <Radio.Button value="GOLD_PASS">直通达标</Radio.Button>
            <Radio.Button value="REPAIRED_PASS">修复合格</Radio.Button>
            <Radio.Button value="MANUAL_APPROVED">人工放行</Radio.Button>
            <Radio.Button value="DISCARDED">淘汰库</Radio.Button>
          </Radio.Group>

          <Select
            value={selectedRuleFilter}
            onChange={(val) => setSelectedRuleFilter(val)}
            className="w-44"
            size="small"
            popupMatchSelectWidth={false}
            options={[
              { value: 'ALL', label: '全部场景规则' },
              ...rules.map((r) => ({ value: r.rule_id, label: r.project_name })),
            ]}
          />

          <Button size="small" icon={<ReloadOutlined />} onClick={() => loadData()}>
            刷新
          </Button>

          <span className="text-xs text-slate-400">
            共载入 <strong className="text-slate-600">{samples.length}</strong> 条样本
          </span>
        </div>

        <Space size="middle">
          <Button
            icon={<ExperimentOutlined />}
            onClick={() => {
              batchForm.resetFields();
              const defaultRule =
                selectedRuleFilter !== 'ALL' ? selectedRuleFilter : rules[0]?.rule_id || '';
              batchForm.setFieldsValue({
                rule_id: defaultRule,
                judge_model_id: resolvePreferredModelId('judge'),
                remediate_model_id: resolvePreferredModelId('remediate'),
              });
              setBatchModalVisible(true);
            }}
          >
            存量语料批量治理
          </Button>

          <Button
            type="primary"
            icon={<ExportOutlined />}
            onClick={() => {
              exportForm.resetFields();
              if (rules.length > 0) {
                exportForm.setFieldsValue({ rule_id: rules[0].rule_id, version_tag: 'v1.0' });
              }
              setExportModalVisible(true);
            }}
          >
            导出黄金微调训练集
          </Button>
        </Space>
      </div>

      {/* 批量管理操作浮动操作条 (当勾选样本时激活显示) */}
      {selectedRowKeys.length > 0 && (
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 bg-gradient-to-r from-blue-50/90 to-indigo-50/90 border border-indigo-200 p-3 rounded-lg shadow-xs">
          <div className="flex items-center gap-3">
            <Badge count={selectedRowKeys.length} overflowCount={999} style={{ backgroundColor: '#4f46e5' }} />
            <span className="text-sm font-semibold text-indigo-950">
              已选中 <span className="font-mono text-indigo-700 text-base">{selectedRowKeys.length}</span> 条样本
            </span>
            <Button
              type="link"
              size="small"
              onClick={() => setSelectedRowKeys([])}
              className="text-xs text-slate-500 hover:text-slate-700 p-0"
            >
              清空选择
            </Button>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            {/* 批量修改状态下拉 */}
            <Dropdown
              menu={{
                items: [
                  {
                    key: 'GOLD_PASS',
                    icon: <CheckOutlined className="text-emerald-600" />,
                    label: '批量标定为黄金样本 (GOLD_PASS)',
                    onClick: () => openBatchStatusModal('GOLD_PASS'),
                  },
                  {
                    key: 'MANUAL_APPROVED',
                    icon: <CheckOutlined className="text-purple-600" />,
                    label: '批量人工放行 (MANUAL_APPROVED)',
                    onClick: () => openBatchStatusModal('MANUAL_APPROVED'),
                  },
                  {
                    key: 'DISCARDED',
                    icon: <StopOutlined className="text-red-600" />,
                    label: '批量打入淘汰库 (DISCARDED)',
                    onClick: () => openBatchStatusModal('DISCARDED'),
                  },
                ],
              }}
            >
              <Button size="small" icon={<SafetyOutlined />} loading={batchActionLoading}>
                批量流转状态 <DownOutlined className="text-3xs" />
              </Button>
            </Dropdown>

            {/* 批量重新质检 (按新规则重新校验客观与白盒标准) */}
            <Button
              size="small"
              type="primary"
              ghost
              icon={<AuditOutlined />}
              onClick={openBatchJudgeModal}
              loading={batchActionLoading}
              className="border-indigo-400 text-indigo-700 hover:text-indigo-800"
            >
              按新标准重新质检
            </Button>

            {/* 批量重新修复数据 */}
            <Button
              size="small"
              type="primary"
              ghost
              icon={<SyncOutlined />}
              onClick={openBatchRemediateModal}
              loading={batchActionLoading}
            >
              批量重新修复数据
            </Button>

            {/* 批量导出选定样本 (SFT 对话) */}
            <Button
              size="small"
              icon={<ExportOutlined />}
              onClick={openBatchExportSelectedModal}
              loading={batchActionLoading}
            >
              导出 SFT 语料
            </Button>

            {/* 批量导出为 Benchmark 评测集 */}
            <Button
              size="small"
              type="primary"
              icon={<RocketOutlined />}
              onClick={() => {
                batchExportBenchmarkForm.resetFields();
                const nowStr = new Date().toISOString().slice(0, 10).replace(/-/g, '');
                batchExportBenchmarkForm.setFieldsValue({
                  dataset_id: `flywheel_eval_${nowStr}`,
                  name: `飞轮质检基准评测集 (${selectedRowKeys.length}条样本)`,
                  strategy: 'all_defects',
                  mode: 'create',
                });
                setBatchExportBenchmarkModalVisible(true);
              }}
              loading={batchActionLoading}
            >
              批量生成评测集
            </Button>

            {/* 批量删除 */}
            <Popconfirm
              title={`确定彻底删除选中的 ${selectedRowKeys.length} 条样本？`}
              description="删除后无法恢复，数据将从 SQLite 数据库中永久抹除。"
              onConfirm={handleBatchDeleteSubmit}
              okText="确定删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
            >
              <Button size="small" danger icon={<DeleteOutlined />} loading={batchActionLoading}>
                批量删除
              </Button>
            </Popconfirm>
          </div>
        </div>
      )}

      {/* 飞轮样本复核列表 */}
      <div className="bg-white rounded-lg border border-slate-200 shadow-2xs p-4">
        <Table
          rowKey="id"
          loading={loading}
          dataSource={samples}
          rowSelection={{
            selectedRowKeys,
            onChange: (newSelectedRowKeys) => setSelectedRowKeys(newSelectedRowKeys),
            selections: [
              Table.SELECTION_ALL,
              Table.SELECTION_INVERT,
              Table.SELECTION_NONE,
              {
                key: 'select_all_data',
                text: `跨页全选所有 (${samples.length} 条)`,
                onSelect: () => setSelectedRowKeys(samples.map((s) => s.id)),
              },
            ],
          }}
          pagination={{
            current: flywheelPagination.current,
            pageSize: flywheelPagination.pageSize,
            showSizeChanger: true,
            pageSizeOptions: ['10', '15', '20', '30', '50', '100'],
            showTotal: (total) => `共 ${total} 条样本 (已勾选 ${selectedRowKeys.length} 条)`,
            onChange: (page, pageSize) => {
              setFlywheelPagination({ current: page, pageSize });
            },
          }}
          size="middle"
          columns={[
            {
              title: '样本标识 (ID)',
              dataIndex: 'id',
              width: 190,
              render: (id) => <span className="font-mono text-xs font-semibold">{id}</span>,
            },
            {
              title: '规则工程',
              dataIndex: 'rule_id',
              width: 140,
              render: (rid) => <Tag color="blue">{rid}</Tag>,
            },
            {
              title: '剧本场景',
              dataIndex: 'scenario_id',
              width: 130,
              render: (sc) => <span className="text-xs text-slate-600">{sc}</span>,
            },
            {
              title: '综合得分',
              dataIndex: 'score',
              width: 100,
              render: (score: number) => (
                <span className={`font-bold ${score >= 85 ? 'text-emerald-600' : 'text-amber-600'}`}>
                  {score} 分
                </span>
              ),
            },
            {
              title: '治理状态',
              dataIndex: 'status',
              width: 220,
              render: (status, record) => (
                <Space size={4} wrap>
                  {getStatusTag(status)}
                  {record.exported_to_benchmark && (
                    <Tag color="purple" className="m-0 text-3xs">
                      🔒 已进评测集
                    </Tag>
                  )}
                </Space>
              ),
            },
            {
              title: '质检缺陷简报',
              render: (_, record) => {
                const evalData = record.verified_eval || record.initial_eval;
                if (evalData?.deductions && evalData.deductions.length > 0) {
                  const d0 = evalData.deductions[0];
                  const d0Text =
                    typeof d0 === 'string'
                      ? d0
                      : `[-${d0.points ?? 0}分] ${d0.item ? `[${d0.item}] ` : ''}${d0.reason || ''}`;
                  const allText = evalData.deductions
                    .map((d: any) =>
                      typeof d === 'string' ? d : `[-${d.points ?? 0}分] ${d.item ? `[${d.item}] ` : ''}${d.reason || ''}`
                    )
                    .join('； ');
                  return (
                    <span className="text-xs text-red-600 truncate block max-w-xs" title={allText}>
                      {d0Text} {evalData.deductions.length > 1 ? `等(${evalData.deductions.length})` : ''}
                    </span>
                  );
                }
                const defects = evalData?.detected_defects || [];
                const onlyTopoNotice =
                  defects.length > 0 &&
                  defects.every((d: string) => String(d).trim().startsWith('【拓扑检测元数据】'));
                return defects.length > 0 ? (
                  <span
                    className={`text-xs ${onlyTopoNotice ? 'text-amber-600' : 'text-red-600'} truncate block max-w-xs`}
                    title={defects.join('; ')}
                  >
                    {defects[0]} {defects.length > 1 ? `等(${defects.length})` : ''}
                  </span>
                ) : (
                  <span className="text-xs text-emerald-600">无缺陷</span>
                );
              },
            },
            {
              title: '复核操作',
              width: 190,
              render: (_, record) => (
                <Space size="small">
                  <Button type="primary" size="small" ghost onClick={() => openReviewDrawer(record)}>
                    深度复核
                  </Button>
                  <Button
                    size="small"
                    icon={<AuditOutlined />}
                    onClick={() => {
                      setCurrentSample(record);
                      singleJudgeForm.resetFields();
                      singleJudgeForm.setFieldsValue({
                        rule_id: record.rule_id || (rules[0]?.rule_id || ''),
                        judge_model_id: models[0]?.id || '',
                      });
                      setSingleJudgeModalVisible(true);
                    }}
                  >
                    重新质检
                  </Button>
                  <Popconfirm
                    title="确定删除此条复核样本？"
                    description="删除后无法恢复，将从 SQLite 数据库中移除。"
                    onConfirm={async () => {
                      try {
                        await flywheelApi.deleteSample(record.id);
                        message.success('样本已成功删除');
                        loadData();
                      } catch (e: any) {
                        message.error(`删除失败: ${e.message}`);
                      }
                    }}
                    okText="删除"
                    cancelText="取消"
                  >
                    <Button size="small" danger type="text" icon={<DeleteOutlined />} />
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </div>

      {/* 批量状态流转与评审备注模态框 */}
      <Modal
        title={`批量流转样本状态 (${selectedRowKeys.length} 条已选)`}
        open={batchStatusModalVisible}
        onOk={handleBatchStatusSubmit}
        confirmLoading={batchActionLoading}
        onCancel={() => setBatchStatusModalVisible(false)}
        okText="确认批量修改"
        cancelText="取消"
      >
        <div className="py-2 space-y-3">
          <div className="flex items-center gap-2">
            <span className="text-sm text-slate-600">目标治理状态:</span>
            {getStatusTag(targetBatchStatus)}
          </div>
          <Form form={batchStatusForm} layout="vertical">
            <Form.Item name="reviewer_comment" label="批量复核说明 / 专家评审意见 (可选)">
              <Input.TextArea
                rows={3}
                placeholder="例如：经人工抽检复核，话术符合业务要求予以放行"
              />
            </Form.Item>
          </Form>
        </div>
      </Modal>

      {/* 批量重新质检模态框 */}
      <Modal
        title={`批量按新标准重新质检 (${selectedRowKeys.length} 条样本)`}
        open={batchJudgeModalVisible}
        onOk={handleBatchJudgeSubmit}
        confirmLoading={batchActionLoading}
        onCancel={() => setBatchJudgeModalVisible(false)}
        okText="开始重新质检"
        cancelText="取消"
      >
        <div className="py-2 space-y-3">
          <Alert
            type="info"
            showIcon
            message="💡 批量质检机制说明 (只检不修)"
            description={
              <div className="text-xs space-y-1 text-slate-600">
                <p>1. <strong>评估对象</strong>：系统将针对选中样本的【原实战会话】进行全新白盒初检，评估在新标准下能否免除修复直接达标。</p>
                <p>2. <strong>只检不修</strong>：质检仅输出客观扣分诊断，<strong>不会自动修改任何话术</strong>。达标者晋升为黄金样本 (GOLD_PASS)；未达标者仅刷新缺陷诊断。</p>
                <p>3. <strong>批量修复</strong>：若需对未达标样本进行批量话术重塑，请在操作栏选择【批量定点补考】。</p>
              </div>
            }
          />
          <Form form={batchJudgeForm} layout="vertical">
            <Form.Item name="rule_id" label="重新质检规则标准" rules={[{ required: true, message: '请选择规则标准' }]}>
              <Select popupMatchSelectWidth={false} options={rules.map((r) => ({ value: r.rule_id, label: r.project_name }))} />
            </Form.Item>
            <Form.Item name="judge_model_id" label="质检法官模型" rules={[{ required: true, message: '请选择质检模型' }]}>
              <Select popupMatchSelectWidth={false} options={models.map((m) => ({ value: m.id, label: m.name }))} />
            </Form.Item>
          </Form>
        </div>
      </Modal>

      {/* 批量定点补考模态框 */}
      <Modal
        title={`批量定点修复 (${selectedRowKeys.length} 条样本)`}
        open={batchRemediateModalVisible}
        onOk={handleBatchRemediateSubmit}
        confirmLoading={batchActionLoading}
        onCancel={() => setBatchRemediateModalVisible(false)}
        okText="开始批量修复"
        cancelText="取消"
      >
        <div className="py-2 space-y-3">
          <Alert
            type="info"
            showIcon
            message="⚡ 批量修复执行策略"
            description="默认直接复用各样本当前已有的质检诊断执行修复（不重跑初检、满分不自动跳过、修复后不自动二次质检），可按需开启下列选项。"
          />
          <Form form={batchRemediateForm} layout="vertical">
            <Form.Item name="rule_id" label="修复标准规则" rules={[{ required: true, message: '请选择规则标准' }]}>
              <Select popupMatchSelectWidth={false} options={rules.map((r) => ({ value: r.rule_id, label: r.project_name }))} />
            </Form.Item>
            <Form.Item name="remediate_model_id" label="定向修复 Teacher 模型" rules={[{ required: true, message: '请选择修复模型' }]}>
              <Select popupMatchSelectWidth={false} options={models.map((m) => ({ value: m.id, label: m.name }))} />
            </Form.Item>
            <div className="bg-slate-50 border border-slate-200 rounded-md p-3 mb-3 space-y-2">
              <Form.Item
                name="run_initial_judge"
                label="修复前重新执行初检"
                valuePropName="checked"
                initialValue={false}
                extra="默认关闭：直接使用当前已有质检结果作为修复依据"
                className="mb-2"
              >
                <Switch />
              </Form.Item>
              <Form.Item
                name="skip_if_gold_pass"
                label="初检满分/达标时免修直接通过 (GOLD_PASS)"
                valuePropName="checked"
                initialValue={false}
                extra="默认关闭：即使当前为满分，手动修复也不跳过直接通过"
                className="mb-2"
              >
                <Switch />
              </Form.Item>
              <Form.Item
                name="run_verified_judge"
                label="修复后自动继续二次质检验收"
                valuePropName="checked"
                initialValue={false}
                extra="默认关闭：修复完成后暂标为待复核 (PENDING)，不自动质检"
                className="mb-0"
              >
                <Switch />
              </Form.Item>
            </div>
            {(watchBatchRunInitial || watchBatchRunVerified) && (
              <Form.Item name="judge_model_id" label="质检法官模型" rules={[{ required: true, message: '已开启质检选项，请选择质检模型' }]}>
                <Select popupMatchSelectWidth={false} options={models.map((m) => ({ value: m.id, label: m.name }))} />
              </Form.Item>
            )}
          </Form>
        </div>
      </Modal>

      {/* 批量导出选定样本模态框 */}
      <Modal
        title={`导出选中的样本为训练集 (${selectedRowKeys.length} 条)`}
        open={batchExportSelectedModalVisible}
        onOk={handleBatchExportSelectedSubmit}
        confirmLoading={batchActionLoading}
        onCancel={() => setBatchExportSelectedModalVisible(false)}
        okText="立即导出"
        cancelText="取消"
      >
        <div className="py-2">
          <Form form={batchExportSelectedForm} layout="vertical">
            <Form.Item name="version_tag" label="数据集版本标识" initialValue="selected-v1.0" rules={[{ required: true }]}>
              <Input placeholder="例如 selected-v1.0 或 batch-exp-v1" />
            </Form.Item>
            <Form.Item name="inject_thinking" label="保留思考链 (Thinking Mode)" valuePropName="checked" initialValue={false}>
              <Switch />
            </Form.Item>
          </Form>
        </div>
      </Modal>

      {/* 深度 Diff 复核抽屉 (Dual-View Diff Editor) */}
      <Drawer
        title={
          <div className="flex items-center justify-between mr-6">
            <div className="flex items-center gap-2">
              <span className="font-bold">样本深度复核与安全修复</span>
              {currentSample && (
                <span className="text-xs font-mono text-slate-500">[{currentSample.id}]</span>
              )}
            </div>
            {currentSample && (
              <div className="flex items-center gap-2">
                <Button
                  size="small"
                  icon={<CodeOutlined />}
                  onClick={() => setMonacoModalVisible(true)}
                >
                  Monaco 源码 Diff
                </Button>
              </div>
            )}
          </div>
        }
        width={960}
        open={reviewDrawerVisible}
        onClose={() => setReviewDrawerVisible(false)}
        footer={
          <div className="flex items-center justify-between py-2">
            <div className="flex items-center gap-2 flex-1 mr-4">
              <span className="text-xs text-slate-500 shrink-0">评审备注:</span>
              <Input
                size="small"
                placeholder="填写专家评审意见或打标说明"
                value={reviewerComment}
                onChange={(e) => setReviewerComment(e.target.value)}
                className="max-w-md"
              />
            </div>

            <Space>
              <Button onClick={() => setReviewDrawerVisible(false)}>取消</Button>
              <Button
                icon={<SaveOutlined />}
                onClick={() => handleUpdateStatus('PENDING')}
              >
                保存微调 (待复核)
              </Button>
              <Button
                danger
                icon={<StopOutlined />}
                onClick={() => handleUpdateStatus('DISCARDED')}
              >
                打入淘汰库 (DISCARDED)
              </Button>
              <Button
                type="primary"
                ghost
                icon={<CheckOutlined />}
                onClick={() => handleUpdateStatus('MANUAL_APPROVED')}
              >
                人工放行 (APPROVED)
              </Button>
              <Button
                type="primary"
                icon={<CheckOutlined />}
                onClick={() => handleUpdateStatus('GOLD_PASS')}
              >
                标定为黄金样本 (GOLD_PASS)
              </Button>
            </Space>
          </div>
        }
      >
        {currentSample && (
          <div className="space-y-4">
            {/* 顶栏显式规则标准选择与即时质检/修复条 */}
            <div className="bg-indigo-50/70 border border-indigo-200 rounded-lg p-3 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 shadow-xs">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs font-semibold text-indigo-950">当前检验标准 (Rule):</span>
                <Select
                  value={currentSample.rule_id}
                  onChange={(newRuleId) => {
                    setCurrentSample({ ...currentSample, rule_id: newRuleId });
                    message.info(`已切换当前样本质检标准为: ${newRuleId}，可点击右侧按钮重新质检或修复`);
                  }}
                  className="min-w-64"
                  size="small"
                  popupMatchSelectWidth={false}
                  options={rules.map((r) => ({ value: r.rule_id, label: `${r.project_name} (${r.rule_id})` }))}
                />
                <Tag color="cyan" className="font-mono text-xs">{currentSample.scenario_id}</Tag>
              </div>

              <Space size="small">
                <Tooltip title="只检不修: 仅执行白盒拓扑连通性与红线扣分评测，可自由选择核验原实战会话或右侧修复语料，不会自动修改任何话术">
                  <Button
                    size="small"
                    type="primary"
                    ghost
                    icon={<AuditOutlined />}
                    loading={singleJudgeLoading}
                    onClick={() => {
                      singleJudgeForm.resetFields();
                      singleJudgeForm.setFieldsValue({
                        rule_id: currentSample.rule_id || (rules[0]?.rule_id || ''),
                        judge_model_id: resolvePreferredModelId('judge', currentSample),
                        target_data: 'raw',
                      });
                      setSingleJudgeModalVisible(true);
                    }}
                  >
                    按此标准重新质检
                  </Button>
                </Tooltip>
                <Tooltip title="手动触发 AI 修复: 默认直接基于当前质检结果修复（不自动重检、不自动二次验收、满分也不直接通过），可在弹窗内按需开启">
                  <Button
                    size="small"
                    type="primary"
                    danger
                    icon={<SyncOutlined />}
                    loading={remediateLoading}
                    onClick={() => {
                      remediateForm.resetFields();
                      remediateForm.setFieldsValue({
                        rule_id: currentSample.rule_id || (rules[0]?.rule_id || ''),
                        judge_model_id: resolvePreferredModelId('judge', currentSample),
                        remediate_model_id: resolvePreferredModelId('remediate', currentSample),
                        run_initial_judge: false,
                        skip_if_gold_pass: false,
                        run_verified_judge: false,
                      });
                      setRemediateModalVisible(true);
                    }}
                  >
                    重新修复数据
                  </Button>
                </Tooltip>
              </Space>
            </div>

            {/* 顶栏质检与修复行为指引条 */}
            <div className="bg-slate-50 border border-slate-200 rounded-md px-3 py-2 flex items-center justify-between text-xs text-slate-600">
              <div className="flex items-center gap-2">
                <InfoCircleOutlined className="text-blue-500 shrink-0 text-sm" />
                <span>
                  <strong>机制说明:</strong>【按标准重新质检】仅做白盒打分与扣分诊断，<span className="text-amber-700 font-semibold">只检不修</span>；【重新修复数据】默认直接复用当前质检结果执行修复（默认不重新初检、满分不跳过、修复后不自动二次质检，均可在弹窗内开启）。
                </span>
              </div>
            </div>

            {/* 抽屉双选项卡: 多轮会话 Diff 与 测评切题参考答案 */}
            <Tabs
              activeKey={activeDrawerTab}
              onChange={(k) => setActiveDrawerTab(k)}
              type="card"
              className="mt-3"
              items={[
                {
                  key: 'diff',
                  label: <span className="font-semibold text-xs">🔀 多轮会话 Diff 与微调</span>,
                  children: (
                    <div className="mt-1">
                      <TurnDiffViewer
                        originalMessages={currentSample.raw_messages}
                        remediatedMessages={remediatedMessages}
                        initialEval={currentSample.initial_eval}
                        verifiedEval={currentSample.verified_eval}
                        remediationDebug={currentSample.remediation_debug}
                        editable={true}
                        onSave={(updated) => setRemediatedMessages(updated)}
                      />
                    </div>
                  ),
                },
                {
                  key: 'slices',
                  label: (
                    <span className="font-semibold text-xs flex items-center gap-1.5">
                      <span>📝 测评切题与参考答案</span>
                      <Tag color="cyan" className="font-mono text-3xs ml-1">{currentSlices.length}</Tag>
                    </span>
                  ),
                  children: (
                    <div className="space-y-3 mt-1">
                      {/* 切题顶栏操作条 */}
                      <div className="flex items-center justify-between bg-cyan-50/70 border border-cyan-200 p-2.5 rounded-lg">
                        <div className="text-xs text-cyan-950">
                          <span className="font-bold">🎯 累积纠正考题切片 (共 {currentSlices.length} 道)</span>
                          <span className="text-3xs text-cyan-700 ml-2">
                            （依托原始序号防剪枝错位，后序考题继承前序黄金上文；保存修改将双向同步回写完整会话）
                          </span>
                        </div>
                        <Space size="small">
                          <Button
                            size="small"
                            type="primary"
                            icon={<SaveOutlined />}
                            loading={sliceSaving}
                            disabled={currentSlices.length === 0}
                            onClick={handleSaveSlices}
                          >
                            保存切片修改
                          </Button>
                          <Button
                            size="small"
                            icon={<RocketOutlined />}
                            disabled={currentSlices.length === 0}
                            onClick={() => {
                              singleExportForm.resetFields();
                              const nowStr = new Date().toISOString().slice(0, 10).replace(/-/g, '');
                              singleExportForm.setFieldsValue({
                                dataset_id: `eval_${currentSample.id}`,
                                name: `飞轮切题评测集 (${currentSample.id})`,
                                mode: 'create',
                              });
                              setExportSingleBenchmarkVisible(true);
                            }}
                          >
                            保存为评测集
                          </Button>
                        </Space>
                      </div>

                      {slicesLoading ? (
                        <div className="flex items-center justify-center py-12">
                          <Spin tip="正在生成/提取累积纠正考题切片..." />
                        </div>
                      ) : currentSlices.length === 0 ? (
                        <Alert
                          type="info"
                          showIcon
                          message="当前会话暂未切出针对性考题"
                          description="该样本可能初检直接满分直通无扣分缺陷，或本次修复未执行局部 replace 话术替换。如需切题，可点击上方【重新修复数据】。"
                        />
                      ) : (
                        <div className="space-y-4">
                          {currentSlices.map((sl, sIdx) => (
                            <Card
                              key={sl.slice_id || sIdx}
                              size="small"
                              className="border-slate-200 shadow-2xs"
                              title={
                                <div className="flex items-center justify-between flex-wrap gap-2 text-xs">
                                  <div className="flex items-center gap-1.5">
                                    <Tag color="magenta" className="font-mono font-bold">
                                      考点 #{sl.target_turn} 轮
                                    </Tag>
                                    <span className="font-bold text-slate-800">{sl.defect_item}</span>
                                  </div>
                                  <Tag color="blue">{sl.category || '飞轮实战质检'}</Tag>
                                </div>
                              }
                            >
                              <div className="space-y-3">
                                {/* 扣分事实说明 */}
                                <div className="bg-amber-50/70 border border-amber-200 rounded p-2 text-xs text-amber-900">
                                  <span className="font-semibold">⚠️ 质检扣分依据与背景: </span>
                                  <span>{sl.defect_reason}</span>
                                </div>

                                {/* 纯净考题上下文折叠面板 */}
                                <Collapse
                                  size="small"
                                  items={[
                                    {
                                      key: '1',
                                      label: (
                                        <span className="text-xs text-slate-600 font-medium">
                                          展开查看考题输入上下文 (messages: 共 {sl.messages.length} 轮，已剔除剪枝废话并继承前序修正)
                                        </span>
                                      ),
                                      children: (
                                        <div className="space-y-1.5 max-h-60 overflow-y-auto pr-1">
                                          {sl.messages.map((m, mIdx) => (
                                            <div
                                              key={mIdx}
                                              className={`p-1.5 rounded text-xs ${
                                                m.role === 'system'
                                                  ? 'bg-purple-50 text-purple-900 border border-purple-200'
                                                  : m.role === 'user'
                                                  ? 'bg-blue-50 text-blue-900 border border-blue-200'
                                                  : 'bg-emerald-50 text-emerald-900 border border-emerald-200'
                                              }`}
                                            >
                                              <span className="font-bold uppercase tracking-wider block text-3xs opacity-75">
                                                {m.role}:
                                              </span>
                                              <p className="whitespace-pre-wrap mb-0">{m.content}</p>
                                            </div>
                                          ))}
                                        </div>
                                      ),
                                    },
                                  ]}
                                />

                                {/* 标准参考答案在线编辑 */}
                                <div>
                                  <div className="text-xs font-semibold text-emerald-800 mb-1 flex items-center justify-between">
                                    <span>✅ 标准参考答案 (Ground Truth Reference，已按黄金话术微调):</span>
                                    <span className="text-3xs text-slate-400 font-normal">支持直接在线编辑改字</span>
                                  </div>
                                  <Input.TextArea
                                    rows={3}
                                    value={sl.reference}
                                    onChange={(e) => {
                                      const newText = e.target.value;
                                      setCurrentSlices((prev) =>
                                        prev.map((item, idx) =>
                                          idx === sIdx ? { ...item, reference: newText } : item
                                        )
                                      );
                                    }}
                                    className="font-mono text-xs text-slate-800 bg-emerald-50/20 border-emerald-300 focus:border-emerald-500"
                                  />
                                </div>

                                {/* 评分细则在线编辑 */}
                                <div>
                                  <div className="text-xs font-semibold text-slate-700 mb-1">
                                    📋 评分量规与细则 (Rubric):
                                  </div>
                                  <Input.TextArea
                                    rows={2}
                                    value={sl.rubric}
                                    onChange={(e) => {
                                      const newRubric = e.target.value;
                                      setCurrentSlices((prev) =>
                                        prev.map((item, idx) =>
                                          idx === sIdx ? { ...item, rubric: newRubric } : item
                                        )
                                      );
                                    }}
                                    className="text-xs font-mono"
                                  />
                                </div>

                                {/* 正反向正则约束 */}
                                <div>
                                  <div className="text-xs font-semibold text-slate-700 mb-1 flex items-center justify-between">
                                    <span>🛡️ 正向必含与反向禁含正则约束 (Regex Constraints):</span>
                                  </div>
                                  <div className="flex items-center gap-1.5 flex-wrap">
                                    {sl.regex_constraints.map((rc, rIdx) => {
                                      const isNeg = rc.startsWith('!');
                                      return (
                                        <Tag
                                          key={rIdx}
                                          color={isNeg ? 'red' : 'green'}
                                          closable
                                          onClose={() => {
                                            setCurrentSlices((prev) =>
                                              prev.map((item, idx) =>
                                                idx === sIdx
                                                  ? {
                                                      ...item,
                                                      regex_constraints: item.regex_constraints.filter(
                                                        (_, i) => i !== rIdx
                                                      ),
                                                    }
                                                  : item
                                              )
                                            );
                                          }}
                                          className="font-mono text-xs"
                                        >
                                          {isNeg ? `禁含: ${rc}` : `必含: ${rc}`}
                                        </Tag>
                                      );
                                    })}
                                    <Button
                                      size="small"
                                      type="dashed"
                                      icon={<PlusOutlined />}
                                      onClick={() => {
                                        Modal.confirm({
                                          title: '添加正则约束',
                                          content: (
                                            <div className="py-2 text-xs">
                                              <p className="text-slate-500 mb-2">输入正则模式。以 ! 开头表示反向禁含（如 !误导承诺|恐吓），普通文本表示必含正则。</p>
                                              <Input id="new-regex-input" placeholder="例如: !违规承诺|私下赔偿" className="font-mono text-xs" />
                                            </div>
                                          ),
                                          onOk: () => {
                                            const el = document.getElementById('new-regex-input') as HTMLInputElement;
                                            const val = el?.value?.trim();
                                            if (val) {
                                              setCurrentSlices((prev) =>
                                                prev.map((item, idx) =>
                                                  idx === sIdx
                                                    ? {
                                                        ...item,
                                                        regex_constraints: [...item.regex_constraints, val],
                                                      }
                                                    : item
                                                )
                                              );
                                            }
                                          },
                                        });
                                      }}
                                    >
                                      添加正则
                                    </Button>
                                  </div>
                                </div>
                              </div>
                            </Card>
                          ))}
                        </div>
                      )}
                    </div>
                  ),
                },
              ]}
            />
          </div>
        )}
      </Drawer>

      {/* Monaco 双栏源码 Diff 模态框 */}
      {currentSample && (
        <MonacoDiffModal
          visible={monacoModalVisible}
          onClose={() => setMonacoModalVisible(false)}
          originalMessages={currentSample.raw_messages}
          remediatedMessages={remediatedMessages}
          onSave={(updated) => {
            setRemediatedMessages(updated);
            message.success('Monaco 源码编辑已同步至当前复核样本');
          }}
        />
      )}

      {/* 批量存量语料治理模态框 (原功能) */}
      <Modal
        title="存量多轮对弈语料批量飞轮治理"
        open={batchModalVisible}
        onOk={handleBatchSubmit}
        onCancel={() => setBatchModalVisible(false)}
        okText="提交批量治理"
        cancelText="取消"
      >
        <Form form={batchForm} layout="vertical" className="mt-3">
          <Form.Item name="rule_id" label="场景规则配置" rules={[{ required: true }]}>
            <Select
              popupMatchSelectWidth={false}
              options={rules.map((r) => ({ value: r.rule_id, label: r.project_name }))}
            />
          </Form.Item>

          <Form.Item
            name="input_file_path"
            label="存量待治理 JSONL 文件路径"
            initialValue="data/datasets/demo_benchmark.jsonl"
            rules={[{ required: true }]}
          >
            <Input />
          </Form.Item>

          <Form.Item name="judge_model_id" label="白盒质检裁判模型" rules={[{ required: true }]}>
            <Select popupMatchSelectWidth={false} options={models.map((m) => ({ value: m.id, label: m.name }))} />
          </Form.Item>

          <Form.Item name="remediate_model_id" label="AI定向修复模型 (留空同质检)">
            <Select popupMatchSelectWidth={false} allowClear options={models.map((m) => ({ value: m.id, label: m.name }))} />
          </Form.Item>

          <Form.Item name="workers" label="并发质检 Worker 数量" initialValue={4}>
            <InputNumber min={1} max={16} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 单条重新质检模态框 */}
      <Modal
        title="按指定标准重新质检 (白盒拓扑 + 红线核验)"
        open={singleJudgeModalVisible}
        onOk={handleSingleJudgeSubmit}
        confirmLoading={singleJudgeLoading}
        maskClosable={!singleJudgeLoading}
        onCancel={handleCancelSingleJudge}
        okText={singleJudgeLoading ? '正在核验打分中...' : '立即重新质检'}
        cancelText={singleJudgeLoading ? '中止并关闭' : '取消'}
      >
        <div className="py-2">
          {singleJudgeLoading ? (
            <div className="py-8 text-center space-y-3">
              <Spin size="large" />
              <p className="text-sm text-indigo-700 font-medium">大模型法官正在按照选定规则标准深入核验拓扑连通性与红线检查点，请稍候...</p>
            </div>
          ) : (
            <div className="space-y-3">
              <Alert
                type="info"
                showIcon
                message="💡 质检机制核心说明 (只检不修原则)"
                description={
                  <div className="text-xs space-y-1 text-slate-600">
                    <div>
                      1. <strong>只检不修</strong>：重新质检仅调用大模型法官进行白盒拓扑连通性与红线扣分诊断，<span className="text-amber-700 font-semibold">绝对不会自动修改或重塑任何话术</span>。
                    </div>
                    <div>
                      2. <strong>质检对象</strong>：默认对左侧【原实战会话】进行重新初检；也可选择对右侧【当前修复语料】执行二次质检验收。
                    </div>
                    <div>
                      3. <strong>修复指引</strong>：质检后若仍检出缺陷，请在抽屉右上角点击【重新修复数据】按钮，系统将调用 Teacher 模型定向重塑并自动完成二次质检验收。
                    </div>
                  </div>
                }
              />

              <Form form={singleJudgeForm} layout="vertical" className="mt-2">
                <Form.Item
                  name="target_data"
                  label="质检目标数据源"
                  initialValue="raw"
                  rules={[{ required: true }]}
                >
                  <Radio.Group className="w-full">
                    <div className="space-y-2">
                      <Radio value="raw" className="items-start">
                        <span className="font-semibold text-slate-800">原实战会话 (左侧 raw_messages · 重新初检)</span>
                        <span className="text-3xs text-slate-500 block ml-6">
                          评估在新规则标准下原会话能否直接达标免修（若及格将直接晋升为 GOLD_PASS，不修改文本）
                        </span>
                      </Radio>
                      <Radio value="remediated" className="items-start">
                        <span className="font-semibold text-slate-800">当前修复/微调后语料 (右侧 remediated_messages · 二次验收)</span>
                        <span className="text-3xs text-slate-500 block ml-6">
                          对当前已修复或手动行内微调后的 Assistant 话术执行合规验收（若及格晋升 REPAIRED_PASS）
                        </span>
                      </Radio>
                    </div>
                  </Radio.Group>
                </Form.Item>

                <Form.Item name="rule_id" label="质检标准规则" rules={[{ required: true, message: '请选择规则标准' }]}>
                  <Select
                    popupMatchSelectWidth={false}
                    options={rules.map((r) => ({ value: r.rule_id, label: `${r.project_name} (${r.rule_id})` }))}
                  />
                </Form.Item>
                <Form.Item name="judge_model_id" label="质检法官模型" rules={[{ required: true, message: '请选择质检模型' }]}>
                  <Select popupMatchSelectWidth={false} options={models.map((m) => ({ value: m.id, label: m.name }))} />
                </Form.Item>
              </Form>
            </div>
          )}
        </div>
      </Modal>

      {/* 单条数据重新修复模型配置模态框 */}
      <Modal
        title="重新修复数据 (基于当前质检结果修复)"
        open={remediateModalVisible}
        onOk={handleRemediateSubmit}
        confirmLoading={remediateLoading}
        okButtonProps={{
          disabled: !remediateLoading && !watchSingleRunInitial && !singleEvalCheck.remediable,
        }}
        maskClosable={!remediateLoading}
        onCancel={handleCancelRemediate}
        okText={remediateLoading ? '正在重塑修复中...' : '开始重新修复'}
        cancelText={remediateLoading ? '中止并关闭' : '取消'}
      >
        <div className="py-2">
          {remediateLoading ? (
            <div className="py-8 text-center space-y-3">
              <Spin size="large" />
              <p className="text-sm text-emerald-700 font-medium">
                {watchSingleRunVerified
                  ? '修复 Teacher 模型正在靶向微创重塑话术并执行二次质检验收，请稍候...'
                  : '修复 Teacher 模型正在基于当前质检诊断执行靶向微创重塑，请稍候...'}
              </p>
            </div>
          ) : (
            <>
              {!watchSingleRunInitial && !singleEvalCheck.remediable ? (
                <Alert
                  type="warning"
                  showIcon
                  className="mb-3 text-xs"
                  message="当前暂无异常可以修复"
                  description={
                    <div className="space-y-1">
                      <div>{singleEvalCheck.reason}</div>
                      <div className="text-slate-500">
                        如需重新排查对话问题，请先关闭此弹窗并点击【按此标准重新质检】，或直接打开下方「修复前重新执行质检」开关。
                      </div>
                    </div>
                  }
                />
              ) : (
                <p className="text-xs text-slate-500 mb-3">
                  默认直接使用当前已有质检结果调用 Teacher 模型修复话术（不重新初检、满分不自动跳过、修复后不自动继续质检），可按需开启下方选项。
                </p>
              )}
              <Form form={remediateForm} layout="vertical" className="mt-2">
                <Form.Item name="rule_id" label="修复标准规则" rules={[{ required: true, message: '请选择规则标准' }]}>
                  <Select
                    popupMatchSelectWidth={false}
                    options={rules.map((r) => ({ value: r.rule_id, label: `${r.project_name} (${r.rule_id})` }))}
                  />
                </Form.Item>

                <Form.Item name="remediate_model_id" label="修复 Teacher 模型" rules={[{ required: true, message: '请选择修复模型' }]}>
                  <Select popupMatchSelectWidth={false} options={models.map((m) => ({ value: m.id, label: m.name }))} />
                </Form.Item>

                <div className="bg-slate-50 border border-slate-200 rounded-md p-3 mb-3 space-y-2">
                  <Form.Item
                    name="run_initial_judge"
                    label="修复前重新执行质检"
                    valuePropName="checked"
                    initialValue={false}
                    extra="默认关闭：直接以当前已有质检诊断作为依据进行修复"
                    className="mb-2"
                  >
                    <Switch />
                  </Form.Item>
                  <Form.Item
                    name="skip_if_gold_pass"
                    label="初检满分/达标时免修直接通过 (GOLD_PASS)"
                    valuePropName="checked"
                    initialValue={false}
                    extra="默认关闭：即使当前为满分，手动修复后也不直接通过，仍执行修复"
                    className="mb-2"
                  >
                    <Switch />
                  </Form.Item>
                  <Form.Item
                    name="run_verified_judge"
                    label="修复后自动继续质检验收"
                    valuePropName="checked"
                    initialValue={false}
                    extra="默认关闭：修复后仅落库修复语料与切题并标为待复核，也可开启直接质检"
                    className="mb-0"
                  >
                    <Switch />
                  </Form.Item>
                </div>

                {(watchSingleRunInitial || watchSingleRunVerified) && (
                  <Form.Item name="judge_model_id" label="质检法官模型" rules={[{ required: true, message: '已开启质检选项，请选择质检模型' }]}>
                    <Select popupMatchSelectWidth={false} options={models.map((m) => ({ value: m.id, label: m.name }))} />
                  </Form.Item>
                )}
              </Form>
            </>
          )}
        </div>
      </Modal>

      {/* 导出黄金微调数据集模态框 (全规则导出) */}
      <Modal
        title="一键导出达标黄金微调训练集 (.jsonl)"
        open={exportModalVisible}
        onOk={handleExportSubmit}
        confirmLoading={exportLoading}
        onCancel={() => setExportModalVisible(false)}
        okText="导出数据集"
        cancelText="取消"
      >
        <Form form={exportForm} layout="vertical" className="mt-3">
          <Form.Item name="rule_id" label="所属场景规则" rules={[{ required: true }]}>
            <Select popupMatchSelectWidth={false} options={rules.map((r) => ({ value: r.rule_id, label: r.project_name }))} />
          </Form.Item>

          <Form.Item name="version_tag" label="训练集版本标签" initialValue="v1.0" rules={[{ required: true }]}>
            <Input placeholder="如 v1.0, v2.1-dpo" />
          </Form.Item>

          <Form.Item name="include_manual_approved" label="包含人工放行样本 (MANUAL_APPROVED)" valuePropName="checked" initialValue={true}>
            <Switch />
          </Form.Item>

          <Form.Item name="inject_thinking" label="保留思考链用于推理微调 (Thinking Mode)" valuePropName="checked" initialValue={false}>
            <Switch />
          </Form.Item>

          <Form.Item
            name="exclude_benchmark_exported"
            label="源头互斥：排除已导出至评测集的同源对话 (防数据泄漏)"
            valuePropName="checked"
            initialValue={true}
            extra="强烈建议开启，确保微调训练集与留出评测集零重叠"
          >
            <Switch checkedChildren="默认排除" unCheckedChildren="允许导出" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 单样本测评切题导出为评测集模态框 */}
      <Modal
        title={`保存当前样本切题为基准评测集 (${currentSlices.length} 道考题)`}
        open={exportSingleBenchmarkVisible}
        onOk={handleSingleExportBenchmark}
        onCancel={() => setExportSingleBenchmarkVisible(false)}
        okText="确认保存并注册"
        cancelText="取消"
      >
        <Form form={singleExportForm} layout="vertical" className="mt-3">
          <Form.Item
            name="dataset_id"
            label="评测集标识 (ID)"
            rules={[
              { required: true, message: '请输入评测集ID' },
              { pattern: /^[a-zA-Z0-9_\-\.]+$/, message: '仅支持字母、数字、下划线、短横线与点' },
            ]}
            extra="将生成对应的 .jsonl 文件并在数据资产中心注册"
          >
            <Input placeholder="例如: demo_slice_sample01_eval" />
          </Form.Item>

          <Form.Item
            name="name"
            label="评测集显示名称"
            rules={[{ required: true, message: '请输入评测集名称' }]}
          >
            <Input placeholder="例如: 某某会话针对性错题集" />
          </Form.Item>

          <Form.Item name="mode" label="写入模式" rules={[{ required: true }]} initialValue="create">
            <Radio.Group>
              <Radio value="create">覆盖创建新评测集</Radio>
              <Radio value="append">追加至现有同名评测集 (按切片ID去重)</Radio>
            </Radio.Group>
          </Form.Item>
        </Form>
      </Modal>

      {/* 批量样本切题合并导出为基准评测集模态框 */}
      <Modal
        title={`批量将所选 ${selectedRowKeys.length} 条样本生成基准评测集`}
        open={batchExportBenchmarkModalVisible}
        onOk={handleBatchExportBenchmarkSubmit}
        confirmLoading={batchActionLoading}
        onCancel={() => setBatchExportBenchmarkModalVisible(false)}
        okText="开始合并导出"
        cancelText="取消"
        width={620}
      >
        <div className="py-1">
          <Alert
            type="info"
            showIcon
            className="mb-3 text-xs"
            message="飞轮质检数据 ➔ 基准评测集转化机制"
            description="系统将自动提取所选样本中所有经过 AI 智能修复与人工微调的靶向轮次，生成标准评测考题。后序考题已采用纠正后的黄金上文，消除位移漂移与带病上文污染。"
          />
          <Form form={batchExportBenchmarkForm} layout="vertical">
            <Form.Item
              name="dataset_id"
              label="目标评测集标识 (ID)"
              rules={[
                { required: true, message: '请输入评测集ID' },
                { pattern: /^[a-zA-Z0-9_\-\.]+$/, message: '仅支持字母、数字、下划线、短横线与点' },
              ]}
              extra="导出后可在数据资产中心与评测中心直接查阅并一键发起横向对比评测"
            >
              <Input placeholder="例如: flywheel_benchmark_batch_v1" />
            </Form.Item>

            <Form.Item
              name="name"
              label="评测集名称"
              rules={[{ required: true, message: '请输入评测集名称' }]}
            >
              <Input placeholder="例如: 售后实战飞轮第1期评测题库" />
            </Form.Item>

            <Form.Item
              name="strategy"
              label="切题筛选策略"
              rules={[{ required: true }]}
              initialValue="all_defects"
              extra="如果担心同一会话多道考题导致前缀数据倾斜，可选择每会话仅精选首错题"
            >
              <Radio.Group buttonStyle="solid" className="w-full">
                <Radio.Button value="all_defects" className="w-1/2 text-center">
                  🎯 全量缺陷切题 (所有修复轮次)
                </Radio.Button>
                <Radio.Button value="primary_defect_only" className="w-1/2 text-center">
                  ⭐ 每会话精选首错题 (去冗余防倾斜)
                </Radio.Button>
              </Radio.Group>
            </Form.Item>

            <Form.Item name="mode" label="写入模式" rules={[{ required: true }]} initialValue="create">
              <Radio.Group>
                <Radio value="create">覆盖创建新文件</Radio>
                <Radio value="append">追加至现有同名评测集 (按切片ID去重更新)</Radio>
              </Radio.Group>
            </Form.Item>
          </Form>
        </div>
      </Modal>
    </div>
  );
};
