import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  Alert,
  Tabs,
  Divider,
  Form,
  Input,
  Select,
  InputNumber,
  Switch,
  Button,
  Table,
  Card,
  Tag,
  Space,
  Modal,
  message,
  Typography,
  Popconfirm,
  Spin,
  Tooltip,
} from 'antd';
import {
  SaveOutlined,
  PlusOutlined,
  CopyOutlined,
  BranchesOutlined,
  FileTextOutlined,
  SecurityScanOutlined,
  ToolOutlined,
  TeamOutlined,
  QuestionCircleOutlined,
  ExperimentOutlined,
  PlayCircleOutlined,
  ThunderboltOutlined,
  ReloadOutlined,
  CheckCircleOutlined,
  CodeOutlined,
  DatabaseOutlined,
  DownloadOutlined,
} from '@ant-design/icons';
import { rulesApi, modelsApi } from '../api';
import { MermaidViewer } from '../components/MermaidViewer';
import {
  copyTextSafely,
  exportPromptDebugJson,
  exportPromptDebugMarkdown,
} from '../utils/promptDebugExport';
import type {
  RuleSummary,
  UniversalRuleSchema,
  NodeCheckpoint,
  PersonaDefinition,
  FAQItem,
  FAQInspectionConfig,
  EmbeddingConfig,
  DefaultTemplatesResponse,
  DebugPromptResponse,
  TestPromptResponse,
  RegisteredModel,
  RulePromptDebugType,
} from '../types';

const { Text, Paragraph } = Typography;
const { TextArea } = Input;

const formatMarkdownBulletsText = (text: string): string => {
  if (!text || !text.trim()) return '';
  return text
    .split('\n')
    .map((line) => {
      const cleaned = line.trim().replace(/^[-*]\s+/, '').trim();
      return cleaned ? `- ${cleaned}` : '';
    })
    .filter(Boolean)
    .join('\n');
};

const arrayToMarkdownBullets = (items?: string[]): string => {
  if (!items || items.length === 0) return '';
  return items
    .map((item) => {
      const cleaned = String(item || '').trim().replace(/^[-*]\s+/, '').trim();
      return cleaned ? `- ${cleaned}` : '';
    })
    .filter(Boolean)
    .join('\n');
};

const parseMarkdownBullets = (text: string): string[] => {
  if (!text || !text.trim()) return [];
  return text
    .split('\n')
    .map((line) => line.trim().replace(/^[-*]\s+/, '').trim())
    .filter(Boolean);
};

const VALID_RULE_TABS = new Set(['basic', 'mermaid', 'faq', 'checkpoints', 'remediation', 'scenarios']);
const VALID_DEBUG_PROMPT_TYPES = new Set<RulePromptDebugType>([
  'trajectory_extract',
  'judge',
  'judge_plain',
  'faq_extract',
  'faq_judge',
  'remediate',
  'client_simulator',
]);

export const RuleCenter: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [ruleSummaries, setRuleSummaries] = useState<RuleSummary[]>([]);
  const [selectedRuleId, setSelectedRuleId] = useState<string>('');
  const [currentRule, setCurrentRule] = useState<UniversalRuleSchema | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [saving, setSaving] = useState<boolean>(false);

  // Mermaid 实时编辑预览
  const [mermaidText, setMermaidText] = useState<string>('');
  const [ruleSearch, setRuleSearch] = useState<string>('');
  const [activeTabKey, setActiveTabKey] = useState<string>('basic');
  const [incomingPromptKey, setIncomingPromptKey] = useState<RulePromptDebugType | null>(null);
  const [showMermaidPanel, setShowMermaidPanel] = useState<boolean>(true);
  const [redlinesText, setRedlinesText] = useState<string>('');
  const [rubricText, setRubricText] = useState<string>('');

  // 从提示词管理中心带参直达（/rules?tab=...&prompt=...）时自动定位 Tab 与调试引导
  useEffect(() => {
    const tabParam = searchParams.get('tab');
    const promptParam = searchParams.get('prompt') as RulePromptDebugType | null;
    if (tabParam && VALID_RULE_TABS.has(tabParam)) {
      setActiveTabKey(tabParam);
    }
    if (promptParam && VALID_DEBUG_PROMPT_TYPES.has(promptParam)) {
      setIncomingPromptKey(promptParam);
    }
  }, [searchParams]);

  // 客户画像编辑模态框
  const [personaModalVisible, setPersonaModalVisible] = useState<boolean>(false);
  const [editingPersona, setEditingPersona] = useState<PersonaDefinition | null>(null);
  const [personaForm] = Form.useForm();

  // 节点检查点编辑模态框
  const [checkpointModalVisible, setCheckpointModalVisible] = useState<boolean>(false);
  const [editingNodeKey, setEditingNodeKey] = useState<string>('');
  const [checkpointForm] = Form.useForm();

  // FAQ 知识库条目编辑模态框
  const [faqModalVisible, setFaqModalVisible] = useState<boolean>(false);
  const [editingFaqId, setEditingFaqId] = useState<string>('');
  const [faqForm] = Form.useForm();

  // Prompt 模板与在线调试实测状态
  const [defaultTemplates, setDefaultTemplates] = useState<DefaultTemplatesResponse | null>(null);
  const [registeredModels, setRegisteredModels] = useState<RegisteredModel[]>([]);
  const [debugModalVisible, setDebugModalVisible] = useState<boolean>(false);
  const [debugPromptType, setDebugPromptType] = useState<RulePromptDebugType>('judge');
  const [debugScenarioId, setDebugScenarioId] = useState<string>('');
  const [debugLoading, setDebugLoading] = useState<boolean>(false);
  const [debugData, setDebugData] = useState<DebugPromptResponse | null>(null);
  const [testModelId, setTestModelId] = useState<string>('');
  const [testRunning, setTestRunning] = useState<boolean>(false);
  const [testResult, setTestResult] = useState<TestPromptResponse | null>(null);

  const loadRules = async () => {
    setLoading(true);
    try {
      const [list, tpls, models] = await Promise.all([
        rulesApi.list(),
        rulesApi.getDefaultTemplates().catch(() => null),
        modelsApi.list().catch(() => []),
      ]);
      setRuleSummaries(list);
      if (tpls) setDefaultTemplates(tpls);
      if (models) {
        setRegisteredModels(models);
        if (models.length > 0 && !testModelId) {
          setTestModelId(models[0].id);
        }
      }
      if (list.length > 0 && !selectedRuleId) {
        setSelectedRuleId(list[0].rule_id);
      }
    } catch (e: any) {
      message.error(`获取规则列表失败: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadRules();
  }, []);

  const loadRuleDetail = async (ruleId: string) => {
    setLoading(true);
    try {
      const data = await rulesApi.get(ruleId);
      setCurrentRule(data);
      setMermaidText(data.topology_sop?.mermaid_graph || '');
      setRedlinesText(arrayToMarkdownBullets(data.inspection_rules?.global_redlines));
      setRubricText(formatMarkdownBulletsText(data.inspection_rules?.custom_rubric || ''));
    } catch (e: any) {
      message.error(`获取规则详情失败: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (selectedRuleId && currentRule?.rule_id !== selectedRuleId) {
      loadRuleDetail(selectedRuleId);
    }
  }, [selectedRuleId, currentRule?.rule_id]);

  const handleSave = async () => {
    if (!currentRule) return;
    setSaving(true);
    try {
      const formattedRedlines = parseMarkdownBullets(redlinesText);
      const formattedRubric = formatMarkdownBulletsText(rubricText);
      const updatedRule: UniversalRuleSchema = {
        ...currentRule,
        topology_sop: {
          ...currentRule.topology_sop,
          mermaid_graph: mermaidText,
        },
        inspection_rules: {
          ...currentRule.inspection_rules,
          global_redlines: formattedRedlines,
          custom_rubric: formattedRubric,
        },
      };
      setRedlinesText(arrayToMarkdownBullets(formattedRedlines));
      setRubricText(formattedRubric);
      setCurrentRule(updatedRule);
      await rulesApi.save(updatedRule);
      message.success('场景规则配置已持久化保存');
      loadRules();
    } catch (e: any) {
      message.error(`保存失败: ${e.message}`);
    } finally {
      setSaving(false);
    }
  };

  const handleDuplicateRule = () => {
    if (!currentRule) return;
    const newId = `${currentRule.rule_id}_copy_${Date.now().toString(36)}`;
    setSelectedRuleId(newId);
    setCurrentRule({
      ...currentRule,
      rule_id: newId,
      project_name: `${currentRule.project_name} (副本)`,
    });
    message.success('已复制为新规则模板，修改后点击保存即可');
  };

  const buildDraftRuleSnapshot = (): UniversalRuleSchema | null => {
    if (!currentRule) return null;
    return {
      ...currentRule,
      topology_sop: {
        ...currentRule.topology_sop,
        mermaid_graph: mermaidText,
      },
      inspection_rules: {
        ...currentRule.inspection_rules,
        global_redlines: parseMarkdownBullets(redlinesText),
        custom_rubric: formatMarkdownBulletsText(rubricText),
      },
    };
  };

  const getTemplateByType = (type: RulePromptDebugType): string | undefined => {
    if (!currentRule) return undefined;
    if (type === 'trajectory_extract') {
      return currentRule.topology_sop?.trajectory_extract_prompt_template;
    }
    if (type === 'judge') {
      return currentRule.inspection_rules?.judge_prompt_template;
    }
    if (type === 'judge_plain') {
      return undefined;
    }
    if (type === 'faq_extract') {
      return currentRule.faq_inspection?.faq_extract_prompt_template;
    }
    if (type === 'faq_judge') {
      return currentRule.faq_inspection?.faq_judge_prompt_template;
    }
    if (type === 'client_simulator') {
      return undefined;
    }
    return currentRule.remediation_config?.remediation_prompt_template;
  };

  const getPromptTypeMeta = (type: RulePromptDebugType): { title: string; color: string } => {
    switch (type) {
      case 'trajectory_extract':
        return { title: '拓扑轨迹检测与槽位抽取', color: 'blue' };
      case 'judge':
        return { title: '白盒流程质检裁判 (Mermaid)', color: 'purple' };
      case 'judge_plain':
        return { title: '纯规则与红线质检 (无 Mermaid)', color: 'magenta' };
      case 'faq_extract':
        return { title: 'FAQ 提问检测与检索改写', color: 'geekblue' };
      case 'faq_judge':
        return { title: '专职 FAQ 准确度质检裁判', color: 'cyan' };
      case 'client_simulator':
        return { title: 'AI 模拟客户人设驱动', color: 'orange' };
      case 'remediate':
      default:
        return { title: '精修处方单 (双工具)', color: 'green' };
    }
  };

  // 打开 Prompt 调试与实测抽屉
  const handleOpenDebugger = async (type: RulePromptDebugType) => {
    if (!currentRule) return;
    setDebugPromptType(type);
    const defaultScId = currentRule.scenarios?.[0]?.id || '';
    setDebugScenarioId(defaultScId);
    setTestResult(null);
    setDebugModalVisible(true);
    await triggerAssemble(type, defaultScId);
  };

  const triggerAssemble = async (type: RulePromptDebugType, scId?: string) => {
    const draftRule = buildDraftRuleSnapshot();
    if (!draftRule) return;
    setDebugLoading(true);
    try {
      const currentTemplate = getTemplateByType(type);
      const res = await rulesApi.debugPrompt({
        rule_id: draftRule.rule_id,
        prompt_type: type,
        template: currentTemplate || undefined,
        scenario_id: (scId !== undefined ? scId : debugScenarioId) || undefined,
        rule_override: draftRule,
      });
      setDebugData(res);
    } catch (e: any) {
      message.error(`组装提示词失败: ${e.message}`);
    } finally {
      setDebugLoading(false);
    }
  };

  // 快捷拼装并导出调试 JSON 包
  const handleQuickExportDebug = async (type: RulePromptDebugType) => {
    const draftRule = buildDraftRuleSnapshot();
    if (!draftRule) return;
    try {
      const res = await rulesApi.debugPrompt({
        rule_id: draftRule.rule_id,
        prompt_type: type,
        template: getTemplateByType(type) || undefined,
        scenario_id: draftRule.scenarios?.[0]?.id || undefined,
        rule_override: draftRule,
      });
      const meta = getPromptTypeMeta(type);
      exportPromptDebugJson({
        title: meta.title,
        prompt_key: type,
        rule_id: res.rule_id,
        scenario_id: res.scenario_id,
        assembled_messages: res.assembled_messages,
        assembled_prompt: res.assembled_prompt,
        sample_dialogue: res.sample_dialogue,
        recommended_config: res.recommended_config,
      });
      message.success(`已导出「${meta.title}」调试报文 JSON`);
    } catch (e: any) {
      message.error(`导出调试失败: ${e.message}`);
    }
  };

  // 发起在线模型实测
  const handleRunPromptTest = async () => {
    if (!debugData?.assembled_prompt) {
      message.warning('请先组装有效的 Prompt');
      return;
    }
    if (!testModelId) {
      message.warning('请选择用于实测的模型');
      return;
    }
    setTestRunning(true);
    setTestResult(null);
    try {
      const res = await rulesApi.testPrompt({
        model_id: testModelId,
        prompt_type: debugPromptType,
        assembled_prompt: debugData.assembled_prompt,
        assembled_messages: debugData.assembled_messages,
        sample_dialogue: debugData.sample_dialogue,
      });
      setTestResult(res);
      if (res.status === 'success') {
        message.success(`模型实测成功 (耗时: ${res.latency_ms}ms)`);
      } else {
        message.error(`模型响应异常: ${res.error || '未知错误'}`);
      }
    } catch (e: any) {
      message.error(`请求测试失败: ${e.message}`);
    } finally {
      setTestRunning(false);
    }
  };

  // 保存检查点
  const handleSaveCheckpoint = async () => {
    try {
      const values = await checkpointForm.validateFields();
      if (!currentRule) return;
      const key = editingNodeKey || values.node_key;
      const newCheckpoints = {
        ...currentRule.inspection_rules.node_checkpoints,
        [key]: {
          name: values.name,
          description: values.description ? String(values.description).trim() : '',
          required_elements: values.required_elements
            ? String(values.required_elements).split('\n').map((s) => s.trim()).filter(Boolean)
            : [],
          forbidden_keywords: values.forbidden_keywords
            ? String(values.forbidden_keywords).split('\n').map((s) => s.trim()).filter(Boolean)
            : [],
        },
      };
      setCurrentRule({
        ...currentRule,
        inspection_rules: {
          ...currentRule.inspection_rules,
          node_checkpoints: newCheckpoints,
        },
      });
      setCheckpointModalVisible(false);
    } catch (e: any) {
      message.error(`保存检查点失败: ${e.message}`);
    }
  };

  // 保存 FAQ 知识库条目
  const handleSaveFAQItem = async () => {
    try {
      const values = await faqForm.validateFields();
      if (!currentRule) return;

      const defaultFaqConfig: FAQInspectionConfig = currentRule.faq_inspection || {
        enabled: true,
        retrieval_mode: 'hybrid',
        knowledge_base: [],
        top_k: 3,
        fallback_policy: '若问题超出知识库范围，指引联系官方售后热线或登录官方 App 查询，严禁捏造。',
        embedding_config: {
          provider_type: 'local_cpu',
          local_model_name_or_path: 'BAAI/bge-small-zh-v1.5',
          remote_model_name: 'text-embedding-3-small',
        },
        hybrid_weights: { lexical: 0.4, dense: 0.6 },
      };

      const newItem: FAQItem = {
        id: values.id.trim(),
        category: values.category?.trim() || '通用',
        question: values.question.trim(),
        answer: values.answer.trim(),
      };

      const existingIdx = defaultFaqConfig.knowledge_base.findIndex((it) => it.id === newItem.id);
      let updatedKB = [...defaultFaqConfig.knowledge_base];
      if (existingIdx >= 0) {
        updatedKB[existingIdx] = newItem;
      } else {
        updatedKB.push(newItem);
      }

      setCurrentRule({
        ...currentRule,
        faq_inspection: {
          ...defaultFaqConfig,
          knowledge_base: updatedKB,
        },
      });
      setFaqModalVisible(false);
      message.success(`已保存 FAQ 知识点 [${newItem.id}]`);
    } catch (e: any) {
      message.error(`保存 FAQ 失败: ${e.message}`);
    }
  };

  // 删除 FAQ 条目
  const handleDeleteFAQItem = (id: string) => {
    if (!currentRule || !currentRule.faq_inspection) return;
    const updatedKB = currentRule.faq_inspection.knowledge_base.filter((it) => it.id !== id);
    setCurrentRule({
      ...currentRule,
      faq_inspection: {
        ...currentRule.faq_inspection,
        knowledge_base: updatedKB,
      },
    });
    message.success(`已删除 FAQ 知识点 [${id}]`);
  };

  // 保存客户画像
  const handleSavePersona = async () => {
    try {
      const values = await personaForm.validateFields();
      if (!currentRule) return;
      let personaJson = {};
      try {
        personaJson = values.persona ? JSON.parse(values.persona) : {};
      } catch {
        message.error('Persona 必须为合法 JSON 字符串');
        return;
      }

      const newPersona: PersonaDefinition = {
        id: values.id,
        name: values.name,
        category: values.category || '默认分类',
        instruction: values.instruction || '',
        persona: personaJson,
      };

      const existingIndex = currentRule.scenarios.findIndex((s) => s.id === newPersona.id);
      let newScenarios = [...currentRule.scenarios];
      if (existingIndex >= 0) {
        newScenarios[existingIndex] = newPersona;
      } else {
        newScenarios.push(newPersona);
      }

      setCurrentRule({
        ...currentRule,
        scenarios: newScenarios,
      });
      setPersonaModalVisible(false);
    } catch (e: any) {
      message.error(`保存画像失败: ${e.message}`);
    }
  };

  return (
    <div className="grid grid-cols-12 gap-4 h-[calc(100vh-140px)] min-h-0">
      {/* 左侧：规则模板列表 */}
      <div className="col-span-12 xl:col-span-2 lg:col-span-3 bg-white p-4 rounded-lg border border-slate-200 shadow-2xs flex flex-col h-full min-h-0">
        <div className="flex items-center justify-between pb-3 border-b border-slate-100">
          <Text strong className="text-slate-800 text-sm">
            场景规则库
          </Text>
          <Button
            size="small"
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              const newId = `rule_${Date.now().toString(36)}`;
              setSelectedRuleId(newId);
              setCurrentRule({
                rule_id: newId,
                project_name: '新场景项目',
                description: '新场景规则描述',
                bot_config: {
                  system_prompt: '你是一名专业的外呼坐席。',
                  initiator: 'user',
                  max_turns: 40,
                  closing_keywords: ['祝您生活愉快，再见', '再见！'],
                },
                topology_sop: {
                  enabled: true,
                  mermaid_graph: 'graph TD\n  Start[开始] --> Step1[步骤1]\n  Step1 --> End[结束]',
                  entry_nodes: ['Start'],
                  terminal_nodes: ['End'],
                  allow_self_loop: true,
                  allow_slot_shortcut: true,
                  stage_groups: [],
                },
                inspection_rules: {
                  pass_score_threshold: 85,
                  global_redlines: ['严禁侮辱辱骂客户', '严禁泄露第三方个人隐私'],
                  node_checkpoints: {},
                },
                remediation_config: {
                  target_turn_range: [20, 60],
                  inject_empty_thinking: false,
                  require_assistant_ending: true,
                  strict_alternating_roles: true,
                },
                scenarios: [],
              });
              setMermaidText('graph TD\n  Start[开始] --> Step1[步骤1]\n  Step1 --> End[结束]');
              setRedlinesText('- 严禁侮辱辱骂客户\n- 严禁泄露第三方个人隐私');
              setRubricText('');
            }}
          >
            新建规则
          </Button>
        </div>

        <Input
          placeholder="搜索场景名称或ID..."
          value={ruleSearch}
          onChange={(e) => setRuleSearch(e.target.value)}
          allowClear
          className="mt-2 text-xs"
        />

        <div className="flex-1 overflow-auto mt-2 space-y-2">
          {ruleSummaries
            .filter(
              (r) =>
                r.project_name.toLowerCase().includes(ruleSearch.toLowerCase()) ||
                r.rule_id.toLowerCase().includes(ruleSearch.toLowerCase())
            )
            .map((r) => (
            <div
              key={r.rule_id}
              onClick={() => setSelectedRuleId(r.rule_id)}
              className={`p-3 rounded-lg border cursor-pointer transition-all ${
                selectedRuleId === r.rule_id
                  ? 'bg-blue-50/70 border-blue-400 text-blue-900 shadow-2xs'
                  : 'bg-white hover:bg-slate-50 border-slate-200 text-slate-700'
              }`}
            >
              <div className="font-semibold text-sm truncate">{r.project_name}</div>
              <div className="text-xs text-slate-400 font-mono mt-0.5 truncate">{r.rule_id}</div>
              <div className="text-xs text-slate-500 mt-1 line-clamp-2">{r.description || '无详细描述'}</div>
            </div>
          ))}
        </div>
      </div>

      {/* 右侧：规则详情多标签页 */}
      <div className={`col-span-12 ${showMermaidPanel ? 'xl:col-span-5 lg:col-span-5' : 'xl:col-span-10 lg:col-span-9'} bg-white p-4 rounded-lg border border-slate-200 shadow-2xs flex flex-col h-full min-h-0 overflow-hidden`}>
        {loading || !currentRule ? (
          <div className="flex-1 flex items-center justify-center">
            <Spin size="large" />
          </div>
        ) : (
          <>
            {/* 顶部标题与保存 */}
            <div className="flex flex-wrap items-center justify-between gap-2.5 pb-3 border-b border-slate-200">
              <div className="flex items-center gap-2 min-w-[220px] flex-1">
                <Input
                  value={currentRule.project_name}
                  onChange={(e) => setCurrentRule({ ...currentRule, project_name: e.target.value })}
                  className="font-bold text-base flex-1 min-w-[140px] max-w-xs"
                />
                <Tag color="blue" className="shrink-0 m-0">
                  {currentRule.rule_id}
                </Tag>
              </div>

              <div className="flex items-center gap-2 flex-wrap shrink-0">
                <Button
                  size="small"
                  icon={<BranchesOutlined />}
                  type={showMermaidPanel ? 'default' : 'primary'}
                  onClick={() => setShowMermaidPanel(!showMermaidPanel)}
                >
                  {showMermaidPanel ? '收起拓扑图' : '展开SOP拓扑图'}
                </Button>
                <Button
                  size="small"
                  icon={<CopyOutlined />}
                  onClick={handleDuplicateRule}
                >
                  基于此模板克隆
                </Button>
                <Button
                  size="small"
                  type="primary"
                  icon={<SaveOutlined />}
                  loading={saving}
                  onClick={handleSave}
                >
                  保存规则配置
                </Button>
              </div>
            </div>

            {/* 从提示词管理中心带参跳转时的动态引导与前置开关校验条 */}
            {incomingPromptKey && (() => {
              const meta = getPromptTypeMeta(incomingPromptKey);
              const mermaidOn = Boolean(currentRule.topology_sop?.enabled);
              const faqOn = Boolean(currentRule.faq_inspection?.enabled);
              const ruleTemplateValue = getTemplateByType(incomingPromptKey);
              const hasRuleOverride = Boolean(ruleTemplateValue && ruleTemplateValue.trim());

              let blockedReason: string | null = null;
              if ((incomingPromptKey === 'trajectory_extract' || incomingPromptKey === 'judge') && !mermaidOn) {
                blockedReason = `当前规则「${currentRule.project_name}」未开启「Mermaid 流程图质检」开关，无法调试「${meta.title}」。请在左侧切换已开启 Mermaid 的规则，或在「Mermaid SOP 与节点检查」Tab 开启开关。`;
              } else if (incomingPromptKey === 'judge_plain' && mermaidOn) {
                blockedReason = `当前规则「${currentRule.project_name}」已开启 Mermaid 开关，运行时走白盒流程图质检 (judge) 而非纯规则质检 (judge_plain)。请在左侧切换未开启 Mermaid 的规则，或在「Mermaid SOP 与节点检查」Tab 关闭开关。`;
              } else if ((incomingPromptKey === 'faq_extract' || incomingPromptKey === 'faq_judge') && !faqOn) {
                blockedReason = `当前规则「${currentRule.project_name}」未开启「FAQ 问答准确度质检」开关，无法调试「${meta.title}」。请在左侧切换已开启 FAQ 的规则，或在「FAQ 问答质检与知识库」Tab 开启开关。`;
              }

              const clearIncoming = () => {
                setIncomingPromptKey(null);
                setSearchParams({}, { replace: true });
              };

              return (
                <Alert
                  className="mt-2.5"
                  type={blockedReason ? 'warning' : 'info'}
                  showIcon
                  closable
                  onClose={clearIncoming}
                  message={
                    <span className="text-xs font-semibold">
                      {blockedReason
                        ? `待调试模块「${meta.title}」未满足当前规则前置条件`
                        : `已定位到「${meta.title}」调试位置（当前规则：${currentRule.project_name}）`}
                    </span>
                  }
                  description={
                    <div className="text-xs space-y-0.5">
                      {blockedReason ? (
                        <div>{blockedReason}</div>
                      ) : (
                        <div>
                          点击右侧按钮即可基于当前规则配置进行单次拼装与在线模型实测（不影响其他规则，也不会修改全局提示词）。
                          {hasRuleOverride && (
                            <span className="text-amber-700 font-semibold ml-1">
                              注意：本规则已配置专属覆盖模板，当前未跟随「提示词管理中心」全局模板；如需验证全局模板，请先清空本规则模板框。
                            </span>
                          )}
                        </div>
                      )}
                    </div>
                  }
                  action={
                    !blockedReason ? (
                      <Button
                        size="small"
                        type="primary"
                        icon={<ExperimentOutlined />}
                        onClick={() => handleOpenDebugger(incomingPromptKey)}
                      >
                        立即调试「{meta.title}」
                      </Button>
                    ) : undefined
                  }
                />
              );
            })()}

            {/* 6 大 Tabs */}
            <Tabs
              activeKey={activeTabKey}
              onChange={setActiveTabKey}
              animated={false}
              className="flex-1 overflow-hidden flex flex-col rule-center-tabs"
              items={[
                {
                  key: 'basic',
                  label: (
                    <span>
                      <FileTextOutlined /> 业务基础配置
                    </span>
                  ),
                  children: (
                    <div className="space-y-4 pr-1 pb-12">
                      <div>
                        <Text strong className="text-sm block mb-1">
                          规则描述 (Description)
                        </Text>
                        <Input
                          value={currentRule.description}
                          onChange={(e) => setCurrentRule({ ...currentRule, description: e.target.value })}
                        />
                      </div>

                      <div>
                        <Text strong className="text-sm block mb-1">
                          被测机器人 System Prompt (系统核心人设与业务指示)
                        </Text>
                        <TextArea
                          rows={8}
                          value={currentRule.bot_config.system_prompt}
                          onChange={(e) =>
                            setCurrentRule({
                              ...currentRule,
                              bot_config: { ...currentRule.bot_config, system_prompt: e.target.value },
                            })
                          }
                          className="font-mono text-xs"
                        />
                      </div>

                      <div className="grid grid-cols-2 gap-4">
                        <div>
                          <Text strong className="text-sm block mb-1">
                            开场发起角色 (Initiator)
                          </Text>
                          <Select
                            value={currentRule.bot_config.initiator}
                            onChange={(val) =>
                              setCurrentRule({
                                ...currentRule,
                                bot_config: { ...currentRule.bot_config, initiator: val },
                              })
                            }
                            className="w-full"
                          >
                            <Select.Option value="user">User (客户先说话)</Select.Option>
                            <Select.Option value="assistant">Assistant (坐席外呼主动问候)</Select.Option>
                          </Select>
                        </div>

                        <div>
                          <Text strong className="text-sm block mb-1">
                            最大允许对弈轮数 (Max Turns)
                          </Text>
                          <InputNumber
                            min={10}
                            max={100}
                            value={currentRule.bot_config.max_turns}
                            onChange={(val) =>
                              setCurrentRule({
                                ...currentRule,
                                bot_config: { ...currentRule.bot_config, max_turns: val || 40 },
                              })
                            }
                            className="w-full"
                          />
                        </div>
                      </div>

                      <div>
                        <Text strong className="text-sm block mb-1">
                          通话结束收口关键词列表 (命中则自动识别为通话完成)
                        </Text>
                        <Select
                          mode="tags"
                          value={currentRule.bot_config.closing_keywords}
                          onChange={(vals) =>
                            setCurrentRule({
                              ...currentRule,
                              bot_config: { ...currentRule.bot_config, closing_keywords: vals },
                            })
                          }
                          className="w-full"
                          placeholder="输入结束语后按 Enter"
                        />
                      </div>
                    </div>
                  ),
                },
                {
                  key: 'mermaid',
                  label: (
                    <span>
                      <BranchesOutlined /> Mermaid SOP 与节点检查
                    </span>
                  ),
                  children: (
                    <div className="space-y-4 pr-1 pb-12">
                      {/* Mermaid 总开关控制卡片 */}
                      <div className="p-3.5 bg-blue-50/50 rounded-lg border border-blue-200 flex items-center justify-between">
                        <div>
                          <div className="text-xs font-semibold text-blue-900 flex items-center gap-2">
                            <BranchesOutlined className="text-blue-600" />
                            启用 Mermaid SOP 流程图与白盒节点质检
                          </div>
                          <div className="text-xs text-slate-500 mt-0.5">
                            开启后通话结束将专职抽取节点轨迹并进行拓扑连通性校验；关闭则直接走全局红线与质检标准规则。
                          </div>
                        </div>
                        <Switch
                          checked={currentRule.topology_sop.enabled}
                          onChange={(val) =>
                            setCurrentRule({
                              ...currentRule,
                              topology_sop: { ...currentRule.topology_sop, enabled: val },
                            })
                          }
                        />
                      </div>

                      {currentRule.topology_sop.enabled ? (
                        <>
                          {/* Mermaid 源码编辑与高级动力学参数 */}
                          <div>
                            <div className="flex items-center justify-between mb-1">
                              <Text strong className="text-xs text-slate-700">
                                Mermaid 状态机流程图源码
                              </Text>
                              <Button size="small" type="link" onClick={() => setMermaidText(mermaidText)}>
                                刷新渲染
                              </Button>
                            </div>
                            <TextArea
                              rows={8}
                              value={mermaidText}
                              onChange={(e) => setMermaidText(e.target.value)}
                              className="font-mono text-xs bg-slate-50"
                            />
                          </div>

                          {/* 拓扑动力学开关 */}
                          <div className="p-3 bg-slate-50 rounded border border-slate-200 space-y-3">
                            <Text strong className="text-xs text-slate-700 block">
                              图论与状态机动力学刚性约束
                            </Text>
                            <div className="flex items-center justify-between">
                              <span className="text-xs text-slate-600">允许节点内自旋 FAQ 安抚 (Allow Self-Loop)</span>
                              <Switch
                                checked={currentRule.topology_sop.allow_self_loop}
                                onChange={(val) =>
                                  setCurrentRule({
                                    ...currentRule,
                                    topology_sop: { ...currentRule.topology_sop, allow_self_loop: val },
                                  })
                                }
                              />
                            </div>
                            <div className="flex items-center justify-between">
                              <div className="flex items-center gap-1.5">
                                <span className="text-xs text-slate-600 font-medium">允许下游拓扑顺承跳步 (Allow Downstream Shortcut)</span>
                                <Tooltip title="基于大模型语义识别已收集信息后，允许跳过中间节点顺承推进至下游；图论引擎会自动校验目标节点在拓扑中是否属于合法下游可达支线，严禁窜入互斥的并行分支或逆向倒流">
                                  <QuestionCircleOutlined className="text-blue-500 cursor-pointer text-xs" />
                                </Tooltip>
                              </div>
                              <Switch
                                checked={currentRule.topology_sop.allow_slot_shortcut}
                                onChange={(val) =>
                                  setCurrentRule({
                                    ...currentRule,
                                    topology_sop: { ...currentRule.topology_sop, allow_slot_shortcut: val },
                                  })
                                }
                              />
                            </div>
                          </div>

                          {/* 入口与出口节点 */}
                          <div className="grid grid-cols-2 gap-2">
                            <div>
                              <span className="text-xs text-slate-500 block mb-1">法定入口节点 (Entry Nodes)</span>
                              <Select
                                mode="tags"
                                value={currentRule.topology_sop.entry_nodes}
                                onChange={(vals) =>
                                  setCurrentRule({
                                    ...currentRule,
                                    topology_sop: { ...currentRule.topology_sop, entry_nodes: vals },
                                  })
                                }
                                className="w-full text-xs"
                              />
                            </div>
                            <div>
                              <span className="text-xs text-slate-500 block mb-1">法定终结节点 (Terminal Nodes)</span>
                              <Select
                                mode="tags"
                                value={currentRule.topology_sop.terminal_nodes}
                                onChange={(vals) =>
                                  setCurrentRule({
                                    ...currentRule,
                                    topology_sop: { ...currentRule.topology_sop, terminal_nodes: vals },
                                  })
                                }
                                className="w-full text-xs"
                              />
                            </div>
                          </div>

                          <Divider className="my-2" />

                          {/* 节点检查点表格 */}
                      <div>
                        <div className="flex items-center justify-between mb-2">
                          <Text strong className="text-sm text-slate-800">
                            节点精细化检查点 (Node Checkpoints)
                          </Text>
                          <Button
                            size="small"
                            icon={<PlusOutlined />}
                            onClick={() => {
                              setEditingNodeKey('');
                              checkpointForm.resetFields();
                              setCheckpointModalVisible(true);
                            }}
                          >
                            添加节点检查点
                          </Button>
                        </div>
                        <Table
                          size="small"
                          pagination={false}
                          rowKey="key"
                          dataSource={Object.entries(currentRule.inspection_rules.node_checkpoints).map(
                            ([key, cp]) => ({
                              key,
                              ...cp,
                            })
                          )}
                          columns={[
                            { title: '节点ID', dataIndex: 'key', width: 90 },
                            {
                              title: '节点名称 & 说明',
                              dataIndex: 'name',
                              width: 150,
                              render: (name: string, record: any) => (
                                <div>
                                  <div className="font-medium text-slate-800">{name}</div>
                                  {record.description ? (
                                    <div className="text-3xs text-slate-400 mt-0.5 leading-snug">
                                      {record.description}
                                    </div>
                                  ) : null}
                                </div>
                              ),
                            },
                            {
                              title: '必含要素 & 违禁词',
                              dataIndex: 'required_elements',
                              render: (elems: string[], record: any) => (
                                <Space size={[0, 4]} wrap>
                                  {(elems || []).map((el, i) => (
                                    <Tag key={`req-${i}`} color="blue">
                                      {el}
                                    </Tag>
                                  ))}
                                  {(record.forbidden_keywords || []).map((kw: string, i: number) => (
                                    <Tag key={`forb-${i}`} color="red">
                                      !{kw}
                                    </Tag>
                                  ))}
                                </Space>
                              ),
                            },
                            {
                              title: '操作',
                              width: 100,
                              render: (_, record: any) => (
                                <Space size="small">
                                  <Button
                                    type="link"
                                    size="small"
                                    onClick={() => {
                                      setEditingNodeKey(record.key);
                                      checkpointForm.setFieldsValue({
                                        node_key: record.key,
                                        name: record.name,
                                        description: record.description || '',
                                        required_elements: (record.required_elements || []).join('\n'),
                                        forbidden_keywords: (record.forbidden_keywords || []).join('\n'),
                                      });
                                      setCheckpointModalVisible(true);
                                    }}
                                  >
                                    编辑
                                  </Button>
                                  <Popconfirm
                                    title="删除检查点？"
                                    onConfirm={() => {
                                      const newCp = { ...currentRule.inspection_rules.node_checkpoints };
                                      delete newCp[record.key];
                                      setCurrentRule({
                                        ...currentRule,
                                        inspection_rules: {
                                          ...currentRule.inspection_rules,
                                          node_checkpoints: newCp,
                                        },
                                      });
                                    }}
                                  >
                                    <Button type="link" danger size="small">
                                      删除
                                    </Button>
                                  </Popconfirm>
                                </Space>
                              ),
                            },
                          ]}
                        />
                      </div>

                          {/* 拓扑轨迹检测 Prompt 模板与拼装导出调试 */}
                          <div className="p-4 bg-blue-50/50 rounded border border-blue-200 space-y-3">
                            <div className="flex items-center justify-between flex-wrap gap-2">
                              <div>
                                <div className="font-semibold text-sm text-blue-950 flex items-center gap-1.5 flex-wrap">
                                  <BranchesOutlined className="text-blue-600" />
                                  拓扑轨迹检测 Prompt 模板与拼装调试 (Step A1 · Trajectory Extract)
                                  {currentRule.topology_sop?.trajectory_extract_prompt_template?.trim() ? (
                                    <Tag color="orange" className="m-0 text-3xs">
                                      本规则专属覆盖（不跟随全局）
                                    </Tag>
                                  ) : (
                                    <Tag color="green" className="m-0 text-3xs">
                                      留空 · 跟随全局激活模板
                                    </Tag>
                                  )}
                                </div>
                                <div className="text-xs text-blue-800/80 mt-0.5 space-y-0.5">
                                  <div>
                                    用于专职识别坐席推进的主节点 (node_id) 与客户提前交代槽位 (user_slots)。支持占位符：{'{mermaid_sop}'}, {'{node_checkpoints_summary}'}, {'{dialogue}'}。
                                  </div>
                                  <div className="text-amber-700">
                                    作用域说明：仅作用于当前规则（不影响其他规则与全局提示词中心）；一旦填写并保存（含载入默认模板后保存）将屏蔽全局更新，需跟随全局请保持留空。
                                  </div>
                                </div>
                              </div>
                              <Space wrap>
                                {currentRule.topology_sop?.trajectory_extract_prompt_template?.trim() && (
                                  <Button
                                    size="small"
                                    danger
                                    ghost
                                    onClick={() => {
                                      setCurrentRule({
                                        ...currentRule,
                                        topology_sop: {
                                          ...currentRule.topology_sop,
                                          trajectory_extract_prompt_template: '',
                                        },
                                      });
                                      message.info('已清空本规则专属覆盖，保存规则后将跟随全局激活模板');
                                    }}
                                  >
                                    清空跟随全局
                                  </Button>
                                )}
                                <Button
                                  size="small"
                                  onClick={() => {
                                    if (defaultTemplates?.trajectory_extract_template) {
                                      setCurrentRule({
                                        ...currentRule,
                                        topology_sop: {
                                          ...currentRule.topology_sop,
                                          trajectory_extract_prompt_template:
                                            defaultTemplates.trajectory_extract_template,
                                        },
                                      });
                                      message.info('已载入系统内置拓扑轨迹检测 Prompt 模板');
                                    }
                                  }}
                                >
                                  载入内置默认模板
                                </Button>
                                <Button
                                  size="small"
                                  icon={<DownloadOutlined />}
                                  onClick={() => handleQuickExportDebug('trajectory_extract')}
                                >
                                  导出调试报文
                                </Button>
                                <Button
                                  type="primary"
                                  size="small"
                                  icon={<ExperimentOutlined />}
                                  onClick={() => handleOpenDebugger('trajectory_extract')}
                                >
                                  拼装预览与实测
                                </Button>
                              </Space>
                            </div>

                            <Input.TextArea
                              rows={5}
                              placeholder={
                                defaultTemplates?.trajectory_extract_template ||
                                '留空自动使用系统内置 / 提示词管理中心激活的拓扑轨迹抽取模板'
                              }
                              value={currentRule.topology_sop?.trajectory_extract_prompt_template || ''}
                              onChange={(e) =>
                                setCurrentRule({
                                  ...currentRule,
                                  topology_sop: {
                                    ...currentRule.topology_sop,
                                    trajectory_extract_prompt_template: e.target.value,
                                  },
                                })
                              }
                              className="font-mono text-xs"
                            />
                          </div>
                        </>
                      ) : (
                        <div className="p-8 text-center bg-slate-50 rounded-lg border border-dashed border-slate-300 my-4">
                          <BranchesOutlined className="text-3xl text-slate-400 mb-2" />
                          <div className="text-sm font-semibold text-slate-600">未启用 Mermaid 流程图质检</div>
                          <div className="text-xs text-slate-400 mt-1 max-w-md mx-auto">
                            当前场景在对弈与质检时将直接依据【质检标准与红线】中的规则打分，无需进行 SOP 状态机节点轨迹提取。如需启用图拓扑质检，请开启上方开关。
                          </div>
                        </div>
                      )}
                    </div>),
                },
                {
                  key: 'faq',
                  label: (
                    <span>
                      <QuestionCircleOutlined /> FAQ 问答质检与知识库
                    </span>
                  ),
                  children: (
                    <div className="space-y-4 pr-1 pb-12">
                      {/* FAQ 总开关控制卡片 */}
                      <div className="p-3.5 bg-emerald-50/50 rounded-lg border border-emerald-200 flex items-center justify-between">
                        <div>
                          <div className="text-xs font-semibold text-emerald-950 flex items-center gap-2">
                            <QuestionCircleOutlined className="text-emerald-600" />
                            启用 FAQ 问答准确度质检与混合检索 (Hybrid FAQ Inspection)
                          </div>
                          <div className="text-xs text-slate-500 mt-0.5">
                            与 Mermaid 流程开关正交独立（可同时开启）。开启后系统将自动识别客户插播提问，基于知识库执行混合检索并由专职法官核对政策准确度。
                          </div>
                        </div>
                        <Switch
                          checked={currentRule.faq_inspection?.enabled || false}
                          onChange={(val) => {
                            const defaultFaqConfig: FAQInspectionConfig = currentRule.faq_inspection || {
                              enabled: val,
                              retrieval_mode: 'hybrid',
                              knowledge_base: [],
                              top_k: 3,
                              fallback_policy: '若问题超出知识库范围，必须委婉说明并指引联系官方售后热线或登录官方 App 查询，严禁凭空捏造政策。',
                              embedding_config: {
                                provider_type: 'local_cpu',
                                local_model_name_or_path: 'BAAI/bge-small-zh-v1.5',
                                remote_model_name: 'text-embedding-3-small',
                              },
                              hybrid_weights: { lexical: 0.4, dense: 0.6 },
                            };
                            setCurrentRule({
                              ...currentRule,
                              faq_inspection: {
                                ...defaultFaqConfig,
                                enabled: val,
                              },
                            });
                          }}
                        />
                      </div>

                      {currentRule.faq_inspection?.enabled ? (
                        <>
                          {/* 检索模式与核心参数卡片 */}
                          <div className="p-4 bg-slate-50 rounded-lg border border-slate-200 space-y-4">
                            <div className="flex items-center justify-between">
                              <Text strong className="text-xs text-slate-800">
                                检索模式与融合参数配置
                              </Text>
                              <Tag color="cyan">
                                当前收录 FAQ: {currentRule.faq_inspection.knowledge_base?.length || 0} 条
                              </Tag>
                            </div>

                            <div className="grid grid-cols-3 gap-3">
                              <div>
                                <span className="text-xs text-slate-500 block mb-1">
                                  检索匹配模式 (Retrieval Mode)
                                </span>
                                <Select
                                  value={currentRule.faq_inspection.retrieval_mode || 'hybrid'}
                                  onChange={(val) =>
                                    setCurrentRule({
                                      ...currentRule,
                                      faq_inspection: {
                                        ...currentRule.faq_inspection!,
                                        retrieval_mode: val,
                                      },
                                    })
                                  }
                                  className="w-full text-xs"
                                >
                                  <Select.Option value="hybrid">⚡ 混合检索 (BM25 + 向量 RRF 融合，推荐)</Select.Option>
                                  <Select.Option value="vector_only">稠密语义向量 (Vector Only)</Select.Option>
                                  <Select.Option value="lexical_only">纯词法匹配 (Lexical BM25 Only)</Select.Option>
                                </Select>
                              </div>

                              <div>
                                <span className="text-xs text-slate-500 block mb-1">
                                  候选召回数量 (Top-K Candidates)
                                </span>
                                <InputNumber
                                  min={1}
                                  max={10}
                                  value={currentRule.faq_inspection.top_k || 3}
                                  onChange={(val) =>
                                    setCurrentRule({
                                      ...currentRule,
                                      faq_inspection: {
                                        ...currentRule.faq_inspection!,
                                        top_k: val || 3,
                                      },
                                    })
                                  }
                                  className="w-full text-xs"
                                />
                              </div>

                              <div>
                                <span className="text-xs text-slate-500 block mb-1">
                                  向量模型来源 (Embedding Provider)
                                </span>
                                <Select
                                  value={currentRule.faq_inspection.embedding_config?.provider_type || 'local_cpu'}
                                  onChange={(val) =>
                                    setCurrentRule({
                                      ...currentRule,
                                      faq_inspection: {
                                        ...currentRule.faq_inspection!,
                                        embedding_config: {
                                          ...currentRule.faq_inspection!.embedding_config,
                                          provider_type: val,
                                        },
                                      },
                                    })
                                  }
                                  className="w-full text-xs"
                                >
                                  <Select.Option value="local_cpu">🖥️ 本地简易 CPU 向量模型 (零网络依赖)</Select.Option>
                                  <Select.Option value="remote_api">🌐 远程 OpenAI/兼容 Embedding API</Select.Option>
                                  <Select.Option value="disabled">🚫 停用向量化 (仅使用纯词法)</Select.Option>
                                </Select>
                              </div>
                            </div>

                            {/* 向量参数详情 (展开配置) */}
                            {currentRule.faq_inspection.embedding_config?.provider_type === 'local_cpu' && (
                              <div className="p-2.5 bg-white rounded border border-slate-200">
                                <span className="text-xs text-slate-500 block mb-1">
                                  本地 CPU 向量模型标识或目录路径 (支持 bge-small-zh-v1.5 / ONNX)
                                </span>
                                <Input
                                  size="small"
                                  placeholder="BAAI/bge-small-zh-v1.5"
                                  value={currentRule.faq_inspection.embedding_config?.local_model_name_or_path || ''}
                                  onChange={(e) =>
                                    setCurrentRule({
                                      ...currentRule,
                                      faq_inspection: {
                                        ...currentRule.faq_inspection!,
                                        embedding_config: {
                                          ...currentRule.faq_inspection!.embedding_config,
                                          local_model_name_or_path: e.target.value,
                                        },
                                      },
                                    })
                                  }
                                  className="font-mono text-xs"
                                />
                              </div>
                            )}

                            {currentRule.faq_inspection.embedding_config?.provider_type === 'remote_api' && (
                              <div className="grid grid-cols-3 gap-2 p-2.5 bg-white rounded border border-slate-200">
                                <div className="col-span-2">
                                  <span className="text-xs text-slate-500 block mb-1">远程 Embedding API URL</span>
                                  <Input
                                    size="small"
                                    placeholder="https://api.openai.com/v1/embeddings"
                                    value={currentRule.faq_inspection.embedding_config?.api_url || ''}
                                    onChange={(e) =>
                                      setCurrentRule({
                                        ...currentRule,
                                        faq_inspection: {
                                          ...currentRule.faq_inspection!,
                                          embedding_config: {
                                            ...currentRule.faq_inspection!.embedding_config,
                                            api_url: e.target.value,
                                          },
                                        },
                                      })
                                    }
                                    className="font-mono text-xs"
                                  />
                                </div>
                                <div>
                                  <span className="text-xs text-slate-500 block mb-1">远程模型名</span>
                                  <Input
                                    size="small"
                                    placeholder="text-embedding-3-small"
                                    value={currentRule.faq_inspection.embedding_config?.remote_model_name || ''}
                                    onChange={(e) =>
                                      setCurrentRule({
                                        ...currentRule,
                                        faq_inspection: {
                                          ...currentRule.faq_inspection!,
                                          embedding_config: {
                                            ...currentRule.faq_inspection!.embedding_config,
                                            remote_model_name: e.target.value,
                                          },
                                        },
                                      })
                                    }
                                    className="font-mono text-xs"
                                  />
                                </div>
                              </div>
                            )}

                            <div>
                              <div className="flex items-center justify-between mb-1">
                                <span className="text-xs text-slate-700 font-semibold">
                                  兜底规范 (Fallback Policy)
                                </span>
                                <span className="text-3xs text-slate-500">
                                  兜底回复规则和特殊陈述回复触发faq规则
                                </span>
                              </div>
                              <TextArea
                                rows={3}
                                value={currentRule.faq_inspection.fallback_policy || ''}
                                onChange={(e) =>
                                  setCurrentRule({
                                    ...currentRule,
                                    faq_inspection: {
                                      ...currentRule.faq_inspection!,
                                      fallback_policy: e.target.value,
                                    },
                                  })
                                }
                                placeholder="兜底回复规则和特殊陈述回复触发faq规则。例如：&#10;1. 若问题超出知识库范围，引导联系官方售后热线或 App 人工客服，严禁捏造；&#10;2. 客户提及特殊补偿或投诉陈述时触发对应标准 FAQ 核查。"
                                className="text-xs leading-relaxed"
                              />
                            </div>
                          </div>

                          {/* FAQ 知识库表格 */}
                          <div>
                            <div className="flex items-center justify-between mb-2">
                              <Text strong className="text-sm text-slate-800 flex items-center gap-1.5">
                                <DatabaseOutlined className="text-emerald-600" />
                                业务 FAQ 知识库 (Knowledge Base)
                              </Text>
                              <Button
                                size="small"
                                type="primary"
                                icon={<PlusOutlined />}
                                onClick={() => {
                                  setEditingFaqId('');
                                  faqForm.resetFields();
                                  faqForm.setFieldsValue({
                                    id: `FAQ_${String((currentRule.faq_inspection?.knowledge_base?.length || 0) + 1).padStart(3, '0')}`,
                                    category: '业务政策',
                                  });
                                  setFaqModalVisible(true);
                                }}
                                className="bg-emerald-600 hover:bg-emerald-700"
                              >
                                添加 FAQ 知识点
                              </Button>
                            </div>

                            <Table
                              size="small"
                              pagination={{ pageSize: 5 }}
                              rowKey="id"
                              dataSource={currentRule.faq_inspection?.knowledge_base || []}
                              columns={[
                                {
                                  title: 'FAQ ID',
                                  dataIndex: 'id',
                                  width: 110,
                                  render: (id: string) => <Tag color="blue">{id}</Tag>,
                                },
                                {
                                  title: '分类',
                                  dataIndex: 'category',
                                  width: 100,
                                  render: (cat: string) => <Tag>{cat || '通用'}</Tag>,
                                },
                                {
                                  title: '标准问题及相似问 (Question)',
                                  dataIndex: 'question',
                                  width: 220,
                                  ellipsis: true,
                                },
                                {
                                  title: '官方标准答案口径 (Official Answer)',
                                  dataIndex: 'answer',
                                  ellipsis: true,
                                },
                                {
                                  title: '操作',
                                  width: 110,
                                  render: (_, record: FAQItem) => (
                                    <Space size="small">
                                      <Button
                                        type="link"
                                        size="small"
                                        onClick={() => {
                                          setEditingFaqId(record.id);
                                          faqForm.setFieldsValue({
                                            id: record.id,
                                            category: record.category,
                                            question: record.question,
                                            answer: record.answer,
                                          });
                                          setFaqModalVisible(true);
                                        }}
                                      >
                                        编辑
                                      </Button>
                                      <Popconfirm
                                        title={`删除知识点 ${record.id}？`}
                                        onConfirm={() => handleDeleteFAQItem(record.id)}
                                      >
                                        <Button type="link" danger size="small">
                                          删除
                                        </Button>
                                      </Popconfirm>
                                    </Space>
                                  ),
                                },
                              ]}
                            />
                          </div>

                          {/* FAQ 提问轮次检测与检索改写 Prompt 模板与调试 (Step B1) */}
                          <div className="p-4 bg-sky-50/50 rounded border border-sky-200 space-y-3">
                            <div className="flex items-center justify-between flex-wrap gap-2">
                              <div>
                                <div className="font-semibold text-sm text-sky-950 flex items-center gap-1.5 flex-wrap">
                                  <QuestionCircleOutlined className="text-sky-600" />
                                  FAQ 提问检测与检索改写 Prompt 模板 (Step B1 · FAQ Extract)
                                  {currentRule.faq_inspection?.faq_extract_prompt_template?.trim() ? (
                                    <Tag color="orange" className="m-0 text-3xs">
                                      本规则专属覆盖（不跟随全局）
                                    </Tag>
                                  ) : (
                                    <Tag color="green" className="m-0 text-3xs">
                                      留空 · 跟随全局激活模板
                                    </Tag>
                                  )}
                                </div>
                                <div className="text-xs text-sky-800/80 mt-0.5 space-y-0.5">
                                  <div>
                                    用于扫描对话并抽取客户主动插播的政策/费用提问轮次及改写检索词 (rewritten_query)。支持占位符：{'{dialogue}'}, {'{fallback_policy}'}, {'{scenario_id}'}, {'{scenario_name}'}。
                                  </div>
                                  <div className="text-amber-700">
                                    作用域说明：仅作用于当前规则（不影响其他规则与全局提示词中心）；一旦填写并保存（含载入默认模板后保存）将屏蔽全局更新，需跟随全局请保持留空。
                                  </div>
                                </div>
                              </div>
                              <Space wrap>
                                {currentRule.faq_inspection?.faq_extract_prompt_template?.trim() && (
                                  <Button
                                    size="small"
                                    danger
                                    ghost
                                    onClick={() => {
                                      setCurrentRule({
                                        ...currentRule,
                                        faq_inspection: {
                                          ...currentRule.faq_inspection!,
                                          faq_extract_prompt_template: '',
                                        },
                                      });
                                      message.info('已清空本规则专属覆盖，保存规则后将跟随全局激活模板');
                                    }}
                                  >
                                    清空跟随全局
                                  </Button>
                                )}
                                <Button
                                  size="small"
                                  onClick={() => {
                                    if (defaultTemplates?.faq_extract_template) {
                                      setCurrentRule({
                                        ...currentRule,
                                        faq_inspection: {
                                          ...currentRule.faq_inspection!,
                                          faq_extract_prompt_template: defaultTemplates.faq_extract_template,
                                        },
                                      });
                                      message.info('已载入系统内置 FAQ 提问抽取与改写 Prompt 模板');
                                    }
                                  }}
                                >
                                  载入内置默认模板
                                </Button>
                                <Button
                                  size="small"
                                  icon={<DownloadOutlined />}
                                  onClick={() => handleQuickExportDebug('faq_extract')}
                                >
                                  导出调试报文
                                </Button>
                                <Button
                                  type="primary"
                                  size="small"
                                  icon={<ExperimentOutlined />}
                                  onClick={() => handleOpenDebugger('faq_extract')}
                                >
                                  拼装预览与实测
                                </Button>
                              </Space>
                            </div>

                            <Input.TextArea
                              rows={5}
                              placeholder={
                                defaultTemplates?.faq_extract_template ||
                                '留空自动使用系统内置 / 提示词管理中心激活的 FAQ 提问抽取模板'
                              }
                              value={currentRule.faq_inspection?.faq_extract_prompt_template || ''}
                              onChange={(e) =>
                                setCurrentRule({
                                  ...currentRule,
                                  faq_inspection: {
                                    ...currentRule.faq_inspection!,
                                    faq_extract_prompt_template: e.target.value,
                                  },
                                })
                              }
                              className="font-mono text-xs"
                            />
                          </div>

                          {/* 专职 FAQ 质检 Prompt 模板与在线调试实测 (Step B2) */}
                          <div className="p-4 bg-emerald-50/50 rounded border border-emerald-200 space-y-3">
                            <div className="flex items-center justify-between flex-wrap gap-2">
                              <div>
                                <div className="font-semibold text-sm text-emerald-950 flex items-center gap-1.5 flex-wrap">
                                  <ExperimentOutlined className="text-emerald-600" />
                                  专职 FAQ 准确度质检 Prompt 模板与实测 (Step B2 · FAQ Judge)
                                  {currentRule.faq_inspection?.faq_judge_prompt_template?.trim() ? (
                                    <Tag color="orange" className="m-0 text-3xs">
                                      本规则专属覆盖（不跟随全局）
                                    </Tag>
                                  ) : (
                                    <Tag color="green" className="m-0 text-3xs">
                                      留空 · 跟随全局激活模板
                                    </Tag>
                                  )}
                                </div>
                                <div className="text-xs text-emerald-800/80 mt-0.5 space-y-0.5">
                                  <div>
                                    用于驱动专职法官对照 Top-K 检索知识与兜底规范裁决事实一致性。支持占位符：{'{scenario_id}'}, {'{user_turn}'}, {'{assistant_turn}'}, {'{faq_dialogue_slice}'}, {'{candidate_faqs}'}, {'{fallback_policy}'}。
                                  </div>
                                  <div className="text-amber-700">
                                    作用域说明：仅作用于当前规则（不影响其他规则与全局提示词中心）；一旦填写并保存（含载入默认模板后保存）将屏蔽全局更新，需跟随全局请保持留空。
                                  </div>
                                </div>
                              </div>
                              <Space wrap>
                                {currentRule.faq_inspection?.faq_judge_prompt_template?.trim() && (
                                  <Button
                                    size="small"
                                    danger
                                    ghost
                                    onClick={() => {
                                      setCurrentRule({
                                        ...currentRule,
                                        faq_inspection: {
                                          ...currentRule.faq_inspection!,
                                          faq_judge_prompt_template: '',
                                        },
                                      });
                                      message.info('已清空本规则专属覆盖，保存规则后将跟随全局激活模板');
                                    }}
                                  >
                                    清空跟随全局
                                  </Button>
                                )}
                                <Button
                                  size="small"
                                  onClick={() => {
                                    if (defaultTemplates?.faq_judge_template) {
                                      setCurrentRule({
                                        ...currentRule,
                                        faq_inspection: {
                                          ...currentRule.faq_inspection!,
                                          faq_judge_prompt_template: defaultTemplates.faq_judge_template,
                                        },
                                      });
                                      message.info('已载入系统内置专职 FAQ 质检 Prompt 模板');
                                    }
                                  }}
                                >
                                  载入内置默认模板
                                </Button>
                                <Button
                                  size="small"
                                  icon={<DownloadOutlined />}
                                  onClick={() => handleQuickExportDebug('faq_judge')}
                                >
                                  导出调试报文
                                </Button>
                                <Button
                                  type="primary"
                                  size="small"
                                  icon={<ExperimentOutlined />}
                                  onClick={() => handleOpenDebugger('faq_judge')}
                                  className="bg-emerald-600 hover:bg-emerald-700"
                                >
                                  调试组装与实测
                                </Button>
                              </Space>
                            </div>

                            <Input.TextArea
                              rows={5}
                              placeholder={defaultTemplates?.faq_judge_template || '留空自动使用系统内置专职 FAQ 质检模板'}
                              value={currentRule.faq_inspection?.faq_judge_prompt_template || ''}
                              onChange={(e) =>
                                setCurrentRule({
                                  ...currentRule,
                                  faq_inspection: {
                                    ...currentRule.faq_inspection!,
                                    faq_judge_prompt_template: e.target.value,
                                  },
                                })
                              }
                              className="font-mono text-xs"
                            />
                          </div>
                        </>
                      ) : (
                        <div className="p-8 text-center bg-slate-50 rounded-lg border border-dashed border-slate-300 my-4">
                          <QuestionCircleOutlined className="text-3xl text-slate-400 mb-2" />
                          <div className="text-sm font-semibold text-slate-600">未启用 FAQ 问答准确度质检</div>
                          <div className="text-xs text-slate-400 mt-1 max-w-md mx-auto">
                            当前场景在质检时将零开销跳过 FAQ 提问抽取与检索比对。若业务涉及收费标准、质保期限或退换货政策解答，请开启上方开关并维护知识库。
                          </div>
                        </div>
                      )}
                    </div>
                  ),
                },
                {
                  key: 'checkpoints',
                  label: (
                    <span>
                      <SecurityScanOutlined /> 质检标准与红线
                    </span>
                  ),
                  children: (
                    <div className="space-y-5 pr-1 pb-12">
                      {/* 及格分与附加质检准则 (Markdown) */}
                      <div className="space-y-4 p-4 bg-slate-50 rounded border border-slate-200">
                        <div className="flex items-center justify-between gap-4 flex-wrap">
                          <div>
                            <Text strong className="text-xs block">
                              质检及格分门槛 (Pass Score Threshold)
                            </Text>
                            <Text type="secondary" className="text-3xs">
                              低于该分数线或触发高危红线将判定为 FAIL
                            </Text>
                          </div>
                          <InputNumber
                            min={0}
                            max={100}
                            value={currentRule.inspection_rules.pass_score_threshold}
                            onChange={(val) =>
                              setCurrentRule({
                                ...currentRule,
                                inspection_rules: {
                                  ...currentRule.inspection_rules,
                                  pass_score_threshold: val || 85,
                                },
                              })
                            }
                            className="w-32"
                          />
                        </div>

                        <div>
                          <div className="flex items-center justify-between mb-1">
                            <Text strong className="text-xs">
                              附加 LLM 质检准则说明 (Custom Rubric · 支持 `- ` Markdown 列表)
                            </Text>
                            <Text type="secondary" className="text-3xs">
                              每行一条 `- ` 准则
                            </Text>
                          </div>
                          <TextArea
                            rows={4}
                            className="font-mono text-xs leading-relaxed"
                            placeholder="- 客户已主动交代的槽位信息严禁机械重复反问&#10;- 遇非本人严格保守商业秘密&#10;- 由于是在通话中，单轮回复不超过5句话"
                            value={rubricText}
                            onChange={(e) => {
                              const val = e.target.value;
                              setRubricText(val);
                              setCurrentRule({
                                ...currentRule,
                                inspection_rules: {
                                  ...currentRule.inspection_rules,
                                  custom_rubric: val,
                                },
                              });
                            }}
                            onBlur={() => {
                              const formatted = formatMarkdownBulletsText(rubricText);
                              setRubricText(formatted);
                              setCurrentRule({
                                ...currentRule,
                                inspection_rules: {
                                  ...currentRule.inspection_rules,
                                  custom_rubric: formatted,
                                },
                              });
                            }}
                          />
                        </div>
                      </div>

                      {/* 全局红线 (Markdown 文本框) */}
                      <div>
                        <div className="flex items-center justify-between mb-1.5">
                          <Text strong className="text-sm text-red-700">
                            全局高危红线 (触碰一票否决 · 支持 `- ` Markdown 列表)
                          </Text>
                          <Text type="secondary" className="text-3xs">
                            每行一条 `- ` 红线，失焦自动格式化
                          </Text>
                        </div>
                        <TextArea
                          rows={6}
                          className="font-mono text-xs leading-relaxed border-red-200 focus:border-red-400"
                          placeholder="- 严禁未经核实向非下单人泄露客户收货地址与订单隐私信息&#10;- 严禁威胁、恐吓或侮辱谩骂客户&#10;- 严禁口头违规承诺超出售后政策的免费换新或额外赔付"
                          value={redlinesText}
                          onChange={(e) => {
                            const val = e.target.value;
                            setRedlinesText(val);
                            setCurrentRule({
                              ...currentRule,
                              inspection_rules: {
                                ...currentRule.inspection_rules,
                                global_redlines: parseMarkdownBullets(val),
                              },
                            });
                          }}
                          onBlur={() => {
                            const formatted = formatMarkdownBulletsText(redlinesText);
                            setRedlinesText(formatted);
                            setCurrentRule({
                              ...currentRule,
                              inspection_rules: {
                                ...currentRule.inspection_rules,
                                global_redlines: parseMarkdownBullets(formatted),
                              },
                            });
                          }}
                        />
                      </div>

                      {/* 完整质检 Prompt 模板与在线调试 */}
                      <div className="p-4 bg-purple-50/50 rounded border border-purple-200 space-y-3">
                        <div className="flex items-center justify-between">
                          <div>
                            <div className="font-semibold text-sm text-purple-950 flex items-center gap-1.5 flex-wrap">
                              <SecurityScanOutlined /> 白盒质检 Prompt 模板 (Judge Prompt Template)
                              {currentRule.topology_sop?.enabled ? (
                                currentRule.inspection_rules?.judge_prompt_template?.trim() ? (
                                  <Tag color="orange" className="m-0 text-3xs">
                                    本规则专属覆盖（不跟随全局）
                                  </Tag>
                                ) : (
                                  <Tag color="green" className="m-0 text-3xs">
                                    留空 · 跟随全局激活模板
                                  </Tag>
                                )
                              ) : (
                                <Tag color="blue" className="m-0 text-3xs">
                                  未开 Mermaid · 自动使用全局纯规则模板 (judge_plain)
                                </Tag>
                              )}
                            </div>
                            <div className="text-xs text-purple-800/80 mt-0.5 space-y-0.5">
                              <div>
                                用于驱动质检模型进行 SOP 推进、节点达成与红线判定。支持占位符：{'{scenario_id}'}, {'{scenario_name}'}, {'{mermaid_sop}'}, {'{topology_status}'}, {'{topology_error}'}, {'{trajectory}'}, {'{trajectory_details}'}, {'{checkpoints}'}, {'{redlines}'}, {'{custom_rubric}'}, {'{dialogue}'}。
                              </div>
                              <div className="text-amber-700">
                                作用域说明：本框仅在开启 Mermaid 时作用于当前规则（不影响其他规则与全局提示词中心）；填入内容并保存（含载入默认模板后保存）将屏蔽全局更新，需跟随全局请保持留空。关闭 Mermaid 时自动走「提示词管理中心」的纯规则模板 (judge_plain)。
                              </div>
                            </div>
                          </div>
                          <Space wrap>
                            {currentRule.inspection_rules?.judge_prompt_template?.trim() && (
                              <Button
                                size="small"
                                danger
                                ghost
                                onClick={() => {
                                  setCurrentRule({
                                    ...currentRule,
                                    inspection_rules: {
                                      ...currentRule.inspection_rules,
                                      judge_prompt_template: '',
                                    },
                                  });
                                  message.info('已清空本规则专属覆盖，保存规则后将跟随全局激活模板');
                                }}
                              >
                                清空跟随全局
                              </Button>
                            )}
                            <Button
                              size="small"
                              onClick={() => {
                                if (defaultTemplates?.judge_template) {
                                  setCurrentRule({
                                    ...currentRule,
                                    inspection_rules: {
                                      ...currentRule.inspection_rules,
                                      judge_prompt_template: defaultTemplates.judge_template,
                                    },
                                  });
                                  message.info('已载入系统内置标准质检 Prompt 模板');
                                }
                              }}
                            >
                              载入内置默认模板
                            </Button>
                            <Button
                              size="small"
                              icon={<DownloadOutlined />}
                              onClick={() =>
                                handleQuickExportDebug(
                                  currentRule.topology_sop?.enabled ? 'judge' : 'judge_plain'
                                )
                              }
                            >
                              导出调试报文
                            </Button>
                            <Button
                              type="primary"
                              size="small"
                              icon={<ExperimentOutlined />}
                              onClick={() =>
                                handleOpenDebugger(
                                  currentRule.topology_sop?.enabled ? 'judge' : 'judge_plain'
                                )
                              }
                              className="bg-purple-600 hover:bg-purple-700"
                            >
                              {currentRule.topology_sop?.enabled
                                ? '调试组装与实测'
                                : '调试纯规则质检'}
                            </Button>
                          </Space>
                        </div>

                        <Input.TextArea
                          rows={5}
                          placeholder={defaultTemplates?.judge_template || '留空自动使用系统内置标准质检模板'}
                          value={currentRule.inspection_rules?.judge_prompt_template || ''}
                          onChange={(e) =>
                            setCurrentRule({
                              ...currentRule,
                              inspection_rules: {
                                ...currentRule.inspection_rules,
                                judge_prompt_template: e.target.value,
                              },
                            })
                          }
                          className="font-mono text-xs"
                        />
                      </div>
                    </div>
                  ),
                },
                {
                  key: 'remediation',
                  label: (
                    <span>
                      <ToolOutlined /> 数据清洗与修复规范
                    </span>
                  ),
                  children: (
                    <div className="space-y-4 pr-1 pb-12">
                      <div className="p-4 bg-slate-50 rounded border border-slate-200 space-y-4">
                        <div>
                          <Text strong className="text-sm block mb-1">
                            目标轮次生成范围 (Target Turn Range)
                          </Text>
                          <Space>
                            <InputNumber
                              min={10}
                              max={60}
                              value={currentRule.remediation_config.target_turn_range[0]}
                              onChange={(val) =>
                                setCurrentRule({
                                  ...currentRule,
                                  remediation_config: {
                                    ...currentRule.remediation_config,
                                    target_turn_range: [
                                      val || 20,
                                      currentRule.remediation_config.target_turn_range[1],
                                    ],
                                  },
                                })
                              }
                            />
                            <span>至</span>
                            <InputNumber
                              min={20}
                              max={120}
                              value={currentRule.remediation_config.target_turn_range[1]}
                              onChange={(val) =>
                                setCurrentRule({
                                  ...currentRule,
                                  remediation_config: {
                                    ...currentRule.remediation_config,
                                    target_turn_range: [
                                      currentRule.remediation_config.target_turn_range[0],
                                      val || 80,
                                    ],
                                  },
                                })
                              }
                            />
                            <Text type="secondary" className="text-xs">
                              （AI 重塑对话时的目标轮数下限与上限）
                            </Text>
                          </Space>
                        </div>

                        <div className="flex items-center justify-between py-2 border-t border-slate-200">
                          <div>
                            <div className="font-semibold text-sm">注入空思考字段 (Inject Empty Thinking)</div>
                            <div className="text-xs text-slate-500">
                              为 assistant 轮次注入 "thinking": "" 字段，以对齐 DeepSeek-R1 / Qwen 思考微调格式
                            </div>
                          </div>
                          <Switch
                            checked={currentRule.remediation_config.inject_empty_thinking}
                            onChange={(val) =>
                              setCurrentRule({
                                ...currentRule,
                                remediation_config: {
                                  ...currentRule.remediation_config,
                                  inject_empty_thinking: val,
                                },
                              })
                            }
                          />
                        </div>

                        <div className="flex items-center justify-between py-2 border-t border-slate-200">
                          <div>
                            <div className="font-semibold text-sm">强制由 Assistant 机器人收口</div>
                            <div className="text-xs text-slate-500">
                              确保清洗后的 SFT 训练集末轮必为 assistant，避免因客户最后一句话未收口导致模型学会抢答
                            </div>
                          </div>
                          <Switch
                            checked={currentRule.remediation_config.require_assistant_ending}
                            onChange={(val) =>
                              setCurrentRule({
                                ...currentRule,
                                remediation_config: {
                                  ...currentRule.remediation_config,
                                  require_assistant_ending: val,
                                },
                              })
                            }
                          />
                        </div>

                        <div className="flex items-center justify-between py-2 border-t border-slate-200">
                          <div>
                            <div className="font-semibold text-sm">严格角色交替校验 (Strict Alternating Roles)</div>
                            <div className="text-xs text-slate-500">
                              自动合并连续的同角色发言，彻底消除 Unsloth / LLaMA-Factory 训练时报连续 user 错
                            </div>
                          </div>
                          <Switch
                            checked={currentRule.remediation_config.strict_alternating_roles}
                            onChange={(val) =>
                              setCurrentRule({
                                ...currentRule,
                                remediation_config: {
                                  ...currentRule.remediation_config,
                                  strict_alternating_roles: val,
                                },
                              })
                            }
                          />
                        </div>

                        {/* 自定义修复指导与防提前挂机特别强调 */}
                        <div className="py-2 border-t border-slate-200 space-y-2">
                          <div>
                            <div className="font-semibold text-sm">场景专属修复指导与防挂机原则 (Custom Instructions)</div>
                            <div className="text-xs text-slate-500">
                              配置大模型在对本场景缺陷样本重塑修复时必须遵守的特殊原则（如强调严禁客服提前挂机、针对漏讲节点适度展开宣讲、对冗余闲聊予以剔除）
                            </div>
                          </div>
                          <Input.TextArea
                            rows={3}
                            placeholder="例如：1. 严禁在中间轮次提前输出道别辞令；2. 补齐质保条件与退换周期说明；3. 保持与真实客户输入紧密连贯"
                            value={currentRule.remediation_config.custom_instructions || ''}
                            onChange={(e) =>
                              setCurrentRule({
                                ...currentRule,
                                remediation_config: {
                                  ...currentRule.remediation_config,
                                  custom_instructions: e.target.value,
                                },
                              })
                            }
                            className="font-mono text-xs"
                          />
                        </div>

                        {/* 基于声明式双工具 (prune + replace) 的完整自定义修复提示词模板 */}
                        <div className="py-2 border-t border-slate-200 space-y-2">
                          <div className="flex items-center justify-between">
                            <div>
                              <div className="font-semibold text-sm text-emerald-950 flex items-center gap-1.5 flex-wrap">
                                <ToolOutlined /> 声明式精修处方单 Prompt 模板 (Remediation Prompt Template)
                                {currentRule.remediation_config.remediation_prompt_template?.trim() ? (
                                  <Tag color="orange" className="m-0 text-3xs">
                                    本规则专属覆盖（不跟随全局）
                                  </Tag>
                                ) : (
                                  <Tag color="green" className="m-0 text-3xs">
                                    留空 · 跟随全局激活模板
                                  </Tag>
                                )}
                              </div>
                              <div className="text-xs text-slate-500 mt-0.5 space-y-0.5">
                                <div>
                                  规范精修大模型使用平台双工具（<code>prune</code> 成对剪枝 + <code>replace</code> 坐席替换）进行定向微创修复。支持占位符：{'{scenario_id}'}, {'{scenario_name}'}, {'{system_prompt}'}, {'{mermaid_sop}'}, {'{defects}'}, {'{dialogue}'}, {'{custom_instructions_block}'}。
                                </div>
                                <div className="text-amber-700">
                                  作用域说明：仅作用于当前规则（不影响其他规则与全局提示词中心）；一旦填写并保存（含载入默认模板后保存）将屏蔽全局更新，需跟随全局请保持留空。
                                </div>
                              </div>
                            </div>
                            <Space wrap>
                              {currentRule.remediation_config.remediation_prompt_template?.trim() && (
                                <Button
                                  size="small"
                                  danger
                                  ghost
                                  onClick={() => {
                                    setCurrentRule({
                                      ...currentRule,
                                      remediation_config: {
                                        ...currentRule.remediation_config,
                                        remediation_prompt_template: '',
                                      },
                                    });
                                    message.info('已清空本规则专属覆盖，保存规则后将跟随全局激活模板');
                                  }}
                                >
                                  清空跟随全局
                                </Button>
                              )}
                              <Button
                                size="small"
                                onClick={() => {
                                  if (defaultTemplates?.remediation_template) {
                                    setCurrentRule({
                                      ...currentRule,
                                      remediation_config: {
                                        ...currentRule.remediation_config,
                                        remediation_prompt_template: defaultTemplates.remediation_template,
                                      },
                                    });
                                    message.info('已载入系统内置双工具修复 Prompt 模板');
                                  }
                                }}
                              >
                                载入内置默认模板
                              </Button>
                              <Button
                                size="small"
                                icon={<DownloadOutlined />}
                                onClick={() => handleQuickExportDebug('remediate')}
                              >
                                导出调试报文
                              </Button>
                              <Button
                                type="primary"
                                size="small"
                                icon={<ExperimentOutlined />}
                                onClick={() => handleOpenDebugger('remediate')}
                                className="bg-emerald-600 hover:bg-emerald-700"
                              >
                                调试组装与实测
                              </Button>
                            </Space>
                          </div>
                          <Input.TextArea
                            rows={6}
                            placeholder={defaultTemplates?.remediation_template || '留空自动使用系统内置双工具 (prune + replace) 修复模板'}
                            value={currentRule.remediation_config.remediation_prompt_template || ''}
                            onChange={(e) =>
                              setCurrentRule({
                                ...currentRule,
                                remediation_config: {
                                  ...currentRule.remediation_config,
                                  remediation_prompt_template: e.target.value,
                                },
                              })
                            }
                            className="font-mono text-xs"
                          />
                        </div>
                      </div>
                    </div>
                  ),
                },
                {
                  key: 'scenarios',
                  label: (
                    <span>
                      <TeamOutlined /> 虚拟客户剧本库 ({currentRule.scenarios.length})
                    </span>
                  ),
                  children: (
                    <div className="space-y-3 pb-12">
                      <div className="flex justify-between items-center mb-3 flex-wrap gap-2">
                        <Text className="text-xs text-slate-500">
                          用于驱动 AI 模拟对弈的虚拟客户画像与扮演指令库
                        </Text>
                        <Space wrap>
                          <Button
                            size="small"
                            icon={<DownloadOutlined />}
                            onClick={() => handleQuickExportDebug('client_simulator')}
                          >
                            导出客户模拟 Prompt
                          </Button>
                          <Button
                            size="small"
                            icon={<ExperimentOutlined />}
                            onClick={() => handleOpenDebugger('client_simulator')}
                          >
                            调试客户模拟人设
                          </Button>
                          <Button
                            size="small"
                            type="primary"
                            icon={<PlusOutlined />}
                            onClick={() => {
                              setEditingPersona(null);
                              personaForm.resetFields();
                              personaForm.setFieldsValue({
                                id: `sc_${Date.now().toString(36)}`,
                                category: '常规分类',
                                persona: '{\n  "age": 35,\n  "intent": "常规沟通"\n}',
                              });
                              setPersonaModalVisible(true);
                            }}
                          >
                            录入新人设
                          </Button>
                        </Space>
                      </div>

                      <div className="flex-1 overflow-auto grid grid-cols-2 gap-4">
                        {currentRule.scenarios.map((sc) => (
                          <Card
                            key={sc.id}
                            size="small"
                            className="border-slate-200 hover:shadow-sm"
                            title={
                              <div className="flex items-center justify-between">
                                <span className="font-semibold text-sm">{sc.name}</span>
                                <Tag color="blue">{sc.category}</Tag>
                              </div>
                            }
                            extra={
                              <Space size="small">
                                <Button
                                  type="text"
                                  size="small"
                                  onClick={() => {
                                    setEditingPersona(sc);
                                    personaForm.setFieldsValue({
                                      id: sc.id,
                                      name: sc.name,
                                      category: sc.category,
                                      instruction: sc.instruction,
                                      persona: JSON.stringify(sc.persona, null, 2),
                                    });
                                    setPersonaModalVisible(true);
                                  }}
                                >
                                  编辑
                                </Button>
                                <Popconfirm
                                  title="确定移除该剧本？"
                                  onConfirm={() => {
                                    setCurrentRule({
                                      ...currentRule,
                                      scenarios: currentRule.scenarios.filter((s) => s.id !== sc.id),
                                    });
                                  }}
                                >
                                  <Button type="text" danger size="small">
                                    删除
                                  </Button>
                                </Popconfirm>
                              </Space>
                            }
                          >
                            <p className="text-xs text-slate-600 line-clamp-3 mb-2">
                              {sc.instruction || '无特殊扮演指示'}
                            </p>
                            <div className="text-3xs text-slate-400 font-mono bg-slate-50 p-2 rounded truncate">
                              {JSON.stringify(sc.persona)}
                            </div>
                          </Card>
                        ))}
                      </div>
                    </div>
                  ),
                },
              ]}
            />
          </>
        )}
      </div>

      {/* 右侧：Mermaid SOP 状态机独立专属大视窗 (左右并排大图展示) */}
      {showMermaidPanel && (
        <div className="col-span-12 xl:col-span-5 lg:col-span-5 h-full min-h-0 flex flex-col overflow-hidden">
          <MermaidViewer graphDefinition={mermaidText} />
        </div>
      )}

      {/* 检查点编辑模态框 */}
      <Modal
        title={editingNodeKey ? `编辑节点检查点: ${editingNodeKey}` : '添加节点检查点'}
        open={checkpointModalVisible}
        onCancel={() => setCheckpointModalVisible(false)}
        onOk={handleSaveCheckpoint}
        okText="确认"
        cancelText="取消"
      >
        <Form form={checkpointForm} layout="vertical">
          <Form.Item name="node_key" label="节点ID (如 N1_1)" rules={[{ required: true }]}>
            <Input disabled={!!editingNodeKey} />
          </Form.Item>
          <Form.Item name="name" label="节点名称 (如 身份核验)" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="description" label="节点说明 (Description)">
            <Input placeholder="如：核实设备型号与使用环境" />
          </Form.Item>
          <Form.Item name="required_elements" label="必含话术要素 (每行一条)">
            <TextArea rows={3} placeholder="如：请问是下单人本人吗&#10;工号说明" />
          </Form.Item>
          <Form.Item name="forbidden_keywords" label="违禁词 / 禁忌话术 (每行一条，切题时自动继承为 !违禁词)">
            <TextArea rows={2} placeholder="如：拒绝售后&#10;违规承诺全额退款" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 客户画像编辑模态框 */}
      <Modal
        title={editingPersona ? `编辑剧本: ${editingPersona.name}` : '录入新虚拟客户画像'}
        open={personaModalVisible}
        onCancel={() => setPersonaModalVisible(false)}
        onOk={handleSavePersona}
        okText="保存剧本"
        cancelText="取消"
      >
        <Form form={personaForm} layout="vertical">
          <div className="grid grid-cols-2 gap-3">
            <Form.Item name="id" label="剧本唯一ID" rules={[{ required: true }]}>
              <Input disabled={!!editingPersona} />
            </Form.Item>
            <Form.Item name="name" label="剧本名称" rules={[{ required: true }]}>
              <Input placeholder="如：常规配合售后回访客户" />
            </Form.Item>
          </div>
          <Form.Item name="category" label="分类标签">
            <Input placeholder="如：退换货协商 / FAQ 咨询" />
          </Form.Item>
          <Form.Item name="instruction" label="AI 扮演核心指示 (Prompt)">
            <TextArea rows={4} placeholder="指导模拟客户如何应答、情绪特征与反问逻辑" />
          </Form.Item>
          <Form.Item name="persona" label="人设画像槽位 (JSON)">
            <TextArea rows={3} className="font-mono text-xs" />
          </Form.Item>
        </Form>
      </Modal>

      {/* FAQ 知识点编辑模态框 */}
      <Modal
        title={editingFaqId ? `编辑 FAQ 知识点 [${editingFaqId}]` : '添加新 FAQ 知识点'}
        open={faqModalVisible}
        onCancel={() => setFaqModalVisible(false)}
        onOk={handleSaveFAQItem}
        okText="保存知识点"
        cancelText="取消"
        destroyOnClose
      >
        <Form form={faqForm} layout="vertical">
          <div className="grid grid-cols-2 gap-3">
            <Form.Item name="id" label="FAQ 唯一ID" rules={[{ required: true, message: '请输入唯一ID (如 FAQ_001)' }]}>
              <Input placeholder="FAQ_001" disabled={!!editingFaqId} />
            </Form.Item>
            <Form.Item name="category" label="业务分类">
              <Input placeholder="如：退换货政策 / 质保规则" />
            </Form.Item>
          </div>
          <Form.Item name="question" label="标准问题及相似问 (Question)" rules={[{ required: true, message: '请输入标准问题' }]}>
            <Input placeholder="如：七天无理由退换货与质量问题运费承担规则" />
          </Form.Item>
          <Form.Item name="answer" label="官方唯一标准答案口径 (Official Answer)" rules={[{ required: true, message: '请输入官方唯一标准答案' }]}>
            <TextArea rows={4} placeholder="如：未拆封激活商品支持签收起7天内无理由退换，因质量问题退换由商家承担往返运费..." />
          </Form.Item>
        </Form>
      </Modal>

      {/* 质检/修复/抽取 Prompt 在线调试与实测模态框 */}
      <Modal
        title={
          <div className="flex items-center gap-2">
            <ExperimentOutlined className="text-purple-600 text-lg" />
            <span className="font-bold">
              Prompt 模板在线调试、导出与模型实测 [{getPromptTypeMeta(debugPromptType).title}]
            </span>
            <Tag color={getPromptTypeMeta(debugPromptType).color}>
              {currentRule?.rule_id}
            </Tag>
          </div>
        }
        open={debugModalVisible}
        onCancel={() => setDebugModalVisible(false)}
        width={1160}
        footer={null}
        destroyOnClose
      >
        <div className="space-y-4 pt-1">
          <Alert
            type="info"
            showIcon
            message={
              <span className="text-xs">
                <strong>单次规则上下文调试</strong>：当前基于规则「{currentRule?.project_name || currentRule?.rule_id}」的页面实时草稿（含未保存内容）进行临时拼装与模型试跑，<b>不会自动保存规则，也不会修改「提示词管理中心」的全局模板</b>。
                {(debugPromptType === 'judge_plain' || debugPromptType === 'client_simulator') && (
                  <span className="ml-1 text-blue-700">
                    （该模块不在单条规则里单独配置模板，直接读取「提示词管理中心」全局激活模板。）
                  </span>
                )}
              </span>
            }
          />

          {/* 顶部工具栏: 选择测试画像与组装导出控制 */}
          <div className="flex items-center justify-between flex-wrap gap-2 bg-slate-50 p-3 rounded-lg border border-slate-200">
            <div className="flex items-center gap-3">
              <span className="text-xs font-semibold text-slate-700">关联客户剧本 (Persona):</span>
              <Select
                size="small"
                value={debugScenarioId}
                popupMatchSelectWidth={false}
                onChange={(val) => {
                  setDebugScenarioId(val);
                  triggerAssemble(debugPromptType, val);
                }}
                className="w-56"
                placeholder="选择测试剧本"
              >
                {(currentRule?.scenarios || []).map((s) => (
                  <Select.Option key={s.id} value={s.id}>
                    {s.name} ({s.id})
                  </Select.Option>
                ))}
              </Select>
              <Button
                size="small"
                icon={<ReloadOutlined />}
                loading={debugLoading}
                onClick={() => triggerAssemble(debugPromptType, debugScenarioId)}
              >
                重新组装 Prompt
              </Button>
            </div>

            <div className="flex items-center gap-2 flex-wrap">
              <Button
                size="small"
                icon={<CopyOutlined />}
                disabled={!debugData?.assembled_messages?.length}
                onClick={() => {
                  if (debugData?.assembled_messages?.length) {
                    copyTextSafely(
                      JSON.stringify(debugData.assembled_messages, null, 2),
                      '已复制 [system, user] Request Messages (JSON)'
                    );
                  }
                }}
              >
                复制 Request Messages
              </Button>
              <Button
                size="small"
                icon={<CopyOutlined />}
                disabled={!debugData?.assembled_prompt}
                onClick={() => {
                  if (debugData?.assembled_prompt) {
                    copyTextSafely(debugData.assembled_prompt, '已复制组装完成的拼接文本');
                  }
                }}
              >
                复制拼接文本
              </Button>
              <Button
                size="small"
                icon={<DownloadOutlined />}
                disabled={!debugData?.assembled_prompt}
                onClick={() => {
                  if (!debugData) return;
                  exportPromptDebugJson({
                    title: getPromptTypeMeta(debugPromptType).title,
                    prompt_key: debugPromptType,
                    rule_id: debugData.rule_id,
                    scenario_id: debugData.scenario_id,
                    assembled_messages: debugData.assembled_messages,
                    assembled_prompt: debugData.assembled_prompt,
                    sample_dialogue: debugData.sample_dialogue,
                    recommended_config: debugData.recommended_config,
                    test_result: testResult,
                  });
                  message.success('已导出调试 JSON 文件');
                }}
              >
                导出 JSON
              </Button>
              <Button
                size="small"
                icon={<DownloadOutlined />}
                disabled={!debugData?.assembled_prompt}
                onClick={() => {
                  if (!debugData) return;
                  exportPromptDebugMarkdown({
                    title: getPromptTypeMeta(debugPromptType).title,
                    prompt_key: debugPromptType,
                    rule_id: debugData.rule_id,
                    scenario_id: debugData.scenario_id,
                    assembled_messages: debugData.assembled_messages,
                    assembled_prompt: debugData.assembled_prompt,
                    sample_dialogue: debugData.sample_dialogue,
                    recommended_config: debugData.recommended_config,
                    test_result: testResult,
                  });
                  message.success('已导出调试 Markdown 文件');
                }}
              >
                导出 Markdown
              </Button>
            </div>
          </div>

          <Spin spinning={debugLoading} tip="正在根据规则与场景数据组装完整 Prompt...">
            <div className="grid grid-cols-2 gap-4">
              {/* 左栏: 组装好的真实 LLM Request Messages ([system, user] 分角色展示) */}
              <div className="flex flex-col h-[600px] bg-slate-900 rounded-lg border border-slate-700 overflow-hidden text-slate-100">
                <div className="px-3 py-2 bg-slate-800 border-b border-slate-700 flex items-center justify-between text-xs">
                  <span className="font-semibold text-slate-300 flex items-center gap-1.5">
                    <CodeOutlined /> 真实发送 Messages ([system / user] 拆分)
                  </span>
                  <div className="flex items-center gap-1.5">
                    {debugData?.assembled_messages?.length ? (
                      <Tag color="purple" className="font-mono text-3xs m-0">
                        {debugData.assembled_messages.length} 条消息
                      </Tag>
                    ) : null}
                    <Tag color="cyan" className="font-mono text-3xs m-0">
                      字符数: {debugData?.assembled_prompt.length || 0}
                    </Tag>
                  </div>
                </div>
                <div className="flex-1 overflow-auto p-3 space-y-3 selection:bg-purple-900 selection:text-white">
                  {debugData?.assembled_messages?.length ? (
                    debugData.assembled_messages.map((m, idx) => (
                      <div
                        key={idx}
                        className="rounded border border-slate-700/80 bg-slate-950/60 overflow-hidden"
                      >
                        <div className="px-2.5 py-1 bg-slate-800/80 border-b border-slate-700/70 flex items-center justify-between">
                          <Tag
                            color={m.role === 'system' ? 'purple' : m.role === 'user' ? 'blue' : 'green'}
                            className="font-mono text-3xs uppercase m-0"
                          >
                            {m.role}
                          </Tag>
                          <span className="text-3xs text-slate-400 font-mono">
                            {m.content?.length || 0} chars
                          </span>
                        </div>
                        <pre className="p-2.5 font-mono text-xs leading-5 whitespace-pre-wrap break-words text-slate-100 m-0">
                          {m.content}
                        </pre>
                      </div>
                    ))
                  ) : (
                    <div className="font-mono text-xs leading-5 whitespace-pre-wrap text-slate-400">
                      {debugData?.assembled_prompt || '暂无组装内容，请点击重新组装'}
                    </div>
                  )}
                </div>
              </div>

              {/* 右栏: 在线模型实测面板与解析执行结果 */}
              <div className="flex flex-col h-[600px] bg-white rounded-lg border border-slate-200 overflow-hidden">
                {/* 模型选择与触发栏 */}
                <div className="p-3 bg-slate-50 border-b border-slate-200 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-semibold text-slate-700">测试模型:</span>
                    <Select
                      size="small"
                      value={testModelId}
                      popupMatchSelectWidth={false}
                      onChange={(val) => setTestModelId(val)}
                      className="w-52"
                      placeholder="选择测试模型"
                    >
                      {registeredModels.map((m) => (
                        <Select.Option key={m.id} value={m.id}>
                          {m.name} ({m.provider_type})
                        </Select.Option>
                      ))}
                    </Select>
                  </div>
                  <Button
                    type="primary"
                    size="small"
                    icon={<PlayCircleOutlined />}
                    loading={testRunning}
                    onClick={handleRunPromptTest}
                    className={debugPromptType === 'judge' ? 'bg-purple-600 hover:bg-purple-700' : 'bg-emerald-600 hover:bg-emerald-700'}
                  >
                    立即发送测试请求
                  </Button>
                </div>

                {/* 实测结果展示区 */}
                <div className="flex-1 overflow-auto p-3 space-y-3">
                  {testRunning ? (
                    <div className="py-24 text-center">
                      <Spin tip="正在调用大模型生成推理中，请稍候..." />
                    </div>
                  ) : testResult ? (
                    <div className="space-y-3">
                      {/* 耗时与状态状态条 */}
                      <div className="flex items-center justify-between flex-wrap gap-2 p-2 bg-slate-100 rounded text-xs border border-slate-200">
                        <span className="flex items-center gap-2">
                          <Tag color={testResult.status === 'success' ? 'green' : 'red'}>
                            {testResult.status.toUpperCase()}
                          </Tag>
                          <span className="text-slate-600">耗时: {testResult.latency_ms} ms</span>
                        </span>
                        {testResult.effective_config && (
                          <span className="text-3xs text-slate-500 font-mono">
                            temp={testResult.effective_config.temperature} · max_tokens={testResult.effective_config.max_tokens} · thinking={testResult.effective_config.thinking_enabled ? testResult.effective_config.thinking_level : 'OFF'}
                          </span>
                        )}
                      </div>

                      {/* 思考链展示 (若有) */}
                      {testResult.thinking && (
                        <div className="p-2.5 bg-purple-50/60 rounded border border-purple-200 text-xs">
                          <div className="font-semibold text-purple-900 mb-1">🧠 模型思考链 (Thinking):</div>
                          <div className="font-mono text-purple-950 whitespace-pre-wrap max-h-36 overflow-auto text-3xs leading-relaxed">
                            {testResult.thinking}
                          </div>
                        </div>
                      )}

                      {/* 拓扑轨迹检测效果解析 */}
                      {debugPromptType === 'trajectory_extract' && testResult.parsed_data && (
                        <div className="p-3 bg-blue-50/60 rounded border border-blue-200 space-y-2 text-xs">
                          <div className="flex items-center justify-between">
                            <span className="font-bold text-blue-950">SOP 拓扑轨迹抽取结果:</span>
                            <Tag color="blue">
                              节点数: {testResult.parsed_data.trajectory?.length || 0}
                            </Tag>
                          </div>
                          {Array.isArray(testResult.parsed_data.trajectory) &&
                          testResult.parsed_data.trajectory.length > 0 ? (
                            <div className="space-y-1.5 max-h-60 overflow-auto">
                              {testResult.parsed_data.trajectory.map((tr: any, i: number) => (
                                <div key={i} className="p-2 bg-white rounded border border-blue-100">
                                  <div className="flex items-center justify-between">
                                    <Space size="small">
                                      <Tag color="geekblue">#{tr.turn}</Tag>
                                      <Tag color="blue" className="font-mono font-semibold">
                                        {tr.node_id}
                                      </Tag>
                                      {Array.isArray(tr.user_slots) &&
                                        tr.user_slots.map((sl: string, si: number) => (
                                          <Tag key={si} color="cyan">
                                            槽位: {sl}
                                          </Tag>
                                        ))}
                                    </Space>
                                  </div>
                                  {tr.evidence && (
                                    <div className="text-slate-600 mt-1 text-3xs">{tr.evidence}</div>
                                  )}
                                </div>
                              ))}
                            </div>
                          ) : (
                            <div className="text-slate-500">
                              {testResult.parsed_data.parse_error || '未抽取出节点轨迹'}
                            </div>
                          )}
                        </div>
                      )}

                      {/* FAQ 提问轮次抽取与改写效果解析 */}
                      {debugPromptType === 'faq_extract' && testResult.parsed_data && (
                        <div className="p-3 bg-sky-50/60 rounded border border-sky-200 space-y-2 text-xs">
                          <div className="flex items-center justify-between">
                            <span className="font-bold text-sky-950">FAQ 提问轮次抽取与检索改写结果:</span>
                            <Tag color="geekblue">
                              命中问答对: {testResult.parsed_data.faq_rounds?.length || 0}
                            </Tag>
                          </div>
                          {Array.isArray(testResult.parsed_data.faq_rounds) &&
                          testResult.parsed_data.faq_rounds.length > 0 ? (
                            <div className="space-y-1.5 max-h-60 overflow-auto">
                              {testResult.parsed_data.faq_rounds.map((fr: any, i: number) => (
                                <div key={i} className="p-2 bg-white rounded border border-sky-100 space-y-1">
                                  <div className="flex items-center gap-2">
                                    <Tag color="blue">客户 #{fr.user_turn}</Tag>
                                    <Tag color="green">坐席 #{fr.assistant_turn}</Tag>
                                  </div>
                                  <div className="text-slate-700">
                                    <span className="font-semibold">原问题:</span> {fr.raw_user_question}
                                  </div>
                                  <div className="text-sky-800 bg-sky-50 p-1.5 rounded text-3xs font-mono">
                                    检索改写 Query: {fr.rewritten_query}
                                  </div>
                                </div>
                              ))}
                            </div>
                          ) : (
                            <div className="text-slate-500">
                              {testResult.parsed_data.parse_error || '未识别到客户 FAQ 政策提问轮次'}
                            </div>
                          )}
                        </div>
                      )}

                      {/* 模拟客户回复效果解析 */}
                      {debugPromptType === 'client_simulator' && testResult.parsed_data && (
                        <div className="p-3 bg-orange-50/60 rounded border border-orange-200 space-y-1.5 text-xs">
                          <div className="font-bold text-orange-950">🎭 模拟客户口语回复预览:</div>
                          <div className="p-2 bg-white rounded border border-orange-100 text-slate-800 whitespace-pre-wrap">
                            {testResult.parsed_data.reply || testResult.raw_content}
                          </div>
                        </div>
                      )}

                      {/* 质检场景效果解析 */}
                      {(debugPromptType === 'judge' || debugPromptType === 'judge_plain') && testResult.parsed_data && (
                        <div className="p-3 bg-slate-50 rounded border border-purple-200 space-y-2">
                          <div className="flex items-center justify-between">
                            <span className="font-bold text-xs text-slate-800">质检判决结论:</span>
                            <Space>
                              <Tag color={testResult.parsed_data.verdict === 'PASS' ? 'green' : 'red'}>
                                {testResult.parsed_data.verdict || 'FAIL'} · {testResult.parsed_data.score ?? 0} 分
                              </Tag>
                            </Space>
                          </div>
                          {testResult.parsed_data.detected_defects && (
                            <div>
                              <div className="text-3xs font-semibold text-red-700 mb-1">检出缺陷项:</div>
                              <ul className="list-disc pl-4 text-xs text-red-900 space-y-0.5">
                                {testResult.parsed_data.detected_defects.map((d: any, i: number) => (
                                  <li key={i}>{String(d)}</li>
                                ))}
                              </ul>
                            </div>
                          )}
                          {testResult.parsed_data.expert_comments && (
                            <p className="text-xs text-slate-600 italic border-t pt-1 border-slate-200">
                              点评: {testResult.parsed_data.expert_comments}
                            </p>
                          )}
                        </div>
                      )}

                      {/* 专职 FAQ 质检场景效果解析 */}
                      {debugPromptType === 'faq_judge' && testResult.parsed_data && (
                        <div className="p-3 bg-slate-50 rounded border border-emerald-300 space-y-2">
                          <div className="flex items-center justify-between">
                            <span className="font-bold text-xs text-slate-800">FAQ 政策质检核查结果:</span>
                            <Tag color={(!testResult.parsed_data.faq_deductions || testResult.parsed_data.faq_deductions.length === 0) ? 'green' : 'orange'}>
                              {(!testResult.parsed_data.faq_deductions || testResult.parsed_data.faq_deductions.length === 0)
                                ? '政策解答 100% 准确 (0 扣分)'
                                : `检出 ${testResult.parsed_data.faq_deductions.length} 处口径偏差`}
                            </Tag>
                          </div>
                          {testResult.parsed_data.faq_deductions && testResult.parsed_data.faq_deductions.length > 0 ? (
                            <div className="space-y-1.5">
                              {testResult.parsed_data.faq_deductions.map((fd: any, i: number) => (
                                <div key={i} className="p-2 bg-white rounded border border-red-200 text-xs">
                                  <div className="flex items-center justify-between text-red-800 font-semibold">
                                    <span>[#{fd.turn || '未注轮次'}] {fd.item}</span>
                                    <Tag color="red">-{fd.points || 0} 分</Tag>
                                  </div>
                                  <div className="text-slate-600 mt-0.5">{fd.reason}</div>
                                  {fd.reference_answer && (
                                    <div className="mt-1 text-emerald-800 bg-emerald-50 p-1.5 rounded text-3xs font-medium">
                                      官方口径: {fd.reference_answer}
                                    </div>
                                  )}
                                </div>
                              ))}
                            </div>
                          ) : (
                            <div className="text-xs text-emerald-700 bg-emerald-50 p-2 rounded">
                              ✓ 坐席应答与官方知识库标准口径完全一致，无任何虚假承诺与政策幻觉。
                            </div>
                          )}
                        </div>
                      )}

                      {/* 修复场景: 处方单 actions 解析与模拟执行效果 */}
                      {debugPromptType === 'remediate' && testResult.parsed_data && (
                        <div className="p-3 bg-emerald-50/60 rounded border border-emerald-200 space-y-2 text-xs">
                          <div className="font-bold text-emerald-950 flex items-center justify-between">
                            <span>📋 声明式双工具处方单 (Actions):</span>
                            <Tag color="green">
                              操作数量: {testResult.parsed_data.actions?.length || 0}
                            </Tag>
                          </div>

                          {testResult.parsed_data.actions && testResult.parsed_data.actions.length > 0 ? (
                            <div className="space-y-1">
                              {testResult.parsed_data.actions.map((act: any, i: number) => (
                                <div key={i} className="p-2 bg-white rounded border border-emerald-100 flex items-center justify-between text-xs">
                                  <span className="flex items-center gap-1.5">
                                    <Tag color={act.tool === 'prune' ? 'volcano' : 'blue'}>
                                      {act.tool}
                                    </Tag>
                                    <span className="font-mono font-semibold">
                                      {act.tool === 'prune' ? `剪枝轮次 [${act.turns?.join(', ')}]` : `替换坐席轮次 #${act.turn}`}
                                    </span>
                                  </span>
                                  <span className="text-slate-500 text-3xs truncate max-w-xs">{act.reason}</span>
                                </div>
                              ))}
                            </div>
                          ) : (
                            <span className="text-slate-400">未解析到有效的 actions 处方单</span>
                          )}

                          {/* 模拟执行后的对话效果 */}
                          {testResult.simulation_preview && (
                            <div className="mt-2 pt-2 border-t border-emerald-200">
                              <div className="flex items-center justify-between mb-1">
                                <span className="font-semibold text-emerald-900">执行后的新对话预览:</span>
                                <Tag color={testResult.simulation_preview.is_format_valid ? 'success' : 'warning'}>
                                  {testResult.simulation_preview.format_message}
                                </Tag>
                              </div>
                              <div className="max-h-48 overflow-auto border rounded bg-white p-2 space-y-1 font-mono text-3xs">
                                {testResult.simulation_preview.remediated_turns?.map((t: any, i: number) => (
                                  <div key={i} className={t.role === 'user' ? 'text-blue-800' : 'text-slate-800'}>
                                    <span className="font-bold">[{t.role.toUpperCase()}]:</span> {t.content}
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}
                        </div>
                      )}

                      {/* 模型原始纯文本输出 */}
                      <div className="p-3 bg-slate-50 rounded border border-slate-200">
                        <div className="font-semibold text-xs text-slate-700 mb-1">原始回复内容 (Raw Response):</div>
                        <div className="font-mono text-3xs text-slate-800 whitespace-pre-wrap max-h-40 overflow-auto bg-white p-2 rounded border border-slate-200">
                          {testResult.raw_content}
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="py-24 text-center text-slate-400 text-xs">
                      选择测试模型并点击“立即发送测试请求”，查看 LLM 的真实响应与解析效果
                    </div>
                  )}
                </div>
              </div>
            </div>
          </Spin>
        </div>
      </Modal>
    </div>
  );
};
