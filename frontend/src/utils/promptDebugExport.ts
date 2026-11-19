import { message } from 'antd';
import type { ChatMessage } from '../types';

export const copyTextSafely = async (text: string, successMsg = '已复制到剪贴板') => {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      message.success(successMsg);
      return;
    }
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    if (ok) {
      message.success(successMsg);
    } else {
      message.error('复制失败，请手动选中文本复制');
    }
  } catch {
    message.error('浏览器剪贴板权限受限，复制失败，请使用导出功能或手动复制');
  }
};

export interface PromptDebugExportBundle {
  title: string;
  prompt_key: string;
  rule_id?: string;
  scenario_id?: string;
  assembled_messages: ChatMessage[];
  assembled_prompt: string;
  sample_dialogue?: ChatMessage[];
  recommended_config?: Record<string, any>;
  test_result?: {
    status: string;
    latency_ms: number;
    thinking?: string;
    parsed_data?: any;
    raw_content?: string;
    error?: string;
  } | null;
}

const triggerTextDownload = (filename: string, content: string, mimeType: string) => {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
};

export const exportPromptDebugJson = (bundle: PromptDebugExportBundle) => {
  const payload = {
    exported_at: new Date().toISOString(),
    ...bundle,
  };
  const safeKey = (bundle.prompt_key || 'prompt').replace(/[^a-zA-Z0-9_-]/g, '_');
  const safeRule = bundle.rule_id ? `_${bundle.rule_id.replace(/[^a-zA-Z0-9_-]/g, '_')}` : '';
  triggerTextDownload(
    `prompt_debug_${safeKey}${safeRule}.json`,
    JSON.stringify(payload, null, 2),
    'application/json;charset=utf-8'
  );
};

export const exportPromptDebugMarkdown = (bundle: PromptDebugExportBundle) => {
  const lines: string[] = [
    `# Prompt 调试与组装报文导出 — ${bundle.title}`,
    '',
    `- 提示词标识: \`${bundle.prompt_key}\``,
    ...(bundle.rule_id ? [`- 关联规则: \`${bundle.rule_id}\``] : []),
    ...(bundle.scenario_id ? [`- 关联剧本: \`${bundle.scenario_id}\``] : []),
    `- 导出时间: ${new Date().toISOString()}`,
    '',
  ];

  if (bundle.recommended_config) {
    lines.push('## 推荐推理参数', '```json', JSON.stringify(bundle.recommended_config, null, 2), '```', '');
  }

  lines.push('## 组装后 Request Messages ([system / user])', '');
  (bundle.assembled_messages || []).forEach((m, idx) => {
    lines.push(`### Message #${idx + 1} (${m.role.toUpperCase()})`, '```markdown', m.content || '', '```', '');
  });

  if (bundle.test_result) {
    lines.push(
      '## 在线实测结果',
      `- 状态: ${bundle.test_result.status} (${bundle.test_result.latency_ms} ms)`,
      ''
    );
    if (bundle.test_result.thinking) {
      lines.push('### Thinking', '```text', bundle.test_result.thinking, '```', '');
    }
    if (bundle.test_result.parsed_data) {
      lines.push(
        '### Parsed JSON',
        '```json',
        JSON.stringify(bundle.test_result.parsed_data, null, 2),
        '```',
        ''
      );
    }
    if (bundle.test_result.raw_content) {
      lines.push('### Raw Response', '```text', bundle.test_result.raw_content, '```', '');
    }
  }

  const safeKey = (bundle.prompt_key || 'prompt').replace(/[^a-zA-Z0-9_-]/g, '_');
  const safeRule = bundle.rule_id ? `_${bundle.rule_id.replace(/[^a-zA-Z0-9_-]/g, '_')}` : '';
  triggerTextDownload(
    `prompt_debug_${safeKey}${safeRule}.md`,
    lines.join('\n'),
    'text/markdown;charset=utf-8'
  );
};
