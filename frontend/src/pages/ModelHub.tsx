import React, { useEffect, useState } from 'react';
import {
  Card,
  Button,
  Tag,
  Space,
  Modal,
  Form,
  Input,
  Select,
  Switch,
  InputNumber,
  Radio,
  Drawer,
  message,
  Popconfirm,
  Typography,
  Spin,
  Alert,
} from 'antd';
import {
  PlusOutlined,
  ThunderboltOutlined,
  CopyOutlined,
  EditOutlined,
  DeleteOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
} from '@ant-design/icons';
import { modelsApi } from '../api';
import type { RegisteredModel, ModelCreateRequest, ModelUpdateRequest, ModelTestResponse } from '../types';

const { Text, Paragraph } = Typography;

const DEFAULT_VERTEX_PROJECT = 'your-gcp-project-id';
const DEFAULT_VERTEX_LOCATION = 'global';

const parseVertexUrl = (apiUrl?: string) => {
  if (!apiUrl) {
    return { project: DEFAULT_VERTEX_PROJECT, location: DEFAULT_VERTEX_LOCATION };
  }
  const clean = apiUrl.replace(/^vertexai:\/\//i, '').replace(/^\/+|\/+$/g, '');
  const parts = clean.split('/').filter(Boolean);
  if (parts.length >= 2) {
    return { project: parts[0], location: parts[1] };
  }
  if (parts.length === 1) {
    return { project: parts[0], location: DEFAULT_VERTEX_LOCATION };
  }
  return { project: DEFAULT_VERTEX_PROJECT, location: DEFAULT_VERTEX_LOCATION };
};

export const ModelHub: React.FC = () => {
  const [models, setModels] = useState<RegisteredModel[]>([]);
  const [loading, setLoading] = useState(false);
  const [filterProvider, setFilterProvider] = useState<string>('ALL');

  // 模态框状态
  const [modalVisible, setModalVisible] = useState(false);
  const [modalSubmitting, setModalSubmitting] = useState(false);
  const [modalMode, setModalMode] = useState<'create' | 'edit' | 'clone'>('create');
  const [currentModel, setCurrentModel] = useState<RegisteredModel | null>(null);
  const [form] = Form.useForm();
  const watchedProvider = Form.useWatch('provider_type', form);

  // 连通性测试抽屉
  const [testDrawerVisible, setTestDrawerVisible] = useState(false);
  const [testingModel, setTestingModel] = useState<RegisteredModel | null>(null);
  const [testingLoading, setTestingLoading] = useState(false);
  const [testResult, setTestResult] = useState<ModelTestResponse | null>(null);

  const loadModels = async () => {
    setLoading(true);
    try {
      const data = await modelsApi.list();
      setModels(data);
    } catch (e: any) {
      message.error(`加载模型列表失败: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadModels();
  }, []);

  const handleCreateOpen = () => {
    setModalMode('create');
    setCurrentModel(null);
    form.resetFields();
    form.setFieldsValue({
      provider_type: 'openai',
      vertex_project: DEFAULT_VERTEX_PROJECT,
      vertex_location: DEFAULT_VERTEX_LOCATION,
      config: {
        temperature: 0.3,
        top_p: 0.9,
        max_tokens: 4096,
        repetition_penalty: 1.05,
        thinking_enabled: false,
        thinking_level: 'NONE',
      },
    });
    setModalVisible(true);
  };

  const handleEditOpen = (model: RegisteredModel) => {
    setModalMode('edit');
    setCurrentModel(model);
    form.resetFields();
    const { project, location } = parseVertexUrl(model.api_url);
    form.setFieldsValue({
      id: model.id,
      name: model.name,
      provider_type: model.provider_type,
      api_url: model.api_url,
      api_key: model.api_key,
      model_name: model.model_name,
      vertex_project: project,
      vertex_location: location,
      config: model.config || {
        temperature: 0.3,
        top_p: 0.9,
        max_tokens: 4096,
        repetition_penalty: 1.05,
        thinking_enabled: false,
        thinking_level: 'NONE',
      },
    });
    setModalVisible(true);
  };

  const handleCloneOpen = (model: RegisteredModel) => {
    setModalMode('clone');
    setCurrentModel(model);
    form.resetFields();
    const { project, location } = parseVertexUrl(model.api_url);
    form.setFieldsValue({
      id: `${model.id}-clone`,
      name: `${model.name} (副本)`,
      provider_type: model.provider_type,
      api_url: model.api_url,
      api_key: model.api_key,
      model_name: model.model_name,
      vertex_project: project,
      vertex_location: location,
      config: {
        ...(model.config || {}),
        thinking_enabled: true,
        thinking_level: 'HIGH',
      },
    });
    setModalVisible(true);
  };

  const handleProviderChange = (val: string) => {
    if (val === 'vertex') {
      const curProj = form.getFieldValue('vertex_project');
      const curLoc = form.getFieldValue('vertex_location');
      const curModel = form.getFieldValue('model_name');
      form.setFieldsValue({
        vertex_project: curProj || DEFAULT_VERTEX_PROJECT,
        vertex_location: curLoc || DEFAULT_VERTEX_LOCATION,
        model_name: curModel || 'gemini-3.8-flash',
      });
    } else if (val === 'harmony') {
      const curUrl = form.getFieldValue('api_url');
      const curModel = form.getFieldValue('model_name');
      form.setFieldsValue({
        api_url: curUrl && !curUrl.startsWith('vertexai://') ? curUrl : 'http://127.0.0.1:8000/v1/chat/completions',
        model_name: curModel || 'demo-target-model',
      });
    }
  };

  const handleModalSubmit = async () => {
    try {
      const values = await form.validateFields();
      setModalSubmitting(true);
      if (values.provider_type === 'vertex') {
        const proj = (values.vertex_project || DEFAULT_VERTEX_PROJECT).trim();
        const loc = (values.vertex_location || DEFAULT_VERTEX_LOCATION).trim();
        values.api_url = `vertexai://${proj}/${loc}`;
      }
      delete values.vertex_project;
      delete values.vertex_location;

      if (modalMode === 'create' || modalMode === 'clone') {
        const res = await modelsApi.create(values as ModelCreateRequest);
        if (res.vertex_thinking_mode) {
          message.success(
            `模型注册成功（自适应探测思考协议: ${formatVertexThinkingMode(res.vertex_thinking_mode)}）`
          );
        } else {
          message.success('模型注册成功');
        }
      } else if (modalMode === 'edit' && currentModel) {
        // 若编辑时未填入新 key，则不传递 api_key，防止覆盖旧密钥
        if (!values.api_key) {
          delete values.api_key;
        }
        const res = await modelsApi.update(currentModel.id, values as ModelUpdateRequest);
        if (res.vertex_thinking_mode) {
          message.success(
            `模型配置更新成功（思考协议: ${formatVertexThinkingMode(res.vertex_thinking_mode)}）`
          );
        } else {
          message.success('模型配置更新成功');
        }
      }
      setModalVisible(false);
      loadModels();
    } catch (e: any) {
      if (e?.errorFields) return;
      message.error(`提交失败: ${e.message}`);
    } finally {
      setModalSubmitting(false);
    }
  };

  const handleDelete = async (modelId: string) => {
    try {
      await modelsApi.delete(modelId);
      message.success('模型删除成功');
      loadModels();
    } catch (e: any) {
      message.error(`删除失败: ${e.message}`);
    }
  };

  const handleTestConnection = async (model: RegisteredModel) => {
    setTestingModel(model);
    setTestResult(null);
    setTestDrawerVisible(true);
    setTestingLoading(true);
    try {
      const res = await modelsApi.test(model.id);
      setTestResult(res);
      if (res.vertex_thinking_mode) {
        loadModels();
      }
    } catch (e: any) {
      setTestResult({ status: 'FAILED', error: e.message });
    } finally {
      setTestingLoading(false);
    }
  };

  const filteredModels = models.filter((m) =>
    filterProvider === 'ALL' ? true : m.provider_type === filterProvider.toLowerCase()
  );

  const formatVertexThinkingMode = (mode?: string | null) => {
    switch (mode) {
      case 'gemini_native':
        return 'Gemini 原生思考';
      case 'maas_reasoning_effort':
        return 'MaaS (reasoning_effort)';
      case 'maas_chat_template':
        return 'MaaS (chat_template)';
      case 'maas_none':
        return 'MaaS (未支持思考)';
      case 'none':
        return '未支持思考';
      case 'auto':
      default:
        return '待探测';
    }
  };

  const getVertexThinkingModeColor = (mode?: string | null) => {
    switch (mode) {
      case 'gemini_native':
        return 'purple';
      case 'maas_reasoning_effort':
      case 'maas_chat_template':
        return 'geekblue';
      case 'maas_none':
      case 'none':
        return 'default';
      default:
        return 'orange';
    }
  };

  const getProviderColor = (type: string) => {
    switch (type) {
      case 'openai':
        return 'green';
      case 'harmony':
        return 'blue';
      case 'vertex':
        return 'purple';
      case 'mock':
        return 'default';
      default:
        return 'cyan';
    }
  };

  return (
    <div className="space-y-5">
      {/* 顶部操作区 */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 bg-white p-4 rounded-lg border border-slate-200 shadow-2xs">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-slate-600">Provider 筛选:</span>
          <Radio.Group
            value={filterProvider}
            onChange={(e) => setFilterProvider(e.target.value)}
            size="small"
            buttonStyle="solid"
          >
            <Radio.Button value="ALL">全部 ({models.length})</Radio.Button>
            <Radio.Button value="OPENAI">OpenAI</Radio.Button>
            <Radio.Button value="HARMONY">Harmony</Radio.Button>
            <Radio.Button value="VERTEX">Vertex AI</Radio.Button>
            <Radio.Button value="MOCK">Mock</Radio.Button>
          </Radio.Group>
        </div>

        <Button type="primary" icon={<PlusOutlined />} onClick={handleCreateOpen}>
          注册新模型
        </Button>
      </div>

      {/* 模型卡片矩阵 */}
      <Spin spinning={loading}>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filteredModels.map((m) => (
            <Card
              key={m.id}
              className="hover:shadow-md transition-shadow border-slate-200"
              styles={{ body: { padding: '18px' } }}
            >
              <div className="flex items-start justify-between">
                <div>
                  <h3 className="font-semibold text-slate-800 text-base flex items-center gap-2">
                    {m.name}
                    <Tag color={getProviderColor(m.provider_type)} className="text-3xs uppercase">
                      {m.provider_type}
                    </Tag>
                  </h3>
                  <p className="text-xs text-slate-400 font-mono mt-0.5">{m.id}</p>
                </div>
              </div>

              {/* API 终端信息 */}
              <div className="mt-3 py-2 px-2.5 bg-slate-50 rounded border border-slate-100 text-xs text-slate-500 font-mono truncate">
                {m.provider_type === 'vertex' ? (
                  <>
                    <span className="text-purple-600 font-semibold">[ADC免密] </span>
                    <span>{m.api_url || `vertexai://${DEFAULT_VERTEX_PROJECT}/${DEFAULT_VERTEX_LOCATION}`}</span>
                  </>
                ) : (
                  <>
                    <span className="text-slate-400">Endpoint: </span>
                    {m.api_url ? m.api_url.replace(/^(https?:\/\/[^/]+).*/, '$1/...') : '内置 / 离线环境'}
                  </>
                )}
              </div>

              {/* 思考与超参胶囊 */}
              <div className="mt-3 flex items-center gap-2 flex-wrap text-xs">
                {m.model_name && (
                  <Tag color="geekblue" className="font-mono m-0">
                    {m.model_name}
                  </Tag>
                )}
                {m.config?.thinking_enabled ? (
                  <Tag color="cyan" className="m-0">思考: 开启 ({m.config.thinking_level})</Tag>
                ) : (
                  <Tag color="default" className="m-0">思考: 关闭</Tag>
                )}
                {m.provider_type === 'vertex' && (
                  <Tag
                    color={getVertexThinkingModeColor(m.config?.vertex_thinking_mode)}
                    className="m-0"
                  >
                    协议: {formatVertexThinkingMode(m.config?.vertex_thinking_mode)}
                  </Tag>
                )}
                <span className="text-slate-400">Temp: {m.config?.temperature ?? 0.3}</span>
                <span className="text-slate-400">MaxTokens: {m.config?.max_tokens ?? 4096}</span>
              </div>

              {/* 操作按钮组 */}
              <div className="mt-4 pt-3 border-t border-slate-100 flex items-center justify-between">
                <Button
                  size="small"
                  type="primary"
                  ghost
                  icon={<ThunderboltOutlined />}
                  onClick={() => handleTestConnection(m)}
                >
                  连通性测试
                </Button>
                <Space size="small">
                  <Button
                    size="small"
                    icon={<CopyOutlined />}
                    title="克隆此模型并独立配置思考参数"
                    onClick={() => handleCloneOpen(m)}
                  />
                  <Button
                    size="small"
                    icon={<EditOutlined />}
                    onClick={() => handleEditOpen(m)}
                  />
                  <Popconfirm
                    title="确定要注销此模型吗？"
                    okText="确定"
                    cancelText="取消"
                    onConfirm={() => handleDelete(m.id)}
                  >
                    <Button size="small" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                </Space>
              </div>
            </Card>
          ))}
        </div>
      </Spin>

      {/* 注册/编辑/克隆模态框 */}
      <Modal
        title={
          modalMode === 'create'
            ? '注册新模型实例'
            : modalMode === 'clone'
            ? '克隆并衍生模型实例 (独立思考配置)'
            : '编辑模型配置'
        }
        open={modalVisible}
        onCancel={() => !modalSubmitting && setModalVisible(false)}
        onOk={handleModalSubmit}
        confirmLoading={modalSubmitting}
        maskClosable={!modalSubmitting}
        width={650}
        okText={modalSubmitting ? '正在保存并探测...' : '保存提交'}
        cancelText="取消"
      >
        <Form form={form} layout="vertical" className="mt-3">
          <div className="grid grid-cols-2 gap-3">
            <Form.Item
              name="id"
              label="唯一标识 (Model ID)"
              rules={[{ required: true, message: '请输入模型唯一ID' }]}
            >
              <Input placeholder="如 vertex-gemini-flash" disabled={modalMode === 'edit'} />
            </Form.Item>
            <Form.Item
              name="name"
              label="展示名称 (Display Name)"
              rules={[{ required: true, message: '请输入显示名称' }]}
            >
              <Input placeholder="如 Vertex Gemini 3.8 Flash" />
            </Form.Item>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <Form.Item name="provider_type" label="适配器类型 (Provider)" rules={[{ required: true }]}>
              <Select onChange={handleProviderChange}>
                <Select.Option value="openai">标准 OpenAI / vLLM / SGLang</Select.Option>
                <Select.Option value="harmony">Harmony 双通道 (&lt;|channel|&gt;final)</Select.Option>
                <Select.Option value="vertex">Google Vertex AI (ADC 免密)</Select.Option>
                <Select.Option value="mock">离线自测 Mock</Select.Option>
              </Select>
            </Form.Item>
            <Form.Item
              name="model_name"
              label="底座模型标识 (Backend Model Name)"
              extra={
                watchedProvider === 'vertex' ? (
                  <span className="text-xs">
                    快捷填入:{' '}
                    <a
                      onClick={() => form.setFieldValue('model_name', 'gemini-3.8-flash')}
                      className="text-blue-600 hover:underline mr-2 cursor-pointer"
                    >
                      gemini-3.8-flash
                    </a>
                    <a
                      onClick={() =>
                        form.setFieldValue('model_name', 'deepseek-ai/deepseek-v4.1-flash-maas')
                      }
                      className="text-blue-600 hover:underline cursor-pointer"
                    >
                      deepseek-v4.1-flash-maas
                    </a>
                  </span>
                ) : undefined
              }
            >
              <Input
                placeholder={
                  watchedProvider === 'vertex'
                    ? '如 gemini-3.8-flash 或 deepseek-ai/deepseek-v4.1-flash-maas'
                    : '真实推理后端对应的模型名'
                }
              />
            </Form.Item>
          </div>

          {watchedProvider === 'vertex' ? (
            <>
              <Alert
                type="info"
                showIcon
                className="mb-3 text-xs"
                message="Google Cloud Vertex AI 采用本机 ADC (Application Default Credentials) 免密鉴权"
                description="系统会自动读取本机 gcloud auth application-default login 生成的凭证 (%APPDATA%\gcloud\application_default_credentials.json)，并自动感知 Windows 系统代理，无需填写 API Key。"
              />
              <div className="grid grid-cols-2 gap-3">
                <Form.Item
                  name="vertex_project"
                  label="GCP 项目 ID (Project ID)"
                  rules={[{ required: true, message: '请输入 GCP Project ID' }]}
                >
                  <Input placeholder={DEFAULT_VERTEX_PROJECT} />
                </Form.Item>
                <Form.Item
                  name="vertex_location"
                  label="云端区域 (Location)"
                  rules={[{ required: true, message: '请输入 Location' }]}
                >
                  <Input placeholder="global 或 us-central1" />
                </Form.Item>
              </div>
              <Form.Item
                name="api_key"
                label="自定义服务账号 JSON 路径 / API Key (可选，默认留空即走本机 ADC 免密)"
              >
                <Input.Password placeholder="留空即自动使用本机 gcloud ADC 免密鉴权；或填入 service_account.json 路径" />
              </Form.Item>
            </>
          ) : (
            <>
              <Form.Item name="api_url" label="API Endpoint URL">
                <Input placeholder="https://api.openai.com/v1/chat/completions 或 http://localhost:8000/v1/chat/completions" />
              </Form.Item>

              <Form.Item
                name="api_key"
                label={modalMode === 'edit' ? 'API 访问密钥 (Key，留空则保持原密钥)' : 'API 访问密钥 (Key)'}
                extra={modalMode === 'clone' ? '提示：因密钥脱敏，新克隆的模型请填入访问凭证' : undefined}
              >
                <Input.Password placeholder={modalMode === 'edit' ? '留空保持原有密钥不变' : 'sk-... 或 EMPTY'} />
              </Form.Item>
            </>
          )}

          {/* 统一模型超参与思考设置 */}
          <div className="p-3 bg-slate-50 rounded border border-slate-200 mt-2">
            <Text strong className="text-xs uppercase text-slate-600 block mb-3">
              统一推理超参与思考开关映射
            </Text>
            <div className="grid grid-cols-3 gap-3">
              <Form.Item name={['config', 'temperature']} label="Temperature" className="mb-2">
                <InputNumber min={0} max={2} step={0.1} className="w-full" />
              </Form.Item>
              <Form.Item name={['config', 'top_p']} label="Top-P" className="mb-2">
                <InputNumber min={0} max={1} step={0.05} className="w-full" />
              </Form.Item>
              <Form.Item name={['config', 'max_tokens']} label="Max Tokens" className="mb-2">
                <InputNumber min={128} max={32768} step={256} className="w-full" />
              </Form.Item>
            </div>

            <div className="grid grid-cols-2 gap-3 mt-2">
              <Form.Item
                name={['config', 'thinking_enabled']}
                label="思考开关 (Thinking Enabled)"
                valuePropName="checked"
                className="mb-0"
              >
                <Switch />
              </Form.Item>
              <Form.Item
                name={['config', 'thinking_level']}
                label="思考等级 (Thinking Level)"
                className="mb-0"
              >
                <Select>
                  <Select.Option value="NONE">NONE (不思考)</Select.Option>
                  <Select.Option value="LOW">LOW (轻度思考)</Select.Option>
                  <Select.Option value="MEDIUM">MEDIUM (中度思考)</Select.Option>
                  <Select.Option value="HIGH">HIGH (深度思考)</Select.Option>
                </Select>
              </Form.Item>
            </div>
          </div>
        </Form>
      </Modal>

      {/* 连通性测试抽屉 */}
      <Drawer
        title={`模型连通性测试: ${testingModel?.name || ''}`}
        placement="right"
        width={500}
        open={testDrawerVisible}
        onClose={() => setTestDrawerVisible(false)}
      >
        {testingLoading ? (
          <div className="flex flex-col items-center justify-center h-64 gap-3">
            <Spin size="large" />
            <Text type="secondary">正在向模型后端发送握手试探...</Text>
          </div>
        ) : testResult ? (
          <div className="space-y-4">
            <Alert
              message={testResult.status === 'SUCCESS' ? '连通性测试成功' : '连通性握手失败'}
              description={
                testResult.status === 'SUCCESS'
                  ? `端到端网络耗时: ${testResult.latency_ms} ms`
                  : testResult.error
              }
              type={testResult.status === 'SUCCESS' ? 'success' : 'error'}
              showIcon
              icon={
                testResult.status === 'SUCCESS' ? (
                  <CheckCircleOutlined />
                ) : (
                  <CloseCircleOutlined />
                )
              }
            />

            {testResult.vertex_thinking_mode && (
              <div className="flex items-center gap-2 text-xs">
                <span className="text-slate-500">已自适应探测并持久化思考协议:</span>
                <Tag color={getVertexThinkingModeColor(testResult.vertex_thinking_mode)} className="m-0">
                  {formatVertexThinkingMode(testResult.vertex_thinking_mode)} ({testResult.vertex_thinking_mode})
                </Tag>
              </div>
            )}

            {testResult.reply && (
              <div className="p-3 bg-slate-50 border rounded">
                <Text strong className="text-xs text-slate-500 block mb-1">
                  纯净正文回复 (Content):
                </Text>
                <Paragraph className="text-sm font-sans mb-0 whitespace-pre-wrap">
                  {testResult.reply}
                </Paragraph>
              </div>
            )}

            {testResult.thinking && (
              <div className="p-3 bg-cyan-50/60 border border-cyan-200 rounded">
                <Text strong className="text-xs text-cyan-700 block mb-1">
                  剥离的思考链推演 (Thinking / Reasoning):
                </Text>
                <p className="text-xs text-slate-600 font-mono whitespace-pre-wrap mb-0">
                  {testResult.thinking}
                </p>
              </div>
            )}
          </div>
        ) : null}
      </Drawer>
    </div>
  );
};
