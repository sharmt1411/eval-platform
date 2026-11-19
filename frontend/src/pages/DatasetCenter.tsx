import React, { useEffect, useState, useCallback } from 'react';
import {
  Table,
  Button,
  Tag,
  Space,
  Drawer,
  Typography,
  message,
  Card,
  Spin,
  Modal,
  Form,
  Input,
  InputNumber,
  Radio,
  Select,
  AutoComplete,
  Upload,
  Popconfirm,
  Alert,
  Tabs,
  Switch,
} from 'antd';
import {
  DatabaseOutlined,
  EyeOutlined,
  ExperimentOutlined,
  ReloadOutlined,
  DownloadOutlined,
  UploadOutlined,
  DeleteOutlined,
  FileTextOutlined,
  InboxOutlined,
  CopyOutlined,
  CheckCircleOutlined,
  RocketOutlined,
  FilterOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { datasetsApi } from '../api';
import type {
  DatasetRecord,
  BenchmarkSample,
  DatasetImportRequest,
  BenchmarkDatasetFilterOptionsResponse,
  BenchmarkExportPreviewResponse,
  BenchmarkExportFilterRequest,
} from '../types';

const { Text } = Typography;
const { TextArea } = Input;

const DEMO_SAMPLE_JSONL = `{"id": "gen_004", "category": "通用百科常识", "sub_category": "计算机网络", "difficulty": "easy", "source": "MMLU计算机基础", "max_new_tokens": 64, "messages": [{"role": "user", "content": "请简短回答：HTTP状态码 404 和 502 分别代表什么含义？"}], "reference": "404 表示未找到请求的资源（Not Found）；502 表示错误网关（Bad Gateway），即网关或代理服务器从上游服务器收到无效响应。", "rubric": "准确说明 404 为未找到资源/Not Found，502 为网关错误/Bad Gateway。", "regex_constraints": ["未找到|不存在|Not Found", "网关|上游|Bad Gateway", "!星云智联|售后专员"]}`;

export const DatasetCenter: React.FC = () => {
  const navigate = useNavigate();
  const [datasets, setDatasets] = useState<DatasetRecord[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [activeTab, setActiveTab] = useState<string>('benchmark');

  // 外部数据集导入模态框状态
  const [importModalVisible, setImportModalVisible] = useState<boolean>(false);
  const [importSourceType, setImportSourceType] = useState<'file' | 'paste' | 'path'>('file');
  const [uploadedFileName, setUploadedFileName] = useState<string>('');
  const [uploadedContent, setUploadedContent] = useState<string>('');
  const [importing, setImporting] = useState<boolean>(false);
  const [importForm] = Form.useForm();
  const watchedImportDatasetType = Form.useWatch('dataset_type', importForm) || 'benchmark';

  // 抽样预览抽屉
  const [previewDrawerVisible, setPreviewDrawerVisible] = useState<boolean>(false);
  const [previewingDataset, setPreviewingDataset] = useState<DatasetRecord | null>(null);
  const [samples, setSamples] = useState<BenchmarkSample[]>([]);
  const [sampleLoading, setSampleLoading] = useState<boolean>(false);

  // 评测集抽样与条件过滤导出模态框
  const [filterExportVisible, setFilterExportVisible] = useState<boolean>(false);
  const [exportingDataset, setExportingDataset] = useState<DatasetRecord | null>(null);
  const [filterOptions, setFilterOptions] = useState<BenchmarkDatasetFilterOptionsResponse | null>(null);
  const [filterOptionsLoading, setFilterOptionsLoading] = useState<boolean>(false);
  const [exportPreview, setExportPreview] = useState<BenchmarkExportPreviewResponse | null>(null);
  const [exportPreviewLoading, setExportPreviewLoading] = useState<boolean>(false);
  const [exportSubmitting, setExportSubmitting] = useState<boolean>(false);
  const [filterExportForm] = Form.useForm();

  const watchedExportTarget = Form.useWatch('export_target', filterExportForm) || 'download';
  const watchedExportScope = Form.useWatch('export_scope', filterExportForm) || 'sampled';
  const watchedSamplingEnabled = Form.useWatch('sampling_enabled', filterExportForm) ?? true;
  const watchedModelLine = Form.useWatch('model_line', filterExportForm);
  const watchedVersionTag = Form.useWatch('version_tag', filterExportForm);
  const watchedThresholdX = Form.useWatch('pass_threshold_x', filterExportForm);
  const watchedIntervalS = Form.useWatch('interval_step_s', filterExportForm);
  const watchedMaxIntervalN = Form.useWatch('max_interval_n', filterExportForm);
  const watchedMinPassScore = Form.useWatch('min_pass_score', filterExportForm);
  const watchedGroupWakeup = Form.useWatch('enable_group_regression_wakeup', filterExportForm);
  const watchedCategories = Form.useWatch('categories', filterExportForm);
  const watchedDifficulties = Form.useWatch('difficulties', filterExportForm);
  const watchedHoldoutFilter = Form.useWatch('holdout_filter', filterExportForm);
  const watchedKeyword = Form.useWatch('keyword', filterExportForm);

  const loadDatasets = async () => {
    setLoading(true);
    try {
      const list = await datasetsApi.list();
      setDatasets(list);
    } catch (e: any) {
      message.error(`加载数据集列表失败: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadDatasets();
  }, []);

  const handlePreview = async (dataset: DatasetRecord) => {
    setPreviewingDataset(dataset);
    setPreviewDrawerVisible(true);
    setSampleLoading(true);
    try {
      const res = await datasetsApi.preview(dataset.id, 50);
      setSamples(res.samples || []);
    } catch (e: any) {
      message.error(`抽检样本失败: ${e.message}`);
    } finally {
      setSampleLoading(false);
    }
  };

  const handleDownloadDataset = async (dataset: DatasetRecord) => {
    try {
      message.loading({ content: '正在打包数据集...', key: 'dl' });
      const res = await datasetsApi.preview(dataset.id, 5000);
      const content = (res.samples || []).map((s) => JSON.stringify(s, null, 0)).join('\n');
      const blob = new Blob([content], { type: 'application/x-jsonlines;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = dataset.name.endsWith('.jsonl') ? dataset.name : `${dataset.name}.jsonl`;
      a.click();
      URL.revokeObjectURL(url);
      message.success({ content: '下载已启动', key: 'dl' });
    } catch (err: any) {
      message.error({ content: `下载失败: ${err.message}`, key: 'dl' });
    }
  };

  const handleDeleteDataset = async (datasetId: string) => {
    try {
      await datasetsApi.deleteDataset(datasetId);
      message.success(`数据集 [${datasetId}] 已删除`);
      loadDatasets();
    } catch (err: any) {
      message.error(`删除失败: ${err.response?.data?.detail || err.message}`);
    }
  };

  const buildExportFilterPayload = useCallback(
    (values?: any): BenchmarkExportFilterRequest => {
      const v = values || filterExportForm.getFieldsValue();
      const mLine = (v.model_line || '').trim() || null;
      const vTag = (v.version_tag || '').trim() || null;
      return {
        sampling_config: {
          enabled: !!v.sampling_enabled,
          pass_threshold_x: v.pass_threshold_x ?? 3,
          interval_step_s: v.interval_step_s ?? 1,
          max_interval_n: v.max_interval_n ?? 5,
          min_pass_score: v.min_pass_score ?? null,
          model_line: mLine,
          version_tag: vTag,
          benchmark_model_id: mLine,
          tracker_model_keys: [],
          enable_group_regression_wakeup: !!v.enable_group_regression_wakeup,
        },
        export_scope: v.export_scope || 'sampled',
        categories: v.categories || [],
        difficulties: v.difficulties || [],
        holdout_filter: v.holdout_filter || 'all',
        keyword: v.keyword || undefined,
        include_tracker: !!v.include_tracker,
        export_target: v.export_target || 'download',
        target_dataset_id: v.target_dataset_id?.trim() || undefined,
        target_dataset_name: v.target_dataset_name?.trim() || undefined,
        save_mode: v.save_mode || 'overwrite',
      };
    },
    [filterExportForm]
  );

  const refreshExportPreview = useCallback(
    async (datasetId?: string) => {
      const targetId = datasetId || exportingDataset?.id;
      if (!targetId) return;
      setExportPreviewLoading(true);
      try {
        const payload = buildExportFilterPayload();
        const res = await datasetsApi.previewExport(targetId, payload);
        setExportPreview(res);
      } catch (err: any) {
        console.error('过滤导出试算失败', err);
      } finally {
        setExportPreviewLoading(false);
      }
    },
    [exportingDataset, buildExportFilterPayload]
  );

  const openFilterExportModal = async (dataset: DatasetRecord) => {
    setExportingDataset(dataset);
    setFilterOptions(null);
    setExportPreview(null);
    filterExportForm.resetFields();
    filterExportForm.setFieldsValue({
      export_target: 'download',
      export_scope: 'sampled',
      sampling_enabled: true,
      pass_threshold_x: 3,
      interval_step_s: 1,
      max_interval_n: 5,
      holdout_filter: 'all',
      categories: [],
      difficulties: [],
      include_tracker: false,
      enable_group_regression_wakeup: false,
      save_mode: 'overwrite',
      target_dataset_id: `${dataset.id}_filtered`,
      target_dataset_name: `${dataset.id}_filtered.jsonl`,
    });
    setFilterExportVisible(true);
    setFilterOptionsLoading(true);
    try {
      const opts = await datasetsApi.getFilterOptions(dataset.id);
      setFilterOptions(opts);
      if (opts.model_lines && opts.model_lines.length > 0) {
        filterExportForm.setFieldsValue({
          model_line: opts.model_lines[0].model_line,
        });
      }
      await refreshExportPreview(dataset.id);
    } catch (err: any) {
      message.error(`加载评测集过滤选项失败: ${err.response?.data?.detail || err.message}`);
    } finally {
      setFilterOptionsLoading(false);
    }
  };

  useEffect(() => {
    if (!filterExportVisible || !exportingDataset) return;
    const timer = setTimeout(() => {
      refreshExportPreview(exportingDataset.id);
    }, 280);
    return () => clearTimeout(timer);
  }, [
    filterExportVisible,
    exportingDataset,
    watchedExportScope,
    watchedSamplingEnabled,
    watchedModelLine,
    watchedVersionTag,
    watchedThresholdX,
    watchedIntervalS,
    watchedMaxIntervalN,
    watchedMinPassScore,
    watchedGroupWakeup,
    watchedCategories,
    watchedDifficulties,
    watchedHoldoutFilter,
    watchedKeyword,
    refreshExportPreview,
  ]);

  const handleFilterExportSubmit = async () => {
    if (!exportingDataset) return;
    try {
      const values = await filterExportForm.validateFields();
      const payload = buildExportFilterPayload(values);
      setExportSubmitting(true);
      const res = await datasetsApi.exportFiltered(exportingDataset.id, payload);
      if (payload.export_target === 'download' && res.jsonl_content !== undefined) {
        const blob = new Blob([res.jsonl_content], { type: 'application/x-jsonlines;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = res.filename || `${exportingDataset.id}_filtered.jsonl`;
        a.click();
        URL.revokeObjectURL(url);
        message.success(res.message || `已导出 ${res.exported_count} 道考题`);
      } else {
        message.success(res.message || '新评测集另存成功');
        loadDatasets();
      }
      setFilterExportVisible(false);
    } catch (err: any) {
      if (err.errorFields) return;
      message.error(`过滤导出失败: ${err.response?.data?.detail || err.message}`);
    } finally {
      setExportSubmitting(false);
    }
  };

  const openImportModal = () => {
    importForm.resetFields();
    importForm.setFieldsValue({
      dataset_type: activeTab === 'benchmark' ? 'benchmark' : 'flywheel_gold',
      mode: 'overwrite',
      mark_as_holdout: false,
    });
    setImportSourceType('file');
    setUploadedFileName('');
    setUploadedContent('');
    setImportModalVisible(true);
  };

  const handleFileSelect = (file: File) => {
    const reader = new FileReader();
    reader.onload = (e) => {
      const text = (e.target?.result as string) || '';
      setUploadedContent(text);
      setUploadedFileName(file.name);
      const currentId = importForm.getFieldValue('dataset_id');
      if (!currentId) {
        const cleanName = file.name.replace(/\.(jsonl|json)$/i, '');
        importForm.setFieldsValue({ dataset_id: cleanName });
      }
    };
    reader.readAsText(file, 'utf-8');
    return false;
  };

  const handleImportSubmit = async () => {
    try {
      const values = await importForm.validateFields();
      const payload: DatasetImportRequest = {
        dataset_id: values.dataset_id,
        dataset_type: values.dataset_type,
        mode: values.mode,
        mark_as_holdout: values.dataset_type === 'benchmark' ? !!values.mark_as_holdout : false,
      };

      if (importSourceType === 'file') {
        if (!uploadedContent.trim()) {
          message.warning('请先选择要上传的本地 .jsonl 或 .json 数据集文件');
          return;
        }
        payload.content = uploadedContent;
      } else if (importSourceType === 'paste') {
        if (!values.paste_content?.trim()) {
          message.warning('请粘贴 JSONL 或 JSON 数组格式的数据集内容');
          return;
        }
        payload.content = values.paste_content;
      } else if (importSourceType === 'path') {
        if (!values.source_file_path?.trim()) {
          message.warning('请输入服务端本地文件路径');
          return;
        }
        payload.source_file_path = values.source_file_path;
      }

      setImporting(true);
      const res = await datasetsApi.importDataset(payload);
      message.success(res.message);
      setImportModalVisible(false);
      loadDatasets();
    } catch (err: any) {
      if (err.errorFields) return;
      message.error(`导入失败: ${err.response?.data?.detail || err.message}`);
    } finally {
      setImporting(false);
    }
  };

  const getDifficultyTag = (diff?: string) => {
    if (!diff) return null;
    const d = diff.toLowerCase();
    const color = d === 'easy' ? 'green' : d === 'medium' ? 'orange' : d === 'hard' ? 'red' : 'default';
    return <Tag color={color}>难度: {diff}</Tag>;
  };

  // 区分过滤常规评测集 vs 训练对话集
  const benchmarkDatasets = datasets.filter((d) => d.dataset_type === 'benchmark');
  const trainingDatasets = datasets.filter((d) => d.dataset_type !== 'benchmark');

  const renderBenchmarkColumns = () => [
    {
      title: '评测集标识 (ID)',
      dataIndex: 'id',
      width: 240,
      render: (id: string) => <span className="font-mono text-xs font-semibold text-slate-800">{id}</span>,
    },
    {
      title: '数据集名称 / 磁盘路径',
      dataIndex: 'name',
      render: (name: string, rec: DatasetRecord) => (
        <div>
          <div className="text-sm font-medium text-slate-800 flex items-center gap-1.5">
            <span>{name}</span>
            <Tag color="cyan" className="text-3xs">基准评测集</Tag>
          </div>
          <div className="text-3xs text-slate-400 font-mono mt-0.5">{rec.file_path}</div>
        </div>
      ),
    },
    {
      title: '考题规模',
      dataIndex: 'sample_count',
      width: 120,
      render: (cnt: number) => (
        <span className="font-bold text-sm text-blue-600">{cnt} 道考题</span>
      ),
    },
    {
      title: '操作',
      width: 400,
      render: (_: any, record: DatasetRecord) => (
        <Space size="small">
          <Button
            size="small"
            icon={<EyeOutlined />}
            onClick={() => handlePreview(record)}
          >
            抽检考题
          </Button>
          <Button
            size="small"
            type="primary"
            icon={<RocketOutlined />}
            onClick={() =>
              navigate('/benchmarks', {
                state: { datasetPath: record.file_path },
              })
            }
          >
            发起对比评测
          </Button>
          <Button
            size="small"
            icon={<FilterOutlined />}
            onClick={() => openFilterExportModal(record)}
          >
            过滤导出
          </Button>
          <Button
            size="small"
            icon={<DownloadOutlined />}
            onClick={() => handleDownloadDataset(record)}
          >
            全量下载
          </Button>
          <Popconfirm
            title={`确定要删除评测集 [${record.id}] 吗？`}
            onConfirm={() => handleDeleteDataset(record.id)}
            okText="确认删除"
            cancelText="取消"
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const renderTrainingColumns = () => [
    {
      title: '训练集标识 (ID)',
      dataIndex: 'id',
      width: 240,
      render: (id: string) => <span className="font-mono text-xs font-semibold text-slate-800">{id}</span>,
    },
    {
      title: '数据集名称 / 磁盘路径',
      dataIndex: 'name',
      render: (name: string, rec: DatasetRecord) => (
        <div>
          <div className="text-sm font-medium text-slate-800 flex items-center gap-1.5">
            <span>{name}</span>
            <Tag color="gold" className="text-3xs">SFT 微调会话</Tag>
          </div>
          <div className="text-3xs text-slate-400 font-mono mt-0.5">{rec.file_path}</div>
        </div>
      ),
    },
    {
      title: '会话总数',
      dataIndex: 'sample_count',
      width: 120,
      render: (cnt: number) => (
        <span className="font-bold text-sm text-amber-600">{cnt} 篇完整会话</span>
      ),
    },
    {
      title: '操作',
      width: 260,
      render: (_: any, record: DatasetRecord) => (
        <Space size="small">
          <Button
            size="small"
            icon={<EyeOutlined />}
            onClick={() => handlePreview(record)}
          >
            抽检对话
          </Button>
          <Button
            size="small"
            icon={<DownloadOutlined />}
            onClick={() => handleDownloadDataset(record)}
          >
            下载 SFT 语料
          </Button>
          <Popconfirm
            title={`确定要删除训练集 [${record.id}] 吗？`}
            onConfirm={() => handleDeleteDataset(record.id)}
            okText="确认删除"
            cancelText="取消"
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      {/* 顶部操作栏 */}
      <div className="flex justify-between items-center bg-white p-4 rounded-lg border border-slate-200 shadow-2xs">
        <div className="flex items-center gap-2">
          <DatabaseOutlined className="text-blue-500 text-lg" />
          <span className="font-bold text-sm text-slate-800">平台数据资产中心</span>
          <span className="text-xs text-slate-400">
            （双轨分类管理：常规/实战基准评测集 ∥ 飞轮产出的 SFT 微调黄金训练集）
          </span>
        </div>

        <Space size="small">
          <Button
            type="primary"
            size="small"
            icon={<UploadOutlined />}
            onClick={openImportModal}
          >
            导入外部数据集
          </Button>
          <Button size="small" icon={<ReloadOutlined />} onClick={() => loadDatasets()}>
            刷新列表
          </Button>
        </Space>
      </div>

      {/* 双 Tab 解耦展示 */}
      <div className="bg-white rounded-lg border border-slate-200 shadow-2xs p-4">
        <Tabs
          activeKey={activeTab}
          onChange={(key) => setActiveTab(key)}
          items={[
            {
              key: 'benchmark',
              label: (
                <span className="font-semibold text-xs flex items-center gap-1.5">
                  <span>🎯 常规基准评测集</span>
                  <Tag color="cyan" className="ml-1 text-3xs font-mono">{benchmarkDatasets.length}</Tag>
                </span>
              ),
              children: (
                <div>
                  <div className="mb-3 text-xs text-slate-500 bg-cyan-50/60 p-2.5 rounded border border-cyan-100 flex items-center justify-between">
                    <span>💡 基准评测集用于横向打靶评估模型能力，包含题目输入消息 (messages)、标准参考答案 (reference)、评分细则 (rubric) 及正反向正则约束。</span>
                  </div>
                  <Table
                    rowKey="id"
                    loading={loading}
                    dataSource={benchmarkDatasets}
                    pagination={false}
                    columns={renderBenchmarkColumns()}
                  />
                </div>
              ),
            },
            {
              key: 'training',
              label: (
                <span className="font-semibold text-xs flex items-center gap-1.5">
                  <span>📦 训练与对话语料 (SFT)</span>
                  <Tag color="gold" className="ml-1 text-3xs font-mono">{trainingDatasets.length}</Tag>
                </span>
              ),
              children: (
                <div>
                  <div className="mb-3 text-xs text-slate-500 bg-amber-50/60 p-2.5 rounded border border-amber-100 flex items-center justify-between">
                    <span>💡 训练语料为通过数据飞轮质检、AI 微调重塑并经人工复核通过的完整多轮对话 (messages)，用于 LoRA / 全参微调。</span>
                  </div>
                  <Table
                    rowKey="id"
                    loading={loading}
                    dataSource={trainingDatasets}
                    pagination={false}
                    columns={renderTrainingColumns()}
                  />
                </div>
              ),
            },
          ]}
        />
      </div>

      {/* 导入外部数据集模态框 */}
      <Modal
        title="导入外部评测集 / 数据集"
        open={importModalVisible}
        onCancel={() => setImportModalVisible(false)}
        onOk={handleImportSubmit}
        confirmLoading={importing}
        width={760}
        okText="校验并执行导入"
        cancelText="取消"
      >
        <Form form={importForm} layout="vertical" className="mt-2">
          <Alert
            type="info"
            showIcon
            className="mb-4 text-xs"
            message="标准评测集字段约定 (JSONL 每行一条或 JSON 数组)"
            description={
              <div className="text-3xs font-mono text-slate-600 mt-1 break-all">
                {DEMO_SAMPLE_JSONL}
              </div>
            }
          />

          <div className="grid grid-cols-2 gap-4">
            <Form.Item
              name="dataset_id"
              label="数据集唯一标识 (ID)"
              rules={[
                { required: true, message: '请输入数据集ID' },
                { pattern: /^[a-zA-Z0-9_\-\.]+$/, message: '仅支持字母、数字、下划线、短横线与点' },
              ]}
              extra="导入后将以此 ID 命名本地 .jsonl 文件，并作为评测集注册标识"
            >
              <Input placeholder="例如: demo_eval_hard_v1" />
            </Form.Item>

            <Form.Item
              name="dataset_type"
              label="资产分类"
              rules={[{ required: true, message: '请选择数据资产类型' }]}
            >
              <Radio.Group buttonStyle="solid" className="w-full">
                <Radio.Button value="benchmark" className="w-1/2 text-center">🎯 常规基准评测集</Radio.Button>
                <Radio.Button value="flywheel_gold" className="w-1/2 text-center">📦 训练微调语料</Radio.Button>
              </Radio.Group>
            </Form.Item>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <Form.Item name="mode" label="写入模式" rules={[{ required: true }]}>
              <Radio.Group>
                <Radio value="overwrite">覆盖现有数据集 (自动清空旧战绩)</Radio>
                <Radio value="append">追加合并 (按 ID 增量去重更新)</Radio>
              </Radio.Group>
            </Form.Item>

            {watchedImportDatasetType === 'benchmark' && (
              <Form.Item
                name="mark_as_holdout"
                label="标记为纯评测留出题 (is_holdout)"
                valuePropName="checked"
                extra="仅外部导入纯测试集开启；与训练集同源的样本请关闭以禁止降频"
              >
                <Switch checkedChildren="留出评测题 (允许降频)" unCheckedChildren="同源题 (强制全测)" />
              </Form.Item>
            )}
          </div>

          <Form.Item label="数据源提供方式">
            <Radio.Group
              value={importSourceType}
              onChange={(e) => setImportSourceType(e.target.value)}
              buttonStyle="solid"
            >
              <Radio.Button value="file">本地文件上传 (.jsonl / .json)</Radio.Button>
              <Radio.Button value="paste">粘贴 JSONL 文本</Radio.Button>
              <Radio.Button value="path">服务端磁盘路径</Radio.Button>
            </Radio.Group>
          </Form.Item>

          {importSourceType === 'file' && (
            <div className="border border-dashed border-slate-300 rounded-lg p-6 text-center bg-slate-50">
              <Upload.Dragger
                name="file"
                multiple={false}
                accept=".jsonl,.json,.txt"
                beforeUpload={handleFileSelect}
                showUploadList={false}
              >
                <p className="ant-upload-drag-icon">
                  <InboxOutlined className="text-3xl text-blue-500" />
                </p>
                <p className="text-xs font-semibold text-slate-700">点击或将 .jsonl / .json 数据集文件拖拽至此处</p>
                <p className="text-3xs text-slate-400">支持标准 JSONL 格式或 JSON 对象数组，最大 100MB</p>
                {uploadedFileName && (
                  <div className="mt-2 text-xs text-emerald-600 font-medium">
                    已选定文件: {uploadedFileName} (共 {uploadedContent.length} 字符)
                  </div>
                )}
              </Upload.Dragger>
            </div>
          )}

          {importSourceType === 'paste' && (
            <Form.Item
              name="paste_content"
              label="粘贴数据集文本 (每行一条 JSON 或整体为 JSON 数组)"
              rules={[{ required: importSourceType === 'paste', message: '请粘贴数据集文本' }]}
            >
              <TextArea
                rows={7}
                className="font-mono text-xs"
                placeholder={DEMO_SAMPLE_JSONL}
              />
            </Form.Item>
          )}

          {importSourceType === 'path' && (
            <div className="mt-2">
              <Form.Item
                name="source_file_path"
                label="服务端本地文件绝对或相对路径"
                extra="例如: D:\data\my_benchmark.jsonl 或 data/datasets/demo_benchmark.jsonl"
              >
                <Input className="font-mono text-xs" placeholder="请输入本地磁盘上的 .jsonl 或 .json 文件路径" />
              </Form.Item>
            </div>
          )}
        </Form>
      </Modal>

      {/* 评测集抽样与条件过滤导出模态框 */}
      <Modal
        title={`评测集过滤导出: ${exportingDataset?.name || ''}`}
        open={filterExportVisible}
        onCancel={() => setFilterExportVisible(false)}
        onOk={handleFilterExportSubmit}
        confirmLoading={exportSubmitting}
        width={800}
        okText={watchedExportTarget === 'save_dataset' ? '另存为新评测集' : '导出并下载 (.jsonl)'}
        cancelText="取消"
      >
        <Spin spinning={filterOptionsLoading}>
          <Form form={filterExportForm} layout="vertical" className="mt-2 space-y-3">
            {/* 1. 导出方式 */}
            <div className="p-3 bg-slate-50 rounded-lg border border-slate-200 space-y-2.5">
              <div className="flex items-center justify-between flex-wrap gap-2">
                <span className="text-xs font-semibold text-slate-800">1. 导出方式与目标</span>
                {filterOptions && (
                  <span className="text-3xs text-slate-500 font-mono">
                    总计 {filterOptions.total_samples} 题（留出题 {filterOptions.holdout_count} / 同源题 {filterOptions.non_holdout_count}）
                  </span>
                )}
              </div>

              <Form.Item name="export_target" className="m-0">
                <Radio.Group buttonStyle="solid" className="w-full">
                  <Radio.Button value="download" className="w-1/2 text-center">
                    ⬇️ 直接下载为本地 .jsonl 文件
                  </Radio.Button>
                  <Radio.Button value="save_dataset" className="w-1/2 text-center">
                    💾 另存为平台新评测集 (可直接发起评测)
                  </Radio.Button>
                </Radio.Group>
              </Form.Item>

              {watchedExportTarget === 'save_dataset' && (
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 pt-1">
                  <Form.Item
                    name="target_dataset_id"
                    label={<span className="text-3xs text-slate-700">目标评测集 ID</span>}
                    rules={[
                      { required: true, message: '请输入新评测集 ID' },
                      { pattern: /^[a-zA-Z0-9_\-\.]+$/, message: '仅支持字母、数字、下划线、短横线与点' },
                    ]}
                    className="m-0"
                  >
                    <Input size="small" placeholder="例如: demo_active_subset" />
                  </Form.Item>

                  <Form.Item
                    name="target_dataset_name"
                    label={<span className="text-3xs text-slate-700">显示名称</span>}
                    className="m-0"
                  >
                    <Input size="small" placeholder="例如: 必测攻坚子集.jsonl" />
                  </Form.Item>

                  <Form.Item
                    name="save_mode"
                    label={<span className="text-3xs text-slate-700">写入模式</span>}
                    className="m-0"
                  >
                    <Radio.Group size="small">
                      <Radio value="overwrite">覆盖创建</Radio>
                      <Radio value="append">追加合并</Radio>
                    </Radio.Group>
                  </Form.Item>
                </div>
              )}
            </div>

            {/* 2. 抽样测评同款动态降频与战绩池过滤 */}
            <div className="p-3 bg-gradient-to-r from-purple-50/70 to-indigo-50/70 border border-purple-200 rounded-lg space-y-3">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <div className="flex items-center gap-1.5 font-semibold text-slate-800 text-xs">
                    <FilterOutlined className="text-purple-600" />
                    <span>2. 抽样测评同款过滤策略（动态阶梯降频 & 战绩池）</span>
                  </div>
                  <span className="text-3xs text-slate-500 block mt-0.5">
                    完全复用评测引擎三池划分算法与 SQLite 战绩流水，只读计算、绝不修改原题库与战绩。
                  </span>
                </div>
                <Form.Item name="sampling_enabled" valuePropName="checked" className="m-0">
                  <Switch checkedChildren="动态降频开启" unCheckedChildren="降频关闭" />
                </Form.Item>
              </div>

              <Form.Item
                name="export_scope"
                label={<span className="text-3xs font-semibold text-slate-700">导出样本范围</span>}
                className="m-0"
              >
                <Select
                  size="small"
                  popupMatchSelectWidth={false}
                  options={[
                    { value: 'sampled', label: '🎯 本轮抽样命中题单（必测攻坚 + 触顶保底 + 降频轮巡抽中）' },
                    { value: 'active_only', label: '🔴 仅必测攻坚池（I=1：新题 / 未达门槛 / 失败 / 同源非留出题）' },
                    { value: 'cooldown_all', label: '🟢 仅全部熟题池（I>1：已连续通过门槛且为留出题）' },
                    { value: 'skipped', label: '💤 仅本轮休眠免测熟题（本轮降频跳过的题目）' },
                    { value: 'failed_only', label: '❌ 仅最近一次失败题（该模型线下 last_status = FAIL）' },
                    { value: 'all', label: '📦 全量样本（跳过三池抽样，仅应用基础属性过滤）' },
                  ]}
                />
              </Form.Item>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <Form.Item
                  name="model_line"
                  label={<span className="text-3xs text-slate-700">基准模型线 (Model Line)</span>}
                  extra="选择或输入已有战绩的模型线；留空则全部题目按新题 (c=0, I=1) 处理"
                  className="m-0"
                >
                  <AutoComplete
                    allowClear
                    size="small"
                    placeholder="选择或输入模型线标识，如 qwen2.5-7b-sft"
                    options={(filterOptions?.model_lines || []).map((ml) => ({
                      value: ml.model_line,
                      label: `${ml.model_line}（${ml.ledger_count} 条流水，版本: ${ml.version_tags.slice(0, 3).join(', ') || '无'}）`,
                    }))}
                  />
                </Form.Item>

                <Form.Item
                  name="version_tag"
                  label={<span className="text-3xs text-slate-700">版本号 (可选，用于复用同版本抽题快照)</span>}
                  extra="填写已评测过的版本号时，将严格复用该次评测的抽题快照"
                  className="m-0"
                >
                  <AutoComplete
                    allowClear
                    size="small"
                    placeholder="可选，如 v2.1"
                    options={
                      (
                        filterOptions?.model_lines?.find((ml) => ml.model_line === watchedModelLine)
                          ?.version_tags || []
                      ).map((vt) => ({ value: vt, label: vt }))
                    }
                  />
                </Form.Item>
              </div>

              {watchedSamplingEnabled && (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 bg-white p-2.5 rounded border border-purple-100">
                  <Form.Item
                    name="pass_threshold_x"
                    label={<span className="text-3xs text-slate-700">降频门槛 (x 次)</span>}
                    className="m-0"
                  >
                    <InputNumber min={1} max={20} size="small" className="w-full" />
                  </Form.Item>
                  <Form.Item
                    name="interval_step_s"
                    label={<span className="text-3xs text-slate-700">间隔步长 (s 轮)</span>}
                    className="m-0"
                  >
                    <InputNumber min={1} max={10} size="small" className="w-full" />
                  </Form.Item>
                  <Form.Item
                    name="max_interval_n"
                    label={<span className="text-3xs text-slate-700">最大间隔上限 (N)</span>}
                    className="m-0"
                  >
                    <InputNumber min={1} max={50} size="small" className="w-full" />
                  </Form.Item>
                  <Form.Item
                    name="min_pass_score"
                    label={<span className="text-3xs text-slate-700">最低分门槛</span>}
                    className="m-0"
                  >
                    <InputNumber min={0} max={100} size="small" placeholder="默认及格线" className="w-full" />
                  </Form.Item>
                </div>
              )}
            </div>

            {/* 3. 基础题目属性过滤 */}
            <div className="p-3 bg-slate-50 rounded-lg border border-slate-200 space-y-2.5">
              <div className="text-xs font-semibold text-slate-800">3. 基础题目属性叠加过滤（可选）</div>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <Form.Item name="categories" label={<span className="text-3xs text-slate-700">题目分类</span>} className="m-0">
                  <Select
                    mode="multiple"
                    allowClear
                    size="small"
                    placeholder="全部分类"
                    popupMatchSelectWidth={false}
                    options={(filterOptions?.categories || []).map((c) => ({ value: c, label: c }))}
                  />
                </Form.Item>

                <Form.Item name="difficulties" label={<span className="text-3xs text-slate-700">题目难度</span>} className="m-0">
                  <Select
                    mode="multiple"
                    allowClear
                    size="small"
                    placeholder="全部难度"
                    popupMatchSelectWidth={false}
                    options={
                      (filterOptions?.difficulties?.length ? filterOptions.difficulties : ['easy', 'medium', 'hard']).map(
                        (d) => ({ value: d, label: d })
                      )
                    }
                  />
                </Form.Item>

                <Form.Item name="holdout_filter" label={<span className="text-3xs text-slate-700">留出属性</span>} className="m-0">
                  <Select
                    size="small"
                    popupMatchSelectWidth={false}
                    options={[
                      { value: 'all', label: '全部题目' },
                      { value: 'holdout_only', label: '🔒 仅留出评测题 (is_holdout=true)' },
                      { value: 'non_holdout_only', label: '同源非留出题 (is_holdout=false)' },
                    ]}
                  />
                </Form.Item>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 items-end">
                <Form.Item name="keyword" label={<span className="text-3xs text-slate-700">关键词搜索</span>} className="m-0">
                  <Input size="small" allowClear placeholder="按样本 ID / 提问文本 / 参考答案过滤" />
                </Form.Item>

                <Form.Item
                  name="include_tracker"
                  label={<span className="text-3xs text-slate-700">附带 eval_tracker 战绩快照</span>}
                  valuePropName="checked"
                  className="m-0"
                >
                  <Switch checkedChildren="保留战绩字段" unCheckedChildren="纯净考题格式" />
                </Form.Item>
              </div>
            </div>

            {/* 4. 实时过滤试算预览 */}
            <div className="p-3 bg-purple-50/80 rounded-lg border border-purple-200 space-y-1.5">
              <div className="flex items-center justify-between">
                <span className="text-3xs font-bold text-purple-900 flex items-center gap-1">
                  <ExperimentOutlined /> 预计导出题目试算预览
                </span>
                <Button
                  size="small"
                  type="text"
                  icon={<ReloadOutlined spin={exportPreviewLoading} />}
                  onClick={() => refreshExportPreview()}
                  className="text-3xs text-purple-700 h-6 px-1.5"
                >
                  刷新试算
                </Button>
              </div>

              {exportPreview ? (
                <div className="space-y-1.5">
                  <div className="flex items-center justify-between text-xs font-semibold flex-wrap gap-2">
                    <span>
                      原始总题量: <span className="font-mono">{exportPreview.total_samples}</span> 题 ➔ 本次将导出:{' '}
                      <span className="font-mono text-purple-700 text-sm">{exportPreview.exported_count}</span> 题
                    </span>
                    <div className="flex items-center gap-1">
                      {exportPreview.sampling_stats.is_rerun_same_version && (
                        <Tag color="cyan" className="m-0">📌 命中同版本快照复用</Tag>
                      )}
                      <Tag color={exportPreview.exported_count > 0 ? 'purple' : 'red'} className="m-0 font-bold">
                        占比 {exportPreview.total_samples > 0 ? ((exportPreview.exported_count / exportPreview.total_samples) * 100).toFixed(1) : '0.0'}%
                      </Tag>
                    </div>
                  </div>

                  <div className="flex items-center gap-1.5 flex-wrap text-3xs font-mono">
                    <Tag color="red" className="m-0">🔴 必测攻坚: {exportPreview.sampling_stats.active_pool_count} 题</Tag>
                    <Tag color="gold" className="m-0">🟡 触顶必测: {exportPreview.sampling_stats.hard_cap_pool_count} 题</Tag>
                    <Tag color="blue" className="m-0">🟢 降频轮巡: {exportPreview.sampling_stats.regular_cooldown_count} 题</Tag>
                    <Tag color="default" className="m-0">💤 休眠免测: {exportPreview.sampling_stats.skipped_samples} 题</Tag>
                  </div>

                  {exportPreview.sampling_stats.notice && (
                    <div className="text-3xs text-purple-800">{exportPreview.sampling_stats.notice}</div>
                  )}

                  {exportPreview.preview_sample_ids.length > 0 && (
                    <div className="text-3xs text-slate-500 font-mono truncate">
                      命中样本示例: {exportPreview.preview_sample_ids.slice(0, 10).join(', ')}
                      {exportPreview.exported_count > 10 ? ` ... (共 ${exportPreview.exported_count} 条)` : ''}
                    </div>
                  )}
                </div>
              ) : (
                <div className="text-3xs text-purple-700">
                  {exportPreviewLoading ? '正在按抽样与过滤条件试算...' : '调整上方过滤条件以预览导出数量'}
                </div>
              )}
            </div>
          </Form>
        </Spin>
      </Modal>

      {/* 样本抽检抽屉 */}
      <Drawer
        title={`抽样预览: ${previewingDataset?.name || ''} (前 ${samples.length} 条)`}
        placement="right"
        width={760}
        open={previewDrawerVisible}
        onClose={() => setPreviewDrawerVisible(false)}
      >
        {sampleLoading ? (
          <div className="flex items-center justify-center h-64">
            <Spin size="large" />
          </div>
        ) : (
          <div className="space-y-4">
            {samples.map((sample, idx) => {
              const rubricStr =
                typeof sample.rubric === 'string'
                  ? sample.rubric
                  : sample.rubric?.judge_criteria;
              const rubricObj =
                typeof sample.rubric === 'object' && sample.rubric !== null
                  ? sample.rubric
                  : null;

              const isBenchmarkSample = !!(sample.reference || sample.rubric || sample.regex_constraints?.length);

              return (
                <Card
                  key={sample.id || idx}
                  size="small"
                  className="border-slate-200 shadow-2xs"
                  title={
                    <div className="flex items-center justify-between flex-wrap gap-1.5 text-xs">
                      <span className="font-mono font-semibold text-slate-800">
                        #{idx + 1} {sample.id}
                      </span>
                      <Space size={4} wrap>
                        <Tag color={isBenchmarkSample ? 'cyan' : 'gold'}>
                          {isBenchmarkSample ? '🎯 评测考题' : '📦 完整会话'}
                        </Tag>
                        {isBenchmarkSample && (
                          <Tag color={sample.is_holdout ? 'purple' : 'default'}>
                            {sample.is_holdout ? '🔒 留出评测题 (可降频)' : '同源题 (I=1)'}
                          </Tag>
                        )}
                        <Tag color="blue">
                          {sample.category || '默认场景'}
                          {sample.sub_category ? ` / ${sample.sub_category}` : ''}
                        </Tag>
                        {getDifficultyTag(sample.difficulty)}
                        {sample.source && <Tag color="geekblue">来源: {sample.source}</Tag>}
                      </Space>
                    </div>
                  }
                >
                  {/* 对话消息流 */}
                  <div className="space-y-2 mb-3">
                    <div className="text-3xs font-semibold text-slate-500 uppercase tracking-wider mb-1">
                      {isBenchmarkSample ? '考题输入上下文 (Input Messages)' : '多轮对话记录 (Dialogue Messages)'}:
                    </div>
                    {(sample.messages || []).map((m, i) => (
                      <div
                        key={i}
                        className={`p-2 rounded text-xs ${
                          m.role === 'system'
                            ? 'bg-purple-50 text-purple-900 border border-purple-200'
                            : m.role === 'user'
                            ? 'bg-blue-50 text-blue-900 border border-blue-200'
                            : 'bg-emerald-50 text-emerald-900 border border-emerald-200'
                        }`}
                      >
                        <span className="font-bold uppercase tracking-wider block mb-0.5 text-3xs opacity-75">
                          {m.role}:
                        </span>
                        <p className="whitespace-pre-wrap mb-0">{m.content}</p>
                      </div>
                    ))}
                  </div>

                  {/* 参考答案、评分细则与正则约束 (仅评测题展示) */}
                  {isBenchmarkSample && (
                    <div className="p-3 bg-slate-50 border border-slate-200 rounded text-xs text-slate-600 space-y-2">
                      {sample.reference && (
                        <div>
                          <div className="font-semibold text-emerald-700 flex items-center gap-1 mb-0.5">
                            <CheckCircleOutlined /> 标准参考答案 (Ground Truth Reference):
                          </div>
                          <div className="p-2 bg-emerald-50/50 rounded border border-emerald-100 text-slate-800 font-mono whitespace-pre-wrap">
                            {sample.reference}
                          </div>
                        </div>
                      )}

                      {rubricStr && (
                        <div>
                          <span className="font-semibold text-slate-700">评分细则 (Rubric): </span>
                          <span className="text-slate-800">{rubricStr}</span>
                        </div>
                      )}

                      {((sample.regex_constraints && sample.regex_constraints.length > 0) ||
                        rubricObj) && (
                        <div className="flex items-center gap-1.5 flex-wrap text-3xs pt-1.5 border-t border-slate-200">
                          <span className="font-semibold text-slate-600">客观正则约束:</span>
                          {(sample.regex_constraints || []).map((pat, pIdx) => {
                            const isNeg = pat.startsWith('!');
                            return (
                              <Tag
                                key={pIdx}
                                color={isNeg ? 'red' : 'green'}
                                className="font-mono"
                              >
                                {isNeg ? `禁含正则: ${pat}` : `必配正则: ${pat}`}
                              </Tag>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  )}
                </Card>
              );
            })}
          </div>
        )}
      </Drawer>
    </div>
  );
};
