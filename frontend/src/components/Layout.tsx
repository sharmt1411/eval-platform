import React, { useState } from 'react';
import { Layout as AntLayout, Menu, Typography, Badge, Space } from 'antd';
import { useNavigate, useLocation, Outlet } from 'react-router-dom';
import {
  ApartmentOutlined,
  BlockOutlined,
  CompassOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  SyncOutlined,
  GlobalOutlined,
  CodeOutlined,
} from '@ant-design/icons';

const { Header, Sider, Content } = AntLayout;
const { Text } = Typography;

export const AppLayout: React.FC = () => {
  const [collapsed, setCollapsed] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();

  const menuItems = [
    {
      key: '/models',
      icon: <ApartmentOutlined />,
      label: '模型与参数中心',
    },
    {
      key: '/rules',
      icon: <BlockOutlined />,
      label: '场景规则与 SOP',
    },
    {
      key: '/benchmarks',
      icon: <ExperimentOutlined />,
      label: '常规评测工作台',
    },
    {
      key: '/simulation',
      icon: <CompassOutlined />,
      label: 'AI 模拟对弈台',
    },
    {
      key: '/flywheel',
      icon: <SyncOutlined />,
      label: '数据飞轮复核台',
    },
    {
      key: '/datasets',
      icon: <DatabaseOutlined />,
      label: '数据资产中心',
    },
    {
      key: '/prompts',
      icon: <CodeOutlined />,
      label: '提示词管理中心',
    },
  ];

  const currentPath = '/' + location.pathname.split('/')[1] || '/models';

  return (
    <AntLayout className="min-h-screen">
      {/* 侧边导航栏 */}
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={(val) => setCollapsed(val)}
        width={240}
        theme="dark"
        className="shadow-md"
        style={{
          overflow: 'auto',
          height: '100vh',
          position: 'sticky',
          top: 0,
          left: 0,
        }}
      >
        <div className="flex items-center gap-2 px-4 py-5 text-white border-b border-slate-700/60">
          <GlobalOutlined className="text-xl text-blue-400 shrink-0" />
          {!collapsed && (
            <div className="overflow-hidden">
              <h1 className="text-sm font-bold tracking-tight truncate leading-tight">
                通用大模型测试飞轮
              </h1>
              <p className="text-3xs text-slate-400 uppercase tracking-wider">
                Eval & Flywheel Platform
              </p>
            </div>
          )}
        </div>

        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[currentPath]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
          className="mt-3 font-medium text-slate-300"
        />
      </Sider>

      {/* 主工作区 */}
      <AntLayout className="bg-slate-100 flex flex-col min-h-screen">
        {/* 顶部状态栏 */}
        <Header className="bg-white px-6 h-14 border-b border-slate-200 flex items-center justify-between shadow-2xs z-10">
          <div className="flex items-center gap-3">
            <Text strong className="text-base text-slate-800">
              {menuItems.find((m) => m.key === currentPath)?.label || '工作台'}
            </Text>
            <span className="text-xs text-slate-400">|</span>
            <Text type="secondary" className="text-xs">
              企业级大模型测试管理、白盒质检与数据治理闭环
            </Text>
          </div>

          <Space size="middle">
            <div className="flex items-center gap-2 text-xs text-slate-600 bg-slate-50 px-3 py-1 rounded border border-slate-200">
              <Badge status="processing" color="#10b981" />
              <span>服务在线 (FastAPI)</span>
            </div>
            <div className="text-xs text-slate-500 font-mono">
              v1.1 (2026-11)
            </div>
          </Space>
        </Header>

        {/* 页面主视图 */}
        <Content className="p-6 overflow-auto flex-1">
          <Outlet />
        </Content>
      </AntLayout>
    </AntLayout>
  );
};
