import React, { useState, useEffect } from 'react';
import { Modal, message, Alert } from 'antd';
import { DiffEditor } from '@monaco-editor/react';
import type { ChatMessage } from '../types';

interface MonacoDiffModalProps {
  visible: boolean;
  onClose: () => void;
  originalMessages: ChatMessage[];
  remediatedMessages?: ChatMessage[] | null;
  onSave?: (messages: ChatMessage[]) => void;
}

export const MonacoDiffModal: React.FC<MonacoDiffModalProps> = ({
  visible,
  onClose,
  originalMessages,
  remediatedMessages,
  onSave,
}) => {
  // 若左侧原实战会话缺失首轮 system，而右侧包含 system，为左侧智能补齐 system 使 Monaco 双栏严格对齐，消除斜线空行位移
  const normalizedOriginalMessages = React.useMemo(() => {
    if (!originalMessages || originalMessages.length === 0) return [];
    const hasOriginalSystem = originalMessages[0]?.role === 'system';
    const remSystem = (remediatedMessages || []).find((m) => m.role === 'system');

    if (!hasOriginalSystem && remSystem) {
      return [remSystem, ...originalMessages];
    }
    return originalMessages;
  }, [originalMessages, remediatedMessages]);

  const originalJson = JSON.stringify(normalizedOriginalMessages, null, 2);
  const [modifiedJson, setModifiedJson] = useState<string>(
    JSON.stringify(remediatedMessages || normalizedOriginalMessages, null, 2)
  );

  useEffect(() => {
    if (visible) {
      setModifiedJson(JSON.stringify(remediatedMessages || normalizedOriginalMessages, null, 2));
    }
  }, [visible, remediatedMessages, normalizedOriginalMessages]);

  const validateSftFormat = (msgs: any[]): { valid: boolean; error?: string } => {
    if (!Array.isArray(msgs) || msgs.length === 0) {
      return { valid: false, error: '内容必须为非空 JSON 消息数组' };
    }
    for (let i = 0; i < msgs.length; i++) {
      const item = msgs[i];
      if (!item || typeof item !== 'object' || !item.role || typeof item.content !== 'string') {
        return { valid: false, error: `第 ${i + 1} 轮消息格式不合法，缺少 role 或 content 字段` };
      }
      if (!['system', 'user', 'assistant'].includes(item.role)) {
        return { valid: false, error: `第 ${i + 1} 轮角色错误: '${item.role}'，必须为 system/user/assistant` };
      }
    }
    if (msgs[0].role !== 'system') {
      return { valid: false, error: '首轮消息必须为 system 系统指令' };
    }
    for (let i = 1; i < msgs.length; i++) {
      if (msgs[i].role === msgs[i - 1].role && msgs[i].role !== 'system') {
        return { valid: false, error: `第 ${i} 轮与第 ${i + 1} 轮角色连续重复 (${msgs[i].role})，违反交替规则` };
      }
    }
    if (msgs[msgs.length - 1].role !== 'assistant') {
      return { valid: false, error: '末轮消息必须由 assistant (机器人) 收口' };
    }
    return { valid: true };
  };

  const handleSave = () => {
    try {
      const parsed = JSON.parse(modifiedJson);
      const validation = validateSftFormat(parsed);
      if (!validation.valid) {
        message.error(`SFT 格式校验未通过: ${validation.error}`);
        return;
      }
      if (onSave) {
        onSave(parsed);
        message.success('已同步 Monaco 修改至复核池');
      }
      onClose();
    } catch (e: any) {
      message.error(`JSON 语法解析错误: ${e.message}`);
    }
  };

  return (
    <Modal
      title="Monaco 源码级双栏 Diff 编辑器"
      open={visible}
      onCancel={onClose}
      onOk={handleSave}
      okText="应用并保存修改"
      cancelText="取消"
      width={1100}
      styles={{ body: { height: '620px', padding: 0 } }}
    >
      <div className="h-full flex flex-col">
        <Alert
          message="提示：左栏为原缺陷对话，右栏为可编辑的代码。保存时将强制执行严格的 SFT 角色交替与收尾校验。"
          type="info"
          showIcon
          className="rounded-none border-x-0 border-t-0 text-xs"
        />
        <div className="flex-1">
          <DiffEditor
            height="100%"
            language="json"
            original={originalJson}
            modified={modifiedJson}
            onMount={(editor) => {
              const originalEditor = editor.getOriginalEditor();
              const modifiedEditor = editor.getModifiedEditor();

              const applyWrap = () => {
                const wrapOptions: any = {
                  wordWrap: 'on',
                  wordWrapOverride1: 'on',
                  wordWrapOverride2: 'on',
                  wrappingStrategy: 'advanced',
                  wrappingIndent: 'same',
                  scrollBeyondLastLine: false,
                  lineNumbers: 'on',
                  renderWhitespace: 'selection',
                  automaticLayout: true,
                };

                originalEditor.updateOptions(wrapOptions);
                modifiedEditor.updateOptions(wrapOptions);

                editor.updateOptions({
                  diffWordWrap: 'on',
                  wordWrap: 'on',
                  automaticLayout: true,
                });

                editor.layout();
                originalEditor.layout();
                modifiedEditor.layout();
              };

              applyWrap();
              // 等待 AntD Modal 展开动画彻底完成后重新触发 layout，粉碎单边计算失真
              setTimeout(applyWrap, 50);
              setTimeout(applyWrap, 250);

              modifiedEditor.onDidChangeModelContent(() => {
                setModifiedJson(modifiedEditor.getValue());
              });
            }}
            options={{
              readOnly: false,
              originalEditable: false,
              minimap: { enabled: false },
              fontSize: 13,
              scrollBeyondLastLine: false,
              wordWrap: 'on',
              diffWordWrap: 'on',
              wordWrapOverride1: 'on',
              wordWrapOverride2: 'on',
              wrappingStrategy: 'advanced',
              wrappingIndent: 'same',
              renderSideBySide: true,
              ignoreTrimWhitespace: false,
              automaticLayout: true,
            }}
          />
        </div>
      </div>
    </Modal>
  );
};
