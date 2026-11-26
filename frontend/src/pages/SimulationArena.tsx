import React, { useEffect, useState, useRef } from 'react';
import {
  Card,
  Button,
  Select,
  Tag,
  Space,
  Form,
  Switch,
  Input,
  InputNumber,
  Modal,
  Typography,
  message,
  Spin,
  Alert,
  Divider,
  Popconfirm,
  Tooltip,
  Tabs,
  Table,
  Badge,
  Row,
  Col,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  PlayCircleOutlined,
  UserOutlined,
  RobotOutlined,
  ArrowRightOutlined,
  WarningOutlined,
  ReloadOutlined,
  HistoryOutlined,
  DeleteOutlined,
  SendOutlined,
  ClearOutlined,
  DiffOutlined,
  SaveOutlined,
  StopOutlined,
  CompassOutlined,
  EyeOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  ApiOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { modelsApi, rulesApi, flywheelApi, simulationApi } from '../api';
import type {
  RegisteredModel,
  RuleSummary,
  UniversalRuleSchema,
  ChatMessage,
  SimulationBattleResult,
  FlywheelSampleRecord,
  InspectionReport,
  DebugRequestEntry,
} from '../types';
import { MermaidViewer } from '../components/MermaidViewer';
import { MonacoDiffModal } from '../components/MonacoDiffModal';
import { RequestDebugModal, extractDebugEntriesFromEval } from '../components/RequestDebugModal';

const { Text, Paragraph } = Typography;
const { TextArea } = Input;

interface ModelPreset {
  id: string;
  name: string;
  target_model_id: string;
  client_model_id: string;
  judge_model_id: string;
  remediate_model_id?: string;
  auto_remediate?: boolean;
  max_turns?: number;
}

export const SimulationArena: React.FC = () => {
  const navigate = useNavigate();

  // 当前主导航 Tab: 'arena' (AI对弈) | 'playground' (人工自由对练) | 'history' (历史批量管理)
  const [activeTab, setActiveTab] = useState<string>('arena');

  // 全局数据源
  const [rules, setRules] = useState<RuleSummary[]>([]);
  const [models, setModels] = useState<RegisteredModel[]>([]);
  const [selectedRuleId, setSelectedRuleId] = useState<string>('');
  const [currentRuleSchema, setCurrentRuleSchema] = useState<UniversalRuleSchema | null>(null);
  const [loading, setLoading] = useState<boolean>(false);

  // ---------------------------------------------------------------------------
  // Tab 1: AI 模拟对弈状态
  // ---------------------------------------------------------------------------
  const [battling, setBattling] = useState<boolean>(false);
  const [streamingMessages, setStreamingMessages] = useState<ChatMessage[]>([]);
  const [streamingProgressText, setStreamingProgressText] = useState<string>('');
  const [currentStreamingScenarioName, setCurrentStreamingScenarioName] = useState<string>('');

  const [battleResults, setBattleResults] = useState<SimulationBattleResult[]>([]);
  const [activeBattleIndex, setActiveBattleIndex] = useState<number>(0);
  const [selectedBattleSampleId, setSelectedBattleSampleId] = useState<string>('');

  // 双向联动：轮次与 Mermaid 节点互相聚焦
  const [arenaFocusedTurn, setArenaFocusedTurn] = useState<number | null>(null);
  const [arenaActiveNodeId, setArenaActiveNodeId] = useState<string>('');
  const [playgroundFocusedTurn, setPlaygroundFocusedTurn] = useState<number | null>(null);
  const [playgroundActiveNodeId, setPlaygroundActiveNodeId] = useState<string>('');

  // 预设方案状态
  const [presets, setPresets] = useState<ModelPreset[]>([]);
  const [selectedPresetId, setSelectedPresetId] = useState<string>('');
  const [isSavePresetModalOpen, setIsSavePresetModalOpen] = useState<boolean>(false);
  const [newPresetName, setNewPresetName] = useState<string>('');

  // Diff 模态框状态
  const [diffModalVisible, setDiffModalVisible] = useState<boolean>(false);

  // 质检 / 修复报文与结果调试弹窗状态
  const [reqDebugVisible, setReqDebugVisible] = useState<boolean>(false);
  const [reqDebugTitle, setReqDebugTitle] = useState<string>('');
  const [reqDebugEntries, setReqDebugEntries] = useState<DebugRequestEntry[]>([]);
  const [reqDebugRawReport, setReqDebugRawReport] = useState<Record<string, any> | null>(null);

  const openReqDebugModal = (
    title: string,
    entries: DebugRequestEntry[],
    rawReport: Record<string, any> | null = null
  ) => {
    setReqDebugTitle(title);
    setReqDebugEntries(entries);
    setReqDebugRawReport(rawReport);
    setReqDebugVisible(true);
  };

  const [form] = Form.useForm();
  const abortControllerRef = useRef<AbortController | null>(null);

  // ---------------------------------------------------------------------------
  // Tab 2: 人工自由对话 (Playground) 状态
  // ---------------------------------------------------------------------------
  const [playgroundTargetModelId, setPlaygroundTargetModelId] = useState<string>('');
  const [playgroundJudgeModelId, setPlaygroundJudgeModelId] = useState<string>('');
  const [playgroundMessages, setPlaygroundMessages] = useState<ChatMessage[]>([]);
  const [playgroundInput, setPlaygroundInput] = useState<string>('');
  const [playgroundSending, setPlaygroundSending] = useState<boolean>(false);
  const [playgroundEvaluating, setPlaygroundEvaluating] = useState<boolean>(false);
  const [playgroundEval, setPlaygroundEval] = useState<InspectionReport | null>(null);
  const [playgroundSavedSampleId, setPlaygroundSavedSampleId] = useState<string>('');

  // ---------------------------------------------------------------------------
  // Tab 3: 对练历史只读回看状态
  // ---------------------------------------------------------------------------
  const [historyBattles, setHistoryBattles] = useState<any[]>([]);
  const [historyLoading, setHistoryLoading] = useState<boolean>(false);
  const [historyStatusFilter, setHistoryStatusFilter] = useState<string>('all');
  const [historyPagination, setHistoryPagination] = useState<{ current: number; pageSize: number }>({
    current: 1,
    pageSize: 20,
  });

  // 初始化加载
  useEffect(() => {
    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
    };
  }, []);

  const loadPresets = (mList?: RegisteredModel[]) => {
    try {
      const saved = localStorage.getItem('eval_platform_model_presets');
      let presetList: ModelPreset[] = [];
      if (saved) {
        presetList = JSON.parse(saved);
        setPresets(presetList);
      }

      // 优先从缓存获取上次选中的方案，或默认取第一条方案
      const lastPresetId = localStorage.getItem('eval_platform_last_selected_preset_id');
      const targetPreset = presetList.find((p) => p.id === lastPresetId) || presetList[0];

      if (targetPreset) {
        setSelectedPresetId(targetPreset.id);
        form.setFieldsValue({
          target_model_id: targetPreset.target_model_id,
          client_model_id: targetPreset.client_model_id,
          judge_model_id: targetPreset.judge_model_id,
          remediate_model_id: targetPreset.remediate_model_id,
          auto_remediate: targetPreset.auto_remediate ?? true,
          max_turns: targetPreset.max_turns || 60,
        });
      } else if (mList && mList.length > 0) {
        // 若无已存方案，智能回显默认可用模型
        const targetM = mList.find((m) => !m.id.includes('client') && !m.id.includes('judge')) || mList[0];
        const clientM = mList.find((m) => m.id.includes('client')) || mList[1] || mList[0];
        const judgeM = mList.find((m) => m.id.includes('judge')) || mList[2] || mList[0];
        form.setFieldsValue({
          target_model_id: targetM.id,
          client_model_id: clientM.id,
          judge_model_id: judgeM.id,
          max_turns: 60,
          auto_remediate: true,
        });
      }
    } catch (e) {
      console.warn('加载模型预设失败', e);
    }
  };

  const loadInitData = async () => {
    setLoading(true);
    try {
      const [rList, mList] = await Promise.all([rulesApi.list(), modelsApi.list()]);
      setRules(rList);
      setModels(mList);
      if (rList.length > 0) {
        setSelectedRuleId(rList[0].rule_id);
      }
      if (mList.length > 0) {
        setPlaygroundTargetModelId(mList[0].id);
        const jModel = mList.find((m) => m.id.includes('judge')) || mList[0];
        setPlaygroundJudgeModelId(jModel.id);
      }
      loadPresets(mList);
    } catch (e: any) {
      message.error(`加载配置失败: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadInitData();
  }, []);

  const loadRuleSchema = async (ruleId: string) => {
    if (!ruleId) return;
    try {
      const schema = await rulesApi.get(ruleId);
      setCurrentRuleSchema(schema);
    } catch (e: any) {
      console.error('加载场景规则失败', e);
    }
  };

  const loadHistoryBattles = async (ruleId?: string, status?: string) => {
    setHistoryLoading(true);
    try {
      const list = await simulationApi.listRecentBattles({
        rule_id: ruleId || selectedRuleId,
        status: status && status !== 'all' ? status : undefined,
        limit: 100,
      });
      setHistoryBattles(list);
    } catch (e: any) {
      console.error('加载历史会话失败', e);
    } finally {
      setHistoryLoading(false);
    }
  };

  useEffect(() => {
    if (selectedRuleId) {
      loadRuleSchema(selectedRuleId);
      loadHistoryBattles(selectedRuleId, historyStatusFilter);
    }
  }, [selectedRuleId, historyStatusFilter]);

  // 打开保存新方案弹窗
  const handleSaveCurrentAsPreset = () => {
    const values = form.getFieldsValue();
    if (!values.target_model_id || !values.client_model_id || !values.judge_model_id) {
      message.warning('请先在下方完整选择被测模型、模拟客户模型与质检法官模型！');
      return;
    }
    setNewPresetName('');
    setIsSavePresetModalOpen(true);
  };

  // 应用方案
  const handleApplyPreset = (presetId: string) => {
    setSelectedPresetId(presetId);
    if (!presetId) {
      localStorage.removeItem('eval_platform_last_selected_preset_id');
      return;
    }
    const p = presets.find((x) => x.id === presetId);
    if (!p) return;
    localStorage.setItem('eval_platform_last_selected_preset_id', presetId);
    form.setFieldsValue({
      target_model_id: p.target_model_id,
      client_model_id: p.client_model_id,
      judge_model_id: p.judge_model_id,
      remediate_model_id: p.remediate_model_id,
      auto_remediate: p.auto_remediate ?? true,
      max_turns: p.max_turns || 60,
    });
    message.success(`已应用模型方案【${p.name}】，模型参数已全部自动加载到表单！`);
  };

  const handleConfirmSavePreset = () => {
    if (!newPresetName.trim()) {
      message.error('请输入方案名称');
      return;
    }
    const values = form.getFieldsValue();
    const newPreset: ModelPreset = {
      id: `preset_${Date.now()}`,
      name: newPresetName.trim(),
      target_model_id: values.target_model_id,
      client_model_id: values.client_model_id,
      judge_model_id: values.judge_model_id,
      remediate_model_id: values.remediate_model_id,
      auto_remediate: values.auto_remediate ?? true,
      max_turns: values.max_turns || 60,
    };
    const updated = [...presets, newPreset];
    setPresets(updated);
    localStorage.setItem('eval_platform_model_presets', JSON.stringify(updated));
    localStorage.setItem('eval_platform_last_selected_preset_id', newPreset.id);
    setSelectedPresetId(newPreset.id);
    setIsSavePresetModalOpen(false);
    message.success(`模型方案【${newPreset.name}】已保存并成功自动加载！`);
  };

  const handleDeletePreset = (presetId: string) => {
    const updated = presets.filter((p) => p.id !== presetId);
    setPresets(updated);
    localStorage.setItem('eval_platform_model_presets', JSON.stringify(updated));
    if (selectedPresetId === presetId) {
      setSelectedPresetId('');
      localStorage.removeItem('eval_platform_last_selected_preset_id');
    }
    message.info('方案已删除');
  };

  // ---------------------------------------------------------------------------
  // Tab 1 操作：启动实战对弈 / 中止
  // ---------------------------------------------------------------------------
  const handleStopSimulation = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setBattling(false);
    setStreamingProgressText('');
    message.info('模拟对弈已中止');
  };

  const handleStartSimulation = async () => {
    try {
      const values = await form.validateFields();
      const controller = new AbortController();
      abortControllerRef.current = controller;

      setBattling(true);
      setStreamingMessages([]);
      setBattleResults([]);
      setActiveBattleIndex(0);
      setSelectedBattleSampleId('');
      setStreamingProgressText('准备建立实时流式对弈通道...');
      setCurrentStreamingScenarioName('');

      const remediateModelId = (values.auto_remediate ?? true)
        ? (values.remediate_model_id || values.judge_model_id)
        : undefined;

      try {
        if (values.judge_model_id) {
          localStorage.setItem('eval_platform_flywheel_last_judge_model_id', values.judge_model_id);
        }
        if (remediateModelId) {
          localStorage.setItem('eval_platform_flywheel_last_remediate_model_id', remediateModelId);
        }
      } catch {
        // ignore storage errors
      }

      const scenarioIds =
        values.scenario_ids && values.scenario_ids.length > 0
          ? values.scenario_ids
          : ['all'];

      const accumulatedResults: SimulationBattleResult[] = [];

      await simulationApi.simulateStream(
        {
          rule_id: selectedRuleId,
          target_model_id: values.target_model_id,
          client_model_id: values.client_model_id,
          judge_model_id: values.judge_model_id,
          remediate_model_id: remediateModelId,
          scenario_ids: scenarioIds,
          max_turns: values.max_turns || 60,
          auto_remediate: values.auto_remediate ?? true,
        },
        (eventType, payload) => {
          if (eventType === 'scenario_start') {
            setCurrentStreamingScenarioName(payload.scenario_name);
            setStreamingProgressText(
              `[${payload.scenario_index + 1}/${payload.total_scenarios}] 人设剧本: 【${payload.scenario_name}】对弈进行中...`
            );
            setStreamingMessages([]);
          } else if (eventType === 'turn') {
            setStreamingMessages((prev) => [...prev, payload.message]);
            setStreamingProgressText(
              `正在对弈: 【${currentStreamingScenarioName || '进行中'}】· 轮次 #${payload.turn_index}`
            );
          } else if (eventType === 'judging') {
            setStreamingProgressText(
              `【${currentStreamingScenarioName}】通话已收口，正在由法官进行白盒拓扑连通性与规则红线质检...`
            );
          } else if (eventType === 'remediating') {
            setStreamingProgressText(
              `【${currentStreamingScenarioName}】初检未达标，AI 修复模型正在定向重塑黄金对话...`
            );
          } else if (eventType === 'scenario_done') {
            accumulatedResults.push(payload);
            setBattleResults([...accumulatedResults]);
            setActiveBattleIndex(accumulatedResults.length - 1);
            setSelectedBattleSampleId(payload.sample_id);
            localStorage.setItem('eval_platform_last_battle_id', payload.sample_id);
          } else if (eventType === 'finish') {
            message.success(
              `全部 ${payload.total_scenarios} 通对弈与质检修复圆满完成！已自动持久化落库。`
            );
            loadHistoryBattles(selectedRuleId);
          } else if (eventType === 'error') {
            message.error(`对弈发生异常: ${payload.message}`);
          }
        },
        controller.signal
      );
    } catch (e: any) {
      if (
        e.name === 'AbortError' ||
        e.message?.includes('aborted') ||
        e.message?.includes('The user aborted a request')
      ) {
        message.info('模拟对弈已中止');
      } else {
        message.error(`对弈执行失败: ${e.message}`);
      }
    } finally {
      abortControllerRef.current = null;
      setBattling(false);
      setStreamingProgressText('');
    }
  };

  const currentBattle = battleResults[activeBattleIndex];
  const dialogueMessages = battling ? streamingMessages : currentBattle?.dialogue || [];
  const initialEval = battling ? undefined : currentBattle?.initial_eval;
  const verifiedEval = battling ? undefined : currentBattle?.verified_eval;
  const hasRemediatedData = Boolean(currentBattle?.remediated && currentBattle.remediated.length > 0);

  // ---------------------------------------------------------------------------
  // Tab 2 操作：人工自由对话 (Playground)
  // ---------------------------------------------------------------------------
  // 客服主动开场打招呼
  const handlePlaygroundBotGreeting = async () => {
    if (!playgroundTargetModelId) {
      message.warning('请先选择被测客服模型！');
      return;
    }
    setPlaygroundSending(true);
    try {
      const sysPrompt = currentRuleSchema?.bot_config.system_prompt || '你是星云智联专业售后客服。';
      const greetingPrompt = [
        { role: 'user', content: '[电话接通，请开始客服标准开场问候]' },
      ];
      const res = await modelsApi.chat(playgroundTargetModelId, {
        messages: greetingPrompt,
        system_prompt: sysPrompt,
      });
      const botMsg: ChatMessage = {
        role: 'assistant',
        content: res.reply,
        thinking: res.thinking,
      };
      setPlaygroundMessages([botMsg]);
      message.success('机器人已标准开场！');
    } catch (e: any) {
      message.error(`机器人开场失败: ${e.message}`);
    } finally {
      setPlaygroundSending(false);
    }
  };

  // 发送用户人工发言
  const handlePlaygroundSend = async () => {
    if (!playgroundInput.trim()) return;
    if (!playgroundTargetModelId) {
      message.warning('请先选择被测客服模型！');
      return;
    }
    const userText = playgroundInput.trim();
    const newUserMsg: ChatMessage = { role: 'user', content: userText };
    const nextHistory = [...playgroundMessages, newUserMsg];
    setPlaygroundMessages(nextHistory);
    setPlaygroundInput('');
    setPlaygroundSending(true);

    try {
      const sysPrompt = currentRuleSchema?.bot_config.system_prompt || '你是星云智联专业售后客服。';
      const res = await modelsApi.chat(playgroundTargetModelId, {
        messages: nextHistory,
        system_prompt: sysPrompt,
      });
      const newBotMsg: ChatMessage = {
        role: 'assistant',
        content: res.reply,
        thinking: res.thinking,
      };
      setPlaygroundMessages([...nextHistory, newBotMsg]);
    } catch (e: any) {
      message.error(`模型回复失败: ${e.message}`);
    } finally {
      setPlaygroundSending(false);
    }
  };

  // 清空人工自由对话
  const handlePlaygroundClear = () => {
    setPlaygroundMessages([]);
    setPlaygroundEval(null);
    setPlaygroundSavedSampleId('');
    message.info('自由对话视窗已清空');
  };

  // 一键对人工对话进行白盒质检
  const handlePlaygroundEvaluate = async () => {
    if (playgroundMessages.length < 2) {
      message.warning('至少需要 2 轮以上对话才可进行质检！');
      return;
    }
    if (!playgroundJudgeModelId) {
      message.warning('请选择白盒质检法官模型！');
      return;
    }
    try {
      localStorage.setItem('eval_platform_flywheel_last_judge_model_id', playgroundJudgeModelId);
    } catch {
      // ignore storage errors
    }
    setPlaygroundEvaluating(true);
    try {
      const res = await simulationApi.evaluateDialogue({
        rule_id: selectedRuleId,
        scenario_id: 'manual_playground_test',
        dialogue: playgroundMessages,
        judge_model_id: playgroundJudgeModelId,
        save_as_sample: true,
      });
      setPlaygroundEval(res.eval_report);
      if (res.sample_id) {
        setPlaygroundSavedSampleId(res.sample_id);
      }
      message.success(`白盒质检完成！得分: ${res.eval_report.score} 分 (${res.eval_report.verdict})`);
      loadHistoryBattles(selectedRuleId);
    } catch (e: any) {
      message.error(`质检评估失败: ${e.message}`);
    } finally {
      setPlaygroundEvaluating(false);
    }
  };

  // ---------------------------------------------------------------------------
  // Tab 3 操作：对练历史回看
  // ---------------------------------------------------------------------------
  const handleLoadBattleToArena = (record: any) => {
    setBattleResults([record]);
    setActiveBattleIndex(0);
    setSelectedBattleSampleId(record.sample_id);
    setActiveTab('arena');
    message.success(`已载入对弈会话【${record.sample_id}】至主对弈台`);
  };

  const historyColumns: ColumnsType<any> = [
    {
      title: '样本编号 / 会话ID',
      dataIndex: 'sample_id',
      key: 'sample_id',
      width: 240,
      render: (id: string, record) => (
        <div>
          <div className="font-mono text-xs font-semibold text-blue-900 truncate max-w-[220px]" title={id}>
            {id}
          </div>
          <div className="flex items-center gap-1 mt-1">
            <Tag color="cyan" className="text-3xs">
              {record.scenario_name || record.scenario_id}
            </Tag>
            <Tag color="default" className="text-3xs">
              {record.total_turns} 轮
            </Tag>
          </div>
        </div>
      ),
    },
    {
      title: '初检结论',
      key: 'initial_eval',
      width: 140,
      render: (_, record) => {
        const evalRep = record.initial_eval;
        if (!evalRep || !evalRep.verdict) return <span className="text-slate-400 text-xs">-</span>;
        const isPass = evalRep.verdict === 'PASS';
        return (
          <Space orientation="vertical" size={2}>
            <Tag color={isPass ? 'green' : 'red'} className="font-semibold text-xs">
              {evalRep.verdict} · {evalRep.score}分
            </Tag>
            <span className="text-3xs text-slate-500">
              {evalRep.topology_valid ? '✓ 拓扑合规' : '✗ 拓扑断路'}
            </span>
          </Space>
        );
      },
    },
    {
      title: '治理状态',
      dataIndex: 'status',
      key: 'status',
      width: 130,
      render: (st: string) => {
        let color = 'default';
        let label = st;
        if (st === 'GOLD_PASS' || st === 'MANUAL_APPROVED') {
          color = 'green';
          label = '黄金标通';
        } else if (st === 'REPAIRED_PASS') {
          color = 'blue';
          label = '修复通关';
        } else if (st === 'DISCARDED') {
          color = 'red';
          label = '质检淘汰';
        } else if (st === 'PENDING') {
          color = 'orange';
          label = '待治理复核';
        }
        return <Tag color={color}>{label}</Tag>;
      },
    },
    {
      title: '节点流转轨迹',
      key: 'trajectory',
      render: (_, record) => {
        const traj: string[] = record.initial_eval?.node_trajectory || [];
        if (traj.length === 0) return <span className="text-slate-400 text-xs">未记录</span>;
        return (
          <div className="flex flex-wrap gap-1 max-w-xs">
            {traj.slice(0, 5).map((node, i) => (
              <Tag key={i} className="text-3xs font-mono mr-0">
                {node}
              </Tag>
            ))}
            {traj.length > 5 && (
              <Tag className="text-3xs text-slate-400 mr-0">+{traj.length - 5}步</Tag>
            )}
          </div>
        );
      },
    },
    {
      title: '时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 140,
      render: (ts: number) => (
        <span className="text-xs text-slate-500">
          {ts ? new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }) : '-'}
        </span>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 170,
      render: (_, record) => (
        <Space size="small">
          <Button
            size="small"
            type="primary"
            ghost
            icon={<EyeOutlined />}
            onClick={() => handleLoadBattleToArena(record)}
          >
            回放
          </Button>
          <Button
            size="small"
            icon={<ArrowRightOutlined />}
            onClick={() => {
              navigate(`/flywheel?rule_id=${selectedRuleId}&sample_id=${record.sample_id}`);
            }}
          >
            复核
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      {/* 顶部统一业务场景与规则选择条 */}
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 bg-white p-3 rounded-lg border border-slate-200 shadow-2xs">
        <div className="flex items-center gap-2">
          <CompassOutlined className="text-blue-600 text-base" />
          <span className="font-bold text-sm text-slate-800">当前测试业务 SOP:</span>
          <Select
            value={selectedRuleId}
            onChange={(val) => {
              setSelectedRuleId(val);
              setBattleResults([]);
              setSelectedBattleSampleId('');
            }}
            className="w-72"
            options={rules.map((r) => ({
              value: r.rule_id,
              label: (
                <div className="flex items-center justify-between">
                  <span className="font-medium text-xs">{r.project_name}</span>
                  <span className="text-3xs text-slate-400">({r.rule_id})</span>
                </div>
              ),
            }))}
          />
        </div>

        {/* 顶部 Tabs 导航 */}
        <div className="w-full sm:w-auto">
          <Tabs
            activeKey={activeTab}
            onChange={setActiveTab}
            size="small"
            type="card"
            className="mb-0"
            items={[
              {
                key: 'arena',
                label: (
                  <span>
                    <RobotOutlined /> AI 自动化实战对弈
                  </span>
                ),
              },
              {
                key: 'playground',
                label: (
                  <span>
                    <UserOutlined /> 人工自由对练测试
                  </span>
                ),
              },
              {
                key: 'history',
                label: (
                  <span>
                    <HistoryOutlined /> 对练历史回看 ({historyBattles.length})
                  </span>
                ),
              },
            ]}
          />
        </div>
      </div>

      {/* ========================================================================= */}
      {/* TAB 1: 🤖 AI 实战自动化对弈                                                */}
      {/* ========================================================================= */}
      {activeTab === 'arena' && (
        <div className="space-y-4">
          {/* 对弈配置控制卡片 */}
          <Card size="small" className="shadow-2xs border-slate-200">
            {/* 顶部方案预设选择与保存条 */}
            <div className="flex items-center justify-between pb-2.5 mb-3 border-b border-slate-100 flex-wrap gap-2">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs font-bold text-slate-700">模型方案预设:</span>
                <Select
                  placeholder="选择已保存方案一键自动加载"
                  value={selectedPresetId || undefined}
                  onChange={handleApplyPreset}
                  className="w-64"
                  popupMatchSelectWidth={false}
                  allowClear
                  onClear={() => handleApplyPreset('')}
                  options={presets.map((p) => ({
                    value: p.id,
                    label: (
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-xs font-medium text-slate-800">{p.name}</span>
                        <Button
                          size="small"
                          type="text"
                          danger
                          icon={<DeleteOutlined />}
                          onClick={(e) => {
                            e.stopPropagation();
                            handleDeletePreset(p.id);
                          }}
                        />
                      </div>
                    ),
                  }))}
                />
                {selectedPresetId ? (
                  <Tag color="blue" className="text-3xs">
                    ✓ 已自动加载生效
                  </Tag>
                ) : (
                  <span className="text-3xs text-slate-400">选择方案将自动填充被测、客户与法官模型</span>
                )}
              </div>

              <Button
                size="small"
                icon={<SaveOutlined />}
                onClick={handleSaveCurrentAsPreset}
              >
                保存当前配置为新方案
              </Button>
            </div>

            <Form
              form={form}
              layout="inline"
              className="flex flex-wrap items-center gap-y-3"
              initialValues={{
                max_turns: 60,
                auto_remediate: true,
              }}
            >
              <Form.Item
                name="target_model_id"
                label="被测客服模型"
                rules={[{ required: true, message: '请选被测模型' }]}
                className="mb-0"
              >
                <Select
                  placeholder="选择被测微调模型"
                  className="w-56"
                  popupMatchSelectWidth={false}
                  options={models.map((m) => ({
                    value: m.id,
                    label: (
                      <div className="flex items-center gap-1.5 py-0.5" title={m.name}>
                        <Tag color="blue" className="text-3xs mr-0 uppercase font-mono">
                          {m.provider_type}
                        </Tag>
                        <span className="truncate text-xs">{m.name}</span>
                      </div>
                    ),
                  }))}
                />
              </Form.Item>

              <Form.Item
                name="client_model_id"
                label="模拟客户模型"
                rules={[{ required: true, message: '请选客户模型' }]}
                className="mb-0"
              >
                <Select
                  placeholder="选择模拟客户模型"
                  className="w-56"
                  popupMatchSelectWidth={false}
                  options={models.map((m) => ({
                    value: m.id,
                    label: (
                      <div className="flex items-center gap-1.5 py-0.5" title={m.name}>
                        <Tag color="cyan" className="text-3xs mr-0 uppercase font-mono">
                          {m.provider_type}
                        </Tag>
                        <span className="truncate text-xs">{m.name}</span>
                      </div>
                    ),
                  }))}
                />
              </Form.Item>

              <Form.Item
                name="judge_model_id"
                label="白盒质检法官"
                rules={[{ required: true, message: '请选裁判模型' }]}
                className="mb-0"
              >
                <Select
                  placeholder="选择法官模型"
                  className="w-56"
                  popupMatchSelectWidth={false}
                  options={models.map((m) => ({
                    value: m.id,
                    label: (
                      <div className="flex items-center gap-1.5 py-0.5" title={m.name}>
                        <Tag color="purple" className="text-3xs mr-0 uppercase font-mono">
                          {m.provider_type}
                        </Tag>
                        <span className="truncate text-xs">{m.name}</span>
                      </div>
                    ),
                  }))}
                />
              </Form.Item>

              <Form.Item
                name="max_turns"
                label="最大轮次"
                className="mb-0"
                tooltip="对弈双Agent最大交互轮数（默认60轮，微调全流程推荐60~80轮）"
              >
                <InputNumber min={5} max={120} className="w-24" />
              </Form.Item>

              <Form.Item name="scenario_ids" label="客户人设剧本" className="mb-0">
                <Select
                  mode="multiple"
                  maxTagCount={1}
                  placeholder={`全部剧本并发 (共${currentRuleSchema?.scenarios.length || 0}个)`}
                  allowClear
                  className="w-64"
                  popupMatchSelectWidth={false}
                  options={currentRuleSchema?.scenarios.map((sc) => ({
                    value: sc.id,
                    label: (
                      <div className="py-0.5" title={sc.name}>
                        <span className="font-mono text-3xs text-slate-400 mr-1.5">[{sc.id}]</span>
                        <span className="text-xs">{sc.name}</span>
                      </div>
                    ),
                  }))}
                />
              </Form.Item>

              <Form.Item name="auto_remediate" label="初检缺陷自动修复" valuePropName="checked" className="mb-0">
                <Switch />
              </Form.Item>

              <Form.Item className="mb-0 ml-auto">
                {battling ? (
                  <Button danger icon={<StopOutlined />} onClick={handleStopSimulation}>
                    中止对弈
                  </Button>
                ) : (
                  <Button type="primary" icon={<PlayCircleOutlined />} onClick={handleStartSimulation}>
                    启动实战对弈
                  </Button>
                )}
              </Form.Item>
            </Form>
          </Card>

          {/* 实战对弈主体视图：左侧聊天气泡 + 右侧 Mermaid 状态机与白盒诊断 */}
          <div className="grid grid-cols-12 gap-4 h-[720px]">
            {/* 左栏：多轮对话视窗 */}
            <div className="col-span-12 lg:col-span-6 flex flex-col h-full bg-white rounded-lg border border-slate-200 shadow-2xs overflow-hidden">
              <div className="p-3 bg-slate-50 border-b border-slate-200 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="font-bold text-sm text-slate-800">
                    {battling
                      ? currentStreamingScenarioName || '实时实战对弈中...'
                      : currentBattle?.scenario_name || '对话记录'}
                  </span>
                  {battling && (
                    <Tag color="processing" className="animate-pulse">
                      实时生成中
                    </Tag>
                  )}
                  {currentBattle && !battling && (
                    <Tag color="cyan">共 {dialogueMessages.length} 轮发言</Tag>
                  )}
                </div>

                <Button
                  size="small"
                  icon={<HistoryOutlined />}
                  onClick={() => setActiveTab('history')}
                >
                  对练历史 ({historyBattles.length})
                </Button>
              </div>

              {/* 消息滚动流 */}
              <div className="flex-1 p-4 overflow-y-auto space-y-4 bg-slate-50/50">
                {dialogueMessages.length === 0 ? (
                  <div className="h-full flex flex-col items-center justify-center text-slate-400 text-xs">
                    <RobotOutlined className="text-3xl mb-2 text-slate-300" />
                    <span>暂无对话记录，请点击上方“启动实战对弈”</span>
                  </div>
                ) : (
                  dialogueMessages.map((msg, idx) => {
                    const isCustomer = msg.role === 'user';
                    const isFocused = arenaFocusedTurn === idx;
                    const matchedDetail = (initialEval?.trajectory_details || []).find(
                      (d: any) => d.turn === idx || d.turn === idx + 1
                    );
                    const associatedNode = matchedDetail?.node_id;

                    return (
                      <div
                        key={idx}
                        id={`arena-msg-${idx}`}
                        onClick={() => {
                          setArenaFocusedTurn(idx);
                          if (associatedNode) {
                            setArenaActiveNodeId(associatedNode);
                          } else if (initialEval?.node_trajectory && initialEval.node_trajectory.length > 0) {
                            const estIdx = Math.min(Math.floor(idx / 2), initialEval.node_trajectory.length - 1);
                            setArenaActiveNodeId(initialEval.node_trajectory[estIdx]);
                          }
                        }}
                        className={`flex gap-2.5 cursor-pointer p-1.5 rounded-xl transition-all ${
                          isFocused ? 'ring-2 ring-blue-500 bg-blue-50/50 shadow-xs' : 'hover:bg-slate-50'
                        } ${isCustomer ? 'flex-row' : 'flex-row-reverse'}`}
                      >
                        <div
                          className={`w-7 h-7 rounded-full flex items-center justify-center shrink-0 text-white text-xs ${
                            isCustomer ? 'bg-blue-600' : 'bg-emerald-600'
                          }`}
                        >
                          {isCustomer ? <UserOutlined /> : <RobotOutlined />}
                        </div>

                        <div className={`max-w-[80%] ${isCustomer ? 'items-start' : 'items-end'}`}>
                          <div className="text-3xs text-slate-400 mb-1">
                            {isCustomer ? '虚拟客户 (Client)' : '被测客服 (Bot)'} · 轮次 #{idx}
                          </div>
                          <div
                            className={`p-3 rounded-2xl text-sm leading-relaxed shadow-2xs ${
                              isCustomer
                                ? 'bg-blue-50 text-blue-950 border border-blue-200 rounded-tl-none'
                                : 'bg-emerald-50 text-emerald-950 border border-emerald-200 rounded-tr-none'
                            }`}
                          >
                            <p className="whitespace-pre-wrap">{msg.content}</p>
                            {msg.thinking && (
                              <div className="mt-2 text-3xs text-slate-400 font-mono bg-white/80 p-2 rounded border border-slate-200">
                                Thinking: {msg.thinking}
                              </div>
                            )}
                          </div>
                        </div>
                      </div>
                    );
                  })
                )}

                {/* 动态结束文案 */}
                {battling ? (
                  <div className="p-3 bg-blue-50/70 border border-dashed border-blue-200 rounded text-center text-xs text-blue-700 font-medium animate-pulse">
                    💬 双方正在实时对弈中 (当前已生成 {dialogueMessages.length} 轮发言)...
                  </div>
                ) : dialogueMessages.length > 0 ? (
                  (() => {
                    const lastMsg = dialogueMessages[dialogueMessages.length - 1]?.content || '';
                    const isNormalClosing =
                      lastMsg.includes('再见') ||
                      lastMsg.includes('祝您') ||
                      lastMsg.includes('[挂断电话]') ||
                      lastMsg.includes('官方售后热线');
                    const formMaxTurns = form.getFieldValue('max_turns') || 60;
                    const isMaxTurnsReached = dialogueMessages.length >= formMaxTurns;

                    if (isNormalClosing) {
                      return (
                        <div className="p-3 bg-emerald-50/80 border border-dashed border-emerald-300 rounded text-center text-xs text-emerald-700 font-medium">
                          —— 📞 通话已触发合法收口条件并正常结束 ——
                        </div>
                      );
                    } else if (isMaxTurnsReached) {
                      return (
                        <div className="p-3 bg-amber-50/90 border border-dashed border-amber-300 rounded text-center text-xs text-amber-800 font-medium">
                          —— ⚠️ 对话已达到设定最大轮次上限（已交互 {dialogueMessages.length} 条发言），对弈截断并提交质检 ——
                        </div>
                      );
                    } else {
                      return (
                        <div className="p-3 bg-slate-50 border border-dashed border-slate-300 rounded text-center text-xs text-slate-600 font-medium">
                          —— ⏹️ 对话已结束（共 {dialogueMessages.length} 条消息）——
                        </div>
                      );
                    }
                  })()
                ) : null}
              </div>
            </div>

            {/* 右栏：Mermaid 状态机轨迹回放 + 白盒质检诊断面板 */}
            <div className="col-span-12 lg:col-span-6 flex flex-col h-full gap-4 overflow-hidden">
              {/* 上半部：Mermaid 状态机 */}
              <div className="flex-1 overflow-hidden bg-white rounded-lg border border-slate-200 shadow-2xs">
                <MermaidViewer
                  graphDefinition={currentRuleSchema?.topology_sop.mermaid_graph || ''}
                  trajectory={initialEval?.node_trajectory || []}
                  topologyValid={initialEval?.topology_valid ?? true}
                  topologyError={initialEval?.topology_error}
                  autoPlay={battleResults.length > 0}
                  activeNodeId={arenaActiveNodeId}
                  onNodeClick={(nodeId) => {
                    setArenaActiveNodeId(nodeId);
                    const matched = (initialEval?.trajectory_details || []).find(
                      (d: any) => d.node_id === nodeId
                    );
                    const targetTurn = matched
                      ? matched.turn
                      : Math.max(0, (initialEval?.node_trajectory || []).indexOf(nodeId) * 2 + 1);
                    if (targetTurn >= 0 && targetTurn < dialogueMessages.length) {
                      setArenaFocusedTurn(targetTurn);
                      const el = document.getElementById(`arena-msg-${targetTurn}`);
                      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    }
                  }}
                />
              </div>

              {/* 下半部：白盒质检诊断面板 */}
              <div className="h-64 bg-white p-4 rounded-lg border border-slate-200 shadow-2xs flex flex-col justify-between overflow-auto">
                <div>
                  <div className="flex items-center justify-between pb-2 border-b border-slate-100 flex-wrap gap-2">
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-sm text-slate-800">白盒质检诊断结论</span>
                      {initialEval && (
                        <Tag color={initialEval.verdict === 'PASS' ? 'green' : 'red'}>
                          初检: {initialEval.verdict} · {initialEval.score} 分
                        </Tag>
                      )}
                      {verifiedEval && verifiedEval.score !== undefined && verifiedEval.score !== null && (
                        <Tag color={verifiedEval.verdict === 'PASS' ? 'green' : 'red'}>
                          二次验收: {verifiedEval.verdict} · {verifiedEval.score} 分
                        </Tag>
                      )}
                    </div>

                    <div className="flex items-center gap-2">
                      {hasRemediatedData && (
                        <Button
                          size="small"
                          icon={<DiffOutlined />}
                          onClick={() => setDiffModalVisible(true)}
                        >
                          查看修复数据
                        </Button>
                      )}
                      <Button
                        size="small"
                        type="primary"
                        ghost
                        icon={<ArrowRightOutlined />}
                        onClick={() => {
                          const sampleId = currentBattle?.sample_id || '';
                          navigate(`/flywheel?rule_id=${selectedRuleId}&sample_id=${sampleId}`);
                        }}
                      >
                        前往飞轮工作台复核
                      </Button>
                    </div>
                  </div>

                  {initialEval ? (
                    <div className="mt-2 space-y-2 text-xs">
                      {/* 拓扑连通性状态 */}
                      <div className="flex items-center gap-2">
                        <span className="text-slate-500">拓扑连通性校验:</span>
                        {initialEval.topology_valid ? (
                          <span className="text-emerald-600 font-medium">✓ 图状态推进合法无跳步</span>
                        ) : (
                          <span className="text-red-600 font-medium">
                            ✗ 拓扑断路: {initialEval.topology_error}
                          </span>
                        )}
                      </div>

                      {/* 节点检查点核验清单 */}
                      {initialEval.flow_checkpoints && Object.keys(initialEval.flow_checkpoints).length > 0 && (
                        <div>
                          <span className="text-slate-500 block mb-1">节点检查点核验清单:</span>
                          <div className="flex flex-wrap gap-1">
                            {Object.entries(initialEval.flow_checkpoints).map(([cpKey, cpVal]: [string, any]) => {
                              const isPass = typeof cpVal === 'boolean' ? cpVal : cpVal?.passed ?? true;
                              return (
                                <Tag key={cpKey} color={isPass ? 'success' : 'error'} className="text-3xs">
                                  {cpKey}: {isPass ? '达标' : '缺漏/违规'}
                                </Tag>
                              );
                            })}
                          </div>
                        </div>
                      )}

                      {/* 检出缺陷列表 */}
                      <div>
                        <span className="text-slate-500 block mb-1">白盒检出缺陷项:</span>
                        {initialEval.detected_defects && initialEval.detected_defects.length > 0 ? (
                          <div className="space-y-1">
                            {initialEval.detected_defects.map((d, i) => {
                              const isTopoMeta = String(d).trim().startsWith('【拓扑检测元数据】');
                              return (
                                <div
                                  key={i}
                                  className={`flex items-start gap-1 p-1.5 rounded ${
                                    isTopoMeta ? 'text-amber-700 bg-amber-50' : 'text-red-700 bg-red-50'
                                  }`}
                                >
                                  <WarningOutlined className="mt-0.5 shrink-0" />
                                  <span>{d}</span>
                                </div>
                              );
                            })}
                          </div>
                        ) : (
                          <span className="text-emerald-600">✓ 全程无违规红线与要素缺失</span>
                        )}
                      </div>

                      {/* 专家评语 */}
                      {initialEval.expert_comments && (
                        <p className="text-slate-600 italic bg-slate-50 p-2 rounded">
                          法官评语: {initialEval.expert_comments}
                        </p>
                      )}

                      <div className="pt-1 border-t border-slate-100 flex items-center justify-end gap-3 flex-wrap">
                        <Button
                          type="link"
                          size="small"
                          icon={<ApiOutlined />}
                          className="p-0 h-5 text-3xs text-indigo-600"
                          onClick={() =>
                            openReqDebugModal(
                              '白盒初检报文与诊断结果',
                              extractDebugEntriesFromEval(initialEval, '白盒初检质检'),
                              initialEval as Record<string, any>
                            )
                          }
                        >
                          质检报文与结果
                        </Button>
                        {currentBattle?.remediation_debug?.messages?.length ? (
                          <Button
                            type="link"
                            size="small"
                            icon={<ApiOutlined />}
                            className="p-0 h-5 text-3xs text-emerald-700"
                            onClick={() =>
                              openReqDebugModal('AI 修复报文与返回结果', [
                                currentBattle.remediation_debug,
                              ])
                            }
                          >
                            修复报文与结果
                          </Button>
                        ) : null}
                      </div>
                    </div>
                  ) : (
                    <div className="py-8 flex flex-col items-center justify-center text-center text-slate-400 gap-2">
                      {battling ? (
                        <>
                          <Spin size="default" />
                          <div className="text-xs font-medium text-slate-600 mt-2">
                            {streamingProgressText || 'AI 对弈正在进行中...'}
                          </div>
                          <div className="text-3xs text-slate-400">
                            通话触发挂机或达成结束语后，将立即启动白盒拓扑连通性与规则红线质检
                          </div>
                        </>
                      ) : (
                        <div className="text-xs text-slate-400">
                          等待对弈结束后输出白盒质检诊断
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ========================================================================= */}
      {/* TAB 2: 👤 人工自由对话测试 (Human Playground)                            */}
      {/* ========================================================================= */}
      {activeTab === 'playground' && (
        <div className="space-y-4">
          <Card size="small" className="shadow-2xs border-slate-200">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-3">
                <div className="flex items-center gap-1.5">
                  <span className="text-xs font-bold text-slate-700">被测客服模型:</span>
                  <Select
                    value={playgroundTargetModelId}
                    onChange={setPlaygroundTargetModelId}
                    className="w-56"
                    popupMatchSelectWidth={false}
                    options={models.map((m) => ({
                      value: m.id,
                      label: (
                        <div className="flex items-center gap-1.5 py-0.5">
                          <Tag color="blue" className="text-3xs mr-0 uppercase font-mono">
                            {m.provider_type}
                          </Tag>
                          <span className="truncate text-xs">{m.name}</span>
                        </div>
                      ),
                    }))}
                  />
                </div>

                <div className="flex items-center gap-1.5">
                  <span className="text-xs font-bold text-slate-700">质检法官模型:</span>
                  <Select
                    value={playgroundJudgeModelId}
                    onChange={setPlaygroundJudgeModelId}
                    className="w-56"
                    popupMatchSelectWidth={false}
                    options={models.map((m) => ({
                      value: m.id,
                      label: (
                        <div className="flex items-center gap-1.5 py-0.5">
                          <Tag color="purple" className="text-3xs mr-0 uppercase font-mono">
                            {m.provider_type}
                          </Tag>
                          <span className="truncate text-xs">{m.name}</span>
                        </div>
                      ),
                    }))}
                  />
                </div>
              </div>

              <div className="flex items-center gap-2 ml-auto">
                <Button
                  icon={<RobotOutlined />}
                  onClick={handlePlaygroundBotGreeting}
                  loading={playgroundSending}
                >
                  机器人主动开场
                </Button>
                <Button icon={<ClearOutlined />} onClick={handlePlaygroundClear}>
                  清空对话
                </Button>
                <Button
                  type="primary"
                  icon={<CheckCircleOutlined />}
                  onClick={handlePlaygroundEvaluate}
                  loading={playgroundEvaluating}
                  disabled={playgroundMessages.length < 2}
                >
                  结束并提交白盒质检
                </Button>
              </div>
            </div>
          </Card>

          {/* 人工对练双栏视窗 */}
          <div className="grid grid-cols-12 gap-4 h-[720px]">
            {/* 左栏：人机交互视窗 */}
            <div className="col-span-12 lg:col-span-7 flex flex-col h-full bg-white rounded-lg border border-slate-200 shadow-2xs overflow-hidden">
              <div className="p-3 bg-slate-50 border-b border-slate-200 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <UserOutlined className="text-blue-600" />
                  <span className="font-bold text-sm text-slate-800">人工扮演客户 · 自由对练聊天台</span>
                  <Tag color="blue">当前轮数: {playgroundMessages.length}</Tag>
                </div>
                <span className="text-3xs text-slate-400">支持多轮连贯上下文交互测试</span>
              </div>

              {/* 对话气泡 */}
              <div className="flex-1 p-4 overflow-y-auto space-y-4 bg-slate-50/50">
                {playgroundMessages.length === 0 ? (
                  <div className="h-full flex flex-col items-center justify-center text-slate-400 text-xs gap-2">
                    <UserOutlined className="text-3xl text-slate-300" />
                    <span>暂无对话记录。可点击上方“机器人主动开场”，或直接在下方输入文字发起对话</span>
                  </div>
                ) : (
                  playgroundMessages.map((msg, idx) => {
                    const isHuman = msg.role === 'user';
                    const isFocused = playgroundFocusedTurn === idx;
                    const matchedDetail = (playgroundEval?.trajectory_details || []).find(
                      (d: any) => d.turn === idx || d.turn === idx + 1
                    );
                    const associatedNode = matchedDetail?.node_id;

                    return (
                      <div
                        key={idx}
                        id={`pg-msg-${idx}`}
                        onClick={() => {
                          setPlaygroundFocusedTurn(idx);
                          if (associatedNode) {
                            setPlaygroundActiveNodeId(associatedNode);
                          } else if (playgroundEval?.node_trajectory && playgroundEval.node_trajectory.length > 0) {
                            const estIdx = Math.min(Math.floor(idx / 2), playgroundEval.node_trajectory.length - 1);
                            setPlaygroundActiveNodeId(playgroundEval.node_trajectory[estIdx]);
                          }
                        }}
                        className={`flex gap-2.5 cursor-pointer p-1.5 rounded-xl transition-all ${
                          isFocused ? 'ring-2 ring-emerald-500 bg-emerald-50/50 shadow-xs' : 'hover:bg-slate-50'
                        } ${isHuman ? 'flex-row-reverse' : 'flex-row'}`}
                      >
                        <div
                          className={`w-7 h-7 rounded-full flex items-center justify-center shrink-0 text-white text-xs ${
                            isHuman ? 'bg-blue-600' : 'bg-emerald-600'
                          }`}
                        >
                          {isHuman ? <UserOutlined /> : <RobotOutlined />}
                        </div>

                        <div className={`max-w-[80%] ${isHuman ? 'items-end' : 'items-start'}`}>
                          <div className="text-3xs text-slate-400 mb-1">
                            {isHuman ? '人工测试员 (客户)' : '被测客服 (Bot)'} · #{idx}
                          </div>
                          <div
                            className={`p-3 rounded-2xl text-sm leading-relaxed shadow-2xs ${
                              isHuman
                                ? 'bg-blue-600 text-white rounded-tr-none'
                                : 'bg-emerald-50 text-emerald-950 border border-emerald-200 rounded-tl-none'
                            }`}
                          >
                            <p className="whitespace-pre-wrap">{msg.content}</p>
                            {msg.thinking && (
                              <div className="mt-2 text-3xs text-slate-400 font-mono bg-white/90 p-2 rounded border border-slate-200">
                                Thinking: {msg.thinking}
                              </div>
                            )}
                          </div>
                        </div>
                      </div>
                    );
                  })
                )}

                {playgroundSending && (
                  <div className="flex gap-2.5 flex-row items-center text-slate-400 text-xs">
                    <Spin size="small" />
                    <span>被测模型正在思考并生成回复...</span>
                  </div>
                )}
              </div>

              {/* 底部输入框 */}
              <div className="p-3 bg-white border-t border-slate-200 flex items-end gap-2">
                <TextArea
                  value={playgroundInput}
                  onChange={(e) => setPlaygroundInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault();
                      handlePlaygroundSend();
                    }
                  }}
                  placeholder="以客户身份输入回复... (按 Enter 发送，Shift+Enter 换行)"
                  autoSize={{ minRows: 2, maxRows: 4 }}
                  disabled={playgroundSending}
                  className="resize-none"
                />
                <Button
                  type="primary"
                  icon={<SendOutlined />}
                  onClick={handlePlaygroundSend}
                  loading={playgroundSending}
                  disabled={!playgroundInput.trim()}
                  className="h-10"
                >
                  发送
                </Button>
              </div>
            </div>

            {/* 右栏：Mermaid 流程图 + 人工质检结论 */}
            <div className="col-span-12 lg:col-span-5 flex flex-col h-full gap-4 overflow-hidden">
              <div className="flex-1 overflow-hidden bg-white rounded-lg border border-slate-200 shadow-2xs">
                <MermaidViewer
                  graphDefinition={currentRuleSchema?.topology_sop.mermaid_graph || ''}
                  trajectory={playgroundEval?.node_trajectory || []}
                  topologyValid={playgroundEval?.topology_valid ?? true}
                  topologyError={playgroundEval?.topology_error}
                  autoPlay={Boolean(playgroundEval)}
                  activeNodeId={playgroundActiveNodeId}
                  onNodeClick={(nodeId) => {
                    setPlaygroundActiveNodeId(nodeId);
                    const matched = (playgroundEval?.trajectory_details || []).find(
                      (d: any) => d.node_id === nodeId
                    );
                    const targetTurn = matched
                      ? matched.turn
                      : Math.max(0, (playgroundEval?.node_trajectory || []).indexOf(nodeId) * 2 + 1);
                    if (targetTurn >= 0 && targetTurn < playgroundMessages.length) {
                      setPlaygroundFocusedTurn(targetTurn);
                      const el = document.getElementById(`pg-msg-${targetTurn}`);
                      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    }
                  }}
                />
              </div>

              {/* 人工对话质检诊断面板 */}
              <div className="h-64 bg-white p-4 rounded-lg border border-slate-200 shadow-2xs flex flex-col justify-between overflow-auto">
                <div>
                  <div className="flex items-center justify-between pb-2 border-b border-slate-100 flex-wrap gap-2">
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-sm text-slate-800">人工对话质检报告</span>
                      {playgroundEval && (
                        <Tag color={playgroundEval.verdict === 'PASS' ? 'green' : 'red'}>
                          判定: {playgroundEval.verdict} · {playgroundEval.score} 分
                        </Tag>
                      )}
                    </div>

                    {playgroundSavedSampleId && (
                      <Button
                        size="small"
                        type="primary"
                        ghost
                        icon={<ArrowRightOutlined />}
                        onClick={() => {
                          navigate(`/flywheel?rule_id=${selectedRuleId}&sample_id=${playgroundSavedSampleId}`);
                        }}
                      >
                        已入库，前往飞轮复核
                      </Button>
                    )}
                  </div>

                  {playgroundEval ? (
                    <div className="mt-2 space-y-2 text-xs">
                      <div className="flex items-center gap-2">
                        <span className="text-slate-500">拓扑连通性:</span>
                        {playgroundEval.topology_valid ? (
                          <span className="text-emerald-600 font-medium">✓ 全流程拓扑合规无断路</span>
                        ) : (
                          <span className="text-red-600 font-medium">
                            ✗ 拓扑断路: {playgroundEval.topology_error}
                          </span>
                        )}
                      </div>

                      <div>
                        <span className="text-slate-500 block mb-1">检出缺陷项:</span>
                        {playgroundEval.detected_defects && playgroundEval.detected_defects.length > 0 ? (
                          <div className="space-y-1">
                            {playgroundEval.detected_defects.map((d, i) => {
                              const isTopoMeta = String(d).trim().startsWith('【拓扑检测元数据】');
                              return (
                                <div
                                  key={i}
                                  className={`flex items-start gap-1 p-1.5 rounded ${
                                    isTopoMeta ? 'text-amber-700 bg-amber-50' : 'text-red-700 bg-red-50'
                                  }`}
                                >
                                  <WarningOutlined className="mt-0.5 shrink-0" />
                                  <span>{d}</span>
                                </div>
                              );
                            })}
                          </div>
                        ) : (
                          <span className="text-emerald-600">✓ 人机实战全程合规通过</span>
                        )}
                      </div>

                      {playgroundEval.expert_comments && (
                        <p className="text-slate-600 italic bg-slate-50 p-2 rounded">
                          法官评语: {playgroundEval.expert_comments}
                        </p>
                      )}

                      <div className="pt-1 border-t border-slate-100 flex items-center justify-end">
                        <Button
                          type="link"
                          size="small"
                          icon={<ApiOutlined />}
                          className="p-0 h-5 text-3xs text-indigo-600"
                          onClick={() =>
                            openReqDebugModal(
                              '人工对练质检报文与诊断结果',
                              extractDebugEntriesFromEval(playgroundEval, '人工对话白盒质检'),
                              playgroundEval as Record<string, any>
                            )
                          }
                        >
                          质检报文与结果
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <div className="py-8 flex flex-col items-center justify-center text-center text-slate-400 gap-2">
                      {playgroundEvaluating ? (
                        <>
                          <Spin size="default" />
                          <div className="text-xs text-slate-600">
                            法官模型正在深度比对 Mermaid 状态机图论与规则红线...
                          </div>
                        </>
                      ) : (
                        <div className="text-xs text-slate-400">
                          完成人机对练后，点击上方“结束并提交白盒质检”查看报告与状态机高亮回放
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ========================================================================= */}
      {/* TAB 3: 📋 对练历史只读回看视图 (样本删改统一归数据飞轮工作台)                  */}
      {/* ========================================================================= */}
      {activeTab === 'history' && (
        <Card size="small" className="shadow-2xs border-slate-200">
          <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
            <div className="flex flex-wrap items-center gap-3">
              <div className="flex items-center gap-1.5">
                <span className="text-xs font-bold text-slate-700">质检状态筛选:</span>
                <Select
                  value={historyStatusFilter}
                  onChange={setHistoryStatusFilter}
                  className="w-36"
                  options={[
                    { value: 'all', label: '全部状态' },
                    { value: 'PENDING', label: '🟡 待治理复核 (PENDING)' },
                    { value: 'GOLD_PASS', label: '🟢 黄金标通 (GOLD)' },
                    { value: 'REPAIRED_PASS', label: '🔵 修复通关' },
                    { value: 'DISCARDED', label: '🔴 质检淘汰 (DISCARDED)' },
                  ]}
                />
              </div>

              <Button
                icon={<ReloadOutlined />}
                onClick={() => loadHistoryBattles(selectedRuleId, historyStatusFilter)}
                loading={historyLoading}
              >
                刷新列表
              </Button>
            </div>

            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-500">对练仅负责产生对话，样本池管理与删除请前往飞轮工作台</span>
              <Button
                type="primary"
                ghost
                icon={<ArrowRightOutlined />}
                onClick={() => navigate(`/flywheel?rule_id=${selectedRuleId}`)}
              >
                前往飞轮工作台管理样本池
              </Button>
            </div>
          </div>

          <Table
            rowKey="sample_id"
            columns={historyColumns}
            dataSource={historyBattles}
            loading={historyLoading}
            pagination={{
              current: historyPagination.current,
              pageSize: historyPagination.pageSize,
              showSizeChanger: true,
              pageSizeOptions: ['10', '20', '50', '100'],
              showTotal: (total) => `共 ${total} 条样本池历史对话记录（只读回看）`,
              onChange: (page, pageSize) => {
                setHistoryPagination({ current: page, pageSize });
              },
            }}
            size="middle"
          />
        </Card>
      )}

      {/* 保存为新方案模态框 */}
      <Modal
        title="保存当前模型组合为新方案"
        open={isSavePresetModalOpen}
        onOk={handleConfirmSavePreset}
        onCancel={() => setIsSavePresetModalOpen(false)}
        okText="保存方案"
        cancelText="取消"
      >
        <div className="py-3 space-y-3">
          <Text className="text-xs text-slate-600">
            方案将记住当前选择的【被测模型】、【客户模拟器】、【质检法官】与【AI修复模型】，供后续随时一键套用：
          </Text>
          <Input
            placeholder="例如: DeepSeek-V4 实战对弈组合 / 极速自测方案"
            value={newPresetName}
            onChange={(e) => setNewPresetName(e.target.value)}
            maxLength={32}
            autoFocus
          />
        </div>
      </Modal>

      {/* Monaco 双栏 Diff 模态框 */}
      {diffModalVisible && currentBattle && (
        <MonacoDiffModal
          visible={diffModalVisible}
          onClose={() => setDiffModalVisible(false)}
          originalMessages={currentBattle.dialogue || []}
          remediatedMessages={currentBattle.remediated || []}
          onSave={async (newMsgs) => {
            try {
              await flywheelApi.updateSample(currentBattle.sample_id, {
                remediated_messages: newMsgs,
                status: 'REPAIRED_PASS',
              });
              message.success('修复数据已成功保存！');
              setDiffModalVisible(false);
              loadHistoryBattles(selectedRuleId);
            } catch (e: any) {
              message.error(`保存失败: ${e.message}`);
            }
          }}
        />
      )}

      <RequestDebugModal
        open={reqDebugVisible}
        onClose={() => setReqDebugVisible(false)}
        title={reqDebugTitle}
        entries={reqDebugEntries}
        rawReport={reqDebugRawReport}
      />
    </div>
  );
};
