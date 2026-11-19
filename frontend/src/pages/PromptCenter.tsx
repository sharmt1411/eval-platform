import React, { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  ArrowRightOutlined,
  CheckCircleOutlined,
  CodeOutlined,
  CopyOutlined,
  DiffOutlined,
  DownloadOutlined,
  EditOutlined,
  EyeOutlined,
  ReloadOutlined,
  RollbackOutlined,
  SaveOutlined,
  SearchOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { promptsApi } from '../api';
import {
  copyTextSafely,
  exportPromptDebugJson,
  exportPromptDebugMarkdown,
} from '../utils/promptDebugExport';
import type {
  PromptModuleItem,
  PromptStaticPreviewResponse,
} from '../types';

const { Text } = Typography;
const { TextArea } = Input;

const CATEGORY_COLORS: Record<string, string> = {
  '拓扑与流程质检': 'purple',
  'FAQ 问答质检': 'cyan',
  '数据精修与清洗': 'green',
  '模拟对弈': 'orange',
  '常规评测': 'blue',
};

interface RealDebugGuide {
  route: string;
  buttonLabel: string;
  pathDesc: string;
  precondition?: string;
}

const REAL_DEBUG_GUIDE_MAP: Record<string, RealDebugGuide> = {
  trajectory_extract: {
    route: '/rules?tab=mermaid&prompt=trajectory_extract',
    buttonLabel: '前往规则中心选规则',
    pathDesc: '规则中心 → 选择已开启 Mermaid 的规则 →「Mermaid SOP 与节点检查」Tab →「拼装预览与实测」',
    precondition: '前置条件：目标规则必须已开启「Mermaid 流程图质检」开关（关闭时该调试区会隐藏）。',
  },
  judge: {
    route: '/rules?tab=checkpoints&prompt=judge',
    buttonLabel: '前往规则中心选规则',
    pathDesc: '规则中心 → 选择已开启 Mermaid 的规则 →「质检标准与红线」Tab →「调试组装与实测」',
    precondition: '前置条件：目标规则必须已开启「Mermaid 流程图质检」开关（关闭时会切换为纯规则质检 judge_plain）。',
  },
  judge_plain: {
    route: '/rules?tab=checkpoints&prompt=judge_plain',
    buttonLabel: '前往规则中心选规则',
    pathDesc: '规则中心 → 选择关闭 Mermaid 的规则 →「质检标准与红线」Tab →「调试纯规则质检」',
    precondition: '前置条件：目标规则必须关闭「Mermaid 流程图质检」开关（开启时会走流程图版质检 judge）。',
  },
  faq_extract: {
    route: '/rules?tab=faq&prompt=faq_extract',
    buttonLabel: '前往规则中心选规则',
    pathDesc: '规则中心 → 选择已开启 FAQ 的规则 →「FAQ 问答质检与知识库」Tab → 提问轮次检测「拼装预览与实测」',
    precondition: '前置条件：目标规则必须已开启「FAQ 问答准确度质检」开关（关闭时该调试区会隐藏）。',
  },
  faq_judge: {
    route: '/rules?tab=faq&prompt=faq_judge',
    buttonLabel: '前往规则中心选规则',
    pathDesc: '规则中心 → 选择已开启 FAQ 的规则 →「FAQ 问答质检与知识库」Tab → 专职 FAQ 质检「调试组装与实测」',
    precondition: '前置条件：目标规则必须已开启「FAQ 问答准确度质检」开关（关闭时该调试区会隐藏）。',
  },
  remediate: {
    route: '/rules?tab=remediation&prompt=remediate',
    buttonLabel: '直达规则中心调试',
    pathDesc: '规则中心 → 选择目标规则 →「数据清洗与修复规范」Tab →「调试组装与实测」（读取该规则的自定义修复指导等配置）',
  },
  client_simulator: {
    route: '/rules?tab=scenarios&prompt=client_simulator',
    buttonLabel: '直达规则中心调试',
    pathDesc: '规则中心 → 选择目标规则 →「虚拟客户剧本库」Tab →「调试客户模拟人设」',
  },
  benchmark_judge: {
    route: '/benchmarks?debugJudge=1',
    buttonLabel: '直达常规评测调试',
    pathDesc: '常规评测中心 →「常规评测 Rubric 裁判 Prompt 组装与实测」弹窗（可选真实评测集样本与 Rubric 实测）',
  },
};

export const PromptCenter: React.FC = () => {
  const navigate = useNavigate();
  const [prompts, setPrompts] = useState<PromptModuleItem[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [saving, setSaving] = useState<boolean>(false);
  const [selectedKey, setSelectedKey] = useState<string>('');
  const [draftContent, setDraftContent] = useState<string>('');
  const [searchKeyword, setSearchKeyword] = useState<string>('');
  const [selectedCategory, setSelectedCategory] = useState<string>('ALL');
  const [activeTab, setActiveTab] = useState<string>('edit');

  // 纯静态占位符示例预览
  const [customVarsText, setCustomVarsText] = useState<string>('{}');
  const [previewLoading, setPreviewLoading] = useState<boolean>(false);
  const [previewData, setPreviewData] = useState<PromptStaticPreviewResponse | null>(null);

  // 对比默认模板弹窗
  const [compareModalVisible, setCompareModalVisible] = useState<boolean>(false);

  const selectedPrompt = useMemo(
    () => prompts.find((p) => p.prompt_key === selectedKey) || null,
    [prompts, selectedKey]
  );

  const loadPrompts = async () => {
    setLoading(true);
    try {
      const promptList = await promptsApi.list();
      setPrompts(promptList);
      if (promptList.length > 0 && !selectedKey) {
        const first = promptList[0];
        setSelectedKey(first.prompt_key);
        setDraftContent(first.template_content);
        setCustomVarsText(JSON.stringify(first.sample_vars || {}, null, 2));
      } else if (selectedKey) {
        const prevItem = prompts.find((p) => p.prompt_key === selectedKey);
        const nextItem = promptList.find((p) => p.prompt_key === selectedKey);
        if (nextItem && (!prevItem || draftContent === prevItem.template_content)) {
          setDraftContent(nextItem.template_content);
        }
      }
    } catch (e: any) {
      message.error(`加载提示词模块失败: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadPrompts();
  }, []);

  const runStaticPreview = async (
    key: string,
    templateText: string,
    varsJsonText: string
  ) => {
    let parsedVars: Record<string, any> | undefined;
    try {
      parsedVars = JSON.parse(varsJsonText || '{}');
    } catch {
      message.error('示例变量 JSON 格式不合法，请检查后重试');
      return;
    }

    setPreviewLoading(true);
    try {
      const res = await promptsApi.preview(key, {
        template_override: templateText,
        custom_vars: parsedVars,
      });
      setPreviewData(res);
    } catch (e: any) {
      message.error(`静态示例预览失败: ${e.message}`);
    } finally {
      setPreviewLoading(false);
    }
  };

  const handleSelectPrompt = (item: PromptModuleItem) => {
    const nextVarsText = JSON.stringify(item.sample_vars || {}, null, 2);
    setSelectedKey(item.prompt_key);
    setDraftContent(item.template_content);
    setCustomVarsText(nextVarsText);
    setPreviewData(null);
    if (activeTab === 'preview') {
      runStaticPreview(item.prompt_key, item.template_content, nextVarsText);
    }
  };

  const handleSavePrompt = async () => {
    if (!selectedPrompt) return;
    if (!draftContent.trim()) {
      message.warning('提示词模板内容不能为空');
      return;
    }
    setSaving(true);
    try {
      const res = await promptsApi.update(selectedPrompt.prompt_key, draftContent);
      setPrompts((prev) =>
        prev.map((p) => (p.prompt_key === res.item.prompt_key ? res.item : p))
      );
      setDraftContent(res.item.template_content);
      if (res.item.missing_placeholders && res.item.missing_placeholders.length > 0) {
        message.warning(
          `已保存，但检测到缺少必需占位符: ${res.item.missing_placeholders.map((x) => `{${x}}`).join(', ')}`
        );
      } else {
        message.success(`「${res.item.name}」已保存并实时生效`);
      }
      if (activeTab === 'preview') {
        await runStaticPreview(res.item.prompt_key, res.item.template_content, customVarsText);
      }
    } catch (e: any) {
      message.error(`保存失败: ${e.message}`);
    } finally {
      setSaving(false);
    }
  };

  const handleResetPrompt = async () => {
    if (!selectedPrompt) return;
    setSaving(true);
    try {
      const res = await promptsApi.reset(selectedPrompt.prompt_key);
      setPrompts((prev) =>
        prev.map((p) => (p.prompt_key === res.item.prompt_key ? res.item : p))
      );
      setDraftContent(res.item.template_content);
      message.success(`「${res.item.name}」已恢复为系统内置默认模板`);
      if (activeTab === 'preview') {
        await runStaticPreview(res.item.prompt_key, res.item.template_content, customVarsText);
      }
    } catch (e: any) {
      message.error(`恢复默认失败: ${e.message}`);
    } finally {
      setSaving(false);
    }
  };

  const categories = useMemo(() => {
    const set = new Set<string>();
    prompts.forEach((p) => set.add(p.category));
    return ['ALL', ...Array.from(set)];
  }, [prompts]);

  const filteredPrompts = useMemo(() => {
    const kw = searchKeyword.trim().toLowerCase();
    return prompts.filter((p) => {
      if (selectedCategory !== 'ALL' && p.category !== selectedCategory) return false;
      if (!kw) return true;
      return (
        p.name.toLowerCase().includes(kw) ||
        p.prompt_key.toLowerCase().includes(kw) ||
        p.description.toLowerCase().includes(kw) ||
        p.engine_location.toLowerCase().includes(kw)
      );
    });
  }, [prompts, searchKeyword, selectedCategory]);

  const liveMissingPlaceholders = useMemo(() => {
    if (!selectedPrompt) return [];
    return (selectedPrompt.placeholders || [])
      .filter((ph) => ph.required !== false)
      .map((ph) => ph.name)
      .filter((name) => name && !draftContent.includes(`{${name}}`));
  }, [selectedPrompt, draftContent]);

  const isDirty = useMemo(() => {
    if (!selectedPrompt) return false;
    return draftContent !== selectedPrompt.template_content;
  }, [selectedPrompt, draftContent]);

  return (
    <div className="grid grid-cols-12 gap-4 h-[calc(100vh-112px)] min-h-[640px]">
      {/* 左侧: 提示词模块列表 */}
      <div className="col-span-12 lg:col-span-4 xl:col-span-3 h-full flex flex-col bg-white rounded-lg border border-slate-200 shadow-2xs overflow-hidden">
        <div className="p-3.5 border-b border-slate-200 space-y-2.5 bg-slate-50/70">
          <div className="flex items-center justify-between">
            <div className="font-bold text-sm text-slate-800 flex items-center gap-1.5">
              <CodeOutlined className="text-blue-600" />
              全平台提示词模板 ({prompts.length})
            </div>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              loading={loading}
              onClick={loadPrompts}
            >
              刷新
            </Button>
          </div>
          <Input
            size="small"
            allowClear
            prefix={<SearchOutlined className="text-slate-400" />}
            placeholder="搜索名称 / key / 源码位置..."
            value={searchKeyword}
            onChange={(e) => setSearchKeyword(e.target.value)}
          />
          <Select
            size="small"
            value={selectedCategory}
            onChange={setSelectedCategory}
            className="w-full"
            popupMatchSelectWidth={false}
          >
            {categories.map((cat) => (
              <Select.Option key={cat} value={cat}>
                {cat === 'ALL' ? '全部分类模块' : cat}
              </Select.Option>
            ))}
          </Select>
        </div>

        <div className="flex-1 overflow-auto p-2.5 space-y-2">
          <Spin spinning={loading}>
            {filteredPrompts.length > 0 ? (
              filteredPrompts.map((item) => {
                const active = item.prompt_key === selectedKey;
                return (
                  <div
                    key={item.prompt_key}
                    onClick={() => handleSelectPrompt(item)}
                    className={`p-3 rounded-lg border cursor-pointer transition-all ${
                      active
                        ? 'border-blue-500 bg-blue-50/60 shadow-2xs'
                        : 'border-slate-200 bg-white hover:border-blue-300 hover:bg-slate-50/60'
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <span className="font-semibold text-xs text-slate-800 leading-snug">
                        {item.name}
                      </span>
                      <Tag
                        color={item.is_customized ? 'orange' : 'default'}
                        className="m-0 text-3xs shrink-0"
                      >
                        {item.is_customized ? '已自定义' : '系统默认'}
                      </Tag>
                    </div>
                    <div className="mt-1.5 flex items-center gap-1.5 flex-wrap">
                      <Tag
                        color={CATEGORY_COLORS[item.category] || 'blue'}
                        className="m-0 text-3xs"
                      >
                        {item.category}
                      </Tag>
                      <span className="font-mono text-3xs text-slate-500">{item.prompt_key}</span>
                    </div>
                    <div className="mt-1.5 text-3xs text-slate-400 font-mono truncate">
                      {item.engine_location}
                    </div>
                  </div>
                );
              })
            ) : (
              <Empty description="无匹配提示词模块" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            )}
          </Spin>
        </div>
      </div>

      {/* 右侧: 模板展示、修改与纯静态示例预览 */}
      <div className="col-span-12 lg:col-span-8 xl:col-span-9 h-full flex flex-col bg-white rounded-lg border border-slate-200 shadow-2xs overflow-hidden">
        {selectedPrompt ? (
          <>
            {/* 头部信息与主操作栏 */}
            <div className="px-4 py-3 border-b border-slate-200 bg-slate-50/70 flex items-center justify-between flex-wrap gap-3">
              <div className="space-y-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-bold text-base text-slate-800">
                    {selectedPrompt.name}
                  </span>
                  <Tag color={CATEGORY_COLORS[selectedPrompt.category] || 'blue'}>
                    {selectedPrompt.category}
                  </Tag>
                  <Tag color="geekblue" className="font-mono">
                    {selectedPrompt.prompt_key}
                  </Tag>
                  {selectedPrompt.is_customized ? (
                    <Tag color="orange">已启用自定义版本</Tag>
                  ) : (
                    <Tag color="green" icon={<CheckCircleOutlined />}>
                      系统内置默认
                    </Tag>
                  )}
                  {isDirty && <Tag color="volcano">未保存修改</Tag>}
                </div>
                <div className="text-xs text-slate-500 flex items-center gap-2 flex-wrap">
                  <span>{selectedPrompt.description}</span>
                  <span className="font-mono text-3xs bg-slate-200/70 text-slate-700 px-1.5 py-0.5 rounded">
                    调用点: {selectedPrompt.engine_location}
                  </span>
                </div>
              </div>

              <Space wrap>
                <Button
                  size="small"
                  icon={<DiffOutlined />}
                  onClick={() => setCompareModalVisible(true)}
                >
                  对比默认模板
                </Button>
                <Popconfirm
                  title="确定恢复为系统内置默认模板？"
                  description="恢复后将丢弃当前自定义修改，引擎立即切回内置版本。"
                  onConfirm={handleResetPrompt}
                >
                  <Button
                    size="small"
                    icon={<RollbackOutlined />}
                    disabled={!selectedPrompt.is_customized && !isDirty}
                  >
                    恢复系统默认
                  </Button>
                </Popconfirm>
                <Button
                  size="small"
                  icon={<EyeOutlined />}
                  loading={previewLoading}
                  onClick={() => {
                    setActiveTab('preview');
                    runStaticPreview(selectedPrompt.prompt_key, draftContent, customVarsText);
                  }}
                >
                  静态占位符预览
                </Button>
                <Button
                  type="primary"
                  size="small"
                  icon={<SaveOutlined />}
                  loading={saving}
                  onClick={handleSavePrompt}
                >
                  保存修改
                </Button>
              </Space>
            </div>

            {/* 主体工作区 Tabs */}
            <div className="flex-1 overflow-auto p-4">
              <Tabs
                activeKey={activeTab}
                onChange={(key) => {
                  setActiveTab(key);
                  if (key === 'preview' && selectedPrompt) {
                    runStaticPreview(selectedPrompt.prompt_key, draftContent, customVarsText);
                  }
                }}
                items={[
                  {
                    key: 'edit',
                    label: (
                      <span>
                        <EditOutlined /> 模板展示与修改
                      </span>
                    ),
                    children: (
                      <div className="space-y-3">
                        <Alert
                          type="info"
                          showIcon
                          message={
                            <span className="text-xs">
                              <strong>定位与优先级说明</strong>：本中心专注管理全平台<strong>全局默认激活模板</strong>（生效优先级：单次调试覆盖 &gt; 规则中心单场景自定义模板 &gt; <strong>本中心激活模板</strong> &gt; 系统内置默认）。如需结合真实规则/剧本/知识库或真实评测样本组装并调用模型实测，请前往对应的「场景规则与 SOP」或「常规评测工作台」。
                            </span>
                          }
                        />

                        {liveMissingPlaceholders.length > 0 && (
                          <Alert
                            type="warning"
                            showIcon
                            icon={<WarningOutlined />}
                            message={
                              <span className="text-xs">
                                当前编辑稿缺少以下必需占位符，可能导致运行时上下文注入不完整：{' '}
                                {liveMissingPlaceholders.map((p) => (
                                  <Tag key={p} color="warning" className="font-mono ml-1">
                                    {`{${p}}`}
                                  </Tag>
                                ))}
                              </span>
                            }
                          />
                        )}

                        <Card
                          size="small"
                          title={
                            <span className="text-xs font-semibold text-slate-700">
                              占位符契约速查（点击即可复制）
                            </span>
                          }
                          className="bg-slate-50/60"
                        >
                          <div className="flex flex-wrap gap-2">
                            {(selectedPrompt.placeholders || []).map((ph) => {
                              const token = `{${ph.name}}`;
                              const isRequired = ph.required !== false;
                              const missing = isRequired && !draftContent.includes(token);
                              return (
                                <Tooltip
                                  key={ph.name}
                                  title={`${ph.desc}（${isRequired ? '必需' : '可选'} · 点击复制 ${token}）`}
                                >
                                  <Tag
                                    color={missing ? 'warning' : isRequired ? 'blue' : 'default'}
                                    className="cursor-pointer font-mono text-xs py-0.5 px-2"
                                    onClick={() => copyTextSafely(token, `已复制占位符 ${token}`)}
                                  >
                                    {token} · {ph.desc}
                                    {!isRequired ? ' (可选)' : ''}
                                  </Tag>
                                </Tooltip>
                              );
                            })}
                          </div>
                        </Card>

                        <div>
                          <div className="flex items-center justify-between mb-1.5">
                            <Text strong className="text-xs text-slate-700">
                              提示词模板源码编辑器（支持{' '}
                              <code className="bg-slate-100 px-1 rounded">
                                {'<!-- USER_INPUT_START -->'}
                              </code>{' '}
                              拆分 System / User）
                            </Text>
                            <Space size="small">
                              <Tag className="font-mono text-3xs m-0">
                                字符数: {draftContent.length}
                              </Tag>
                              <Tag className="font-mono text-3xs m-0">
                                行数: {draftContent ? draftContent.split('\n').length : 0}
                              </Tag>
                              <Button
                                size="small"
                                type="link"
                                icon={<CopyOutlined />}
                                onClick={() => copyTextSafely(draftContent, '已复制当前模板全文')}
                              >
                                复制模板
                              </Button>
                            </Space>
                          </div>
                          <TextArea
                            rows={19}
                            value={draftContent}
                            onChange={(e) => setDraftContent(e.target.value)}
                            className="font-mono text-xs leading-6 bg-slate-50"
                            placeholder="请输入提示词模板内容..."
                          />
                        </div>
                      </div>
                    ),
                  },
                  {
                    key: 'preview',
                    label: (
                      <span>
                        <EyeOutlined /> 静态占位符示例预览与导出
                      </span>
                    ),
                    children: (
                      <div className="space-y-3">
                        {(() => {
                          const guide = REAL_DEBUG_GUIDE_MAP[selectedPrompt.prompt_key];
                          return (
                            <Alert
                              type="info"
                              showIcon
                              message={
                                <span className="text-xs font-semibold text-slate-800">
                                  这是「纯静态样例预览」，非真实规则数据
                                </span>
                              }
                              description={
                                <div className="text-xs text-slate-600 space-y-1 mt-0.5">
                                  <div>
                                    • <b>当前预览内容</b>：仅使用下方写死的内置示例变量（可在左侧 JSON 框临时改值测试）验证 <code>{'{占位符}'}</code> 替换与 <code>System / User</code> 消息拆分，<b>不读取业务规则、FAQ 知识库或评测集，也不调用模型</b>。
                                  </div>
                                  <div>
                                    • <b>如何看真实规则拼装与在线实测</b>：
                                    {guide ? (
                                      <span>
                                        前往 <span className="font-semibold text-blue-700">{guide.pathDesc}</span>
                                        。若规则未单独覆盖模板，保存本页全局模板后即自动生效。
                                      </span>
                                    ) : (
                                      <span>请前往对应业务页面（规则中心或常规评测中心）的调试弹窗，基于真实配置完整拼装并试跑模型。</span>
                                    )}
                                  </div>
                                  {guide?.precondition && (
                                    <div className="text-amber-700">
                                      • <b>{guide.precondition}</b>
                                    </div>
                                  )}
                                </div>
                              }
                              action={
                                guide ? (
                                  <Button
                                    size="small"
                                    type="primary"
                                    ghost
                                    icon={<ArrowRightOutlined />}
                                    onClick={() => navigate(guide.route)}
                                  >
                                    {guide.buttonLabel}
                                  </Button>
                                ) : undefined
                              }
                            />
                          );
                        })()}

                        <div className="p-3 bg-slate-50 rounded-lg border border-slate-200 flex items-center justify-between flex-wrap gap-2">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-xs text-slate-600">
                              修改左侧静态样例 JSON 或模板草稿后，点击刷新即可查看替换与拆分结果
                            </span>
                            <Button
                              size="small"
                              type="primary"
                              ghost
                              icon={<ReloadOutlined />}
                              loading={previewLoading}
                              onClick={() =>
                                runStaticPreview(
                                  selectedPrompt.prompt_key,
                                  draftContent,
                                  customVarsText
                                )
                              }
                            >
                              刷新静态预览
                            </Button>
                          </div>

                          <Space wrap>
                            <Button
                              size="small"
                              icon={<CopyOutlined />}
                              disabled={!previewData?.assembled_messages?.length}
                              onClick={() => {
                                if (previewData?.assembled_messages?.length) {
                                  copyTextSafely(
                                    JSON.stringify(previewData.assembled_messages, null, 2),
                                    '已复制 Request Messages JSON'
                                  );
                                }
                              }}
                            >
                              复制 Messages
                            </Button>
                            <Button
                              size="small"
                              icon={<DownloadOutlined />}
                              disabled={!previewData?.assembled_prompt}
                              onClick={() => {
                                if (!previewData) return;
                                exportPromptDebugJson({
                                  title: `${selectedPrompt.name} (静态示例)`,
                                  prompt_key: selectedPrompt.prompt_key,
                                  assembled_messages: previewData.assembled_messages,
                                  assembled_prompt: previewData.assembled_prompt,
                                });
                                message.success('已导出静态预览 JSON 文件');
                              }}
                            >
                              导出 JSON
                            </Button>
                            <Button
                              size="small"
                              icon={<DownloadOutlined />}
                              disabled={!previewData?.assembled_prompt}
                              onClick={() => {
                                if (!previewData) return;
                                exportPromptDebugMarkdown({
                                  title: `${selectedPrompt.name} (静态示例)`,
                                  prompt_key: selectedPrompt.prompt_key,
                                  assembled_messages: previewData.assembled_messages,
                                  assembled_prompt: previewData.assembled_prompt,
                                });
                                message.success('已导出静态预览 Markdown 文件');
                              }}
                            >
                              导出 Markdown
                            </Button>
                          </Space>
                        </div>

                        {previewData?.unresolved_placeholders &&
                          previewData.unresolved_placeholders.length > 0 && (
                            <Alert
                              type="warning"
                              showIcon
                              message={`渲染后仍检测到未替换占位符: ${previewData.unresolved_placeholders
                                .map((p) => `{${p}}`)
                                .join(', ')}`}
                            />
                          )}

                        <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
                          <div className="xl:col-span-1 flex flex-col">
                            <Card
                              size="small"
                              title={
                                <span className="text-xs font-semibold">
                                  静态样例变量 (JSON，可微调测试)
                                </span>
                              }
                              className="h-[520px] flex flex-col"
                              styles={{ body: { flex: 1, display: 'flex', flexDirection: 'column', padding: 12 } }}
                            >
                              <TextArea
                                value={customVarsText}
                                onChange={(e) => setCustomVarsText(e.target.value)}
                                className="font-mono text-xs flex-1"
                                style={{ resize: 'none' }}
                              />
                            </Card>
                          </div>

                          <div className="xl:col-span-2 flex flex-col h-[520px] bg-slate-900 rounded-lg border border-slate-700 overflow-hidden text-slate-100">
                            <div className="px-3 py-2 bg-slate-800 border-b border-slate-700 flex items-center justify-between text-xs shrink-0">
                              <span className="font-semibold text-slate-200 flex items-center gap-1.5">
                                <CodeOutlined /> 静态拼装与 System / User 拆分结果
                              </span>
                              <Tag color="cyan" className="font-mono text-3xs m-0">
                                {previewData?.assembled_prompt?.length || 0} chars
                              </Tag>
                            </div>
                            <div className="relative flex-1 min-h-0 overflow-auto p-3 space-y-3">
                              {previewLoading && (
                                <div className="absolute inset-0 bg-slate-900/60 z-10 flex items-center justify-center">
                                  <Spin />
                                </div>
                              )}
                              {previewData?.assembled_messages?.length ? (
                                previewData.assembled_messages.map((m, idx) => (
                                  <div
                                    key={idx}
                                    className="rounded border border-slate-700/80 bg-slate-950/60 overflow-hidden"
                                  >
                                    <div className="px-2.5 py-1 bg-slate-800/80 border-b border-slate-700/70 flex items-center justify-between">
                                      <Tag
                                        color={
                                          m.role === 'system'
                                            ? 'purple'
                                            : m.role === 'user'
                                            ? 'blue'
                                            : 'green'
                                        }
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
                                <div className="text-slate-400 text-xs text-center py-20">
                                  点击「刷新静态预览」查看占位符替换效果
                                </div>
                              )}
                            </div>
                          </div>
                        </div>
                      </div>
                    ),
                  },
                ]}
              />
            </div>
          </>
        ) : (
          <div className="flex-1 flex items-center justify-center">
            <Empty description="请在左侧选择一个提示词模块" />
          </div>
        )}
      </div>

      {/* 默认模板 vs 当前编辑模板双栏对照模态框 */}
      <Modal
        title={
          <div className="flex items-center gap-2">
            <DiffOutlined className="text-blue-600" />
            <span>默认模板对照预览 — {selectedPrompt?.name}</span>
          </div>
        }
        open={compareModalVisible}
        onCancel={() => setCompareModalVisible(false)}
        width={1100}
        footer={[
          <Button key="close" onClick={() => setCompareModalVisible(false)}>
            关闭
          </Button>,
        ]}
      >
        {selectedPrompt && (
          <div className="grid grid-cols-2 gap-4">
            <div>
              <div className="text-xs font-semibold text-slate-600 mb-1.5">
                系统内置默认模板 ({selectedPrompt.default_content.length} chars)
              </div>
              <pre className="h-[520px] overflow-auto p-3 rounded bg-slate-50 border border-slate-200 font-mono text-xs whitespace-pre-wrap break-words m-0">
                {selectedPrompt.default_content}
              </pre>
            </div>
            <div>
              <div className="text-xs font-semibold text-blue-700 mb-1.5">
                当前编辑草稿 ({draftContent.length} chars)
              </div>
              <pre className="h-[520px] overflow-auto p-3 rounded bg-blue-50/40 border border-blue-200 font-mono text-xs whitespace-pre-wrap break-words m-0">
                {draftContent}
              </pre>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
};
