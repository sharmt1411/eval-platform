import React, { useEffect, useState, useMemo } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  Card,
  Button,
  Select,
  Tag,
  Space,
  Input,
  Typography,
  message,
  Spin,
  Alert,
  Collapse,
  Progress,
  Pagination,
  Popconfirm,
  Tooltip,
  Divider,
  Empty,
  Badge,
} from 'antd';
import {
  ArrowLeftOutlined,
  ReloadOutlined,
  DeleteOutlined,
  SearchOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  WarningOutlined,
  ApartmentOutlined,
  ThunderboltOutlined,
  DatabaseOutlined,
  ClockCircleOutlined,
  FilterOutlined,
  EyeOutlined,
} from '@ant-design/icons';
import { benchmarksApi } from '../api';
import { MetricRadarChart } from '../components/MetricRadarChart';
import type {
  BenchmarkTaskItem,
  SideBySideSample,
  SideBySideResultItem,
} from '../types';

const { Text, Paragraph } = Typography;
const { Panel } = Collapse;

export const BenchmarkReport: React.FC = () => {
  const { taskId } = useParams<{ taskId: string }>();
  const navigate = useNavigate();

  const [taskList, setTaskList] = useState<BenchmarkTaskItem[]>([]);
  const [currentTask, setCurrentTask] = useState<any>(null);
  const [sideBySideData, setSideBySideData] = useState<Record<string, SideBySideSample>>({});
  const [loading, setLoading] = useState<boolean>(false);
  const [deleting, setDeleting] = useState<boolean>(false);

  // 筛选与分页状态
  const [filterStatus, setFilterStatus] = useState<string>('ALL');
  const [filterCategory, setFilterCategory] = useState<string>('ALL');
  const [filterDifficulty, setFilterDifficulty] = useState<string>('ALL');
  const [filterHoldout, setFilterHoldout] = useState<'ALL' | 'HOLDOUT' | 'REGULAR'>('ALL');
  const [searchKeyword, setSearchKeyword] = useState<string>('');
  const [currentPage, setCurrentPage] = useState<number>(1);
  const [pageSize, setPageSize] = useState<number>(10);
  const [expandAllThinking, setExpandAllThinking] = useState<boolean>(false);

  // 加载评测任务列表供快速切换
  const loadTaskList = async () => {
    try {
      const data = await benchmarksApi.listTasks(50);
      setTaskList(data);
    } catch (e) {
      console.error('获取历史任务列表失败', e);
    }
  };

  // 加载当前任务详情与 Side-by-Side 报告
  const loadTaskDetail = async (id: string, silent = false) => {
    if (!id) return;
    if (!silent) setLoading(true);
    try {
      const [taskDetail, sbs] = await Promise.all([
        benchmarksApi.getTask(id),
        benchmarksApi.getSideBySide(id).catch(() => ({})),
      ]);
      setCurrentTask(taskDetail);
      setSideBySideData(sbs || {});
    } catch (e: any) {
      if (!silent) message.error(`加载评测报告失败: ${e.message}`);
    } finally {
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => {
    loadTaskList();
  }, []);

  useEffect(() => {
    if (taskId) {
      setCurrentPage(1);
      loadTaskDetail(taskId);
    }
  }, [taskId]);

  // 异步任务轮询机制
  useEffect(() => {
    if (!taskId) return;
    if (currentTask && (currentTask.status === 'PENDING' || currentTask.status === 'RUNNING')) {
      const timer = setInterval(async () => {
        try {
          const taskDetail = await benchmarksApi.getTask(taskId);
          setCurrentTask(taskDetail);
          benchmarksApi
            .getSideBySide(taskId)
            .then((sbs) => {
              if (sbs && Object.keys(sbs).length > 0) {
                setSideBySideData(sbs);
              }
            })
            .catch(() => {});

          if (taskDetail.status === 'COMPLETED' || taskDetail.status === 'FAILED') {
            clearInterval(timer);
            loadTaskList();
            benchmarksApi
              .getSideBySide(taskId)
              .then((sbs) => {
                setSideBySideData(sbs || {});
              })
              .catch(() => {});
          }
        } catch (err) {
          console.error('轮询评测进度异常', err);
        }
      }, 2000);
      return () => clearInterval(timer);
    }
  }, [taskId, currentTask?.status]);

  // 删除当前评测任务
  const handleDeleteCurrentTask = async () => {
    if (!taskId) return;
    setDeleting(true);
    try {
      await benchmarksApi.deleteTask(taskId);
      message.success(`评测任务 [${taskId}] 已成功删除，即将返回任务列表`);
      navigate('/benchmarks');
    } catch (e: any) {
      message.error(`删除任务失败: ${e.message}`);
    } finally {
      setDeleting(false);
    }
  };

  // 一键撤销本次战绩入账流水
  const handleRevokeLedger = async () => {
    if (!taskId) return;
    try {
      const res = await benchmarksApi.revokeLedger(taskId);
      message.success(res.message);
      await loadTaskDetail(taskId);
    } catch (e: any) {
      message.error(`撤销战绩流水失败: ${e.message}`);
    }
  };

  const taskSummaries = currentTask?.result?.summaries || {};
  const allSamples = useMemo(() => Object.values(sideBySideData), [sideBySideData]);

  // 阶梯降频与抽样状态
  const samplingConfig =
    currentTask?.result?.sampling_config ?? currentTask?.result?.meta?.sampling_config;
  const skippedSamplesCount =
    currentTask?.result?.skipped_mastered_samples ?? currentTask?.result?.meta?.skipped_mastered_samples ?? 0;
  const isSamplingEnabled = !!samplingConfig?.enabled || skippedSamplesCount > 0;
  const datasetTotal =
    currentTask?.result?.dataset_total_samples ?? currentTask?.result?.meta?.dataset_total_samples;
  const sampledTotal =
    currentTask?.result?.sampled_samples ?? currentTask?.result?.meta?.sampled_samples;

  // 分析单个样本在多模型对比中的综合表现状态
  const getSampleEvaluationMeta = (sample: SideBySideSample) => {
    const modelsMap = sample.results || sample.models || {};
    const entries = Object.entries(modelsMap);
    if (entries.length === 0) {
      return {
        type: 'UNKNOWN',
        label: '评测中',
        color: 'default',
        passCount: 0,
        failCount: 0,
        errorCount: 0,
        total: 0,
        hasApiError: false,
      };
    }

    let passCount = 0;
    let failCount = 0;
    let errorCount = 0;

    for (const [, r] of entries) {
      const isErr =
        r.status === 'API_ERROR' ||
        (r.content && r.content.startsWith('[API调用异常')) ||
        (r.reply && r.reply.startsWith('[API调用异常'));
      if (isErr) {
        errorCount++;
      } else if (r.status ? r.status === 'PASS' : r.objective_passed) {
        passCount++;
      } else {
        failCount++;
      }
    }

    const total = entries.length;
    let type: 'ALL_PASS' | 'ALL_FAIL' | 'DIVERGENCE' | 'API_ERROR' = 'DIVERGENCE';
    let label = '⚡ 一方错 (表现分歧)';
    let color = 'purple';

    if (errorCount === total) {
      type = 'API_ERROR';
      label = '⚠️ 全体API异常';
      color = 'orange';
    } else if (passCount === total) {
      type = 'ALL_PASS';
      label = '✅ 全对';
      color = 'green';
    } else if (passCount === 0) {
      type = 'ALL_FAIL';
      label = '❌ 全错';
      color = 'red';
    } else {
      type = 'DIVERGENCE';
      label = '⚡ 一方错 (表现分歧)';
      color = 'purple';
    }

    return {
      type,
      label,
      color,
      passCount,
      failCount,
      errorCount,
      total,
      hasApiError: errorCount > 0,
    };
  };

  // 各筛选状态下的计数统计
  const counts = useMemo(() => {
    let all = allSamples.length;
    let divergence = 0;
    let allFail = 0;
    let allPass = 0;
    let apiError = 0;
    let holdout = 0;
    let regular = 0;

    allSamples.forEach((s) => {
      const meta = getSampleEvaluationMeta(s);
      if (meta.type === 'DIVERGENCE') divergence++;
      if (meta.type === 'ALL_FAIL') allFail++;
      if (meta.type === 'ALL_PASS') allPass++;
      if (meta.hasApiError) apiError++;
      if (s.is_holdout) {
        holdout++;
      } else {
        regular++;
      }
    });

    return { all, divergence, allFail, allPass, apiError, holdout, regular };
  }, [allSamples]);

  // 解析单个模型的留出题 (Holdout) 与普通题 (Regular) 分层通过率（兼容旧版任务从 sideBySide 动态回算）
  const resolveModelHoldoutStats = (mId: string, summary: any) => {
    const hasBackendFields =
      summary?.holdout_total_samples !== undefined ||
      summary?.regular_total_samples !== undefined ||
      summary?.holdout_breakdown !== undefined;

    if (hasBackendFields) {
      const hb = summary?.holdout_breakdown?.holdout || {};
      const rb = summary?.holdout_breakdown?.regular || {};
      const hTotal = summary.holdout_total_samples ?? hb.total ?? 0;
      const hValid = summary.holdout_valid_samples ?? hb.valid_count ?? 0;
      const hPassed = summary.holdout_passed_samples ?? hb.passed ?? 0;
      const hSkipped = summary.holdout_skipped_samples ?? hb.skipped ?? 0;
      const hRate = summary.holdout_pass_rate !== undefined ? summary.holdout_pass_rate : hb.pass_rate ?? null;
      const hEstFull =
        summary.holdout_estimated_full_pass_rate !== undefined
          ? summary.holdout_estimated_full_pass_rate
          : hb.estimated_full_pass_rate ?? null;
      const hJudge =
        summary.holdout_avg_judge_score !== undefined ? summary.holdout_avg_judge_score : hb.avg_judge_score ?? null;

      const rTotal = summary.regular_total_samples ?? rb.total ?? 0;
      const rValid = summary.regular_valid_samples ?? rb.valid_count ?? 0;
      const rPassed = summary.regular_passed_samples ?? rb.passed ?? 0;
      const rSkipped = summary.regular_skipped_samples ?? rb.skipped ?? 0;
      const rRate = summary.regular_pass_rate !== undefined ? summary.regular_pass_rate : rb.pass_rate ?? null;
      const rEstFull =
        summary.regular_estimated_full_pass_rate !== undefined
          ? summary.regular_estimated_full_pass_rate
          : rb.estimated_full_pass_rate ?? null;
      const rJudge =
        summary.regular_avg_judge_score !== undefined ? summary.regular_avg_judge_score : rb.avg_judge_score ?? null;

      return {
        holdout: {
          total: hTotal,
          valid: hValid,
          passed: hPassed,
          skipped: hSkipped,
          passRate: hRate,
          estFullRate: hEstFull,
          avgJudgeScore: hJudge,
        },
        regular: {
          total: rTotal,
          valid: rValid,
          passed: rPassed,
          skipped: rSkipped,
          passRate: rRate,
          estFullRate: rEstFull,
          avgJudgeScore: rJudge,
        },
      };
    }

    // 兼容历史旧报告：从 sideBySideData 动态聚合
    let hTotal = 0;
    let hValid = 0;
    let hPassed = 0;
    let rTotal = 0;
    let rValid = 0;
    let rPassed = 0;
    allSamples.forEach((sample) => {
      const modelsMap = sample.results || sample.models || {};
      const r = modelsMap[mId];
      if (!r) return;
      const isErr =
        r.status === 'API_ERROR' ||
        (r.content && r.content.startsWith('[API调用异常')) ||
        (r.reply && r.reply.startsWith('[API调用异常'));
      const isPass = !isErr && (r.status ? r.status === 'PASS' : !!r.objective_passed);
      if (sample.is_holdout) {
        hTotal++;
        if (!isErr) {
          hValid++;
          if (isPass) hPassed++;
        }
      } else {
        rTotal++;
        if (!isErr) {
          rValid++;
          if (isPass) rPassed++;
        }
      }
    });

    return {
      holdout: {
        total: hTotal,
        valid: hValid,
        passed: hPassed,
        skipped: 0,
        passRate: hValid > 0 ? hPassed / hValid : null,
        estFullRate: null,
        avgJudgeScore: null,
      },
      regular: {
        total: rTotal,
        valid: rValid,
        passed: rPassed,
        skipped: 0,
        passRate: rValid > 0 ? rPassed / rValid : null,
        estFullRate: null,
        avgJudgeScore: null,
      },
    };
  };

  // 可选分类列表
  const categoryOptions = useMemo(() => {
    const set = new Set<string>();
    allSamples.forEach((s) => {
      if (s.category) set.add(s.category);
    });
    return Array.from(set);
  }, [allSamples]);

  // 筛选后的样本列表
  const filteredSamples = useMemo(() => {
    return allSamples.filter((sample) => {
      const meta = getSampleEvaluationMeta(sample);

      if (filterStatus === 'DIVERGENCE' && meta.type !== 'DIVERGENCE') return false;
      if (filterStatus === 'ALL_FAIL' && meta.type !== 'ALL_FAIL') return false;
      if (filterStatus === 'ALL_PASS' && meta.type !== 'ALL_PASS') return false;
      if (filterStatus === 'API_ERROR' && !meta.hasApiError) return false;

      if (filterCategory !== 'ALL' && sample.category !== filterCategory) return false;
      if (filterDifficulty !== 'ALL' && sample.difficulty !== filterDifficulty) return false;
      if (filterHoldout === 'HOLDOUT' && !sample.is_holdout) return false;
      if (filterHoldout === 'REGULAR' && sample.is_holdout) return false;

      if (searchKeyword.trim()) {
        const kw = searchKeyword.trim().toLowerCase();
        const idHit = sample.sample_id?.toLowerCase().includes(kw);
        const refHit = sample.reference?.toLowerCase().includes(kw);
        const msgHit = Array.isArray(sample.messages)
          ? sample.messages.some((m: any) => m.content?.toLowerCase().includes(kw))
          : false;
        if (!idHit && !refHit && !msgHit) return false;
      }

      return true;
    });
  }, [allSamples, filterStatus, filterCategory, filterDifficulty, filterHoldout, searchKeyword]);

  // 分页数据切片
  const pagedSamples = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return filteredSamples.slice(start, start + pageSize);
  }, [filteredSamples, currentPage, pageSize]);

  return (
    <div className="space-y-4">
      {/* 顶部导航与任务概况栏 */}
      <div className="bg-white p-4 rounded-lg border border-slate-200 shadow-2xs flex flex-col md:flex-row justify-between items-start md:items-center gap-3">
        <div className="flex items-center gap-3 flex-wrap">
          <Button
            icon={<ArrowLeftOutlined />}
            onClick={() => navigate('/benchmarks')}
            className="hover:border-blue-400"
          >
            返回评测任务列表
          </Button>

          <Divider type="vertical" className="h-6" />

          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-500 font-semibold">快速切换报告:</span>
            <Select
              value={taskId}
              onChange={(val) => navigate(`/benchmarks/${val}`)}
              className="w-56"
              options={taskList.map((t) => ({
                value: t.id,
                label: (
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-xs">{t.id}</span>
                    <Tag
                      color={
                        t.status === 'COMPLETED'
                          ? 'green'
                          : t.status === 'RUNNING'
                          ? 'blue'
                          : t.status === 'FAILED'
                          ? 'red'
                          : 'default'
                      }
                      className="text-3xs"
                    >
                      {t.status}
                    </Tag>
                  </div>
                ),
              }))}
            />
          </div>

          <Button icon={<ReloadOutlined />} size="small" onClick={() => taskId && loadTaskDetail(taskId)}>
            刷新报告
          </Button>

          {taskId && currentTask?.result?.ledger_id && !currentTask?.result?.ledger_revoked && (
            <Popconfirm
              title="确定撤销此次评测的战绩入账吗？"
              description="撤销后该任务对应的流水记录将标记失效，不影响原始数据集和报告查看，下轮抽样自动按之前状态重算。"
              onConfirm={handleRevokeLedger}
              okText="确认撤销"
              cancelText="取消"
            >
              <Button size="small" type="default">
                撤销战绩入账
              </Button>
            </Popconfirm>
          )}

          {taskId && (
            <Popconfirm
              title="确定删除此评测报告吗？"
              description="删除后将永久清理该任务元数据及产物报告文件。"
              onConfirm={handleDeleteCurrentTask}
              okText="确认删除"
              cancelText="取消"
              okButtonProps={{ danger: true, loading: deleting }}
            >
              <Button danger size="small" icon={<DeleteOutlined />}>
                删除当前报告
              </Button>
            </Popconfirm>
          )}
        </div>

        {/* 任务状态与标签 */}
        {currentTask && (
          <div className="flex items-center gap-2 flex-wrap text-xs text-slate-500">
            <span>
              状态:{' '}
              <Tag
                color={
                  currentTask.status === 'COMPLETED'
                    ? 'green'
                    : currentTask.status === 'RUNNING'
                    ? 'blue'
                    : currentTask.status === 'FAILED'
                    ? 'red'
                    : 'default'
                }
              >
                {currentTask.status}
              </Tag>
            </span>
            {currentTask.result?.max_tokens ? (
              <Tag color="geekblue" className="text-3xs font-mono">
                MaxTokens: {currentTask.result.max_tokens}
              </Tag>
            ) : null}
            {currentTask.result?.ignore_sample_max_tokens ? (
              <Tag color="purple" className="text-3xs">
                已忽略样本Token限制
              </Tag>
            ) : null}
            {isSamplingEnabled ? (
              <Tag color="purple" className="text-3xs font-mono">
                {skippedSamplesCount > 0
                  ? `⚡ 智能降频: 实测 ${sampledTotal ?? '?'} / ${datasetTotal ?? '?'} 题 (休眠 ${skippedSamplesCount} 题)`
                  : `⚡ 智能降频已启用 (全量攻坚中，实测 ${sampledTotal ?? '?'} 题)`}
              </Tag>
            ) : null}
            {samplingConfig?.model_line ? (
              <Tag color="geekblue" className="text-3xs font-mono">
                模型线: {samplingConfig.model_line}
                {samplingConfig.version_tag ? ` @ ${samplingConfig.version_tag}` : ''}
              </Tag>
            ) : null}
            {currentTask.result?.is_rerun_same_version ? (
              <Tag color="cyan" className="text-3xs">
                📌 同版本快照复用
              </Tag>
            ) : null}
            {currentTask.result?.ledger_revoked ? (
              <Tag color="red" className="text-3xs">
                战绩流水已撤销
              </Tag>
            ) : null}
            {currentTask.finished_at ? (
              <span className="font-mono text-3xs text-slate-400">
                完成时间: {new Date(currentTask.finished_at * 1000).toLocaleString()}
              </span>
            ) : null}
          </div>
        )}
      </div>

      {/* 异步任务运行中动态进度卡片 */}
      {currentTask && (currentTask.status === 'RUNNING' || currentTask.status === 'PENDING') && (
        <Card size="small" className="bg-blue-50/70 border-blue-200 shadow-2xs">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <div className="flex items-center gap-3">
              <Spin size="small" />
              <div>
                <div className="text-xs font-semibold text-blue-900">
                  评测任务 [{currentTask.id}] 正在并发推理与质检打分中... ({Math.round((currentTask.progress || 0) * 100)}%)
                </div>
                {currentTask.result?.progress_detail && (
                  <div className="text-3xs text-blue-700 mt-0.5 font-mono">
                    已完成: {currentTask.result.progress_detail.completed} / {currentTask.result.progress_detail.total} 个推理样本 · 当前模型: [
                    {currentTask.result.progress_detail.current_model}] · 样本 ID: {currentTask.result.progress_detail.current_sample_id}
                  </div>
                )}
              </div>
            </div>
            <div className="w-48">
              <Progress percent={Math.round((currentTask.progress || 0) * 100)} size="small" status="active" />
            </div>
          </div>
        </Card>
      )}

      {loading && !currentTask ? (
        <div className="flex justify-center items-center py-24 bg-white rounded-lg border border-slate-200">
          <Spin tip="正在载入评测对比报告与 Side-by-Side 数据..." />
        </div>
      ) : (
        <>
          {/* 第一层: 宏观多模型横向对比指标看板 */}
          {Object.keys(taskSummaries).length > 0 && (
            <div className="space-y-4">
              {/* 智能阶梯降频与双口径通过率说明 Alert */}
              {isSamplingEnabled && (
                <Alert
                  type="info"
                  showIcon
                  className="border-purple-200 bg-purple-50/70"
                  message={
                    <div className="text-xs">
                      <span className="font-bold text-purple-900">
                        🎯 本次评测已启用【智能阶梯降频抽样与战绩治理】{skippedSamplesCount > 0 ? '（双口径指标）' : '（首轮攻坚模式）'}
                      </span>
                      <div className="text-3xs text-slate-600 mt-1 space-y-0.5">
                        <div>
                          • <strong>实测通过率 (Sampled Pass Rate)</strong> = 实测通过数 ÷ 实测有效数 × 100%（针对本次实际抽出的高频错题/新题与到期巡检熟题，反映局部硬仗表现；同时按<strong>留出题 (Holdout)</strong> 与<strong>普通题 (Regular)</strong> 独立区分统计）
                        </div>
                        {skippedSamplesCount > 0 ? (
                          <div>
                            • <strong>全集等效通过率 (Estimated Full Pass Rate，仅基准模型线展示)</strong> = (实测通过数 + {skippedSamplesCount} × 熟题保持率 R_cooldown) ÷ (总题数 - API异常数) × 100%（按本轮抽测熟题的真实保持率加权折算休眠题表现，既不掩盖遗忘又能客观评估全集水位）
                          </div>
                        ) : (
                          <div>
                            • <strong>学会与遗忘说明</strong>：当前题库所有题目均在必测攻坚池，尚未产生休眠熟题。待题目（无论留出题或普通题）在同一模型线下连续通过不同版本达到门槛（x={samplingConfig?.pass_threshold_x ?? 3}）后将自动激活降频与分层折算。
                          </div>
                        )}
                      </div>
                    </div>
                  }
                />
              )}

              <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
                {/* 左侧: ECharts 多模型综合雷达图 */}
                <div className="lg:col-span-5 bg-white p-4 rounded-lg border border-slate-200 shadow-2xs flex flex-col justify-between">
                  <div>
                    <h3 className="text-sm font-bold text-slate-800 flex items-center gap-1.5 mb-1">
                      <ApartmentOutlined className="text-blue-600" />
                      多模型综合表现雷达图 (Radar)
                    </h3>
                    <p className="text-3xs text-slate-400 mb-2">
                      对比各模型在及格率、客观规则、思考深度、延迟与吞吐量综合能力
                    </p>
                  </div>
                  <div className="h-64">
                    <MetricRadarChart summaries={taskSummaries} />
                  </div>
                </div>

                {/* 右侧: 各模型关键性能指标卡片矩阵 */}
                <div className="lg:col-span-7 grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {Object.entries(taskSummaries).map(([mId, s]: [string, any]) => {
                    const passRatePct = Math.round((s.pass_rate || 0) * 100);
                    const objRatePct = Math.round((s.objective_pass_rate || 0) * 100);
                    const hasApiErrors = (s.api_error_samples || 0) > 0;
                    const hStats = resolveModelHoldoutStats(mId, s);
                    const hRatePct =
                      hStats.holdout.passRate !== null && hStats.holdout.passRate !== undefined
                        ? Math.round(hStats.holdout.passRate * 100)
                        : null;
                    const rRatePct =
                      hStats.regular.passRate !== null && hStats.regular.passRate !== undefined
                        ? Math.round(hStats.regular.passRate * 100)
                        : null;
                    return (
                      <Card
                        key={mId}
                        size="small"
                        className="border-slate-200 shadow-2xs flex flex-col justify-between hover:border-blue-400 transition-colors"
                        title={
                          <div className="flex items-center justify-between">
                            <span className="font-bold text-xs truncate max-w-[140px]" title={mId}>
                              {mId}
                            </span>
                            <div className="flex items-center gap-1">
                              {isSamplingEnabled && s.is_benchmark_model && samplingConfig?.model_line && (
                                <Tag color="geekblue" className="m-0 text-3xs">
                                  基准线
                                </Tag>
                              )}
                              <Tag color={passRatePct >= 80 ? 'green' : passRatePct >= 60 ? 'blue' : 'orange'} className="m-0 text-3xs">
                                实测: {passRatePct}%
                              </Tag>
                              {isSamplingEnabled &&
                                s.estimated_full_pass_rate !== undefined &&
                                s.estimated_full_pass_rate !== null &&
                                s.is_benchmark_model !== false && (
                                  <Tag
                                    color="purple"
                                    className="m-0 text-3xs font-bold"
                                    title={
                                      skippedSamplesCount > 0
                                        ? `按实测熟题保持率加权折算的大盘等效通过率: ${Math.round(s.estimated_full_pass_rate * 100)}%`
                                        : '当前为全量攻坚，全集等效等同于实测通过率'
                                    }
                                  >
                                    全集等效: {Math.round(s.estimated_full_pass_rate * 100)}%
                                  </Tag>
                                )}
                            </div>
                          </div>
                        }
                      >
                        <div className="space-y-2 text-xs">
                          <div className="flex justify-between items-center text-3xs text-slate-500">
                            <span>有效测试样本:</span>
                            <span className="font-mono font-semibold text-slate-800">
                              {s.valid_samples || s.passed_samples + s.failed_samples} / {s.total_samples} 题
                              {isSamplingEnabled && ` (全集 ${datasetTotal || s.dataset_total_samples || s.total_samples} 题)`}
                            </span>
                          </div>

                          {/* 留出题 vs 普通题 分层通过率对比面板 */}
                          <div className="grid grid-cols-2 gap-1.5 text-3xs">
                            <div className="px-2 py-1 rounded border border-purple-200 bg-purple-50/60 flex flex-col justify-between">
                              <div className="flex items-center justify-between text-purple-900">
                                <span className="font-semibold">🔒 留出题通过率</span>
                                <span
                                  className={`font-mono font-bold ${
                                    hRatePct === null
                                      ? 'text-slate-400'
                                      : hRatePct >= 80
                                      ? 'text-emerald-700'
                                      : hRatePct >= 60
                                      ? 'text-blue-700'
                                      : 'text-orange-700'
                                  }`}
                                >
                                  {hRatePct !== null ? `${hRatePct}%` : '—'}
                                </span>
                              </div>
                              <div className="text-4xs text-slate-500 font-mono flex items-center justify-between mt-0.5">
                                <span>
                                  {hStats.holdout.valid > 0
                                    ? `${hStats.holdout.passed}/${hStats.holdout.valid} 题`
                                    : '无实测留出题'}
                                </span>
                                {isSamplingEnabled &&
                                  hStats.holdout.skipped > 0 &&
                                  hStats.holdout.estFullRate !== null &&
                                  hStats.holdout.estFullRate !== undefined && (
                                    <span className="text-purple-700 font-semibold" title="留出题全集等效通过率">
                                      等效{Math.round(hStats.holdout.estFullRate * 100)}%
                                    </span>
                                  )}
                              </div>
                            </div>

                            <div className="px-2 py-1 rounded border border-slate-200 bg-slate-50/80 flex flex-col justify-between">
                              <div className="flex items-center justify-between text-slate-800">
                                <span className="font-semibold">📄 普通题通过率</span>
                                <span
                                  className={`font-mono font-bold ${
                                    rRatePct === null
                                      ? 'text-slate-400'
                                      : rRatePct >= 80
                                      ? 'text-emerald-700'
                                      : rRatePct >= 60
                                      ? 'text-blue-700'
                                      : 'text-orange-700'
                                  }`}
                                >
                                  {rRatePct !== null ? `${rRatePct}%` : '—'}
                                </span>
                              </div>
                              <div className="text-4xs text-slate-500 font-mono flex items-center justify-between mt-0.5">
                                <span>
                                  {hStats.regular.valid > 0
                                    ? `${hStats.regular.passed}/${hStats.regular.valid} 题`
                                    : '无实测普通题'}
                                </span>
                                {isSamplingEnabled &&
                                  hStats.regular.skipped > 0 &&
                                  hStats.regular.estFullRate !== null &&
                                  hStats.regular.estFullRate !== undefined && (
                                    <span className="text-purple-700 font-semibold" title="普通题全集等效通过率">
                                      等效{Math.round(hStats.regular.estFullRate * 100)}%
                                    </span>
                                  )}
                              </div>
                            </div>
                          </div>

                          {isSamplingEnabled && (
                            <div className="flex justify-between items-center text-3xs text-purple-700 bg-purple-50 px-1.5 py-0.5 rounded border border-purple-200">
                              <span>休眠免测熟题:</span>
                              <span className="font-mono font-bold">
                                {skippedSamplesCount > 0 ? `${skippedSamplesCount} 题 (免推理调用)` : '0 题 (全量攻坚)'}
                              </span>
                            </div>
                          )}

                          {isSamplingEnabled && (s.cooldown_tested_samples ?? 0) > 0 && (
                            <div className="flex justify-between items-center text-3xs px-1.5 py-0.5 rounded border border-amber-200 bg-amber-50/70 text-amber-900">
                              <span>熟题复测表现:</span>
                              <span className="font-mono font-semibold">
                                测 {s.cooldown_tested_samples} ｜ 过 {s.cooldown_passed_samples} ｜ 败{' '}
                                <span className={(s.cooldown_failed_samples ?? 0) > 0 ? 'text-red-600 font-bold' : ''}>
                                  {s.cooldown_failed_samples ?? 0}
                                </span>
                                {s.cooldown_retention_rate !== null && s.cooldown_retention_rate !== undefined
                                  ? ` (${Math.round(s.cooldown_retention_rate * 100)}%)`
                                  : ''}
                              </span>
                            </div>
                          )}

                          <div className="flex justify-between items-center text-3xs text-slate-500">
                            <span>客观规则通过率:</span>
                            <span className="font-mono text-emerald-600 font-semibold">{objRatePct}%</span>
                          </div>

                          {s.avg_judge_score ? (
                            <div className="flex justify-between items-center text-3xs text-slate-500">
                              <span>LLM裁判平均分:</span>
                              <span className="font-mono text-blue-600 font-semibold">{s.avg_judge_score} 分</span>
                            </div>
                          ) : null}

                          <Divider className="my-1.5" />

                          <div className="grid grid-cols-3 gap-1 text-center font-mono">
                            <div className="bg-slate-50 p-1 rounded">
                              <span className="text-4xs text-slate-400 block">延迟</span>
                              <span className="text-3xs font-semibold">{Math.round(s.avg_latency_ms)}ms</span>
                            </div>
                            <div className="bg-slate-50 p-1 rounded">
                              <span className="text-4xs text-slate-400 block">TTFT</span>
                              <span className="text-3xs font-semibold">{Math.round(s.avg_ttft_ms)}ms</span>
                            </div>
                            <div className="bg-slate-50 p-1 rounded">
                              <span className="text-4xs text-slate-400 block">TPS</span>
                              <span className="text-3xs font-semibold text-emerald-600">
                                {Math.round(s.avg_tps)}
                              </span>
                            </div>
                          </div>

                          {hasApiErrors ? (
                            <div className="bg-orange-50 border border-orange-200 p-1.5 rounded text-3xs text-orange-800 flex items-center justify-between">
                              <span className="flex items-center gap-1 font-semibold">
                                <WarningOutlined /> 底层 API 异常:
                              </span>
                              <span className="font-bold font-mono">
                                {s.api_error_samples} 条 ({Math.round(s.api_error_rate * 100)}%)
                              </span>
                            </div>
                          ) : null}
                        </div>
                      </Card>
                    );
                  })}
                </div>
              </div>

              {/* 题型分层 (留出题 vs 普通题) 与多场景分类折叠表格 */}
              <Collapse ghost size="small" className="bg-white rounded-lg border border-slate-200">
                <Panel
                  header={
                    <span className="text-xs font-semibold text-slate-700 flex items-center gap-1.5">
                      <ThunderboltOutlined className="text-amber-500" />
                      点击展开题型分层（🔒 留出题 vs 📄 普通题）与各场景分类通过率详细 Breakdown
                    </span>
                  }
                  key="breakdown"
                >
                  <div className="space-y-4">
                    {/* 1. 留出题 vs 普通题 分层通过率对比表 */}
                    <div className="overflow-x-auto">
                      <div className="text-xs font-semibold text-slate-700 mb-1.5">
                        1. 题型分层通过率对比（留出题 Holdout vs 普通题 Regular）
                      </div>
                      <table className="w-full text-xs text-left border-collapse border border-slate-200">
                        <thead>
                          <tr className="bg-slate-50 border-b border-slate-200 text-slate-600">
                            <th className="p-2 font-semibold">题型分类</th>
                            {Object.keys(taskSummaries).map((m) => (
                              <th key={m} className="p-2 font-semibold font-mono text-center">
                                {m}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {(
                            [
                              { key: 'holdout', label: '🔒 留出题 (is_holdout=true)', tagColor: 'purple' },
                              { key: 'regular', label: '📄 普通题 (is_holdout=false)', tagColor: 'default' },
                            ] as const
                          ).map((rowDef) => (
                            <tr key={rowDef.key} className="border-b border-slate-100 hover:bg-slate-50/50">
                              <td className="p-2 font-medium text-slate-700">
                                <Tag color={rowDef.tagColor} className="m-0 text-3xs font-semibold">
                                  {rowDef.label}
                                </Tag>
                              </td>
                              {Object.entries(taskSummaries).map(([mId, s]: [string, any]) => {
                                const hStats = resolveModelHoldoutStats(mId, s);
                                const gInfo = hStats[rowDef.key];
                                if (!gInfo || (gInfo.valid === 0 && gInfo.skipped === 0)) {
                                  return (
                                    <td key={mId} className="p-2 text-center text-slate-300 font-mono">
                                      — (0题)
                                    </td>
                                  );
                                }
                                const pct =
                                  gInfo.passRate !== null && gInfo.passRate !== undefined
                                    ? Math.round(gInfo.passRate * 100)
                                    : null;
                                return (
                                  <td key={mId} className="p-2 text-center font-mono">
                                    {pct !== null ? (
                                      <span
                                        className={
                                          pct >= 80
                                            ? 'text-emerald-600 font-bold'
                                            : pct >= 60
                                            ? 'text-blue-600 font-semibold'
                                            : 'text-orange-600 font-bold'
                                        }
                                      >
                                        {pct}%
                                      </span>
                                    ) : (
                                      <span className="text-slate-400">未抽测</span>
                                    )}
                                    <span className="text-4xs text-slate-400 block">
                                      实测 ({gInfo.passed}/{gInfo.valid})
                                      {gInfo.skipped > 0 ? ` · 休眠 ${gInfo.skipped}` : ''}
                                      {gInfo.estFullRate !== null &&
                                      gInfo.estFullRate !== undefined &&
                                      gInfo.skipped > 0
                                        ? ` · 等效 ${Math.round(gInfo.estFullRate * 100)}%`
                                        : ''}
                                      {gInfo.avgJudgeScore !== null && gInfo.avgJudgeScore !== undefined
                                        ? ` · 均分 ${gInfo.avgJudgeScore}`
                                        : ''}
                                    </span>
                                  </td>
                                );
                              })}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>

                    {/* 2. 各业务场景/细分类别通过率对比表 */}
                    <div className="overflow-x-auto">
                      <div className="text-xs font-semibold text-slate-700 mb-1.5">
                        2. 业务场景分类通过率对比（Category Breakdown）
                      </div>
                      <table className="w-full text-xs text-left border-collapse border border-slate-200">
                        <thead>
                          <tr className="bg-slate-50 border-b border-slate-200 text-slate-600">
                            <th className="p-2 font-semibold">分类名称</th>
                            {Object.keys(taskSummaries).map((m) => (
                              <th key={m} className="p-2 font-semibold font-mono text-center">
                                {m}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {categoryOptions.map((cat) => (
                            <tr key={cat} className="border-b border-slate-100 hover:bg-slate-50/50">
                              <td className="p-2 font-medium text-slate-700">{cat}</td>
                              {Object.entries(taskSummaries).map(([mId, s]: [string, any]) => {
                                const cInfo = s.category_breakdown?.[cat];
                                if (!cInfo) return <td key={mId} className="p-2 text-center text-slate-300">—</td>;
                                const pct = Math.round((cInfo.pass_rate || 0) * 100);
                                return (
                                  <td key={mId} className="p-2 text-center font-mono">
                                    <span className={pct >= 80 ? 'text-emerald-600 font-bold' : pct >= 60 ? 'text-blue-600' : 'text-orange-600 font-bold'}>
                                      {pct}%
                                    </span>
                                    <span className="text-4xs text-slate-400 block">
                                      ({cInfo.passed}/{cInfo.valid_count || cInfo.count})
                                    </span>
                                  </td>
                                );
                              })}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                </Panel>
              </Collapse>
            </div>
          )}

          {/* 第二层: 微观平铺式 Side-by-Side 样本级对比视窗 */}
          <div className="bg-white p-4 rounded-lg border border-slate-200 shadow-2xs space-y-4">
            <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-3">
              <div>
                <h3 className="text-sm font-bold text-slate-800 flex items-center gap-1.5 mb-0.5">
                  <EyeOutlined className="text-blue-600" />
                  微观 Side-by-Side 样本级对比视窗
                </h3>
                <p className="text-3xs text-slate-400 mb-0">
                  平铺对比多模型在每道题目的实际回复、思考链过程、客观规则测算及主观裁判扣分点评
                </p>
              </div>

              {/* 思考链一键展开开关 */}
              <div className="flex items-center gap-2">
                <span className="text-xs text-slate-600">全量展开思考链推演:</span>
                <Button
                  size="small"
                  type={expandAllThinking ? 'primary' : 'default'}
                  onClick={() => setExpandAllThinking(!expandAllThinking)}
                >
                  {expandAllThinking ? '已全展开' : '一键展开'}
                </Button>
              </div>
            </div>

            {/* 快速筛选按钮组 */}
            <div className="flex flex-wrap items-center gap-2 pb-2 border-b border-slate-100">
              <Button
                size="small"
                type={filterStatus === 'ALL' ? 'primary' : 'default'}
                onClick={() => {
                  setFilterStatus('ALL');
                  setCurrentPage(1);
                }}
              >
                全部用例 ({counts.all})
              </Button>

              <Button
                size="small"
                type={filterStatus === 'DIVERGENCE' ? 'primary' : 'default'}
                className={filterStatus === 'DIVERGENCE' ? 'bg-purple-600' : 'border-purple-300 text-purple-700 hover:text-purple-800'}
                onClick={() => {
                  setFilterStatus('DIVERGENCE');
                  setCurrentPage(1);
                }}
              >
                ⚡ 一方错 (表现分歧) ({counts.divergence})
              </Button>

              <Button
                size="small"
                type={filterStatus === 'ALL_FAIL' ? 'primary' : 'default'}
                className={filterStatus === 'ALL_FAIL' ? 'bg-red-600' : 'border-red-300 text-red-700 hover:text-red-800'}
                onClick={() => {
                  setFilterStatus('ALL_FAIL');
                  setCurrentPage(1);
                }}
              >
                ❌ 全错 ({counts.allFail})
              </Button>

              <Button
                size="small"
                type={filterStatus === 'ALL_PASS' ? 'primary' : 'default'}
                className={filterStatus === 'ALL_PASS' ? 'bg-green-600' : 'border-green-300 text-green-700 hover:text-green-800'}
                onClick={() => {
                  setFilterStatus('ALL_PASS');
                  setCurrentPage(1);
                }}
              >
                ✅ 全对 ({counts.allPass})
              </Button>

              {counts.apiError > 0 && (
                <Button
                  size="small"
                  type={filterStatus === 'API_ERROR' ? 'primary' : 'default'}
                  className={filterStatus === 'API_ERROR' ? 'bg-orange-600' : 'border-orange-300 text-orange-700 hover:text-orange-800'}
                  onClick={() => {
                    setFilterStatus('API_ERROR');
                    setCurrentPage(1);
                  }}
                >
                  ⚠️ 含有 API 异常 ({counts.apiError})
                </Button>
              )}
            </div>

            {/* 多维度搜索与分类过滤 */}
            <div className="flex flex-wrap items-center gap-3">
              <Input
                placeholder="搜索样本 ID / 题目问答 / 参考答案..."
                prefix={<SearchOutlined className="text-slate-400" />}
                allowClear
                value={searchKeyword}
                onChange={(e) => {
                  setSearchKeyword(e.target.value);
                  setCurrentPage(1);
                }}
                className="w-72"
                size="small"
              />

              <Select
                value={filterCategory}
                onChange={(val) => {
                  setFilterCategory(val);
                  setCurrentPage(1);
                }}
                size="small"
                className="w-44"
                options={[
                  { value: 'ALL', label: '全部分类' },
                  ...categoryOptions.map((c) => ({ value: c, label: c })),
                ]}
              />

              <Select
                value={filterDifficulty}
                onChange={(val) => {
                  setFilterDifficulty(val);
                  setCurrentPage(1);
                }}
                size="small"
                className="w-32"
                options={[
                  { value: 'ALL', label: '全部难度' },
                  { value: 'easy', label: '简单 (Easy)' },
                  { value: 'medium', label: '中等 (Medium)' },
                  { value: 'hard', label: '困难 (Hard)' },
                ]}
              />

              <Select
                value={filterHoldout}
                onChange={(val) => {
                  setFilterHoldout(val);
                  setCurrentPage(1);
                }}
                size="small"
                className="w-40"
                options={[
                  { value: 'ALL', label: `全部题型 (${counts.all})` },
                  { value: 'HOLDOUT', label: `🔒 仅留出题 (${counts.holdout})` },
                  { value: 'REGULAR', label: `📄 仅普通题 (${counts.regular})` },
                ]}
              />

              <span className="text-xs text-slate-500 font-mono ml-auto">
                筛选共 <span className="font-bold text-slate-800">{filteredSamples.length}</span> 条
              </span>
            </div>

            {/* 平铺卡片流渲染 */}
            {filteredSamples.length === 0 ? (
              <Empty description="当前筛选条件下未找到评测用例" className="py-12" />
            ) : (
              <div className="space-y-4">
                {pagedSamples.map((sample, sIdx) => {
                  const meta = getSampleEvaluationMeta(sample);
                  const sampleMessages = sample.messages || sample.input_prompt || [];
                  const sampleModelsMap = sample.results || sample.models || {};

                  return (
                    <Card
                      key={sample.sample_id || sIdx}
                      size="small"
                      className="border-slate-300 shadow-2xs hover:shadow-xs transition-shadow"
                      title={
                        <div className="flex flex-wrap items-center justify-between gap-2 py-1">
                          <div className="flex items-center gap-2 flex-wrap">
                            <Tag color={meta.color} className="font-semibold text-xs">
                              {meta.label}
                            </Tag>
                            <span className="font-mono text-xs font-bold text-slate-800">
                              #{sample.sample_id}
                            </span>
                            <Tag color={sample.is_holdout ? 'purple' : 'default'} className="text-3xs">
                              {sample.is_holdout ? '🔒 留出题 (Holdout)' : '📄 普通题 (Regular)'}
                            </Tag>
                            {sample.category && (
                              <Tag color="blue" className="text-3xs">
                                {sample.category}
                              </Tag>
                            )}
                            {sample.sub_category && (
                              <Tag color="cyan" className="text-3xs">
                                {sample.sub_category}
                              </Tag>
                            )}
                            {sample.difficulty && (
                              <Tag
                                color={
                                  sample.difficulty === 'easy'
                                    ? 'green'
                                    : sample.difficulty === 'medium'
                                    ? 'orange'
                                    : 'red'
                                }
                                className="text-3xs"
                              >
                                难度: {sample.difficulty}
                              </Tag>
                            )}
                            {sample.source && (
                              <Tag color="cyan" className="text-3xs">
                                来源: {sample.source}
                              </Tag>
                            )}
                            {sample.max_new_tokens ? (
                              currentTask?.result?.ignore_sample_max_tokens ? (
                                <Tooltip title={`样本原设定 max_new_tokens: ${sample.max_new_tokens}，本次评测已勾选覆盖，实际生效上限: ${currentTask?.result?.max_tokens || 4096} Token`}>
                                  <Tag color="purple" className="text-3xs font-mono">
                                    <span className="line-through opacity-60 mr-1">原限制: {sample.max_new_tokens}</span>
                                    <span className="font-semibold text-purple-700">已按 {currentTask?.result?.max_tokens || 4096} 覆盖</span>
                                  </Tag>
                                </Tooltip>
                              ) : (
                                <Tooltip title="评测样本原数据中设定的最大生成Token限制 (未勾选覆盖)">
                                  <Tag color="geekblue" className="text-3xs font-mono">
                                    限制最大Token: {sample.max_new_tokens}
                                  </Tag>
                                </Tooltip>
                              )
                            ) : null}
                          </div>

                          <div className="text-xs text-slate-500 font-mono">
                            通过: <span className="text-emerald-600 font-bold">{meta.passCount}</span> / 未通过:{' '}
                            <span className="text-red-600 font-bold">{meta.failCount}</span>
                            {meta.errorCount > 0 && (
                              <span className="text-orange-600 font-bold ml-1.5">
                                / 接口异常: {meta.errorCount}
                              </span>
                            )}
                          </div>
                        </div>
                      }
                    >
                      {/* 题目上下文与参考标准 */}
                      <div className="p-3 bg-slate-50 rounded border border-slate-200 grid grid-cols-12 gap-3 text-xs mb-4">
                        <div className="col-span-12 md:col-span-7">
                          <span className="font-semibold text-slate-700 block mb-1">
                            输入问答 Prompt / 对话消息:
                          </span>
                          <div className="space-y-1 bg-white p-2.5 rounded border border-slate-200 text-slate-700 max-h-48 overflow-y-auto">
                            {Array.isArray(sampleMessages) ? (
                              sampleMessages.map((m: any, i: number) => (
                                <div key={i} className="text-xs leading-relaxed">
                                  <span className="font-bold uppercase text-slate-500">
                                    [{m.role}]:{' '}
                                  </span>
                                  {m.content}
                                </div>
                              ))
                            ) : (
                              <p className="mb-0">{JSON.stringify(sampleMessages)}</p>
                            )}
                          </div>
                        </div>

                        <div className="col-span-12 md:col-span-5 md:border-l md:pl-3 border-slate-200 space-y-1.5">
                          <div>
                            <span className="font-semibold text-slate-700 block mb-0.5">
                              标准参考 (Reference):
                            </span>
                            <p className="text-slate-700 mb-0 bg-white p-2 rounded border border-slate-200 text-xs">
                              {sample.reference || '无指定基准参考'}
                            </p>
                          </div>

                          {sample.rubric && (
                            <div>
                              <span className="font-semibold text-slate-700 block mb-0.5">
                                评分细则 (Rubric):
                              </span>
                              {typeof sample.rubric === 'string' ? (
                                <p className="text-slate-600 mb-0 bg-white p-2 rounded border border-slate-200 text-3xs">
                                  {sample.rubric}
                                </p>
                              ) : (
                                <div className="text-3xs text-slate-500 space-y-0.5 bg-white p-2 rounded border border-slate-200">
                                  {sample.rubric.judge_criteria && (
                                    <div>细则: {sample.rubric.judge_criteria}</div>
                                  )}
                                  {sample.rubric.must_contain?.length ? (
                                    <div>必含: {sample.rubric.must_contain.join(', ')}</div>
                                  ) : null}
                                  {sample.rubric.forbidden?.length ? (
                                    <div>违禁: {sample.rubric.forbidden.join(', ')}</div>
                                  ) : null}
                                </div>
                              )}
                            </div>
                          )}

                          {sample.regex_constraints && sample.regex_constraints.length > 0 && (
                            <div>
                              <span className="font-semibold text-slate-700 block mb-1">
                                客观正则约束 (Regex Constraints):
                              </span>
                              <div className="flex flex-wrap gap-1">
                                {sample.regex_constraints.map((pat, idx) => {
                                  const isNeg = pat.startsWith('!');
                                  return (
                                    <Tag
                                      key={idx}
                                      color={isNeg ? 'red' : 'green'}
                                      className="font-mono text-3xs m-0"
                                    >
                                      {isNeg ? `禁含: ${pat}` : `必配: ${pat}`}
                                    </Tag>
                                  );
                                })}
                              </div>
                            </div>
                          )}
                        </div>
                      </div>

                      {/* 并排多栏展示各对比模型回复 */}
                      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                        {Object.entries(sampleModelsMap).map(([mid, r]: [string, any]) => {
                          const replyText = r.reply ?? r.content ?? '';
                          const isApiError =
                            r.status === 'API_ERROR' ||
                            replyText.startsWith('[API调用异常') ||
                            !!r.error_message;
                          const latencyMs = r.latency_ms ?? r.metrics?.total_latency_ms ?? 0;
                          const ttftMs = r.ttft_ms ?? r.metrics?.ttft_ms ?? 0;
                          const tpsVal = r.tps ?? r.metrics?.tokens_per_second ?? 0;
                          const thinkTokens = r.thinking_tokens ?? r.usage?.thinking_tokens ?? 0;
                          const objDetails = r.objective_details || {};

                          const missingWords =
                            objDetails.must_contain_missing || objDetails.must_contain?.missing || [];
                          const forbiddenHits =
                            objDetails.forbidden_hit || objDetails.forbidden?.hit || [];
                          const regexMissing =
                            objDetails.regex_missing ||
                            objDetails.regex_constraints?.missing ||
                            objDetails.regex?.failed ||
                            [];
                          const regexViolated =
                            objDetails.regex_violated || objDetails.regex_constraints?.violated || [];

                          return (
                            <Card
                              key={mid}
                              size="small"
                              className={`border-slate-300 shadow-2xs flex flex-col justify-between ${
                                isApiError ? 'border-orange-300 bg-orange-50/20' : ''
                              }`}
                              title={
                                <div className="flex items-center justify-between">
                                  <span className="font-bold text-sm text-slate-800">{mid}</span>
                                  {isApiError ? (
                                    <Tag color="orange" className="font-semibold">
                                      ⚠️ API调用异常
                                    </Tag>
                                  ) : (
                                    <Tag color={(r.status ? r.status === 'PASS' : r.objective_passed) ? 'green' : 'red'}>
                                      {(r.status ? r.status === 'PASS' : r.objective_passed) ? '综合通过' : '未完全通过'}
                                    </Tag>
                                  )}
                                </div>
                              }
                              extra={
                                !isApiError && r.judge_score !== undefined && r.judge_score !== null ? (
                                  <Tag color={r.judge_score >= 80 ? 'blue' : 'orange'}>
                                    裁判: {r.judge_score}分
                                  </Tag>
                                ) : null
                              }
                            >
                              <div>
                                {/* 工程指标胶囊 */}
                                <div className="flex items-center gap-1.5 flex-wrap pb-2 border-b border-slate-100 text-3xs text-slate-500 font-mono">
                                  <span className="bg-slate-100 px-1.5 py-0.5 rounded">
                                    ⏱️ {Math.round(latencyMs)}ms
                                  </span>
                                  <span className="bg-slate-100 px-1.5 py-0.5 rounded">
                                    🚀 TTFT {Math.round(ttftMs)}ms
                                  </span>
                                  <span className="bg-slate-100 px-1.5 py-0.5 rounded text-emerald-700">
                                    ⚡ {Math.round(tpsVal)} TPS
                                  </span>
                                  {r.usage?.completion_tokens !== undefined ? (
                                    <span className="bg-blue-50 text-blue-700 px-1.5 py-0.5 rounded">
                                      📝 {r.usage.completion_tokens} 输出Token
                                    </span>
                                  ) : r.usage?.answer_tokens !== undefined ? (
                                    <span className="bg-blue-50 text-blue-700 px-1.5 py-0.5 rounded">
                                      📝 {r.usage.answer_tokens} 输出Token
                                    </span>
                                  ) : null}
                                  {thinkTokens ? (
                                    <span className="bg-cyan-50 text-cyan-700 px-1.5 py-0.5 rounded">
                                      🧠 {thinkTokens} 思考Token
                                    </span>
                                  ) : null}
                                </div>

                                {/* API 异常警示框 */}
                                {isApiError ? (
                                  <div className="my-3 p-3 bg-orange-50 border border-orange-200 rounded text-xs space-y-1">
                                    <div className="font-bold text-orange-900 flex items-center gap-1.5">
                                      <WarningOutlined /> 模型接口调用异常 (已自动重试5次)
                                    </div>
                                    <p className="text-orange-800 font-mono mb-0 text-3xs break-all">
                                      {r.error_message || replyText}
                                    </p>
                                    <div className="text-3xs text-orange-600 mt-1">
                                      * 该异常由底层网络/API 服务端报错引起，已单独隔离，未将其计入模型的业务规则未通过统计。
                                    </div>
                                  </div>
                                ) : (
                                  <div className="py-3 text-sm text-slate-800 leading-relaxed whitespace-pre-wrap max-h-80 overflow-y-auto">
                                    {replyText}
                                  </div>
                                )}

                                {/* 折叠思考链 */}
                                {r.thinking && (
                                  <Collapse
                                    ghost
                                    size="small"
                                    activeKey={expandAllThinking ? ['thinking'] : undefined}
                                    className="mt-2 bg-cyan-50/50 rounded border border-cyan-100"
                                  >
                                    <Panel
                                      header={
                                        <span className="text-xs text-cyan-800 font-medium">
                                          完整思考推演 ({r.thinking.length} 字符)
                                        </span>
                                      }
                                      key="thinking"
                                    >
                                      <p className="text-xs text-slate-600 font-mono whitespace-pre-wrap mb-0 max-h-60 overflow-y-auto">
                                        {r.thinking}
                                      </p>
                                    </Panel>
                                  </Collapse>
                                )}
                              </div>

                              <div>
                                {/* 客观规则校验明细 */}
                                {!isApiError && Object.keys(objDetails).length > 0 && (
                                  <div className="mt-3 p-2 bg-slate-50 border border-slate-200 rounded text-3xs text-slate-600 font-mono space-y-0.5">
                                    <span className="font-semibold text-slate-700 block mb-0.5">
                                      客观规则测算明细:
                                    </span>
                                    {regexMissing.length > 0 && (
                                      <div className="text-red-600">
                                        未命中必配正则: {regexMissing.join(' ; ')}
                                      </div>
                                    )}
                                    {regexViolated.length > 0 && (
                                      <div className="text-red-600">
                                        触发禁含正则: {regexViolated.join(' ; ')}
                                      </div>
                                    )}
                                    {missingWords.length > 0 && (
                                      <div className="text-red-600">
                                        缺失必含词: {missingWords.join(', ')}
                                      </div>
                                    )}
                                    {forbiddenHits.length > 0 && regexViolated.length === 0 && (
                                      <div className="text-red-600">
                                        命中违禁词: {forbiddenHits.join(', ')}
                                      </div>
                                    )}
                                    {objDetails.json_valid !== undefined && (
                                      <div>JSON格式: {objDetails.json_valid ? '合法' : '非法'}</div>
                                    )}
                                    {r.objective_passed && (
                                      <div className="text-emerald-600">
                                        全部正向/反向客观规则校验通过
                                      </div>
                                    )}
                                  </div>
                                )}

                                {/* 裁判扣分点评 */}
                                {!isApiError && r.judge_feedback && (
                                  <div className="mt-3 p-2 bg-amber-50/70 border border-amber-200 rounded text-xs text-amber-900">
                                    <span className="font-semibold block mb-0.5">裁判点评:</span>
                                    {r.judge_feedback}
                                  </div>
                                )}
                              </div>
                            </Card>
                          );
                        })}
                      </div>
                    </Card>
                  );
                })}

                {/* 分页控制器 */}
                <div className="flex flex-col sm:flex-row justify-between items-center gap-3 pt-3 border-t border-slate-200">
                  <div className="text-xs text-slate-500 font-mono">
                    筛选结果共 {filteredSamples.length} 条用例 (当前显示第 {(currentPage - 1) * pageSize + 1} -{' '}
                    {Math.min(currentPage * pageSize, filteredSamples.length)} 条)
                  </div>
                  <Pagination
                    current={currentPage}
                    pageSize={pageSize}
                    total={filteredSamples.length}
                    showSizeChanger
                    pageSizeOptions={['5', '10', '20', '50', '100']}
                    onChange={(page, size) => {
                      setCurrentPage(page);
                      setPageSize(size);
                    }}
                    showQuickJumper
                    size="small"
                  />
                </div>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
};
