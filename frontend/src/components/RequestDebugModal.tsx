import React, { useEffect, useMemo, useState } from 'react';
import { Modal, Button, Tag, Tabs, Space, Alert, message, Typography } from 'antd';
import {
  CopyOutlined,
  ApiOutlined,
  BulbOutlined,
  CodeOutlined,
  FileTextOutlined,
  BarChartOutlined,
} from '@ant-design/icons';
import type { DebugRequestEntry, InspectionReport } from '../types';

const { Text } = Typography;

interface RequestDebugModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  entries: DebugRequestEntry[];
  rawReport?: Record<string, any> | null;
  rawReportLabel?: string;
}

/**
 * 从质检报告中规范化出 DebugRequestEntry 列表
 */
export const extractDebugEntriesFromEval = (
  evalReport?: InspectionReport | null,
  fallbackLabel = '质检裁判请求'
): DebugRequestEntry[] => {
  if (!evalReport) return [];
  if (evalReport.debug_requests && evalReport.debug_requests.length > 0) {
    // 将主裁判请求放到第一位，方便默认展示与复制
    const mainIdx = evalReport.debug_requests.findIndex((e) => e.step === 'main_judge');
    if (mainIdx > 0) {
      const list = [...evalReport.debug_requests];
      const [mainItem] = list.splice(mainIdx, 1);
      return [mainItem, ...list];
    }
    return evalReport.debug_requests;
  }
  if (evalReport.request_messages && evalReport.request_messages.length > 0) {
    return [
      {
        step: 'main_judge',
        label: fallbackLabel,
        messages: evalReport.request_messages,
      },
    ];
  }
  return [];
};

/**
 * 尝试将模型返回的文本美化为 JSON 字符串；若非标准 JSON 则返回原文并标记
 */
const formatResponseDisplay = (
  rawText?: string
): { formatted: string; isJson: boolean; hasContent: boolean } => {
  const trimmed = (rawText || '').trim();
  if (!trimmed) {
    return { formatted: '', isJson: false, hasContent: false };
  }
  const clean = trimmed
    .replace(/^```(?:json)?\s*/i, '')
    .replace(/\s*```$/, '')
    .trim();
  try {
    const parsed = JSON.parse(clean);
    return {
      formatted: JSON.stringify(parsed, null, 2),
      isJson: true,
      hasContent: true,
    };
  } catch {
    return {
      formatted: trimmed,
      isJson: false,
      hasContent: true,
    };
  }
};

export const RequestDebugModal: React.FC<RequestDebugModalProps> = ({
  open,
  onClose,
  title,
  entries,
  rawReport,
  rawReportLabel = '完整诊断结果 (Raw JSON)',
}) => {
  const [activeKey, setActiveKey] = useState<string>('0');

  // 剥离已分步展示的冗余 request_messages 与 debug_requests，只保留干净的诊断结果
  const sanitizedRawReport = useMemo(() => {
    if (!rawReport || typeof rawReport !== 'object') return null;
    const { request_messages: _rm, debug_requests: _dr, ...rest } = rawReport;
    if (Object.keys(rest).length === 0) return null;
    return rest;
  }, [rawReport]);

  useEffect(() => {
    if (open) {
      setActiveKey(entries.length > 0 ? '0' : sanitizedRawReport ? 'raw_report' : '0');
    }
  }, [open, entries, sanitizedRawReport]);

  const isRawTab = activeKey === 'raw_report';
  const activeIdx = isRawTab ? 0 : Math.min(Number(activeKey) || 0, Math.max(entries.length - 1, 0));
  const currentEntry = !isRawTab ? entries[activeIdx] || entries[0] : undefined;

  const responseText = currentEntry?.response_content || currentEntry?.raw_output || '';
  const responseDisplay = useMemo(() => formatResponseDisplay(responseText), [responseText]);

  const handleCopyMessages = () => {
    if (!currentEntry?.messages) return;
    navigator.clipboard.writeText(JSON.stringify(currentEntry.messages, null, 2));
    message.success('已复制 request messages 数组（可直接粘贴至 API messages 字段）');
  };

  const handleCopyFullPayload = () => {
    if (!currentEntry) return;
    const payload: Record<string, any> = {
      ...(currentEntry.model ? { model: currentEntry.model } : {}),
      messages: currentEntry.messages,
      ...(currentEntry.config || {}),
    };
    navigator.clipboard.writeText(JSON.stringify(payload, null, 2));
    message.success('已复制完整 API Request Body JSON');
  };

  const handleCopyPromptText = () => {
    if (!currentEntry?.messages?.length) return;
    const text = currentEntry.messages.map((m) => m.content).join('\n\n');
    navigator.clipboard.writeText(text);
    message.success('已复制纯文本 Prompt 内容');
  };

  const handleCopyResponse = () => {
    if (!responseText) return;
    navigator.clipboard.writeText(responseDisplay.isJson ? responseDisplay.formatted : responseText);
    message.success('已复制模型返回内容');
  };

  const handleCopyRawReport = () => {
    if (!sanitizedRawReport) return;
    navigator.clipboard.writeText(JSON.stringify(sanitizedRawReport, null, 2));
    message.success('已复制诊断结果 JSON');
  };

  const tabItems = [
    ...entries.map((entry, idx) => {
      const respStr = (entry.response_content || entry.raw_output || '').trim();
      const nonJsonWarn =
        !entry.error && respStr.length > 0 && !formatResponseDisplay(respStr).isJson;
      return {
        key: String(idx),
        label: (
          <span className="text-xs flex items-center gap-1">
            <span>{entry.label || entry.step}</span>
            {entry.error ? (
              <Tag color="red" className="m-0 text-3xs">
                异常
              </Tag>
            ) : nonJsonWarn ? (
              <Tag color="orange" className="m-0 text-3xs">
                非JSON
              </Tag>
            ) : null}
          </span>
        ),
      };
    }),
    ...(sanitizedRawReport
      ? [
          {
            key: 'raw_report',
            label: (
              <span className="text-xs flex items-center gap-1 text-purple-700 font-medium">
                <BarChartOutlined />
                <span>{rawReportLabel}</span>
              </span>
            ),
          },
        ]
      : []),
  ];

  return (
    <Modal
      title={
        <div className="flex items-center justify-between mr-6 flex-wrap gap-2">
          <span className="font-bold flex items-center gap-1.5">
            <ApiOutlined className="text-indigo-600" /> {title}
          </span>
          {isRawTab && sanitizedRawReport ? (
            <Button size="small" type="primary" icon={<CopyOutlined />} onClick={handleCopyRawReport}>
              复制诊断结果 JSON
            </Button>
          ) : currentEntry ? (
            <Space size="small" wrap>
              <Button size="small" icon={<CopyOutlined />} onClick={handleCopyPromptText}>
                复制纯文本 Prompt
              </Button>
              <Button size="small" icon={<CodeOutlined />} onClick={handleCopyFullPayload}>
                复制完整 Request Body
              </Button>
              <Button
                size="small"
                type="primary"
                icon={<CopyOutlined />}
                onClick={handleCopyMessages}
              >
                复制 Request Messages
              </Button>
            </Space>
          ) : null}
        </div>
      }
      open={open}
      onCancel={onClose}
      width={1180}
      footer={null}
      destroyOnClose
    >
      {entries.length === 0 && !sanitizedRawReport ? (
        <Alert
          type="info"
          showIcon
          className="my-4"
          message="该记录暂无请求报文与诊断数据"
          description="可能原因：1）该样本初检已免修直通 (GOLD_PASS)，未调用修复模型；2）历史早期版本尚未落库请求报文。如需重新生成完整诊断或修复记录，可执行一次【按此标准重新质检】或【重新修复数据】。"
        />
      ) : (
        <div className="space-y-3">
          {tabItems.length > 1 && (
            <Tabs size="small" activeKey={activeKey} onChange={setActiveKey} items={tabItems} />
          )}

          {isRawTab && sanitizedRawReport ? (
            <div className="space-y-2">
              <Alert
                type="info"
                showIcon
                className="text-xs"
                message="已自动剔除冗长的 request_messages 与 debug_requests，以下为干净的质检/拓扑结构化结果（请求与返回细节请切换上方各步骤标签多栏查看）"
              />
              <pre className="bg-slate-900 text-slate-100 p-3.5 rounded-lg text-xs overflow-auto max-h-[540px] font-mono leading-relaxed whitespace-pre-wrap break-words m-0">
                {JSON.stringify(sanitizedRawReport, null, 2)}
              </pre>
            </div>
          ) : currentEntry ? (
            <div className="space-y-3">
              {/* 顶部元信息条 */}
              <div className="flex items-center justify-between flex-wrap gap-2 bg-slate-50 border border-slate-200 rounded p-2.5 text-xs">
                <Space size="small" wrap>
                  <Tag color="geekblue">{currentEntry.label || currentEntry.step}</Tag>
                  {currentEntry.model && (
                    <Tag color="purple" className="font-mono">
                      model: {currentEntry.model}
                    </Tag>
                  )}
                  {currentEntry.config && (
                    <>
                      <Tag color={currentEntry.config.thinking_enabled ? 'cyan' : 'default'}>
                        thinking:{' '}
                        {currentEntry.config.thinking_enabled
                          ? `ON (${currentEntry.config.thinking_level || 'AUTO'})`
                          : 'OFF'}
                      </Tag>
                      <Tag>temp: {currentEntry.config.temperature ?? '-'}</Tag>
                      <Tag>top_p: {currentEntry.config.top_p ?? '-'}</Tag>
                      <Tag>max_tokens: {currentEntry.config.max_tokens ?? '-'}</Tag>
                      {currentEntry.config.vertex_thinking_mode && (
                        <Tag color="blue">{currentEntry.config.vertex_thinking_mode}</Tag>
                      )}
                    </>
                  )}
                </Space>
                <Space size="middle" className="text-3xs text-slate-500">
                  {typeof currentEntry.latency_ms === 'number' && (
                    <span>耗时: {(currentEntry.latency_ms / 1000).toFixed(2)}s</span>
                  )}
                  {currentEntry.usage && (
                    <span>
                      Tokens: in={currentEntry.usage.prompt_tokens ?? 0} · think=
                      {currentEntry.usage.thinking_tokens ?? 0} · out=
                      {currentEntry.usage.answer_tokens ?? currentEntry.usage.completion_tokens ?? 0}
                    </span>
                  )}
                </Space>
              </div>

              {currentEntry.error && (
                <Alert
                  type="error"
                  showIcon
                  message={`调用/解析异常: ${currentEntry.error}`}
                  className="text-xs"
                />
              )}

              {/* 双栏并排：左侧请求报文，右侧思考链与返回结果 */}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                {/* 左栏：Request Messages */}
                <div className="flex flex-col border border-slate-200 rounded-lg p-2.5 bg-slate-50/50">
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs font-semibold text-slate-700 flex items-center gap-1.5">
                      <CodeOutlined className="text-indigo-600" />
                      请求报文 (Request Messages)
                    </span>
                    <Text type="secondary" className="text-3xs">
                      {currentEntry.messages?.length || 0} 条 ·{' '}
                      {(currentEntry.messages || []).reduce(
                        (acc, m) => acc + (m.content?.length || 0),
                        0
                      )}{' '}
                      字符
                    </Text>
                  </div>
                  <pre className="bg-slate-900 text-slate-100 p-3 rounded text-xs overflow-auto max-h-[500px] min-h-[260px] font-mono leading-relaxed whitespace-pre-wrap break-words m-0 flex-1">
                    {JSON.stringify(currentEntry.messages, null, 2)}
                  </pre>
                </div>

                {/* 右栏：思考链 + 模型返回结果 */}
                <div className="flex flex-col gap-2.5 border border-slate-200 rounded-lg p-2.5 bg-slate-50/50">
                  {currentEntry.thinking ? (
                    <div className="bg-amber-50/80 border border-amber-200 rounded p-2.5">
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-xs font-semibold text-amber-800 flex items-center gap-1">
                          <BulbOutlined /> 模型思考链 (Thinking · {currentEntry.thinking.length} 字)
                        </span>
                        <Button
                          size="small"
                          type="link"
                          className="p-0 h-4 text-3xs"
                          onClick={() => {
                            navigator.clipboard.writeText(currentEntry.thinking || '');
                            message.success('已复制模型思考内容');
                          }}
                        >
                          复制 Thinking
                        </Button>
                      </div>
                      <pre className="text-xs text-amber-950 whitespace-pre-wrap break-words max-h-48 overflow-auto font-mono leading-relaxed m-0">
                        {currentEntry.thinking}
                      </pre>
                    </div>
                  ) : null}

                  <div className="flex flex-col flex-1">
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-xs font-semibold text-slate-700 flex items-center gap-1.5">
                        <FileTextOutlined className="text-emerald-600" />
                        模型返回结果 (Response)
                        {responseDisplay.hasContent && (
                          <Tag
                            color={responseDisplay.isJson ? 'green' : 'orange'}
                            className="m-0 text-3xs"
                          >
                            {responseDisplay.isJson ? 'JSON' : '非JSON原文'}
                          </Tag>
                        )}
                      </span>
                      {responseDisplay.hasContent && (
                        <Button
                          size="small"
                          type="link"
                          icon={<CopyOutlined />}
                          className="p-0 h-4 text-3xs"
                          onClick={handleCopyResponse}
                        >
                          复制返回内容
                        </Button>
                      )}
                    </div>

                    {responseDisplay.hasContent ? (
                      <pre
                        className={`p-3 rounded text-xs overflow-auto font-mono leading-relaxed whitespace-pre-wrap break-words m-0 flex-1 ${
                          currentEntry.thinking
                            ? 'max-h-[300px] min-h-[180px]'
                            : 'max-h-[500px] min-h-[260px]'
                        } ${
                          responseDisplay.isJson
                            ? 'bg-slate-900 text-emerald-200'
                            : 'bg-rose-950 text-rose-100 border border-rose-400'
                        }`}
                      >
                        {responseDisplay.formatted}
                      </pre>
                    ) : (
                      <div className="flex-1 min-h-[220px] flex items-center justify-center bg-white border border-dashed border-slate-300 rounded p-4 text-center text-xs text-slate-400">
                        {currentEntry.error
                          ? '本次请求调用异常或未返回正文内容。'
                          : currentEntry.thinking
                          ? '模型仅输出了思考链但正文内容为空（常见于思考过程耗尽 max_tokens 输出额度导致正文被截断），未产出有效 JSON 结果。'
                          : '本次请求未返回正文内容，或该记录为早期版本生成、尚未落库模型原始返回。'}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          ) : null}
        </div>
      )}
    </Modal>
  );
};
