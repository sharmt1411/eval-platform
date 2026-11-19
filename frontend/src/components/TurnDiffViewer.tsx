import React, { useState, useEffect } from 'react';
import { Tag, Typography, Button, Input, Tooltip, message, Space, Alert } from 'antd';
import {
  EditOutlined,
  CheckOutlined,
  CloseOutlined,
  WarningOutlined,
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  InfoCircleOutlined,
  BranchesOutlined,
  ApiOutlined,
} from '@ant-design/icons';
import type { ChatMessage, InspectionReport, DebugRequestEntry } from '../types';
import { RequestDebugModal, extractDebugEntriesFromEval } from './RequestDebugModal';

const { Text } = Typography;
const { TextArea } = Input;

interface TurnDiffViewerProps {
  originalMessages: ChatMessage[];
  remediatedMessages?: ChatMessage[] | null;
  initialEval?: InspectionReport | null;
  verifiedEval?: InspectionReport | null;
  remediationDebug?: DebugRequestEntry | null;
  editable?: boolean;
  onSave?: (messages: ChatMessage[]) => void;
}

/**
 * 语义化内容归一化函数：
 * 清除换行符差异 (\r\n vs \n)、多余空行及首尾空白，防止显示格式差异导致的假 Diff
 */
const normalizeTextForComparison = (text?: string | null): string => {
  if (!text) return '';
  return text
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .join('\n');
};

/**
 * 纯文字语义对齐展示：
 * 消除多余硬断行干扰，使左右两栏在相同列宽下具备完全一致的自然折行行为
 */
const cleanDisplayContent = (text?: string | null): string => {
  if (!text) return '';
  return text
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')
    .trim();
};

export const TurnDiffViewer: React.FC<TurnDiffViewerProps> = ({
  originalMessages = [],
  remediatedMessages,
  initialEval,
  verifiedEval,
  remediationDebug,
  editable = false,
  onSave,
}) => {
  const [messages, setMessages] = useState<ChatMessage[]>(remediatedMessages || originalMessages || []);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editContent, setEditContent] = useState<string>('');
  const [showSystemInfo, setShowSystemInfo] = useState<boolean>(false);
  const [reqModalVisible, setReqModalVisible] = useState<boolean>(false);
  const [reqModalTitle, setReqModalTitle] = useState<string>('');
  const [reqModalEntries, setReqModalEntries] = useState<DebugRequestEntry[]>([]);
  const [reqModalRawReport, setReqModalRawReport] = useState<Record<string, any> | null>(null);

  const openReqModal = (
    title: string,
    entries: DebugRequestEntry[],
    rawReport: Record<string, any> | null = null
  ) => {
    setReqModalTitle(title);
    setReqModalEntries(entries);
    setReqModalRawReport(rawReport);
    setReqModalVisible(true);
  };

  useEffect(() => {
    setMessages(remediatedMessages || originalMessages || []);
  }, [remediatedMessages, originalMessages]);

  // 提取首轮系统指令 (独立于轮次行号之外，隐藏在顶栏查看)
  const systemMsg =
    messages.find((m) => m.role === 'system') ||
    originalMessages.find((m) => m.role === 'system') ||
    null;

  // 剥离 system，仅保留人机真实对话轮次，杜绝 off-by-one 错位
  const origTurns = (originalMessages || []).filter((m) => m.role !== 'system');
  const remTurns = (messages || []).filter((m) => m.role !== 'system');

  // 格式合法性校验 (针对最终落盘的完整包含 system 的 messages)
  const validateFormat = (msgs: ChatMessage[]): { valid: boolean; errors: string[] } => {
    const errs: string[] = [];
    if (!msgs || msgs.length === 0) {
      errs.push('对话消息数组不能为空');
      return { valid: false, errors: errs };
    }
    if (msgs[0].role !== 'system') {
      errs.push('首轮消息必须为 system 系统指令');
    }
    for (let i = 1; i < msgs.length; i++) {
      if (msgs[i].role === msgs[i - 1].role && msgs[i].role !== 'system') {
        errs.push(`第 ${i} 轮与第 ${i + 1} 轮角色重复: 出现连续 ${msgs[i].role}`);
      }
    }
    if (msgs[msgs.length - 1].role !== 'assistant') {
      errs.push('末轮消息必须由 assistant (机器人) 收口');
    }
    return { valid: errs.length === 0, errors: errs };
  };

  const validation = validateFormat(messages);

  const startEdit = (idx: number, content: string) => {
    setEditingIndex(idx);
    setEditContent(content);
  };

  const saveEdit = (idx: number) => {
    const newRemTurns = [...remTurns];
    newRemTurns[idx] = { ...newRemTurns[idx], content: editContent };

    // 将首轮 system 重新合并回完整 messages 数组中
    const fullMsgs: ChatMessage[] = systemMsg
      ? [systemMsg, ...newRemTurns]
      : newRemTurns;

    setMessages(fullMsgs);
    setEditingIndex(null);
    if (onSave) {
      onSave(fullMsgs);
    }
    message.success('已临时更新单轮话术并同步');
  };

  const handleGlobalSave = () => {
    if (!validation.valid) {
      message.error('当前对话格式不满足 SFT 约束，无法保存');
      return;
    }
    if (onSave) {
      onSave(messages);
      message.success('已保存修改并提交人工复核');
    }
  };

  const extractDowngradeNotices = (
    evalRep?: InspectionReport | null,
    remedDbg?: DebugRequestEntry | null
  ): string[] => {
    const list: string[] = [];
    if (evalRep?.notices?.length) {
      list.push(...evalRep.notices);
    }
    if (evalRep?.debug_requests?.length) {
      for (const d of evalRep.debug_requests) {
        if (d.downgrade_notice && !list.includes(d.downgrade_notice)) {
          list.push(d.downgrade_notice);
        }
      }
    }
    if (remedDbg?.downgrade_notice && !list.includes(remedDbg.downgrade_notice)) {
      list.push(remedDbg.downgrade_notice);
    }
    return list;
  };

  const initialDowngradeNotices = extractDowngradeNotices(initialEval, null);
  const rightDowngradeNotices = extractDowngradeNotices(verifiedEval, remediationDebug);

  // 纯对话最大轮数
  const maxLen = Math.max(origTurns.length, remTurns.length);

  return (
    <div className="flex flex-col h-full bg-slate-50 border border-slate-200 rounded-lg overflow-hidden">
      {/* 顶部校验告警与保存栏 */}
      <div className="px-4 py-2.5 bg-white border-b border-slate-200 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Text strong className="text-sm text-slate-800">
            轮次级安全比对编辑视窗
          </Text>
          {validation.valid ? (
            <Tag icon={<CheckCircleOutlined />} color="success">
              SFT 格式合规
            </Tag>
          ) : (
            <Tooltip title={validation.errors.join('；')}>
              <Tag icon={<ExclamationCircleOutlined />} color="error" className="cursor-pointer">
                格式告警 ({validation.errors.length})
              </Tag>
            </Tooltip>
          )}

          {systemMsg && (
            <Button
              type="link"
              size="small"
              icon={<InfoCircleOutlined />}
              onClick={() => setShowSystemInfo(!showSystemInfo)}
              className="text-xs text-purple-700 hover:text-purple-900 p-0"
            >
              {showSystemInfo ? '收起系统预设指令' : '查看系统预设指令'}
            </Button>
          )}
        </div>

        {editable && onSave && (
          <Button type="primary" size="small" onClick={handleGlobalSave} disabled={!validation.valid}>
            保存当前版本
          </Button>
        )}
      </div>

      {/* 系统预设指令展开条 (只读展示，不占用对话轮次行号) */}
      {showSystemInfo && systemMsg && (
        <div className="bg-purple-50/70 border-b border-purple-200 px-4 py-2 text-xs text-purple-950">
          <span className="font-semibold mr-2">[全局只读 System Prompt]:</span>
          <span className="font-mono">{systemMsg.content}</span>
        </div>
      )}

      {/* 诊断评语与拓扑检测栏 */}
      <div className="grid grid-cols-2 gap-3 p-3 bg-slate-100 border-b border-slate-200 text-xs">
        {/* 左栏初检报告 */}
        {initialEval && (initialEval.score !== undefined || initialEval.verdict) ? (
          <div className="p-3 bg-white rounded border border-red-200 shadow-2xs flex flex-col justify-between">
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <span className="font-semibold text-red-700 flex items-center gap-1">
                  <WarningOutlined /> 初检缺陷诊断
                </span>
                <Space size={4}>
                  {initialEval.topology_valid !== undefined && (
                    <Tag color={initialEval.topology_valid ? 'green' : 'volcano'}>
                      <BranchesOutlined /> 拓扑: {initialEval.topology_valid ? '连通通过' : '断路拦截'}
                    </Tag>
                  )}
                  <Tag color={initialEval.verdict === 'PASS' ? 'green' : 'red'}>
                    {initialEval.verdict || 'FAIL'} · {initialEval.score ?? 0}分
                  </Tag>
                </Space>
              </div>

              {/* 拓扑断路异常警示 */}
              {initialEval.topology_error && (
                <div className="mb-2 p-1.5 bg-red-50 text-red-700 rounded border border-red-200 text-3xs font-mono">
                  <span className="font-bold">🚨 拓扑异常: </span>
                  {initialEval.topology_error}
                </div>
              )}

              {/* 推进轨迹流展示 */}
              {initialEval.node_trajectory && initialEval.node_trajectory.length > 0 && (
                <div className="mb-2 p-1.5 bg-slate-50 rounded border border-slate-200 text-3xs">
                  <span className="font-semibold text-slate-600 mr-1">推进轨迹:</span>
                  <span className="font-mono text-purple-700">{initialEval.node_trajectory.join(' ➔ ')}</span>
                </div>
              )}

              {/* 扣分说明与缺陷明细 (互斥单一渲染，消除重复) */}
              {initialEval.deductions && initialEval.deductions.length > 0 ? (
                <div className="mb-2 p-1.5 bg-amber-50/80 rounded border border-amber-200 text-3xs">
                  <span className="font-semibold text-amber-800 flex items-center gap-1 mb-1">
                    📊 扣分依据与缺陷明细:
                  </span>
                  <ul className="list-disc pl-4 text-amber-900 space-y-0.5">
                    {initialEval.deductions.map((d: any, idx: number) => {
                      if (typeof d === 'string') return <li key={idx}>{d}</li>;
                      return (
                        <li key={idx}>
                          <span className="font-bold text-red-600">-{d.points ?? 0}分</span>
                          {d.turn ? <Tag color="geekblue" className="ml-1 text-3xs">#{d.turn}轮</Tag> : null}
                          {d.item ? ` [${d.item}]` : ''}: {d.reason || ''}
                          {d.reference_answer ? (
                            <div className="text-emerald-700 bg-emerald-50/70 p-1 rounded mt-0.5 font-medium">
                              官方标准口径: {d.reference_answer}
                            </div>
                          ) : null}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              ) : initialEval.detected_defects && initialEval.detected_defects.length > 0 ? (() => {
                const onlyTopoNotice = initialEval.detected_defects.every((d) =>
                  String(d).trim().startsWith('【拓扑检测元数据】')
                );
                return (
                  <div
                    className={`mb-2 p-1.5 rounded border text-3xs ${
                      onlyTopoNotice
                        ? 'bg-amber-50/80 border-amber-200'
                        : 'bg-red-50/80 border-red-200'
                    }`}
                  >
                    <span
                      className={`font-semibold flex items-center gap-1 mb-1 ${
                        onlyTopoNotice ? 'text-amber-800' : 'text-red-800'
                      }`}
                    >
                      {onlyTopoNotice ? '⚠️ 质检参考提示 (未扣分):' : '🚨 质检检出缺陷:'}
                    </span>
                    <ul
                      className={`list-disc pl-4 space-y-0.5 ${
                        onlyTopoNotice ? 'text-amber-900' : 'text-red-900'
                      }`}
                    >
                      {initialEval.detected_defects.map((d, i) => (
                        <li key={i}>{d}</li>
                      ))}
                    </ul>
                  </div>
                );
              })() : (
                <div className="mb-2 p-1.5 bg-emerald-50/80 rounded border border-emerald-200 text-3xs text-emerald-700 font-medium">
                  ✓ 全流程合规通过 (无扣分缺陷)
                </div>
              )}
              {initialDowngradeNotices.length > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  className="mb-2 text-3xs"
                  message={initialDowngradeNotices.join(' ')}
                />
              )}
              {initialEval.expert_comments && (
                <p className="mt-1.5 text-slate-500 italic border-t pt-1 border-slate-100">
                  点评: {initialEval.expert_comments}
                </p>
              )}
            </div>

            <div className="mt-2 pt-1 border-t border-slate-100 flex items-center justify-end gap-3">
              <Button
                type="link"
                size="small"
                icon={<ApiOutlined />}
                onClick={() =>
                  openReqModal(
                    '初检质检报文与诊断结果',
                    extractDebugEntriesFromEval(initialEval, '初检主流程质检'),
                    initialEval as Record<string, any>
                  )
                }
                className="text-3xs text-indigo-600 hover:text-indigo-800 p-0 h-4"
              >
                初检报文与结果
              </Button>
            </div>
          </div>
        ) : (
          <div className="p-3 bg-white rounded border border-slate-200 shadow-2xs flex flex-col justify-between">
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <span className="font-semibold text-slate-600 flex items-center gap-1">
                  <WarningOutlined /> 初检缺陷诊断
                </span>
                <Tag color="default">暂无初检数据</Tag>
              </div>
              <div className="text-slate-400 py-3 text-center text-xs">
                该样本尚未执行初检质检
              </div>
            </div>
            <div className="mt-2 pt-1 border-t border-slate-100 flex justify-end">
              <Button
                type="link"
                size="small"
                icon={<ApiOutlined />}
                disabled={true}
                className="text-3xs text-slate-300 p-0 h-4"
              >
                暂无报文与结果
              </Button>
            </div>
          </div>
        )}

        {/* 右栏复检验收报告 */}
        {verifiedEval && (verifiedEval.score !== undefined || verifiedEval.verdict) ? (
          <div className="p-3 bg-white rounded border border-emerald-200 shadow-2xs flex flex-col justify-between">
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <span className="font-semibold text-emerald-700 flex items-center gap-1">
                  <CheckCircleOutlined /> 修复与验收结果
                </span>
                <Space size={4}>
                  {verifiedEval.topology_valid !== undefined && (
                    <Tag color={verifiedEval.topology_valid ? 'green' : 'volcano'}>
                      <BranchesOutlined /> 拓扑: {verifiedEval.topology_valid ? '通过' : '断路'}
                    </Tag>
                  )}
                  <Tag color={verifiedEval.verdict === 'PASS' ? 'green' : 'orange'}>
                    {verifiedEval.verdict || 'PENDING'} · {verifiedEval.score ?? (initialEval?.score ?? 0)}分
                  </Tag>
                </Space>
              </div>

              {/* 拓扑断路异常警示 */}
              {verifiedEval.topology_error && (
                <div className="mb-2 p-1.5 bg-red-50 text-red-700 rounded border border-red-200 text-3xs font-mono">
                  <span className="font-bold">🚨 验收拓扑异常: </span>
                  {verifiedEval.topology_error}
                </div>
              )}

              {/* 推进轨迹流展示 */}
              {verifiedEval.node_trajectory && verifiedEval.node_trajectory.length > 0 && (
                <div className="mb-2 p-1.5 bg-slate-50 rounded border border-slate-200 text-3xs">
                  <span className="font-semibold text-slate-600 mr-1">验收轨迹:</span>
                  <span className="font-mono text-emerald-700">{verifiedEval.node_trajectory.join(' ➔ ')}</span>
                </div>
              )}

              {/* 验收扣分说明与缺陷明细 (互斥单一渲染，消除重复) */}
              {verifiedEval.deductions && verifiedEval.deductions.length > 0 ? (
                <div className="mb-2 p-1.5 bg-amber-50/80 rounded border border-amber-200 text-3xs">
                  <span className="font-semibold text-amber-800 flex items-center gap-1 mb-1">
                    📊 验收扣分依据与缺陷明细:
                  </span>
                  <ul className="list-disc pl-4 text-amber-900 space-y-0.5">
                    {verifiedEval.deductions.map((d: any, idx: number) => {
                      if (typeof d === 'string') return <li key={idx}>{d}</li>;
                      return (
                        <li key={idx}>
                          <span className="font-bold text-red-600">-{d.points ?? 0}分</span>
                          {d.turn ? <Tag color="geekblue" className="ml-1 text-3xs">#{d.turn}轮</Tag> : null}
                          {d.item ? ` [${d.item}]` : ''}: {d.reason || ''}
                          {d.reference_answer ? (
                            <div className="text-emerald-700 bg-emerald-50/70 p-1 rounded mt-0.5 font-medium">
                              官方标准口径: {d.reference_answer}
                            </div>
                          ) : null}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              ) : verifiedEval.detected_defects && verifiedEval.detected_defects.length > 0 ? (
                <div className="mb-2 p-1.5 bg-amber-50/80 rounded border border-amber-200 text-3xs">
                  <span className="font-semibold text-amber-800 flex items-center gap-1 mb-1">
                    ⚠️ 验收残留缺陷:
                  </span>
                  <ul className="list-disc pl-4 text-amber-900 space-y-0.5">
                    {verifiedEval.detected_defects.map((d, i) => (
                      <li key={i}>{d}</li>
                    ))}
                  </ul>
                </div>
              ) : (
                <div className="mb-2 p-1.5 bg-emerald-50/80 rounded border border-emerald-200 text-3xs text-emerald-700 font-medium">
                  ✓ 质检验收完全达标，无残留缺陷
                </div>
              )}
              {rightDowngradeNotices.length > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  className="mb-2 text-3xs"
                  message={rightDowngradeNotices.join(' ')}
                />
              )}
              {verifiedEval.expert_comments && (
                <p className="mt-1.5 text-slate-500 italic border-t pt-1 border-slate-100">
                  点评: {verifiedEval.expert_comments}
                </p>
              )}
            </div>

            <div className="mt-2 pt-1 border-t border-slate-100 flex items-center justify-end gap-3 flex-wrap">
              {remediationDebug && remediationDebug.messages?.length ? (
                <Button
                  type="link"
                  size="small"
                  icon={<ApiOutlined />}
                  onClick={() =>
                    openReqModal('AI 修复报文与返回结果', [remediationDebug])
                  }
                  className="text-3xs text-emerald-700 hover:text-emerald-900 p-0 h-4"
                >
                  修复报文与结果
                </Button>
              ) : (
                <Tooltip title="初检直接达标 (GOLD_PASS) 时跳过 AI 修复，或历史记录未留存修复请求报文">
                  <Button
                    type="link"
                    size="small"
                    icon={<ApiOutlined />}
                    disabled={true}
                    className="text-3xs text-slate-300 p-0 h-4"
                  >
                    未触发/暂无修复报文
                  </Button>
                </Tooltip>
              )}
              <Button
                type="link"
                size="small"
                icon={<ApiOutlined />}
                onClick={() =>
                  openReqModal(
                    '二次验收质检报文与诊断结果',
                    extractDebugEntriesFromEval(verifiedEval, '二次质检验收'),
                    verifiedEval as Record<string, any>
                  )
                }
                className="text-3xs text-indigo-600 hover:text-indigo-800 p-0 h-4"
              >
                验收报文与结果
              </Button>
            </div>
          </div>
        ) : (
          <div className="p-3 bg-white rounded border border-slate-200 shadow-2xs flex flex-col justify-between">
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <span className="font-semibold text-slate-600 flex items-center gap-1">
                  <CheckCircleOutlined /> 修复与验收结果
                </span>
                <Tag color={remediationDebug && remediationDebug.messages?.length ? 'blue' : 'default'}>
                  {remediationDebug && remediationDebug.messages?.length ? '已修复·待二次验收' : '尚未进行二次验收'}
                </Tag>
              </div>
              {rightDowngradeNotices.length > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  className="mb-2 text-3xs"
                  message={rightDowngradeNotices.join(' ')}
                />
              )}
              <div className="text-slate-400 py-3 text-center text-xs">
                {remediationDebug && remediationDebug.messages?.length
                  ? '已完成 AI 修复（未自动执行二次验收），可点击上方【按此标准重新质检】选择对右侧语料执行验收'
                  : '待触发定向微创修复或二次质检验收'}
              </div>
            </div>
            <div className="mt-2 pt-1 border-t border-slate-100 flex items-center justify-end gap-3 flex-wrap">
              {remediationDebug && remediationDebug.messages?.length ? (
                <Button
                  type="link"
                  size="small"
                  icon={<ApiOutlined />}
                  onClick={() =>
                    openReqModal('AI 修复报文与返回结果', [remediationDebug])
                  }
                  className="text-3xs text-emerald-700 hover:text-emerald-900 p-0 h-4"
                >
                  修复报文与结果
                </Button>
              ) : null}
              <Button
                type="link"
                size="small"
                icon={<ApiOutlined />}
                disabled={true}
                className="text-3xs text-slate-300 p-0 h-4"
              >
                暂无验收报文
              </Button>
            </div>
          </div>
        )}
      </div>

      {/* 左右分栏气泡对比区 (严格等宽等高、统一自然折行，消除换行与格式造成的误 Diff) */}
      <div className="flex-1 overflow-auto p-4">
        <div className="grid grid-cols-2 gap-4">
          {Array.from({ length: maxLen }).map((_, idx) => {
            const orig = origTurns[idx];
            const rem = remTurns[idx];

            // 真正的语义差异判定：排除由于换行符差异或多余回车空行造成的误判
            const origNorm = normalizeTextForComparison(orig?.content);
            const remNorm = normalizeTextForComparison(rem?.content);
            const hasSemanticDiff = !orig || !rem || origNorm !== remNorm;

            // 优化标记：仅在坐席 (assistant) 文本产生真实语义变动时才标记
            const isAssistantOptimized = rem?.role === 'assistant' && hasSemanticDiff;
            const isUserMismatched = rem?.role === 'user' && hasSemanticDiff;

            return (
              <React.Fragment key={idx}>
                {/* 左列：原对话轮次 (严格固定内部边距与标题高度) */}
                <div
                  className={`p-3.5 rounded-lg border text-sm transition-all flex flex-col justify-between w-full overflow-hidden ${
                    !orig
                      ? 'bg-slate-100/50 border-dashed border-slate-200'
                      : orig.role === 'user'
                      ? 'bg-blue-50/60 border-blue-200 text-blue-900'
                      : 'bg-slate-50 border-slate-300 text-slate-800'
                  }`}
                >
                  {orig ? (
                    <div className="w-full">
                      {/* 头部固定高度，保证与右栏基准线严格平齐 */}
                      <div className="h-6 mb-2 flex items-center justify-between text-xs text-slate-400 font-medium border-b border-black/5 pb-1">
                        <span>
                          #{idx + 1} {orig.role.toUpperCase()}
                        </span>
                      </div>
                      {/* 正文样式与右栏绝对 1:1 对齐 */}
                      <p className="font-sans text-sm leading-6 tracking-normal whitespace-pre-wrap break-words min-h-[3rem] text-slate-800">
                        {cleanDisplayContent(orig.content)}
                      </p>
                      {orig.thinking && (
                        <div className="mt-2 text-xs text-slate-400 bg-white/80 p-2 rounded border border-slate-200">
                          <Text type="secondary" italic>
                            Thinking: {orig.thinking}
                          </Text>
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="py-6 text-center text-slate-400 italic text-xs">（此轮在原实战会话中缺失）</div>
                  )}
                </div>

                {/* 右列：重塑/修复后轮次 (严格与左栏保持相同的列宽、内边距与字体样式) */}
                <div
                  className={`p-3.5 rounded-lg border text-sm transition-all flex flex-col justify-between w-full overflow-hidden ${
                    !rem
                      ? 'bg-slate-100/50 border-dashed border-slate-200'
                      : isAssistantOptimized
                      ? 'bg-emerald-50/80 border-emerald-300 text-slate-900 shadow-2xs'
                      : rem.role === 'user'
                      ? 'bg-blue-50/60 border-blue-200 text-blue-900'
                      : 'bg-slate-50/80 border-slate-200 text-slate-800'
                  }`}
                >
                  {rem ? (
                    <div className="w-full">
                      {/* 头部固定高度，与左栏严格水平对齐 */}
                      <div className="h-6 mb-2 flex items-center justify-between text-xs text-slate-400 font-medium border-b border-black/5 pb-1">
                        <span className="flex items-center gap-1.5">
                          #{idx + 1} {rem.role.toUpperCase()}
                          {isAssistantOptimized && (
                            <Tag color="green" className="text-3xs px-1 leading-4">
                              已优化
                            </Tag>
                          )}
                          {isUserMismatched && (
                            <Tag color="orange" className="text-3xs px-1 leading-4">
                              客户文本差异
                            </Tag>
                          )}
                        </span>
                        {editable && editingIndex !== idx && (
                          <Button
                            type="text"
                            size="small"
                            icon={<EditOutlined />}
                            className="text-slate-400 hover:text-emerald-600 h-5 px-1 text-xs"
                            onClick={() => startEdit(idx, rem.content)}
                          >
                            编辑
                          </Button>
                        )}
                      </div>

                      {editingIndex === idx ? (
                        <div className="space-y-2 mt-1">
                          <TextArea
                            rows={3}
                            value={editContent}
                            onChange={(e) => setEditContent(e.target.value)}
                            className="text-sm font-sans leading-6"
                          />
                          <div className="flex justify-end gap-1">
                            <Button size="small" icon={<CloseOutlined />} onClick={() => setEditingIndex(null)}>
                              取消
                            </Button>
                            <Button size="small" type="primary" icon={<CheckOutlined />} onClick={() => saveEdit(idx)}>
                              确认修改
                            </Button>
                          </div>
                        </div>
                      ) : (
                        /* 正文样式与左栏绝对一致：保证相同文字在相同的字数处自然折行 */
                        <p className="font-sans text-sm leading-6 tracking-normal whitespace-pre-wrap break-words min-h-[3rem] text-slate-800">
                          {cleanDisplayContent(rem.content)}
                        </p>
                      )}

                      {rem.thinking && (
                        <div className="mt-2 text-xs text-slate-400 bg-white/80 p-2 rounded border border-emerald-100">
                          <Text type="secondary" italic>
                            Thinking: {rem.thinking}
                          </Text>
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="py-6 text-center text-slate-400 italic text-xs">（此轮已在优化后被剔除）</div>
                  )}
                </div>
              </React.Fragment>
            );
          })}
        </div>
      </div>

      <RequestDebugModal
        open={reqModalVisible}
        onClose={() => setReqModalVisible(false)}
        title={reqModalTitle}
        entries={reqModalEntries}
        rawReport={reqModalRawReport}
      />
    </div>
  );
};
