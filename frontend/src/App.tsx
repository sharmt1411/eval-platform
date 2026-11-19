import React from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { AppLayout } from './components/Layout';
import { ModelHub } from './pages/ModelHub';
import { RuleCenter } from './pages/RuleCenter';
import { BenchmarkList } from './pages/BenchmarkList';
import { BenchmarkReport } from './pages/BenchmarkReport';
import { SimulationArena } from './pages/SimulationArena';
import { FlywheelWorkbench } from './pages/FlywheelWorkbench';
import { DatasetCenter } from './pages/DatasetCenter';
import { PromptCenter } from './pages/PromptCenter';

export const App: React.FC = () => {
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: '#2563eb',
          borderRadius: 6,
          fontFamily:
            '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
        },
      }}
    >
      <BrowserRouter>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="/" element={<Navigate to="/models" replace />} />
            <Route path="/models" element={<ModelHub />} />
            <Route path="/rules" element={<RuleCenter />} />
            <Route path="/benchmarks" element={<BenchmarkList />} />
            <Route path="/benchmarks/:taskId" element={<BenchmarkReport />} />
            <Route path="/simulation" element={<SimulationArena />} />
            <Route path="/flywheel" element={<FlywheelWorkbench />} />
            <Route path="/datasets" element={<DatasetCenter />} />
            <Route path="/prompts" element={<PromptCenter />} />
            <Route path="*" element={<Navigate to="/models" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ConfigProvider>
  );
};

export default App;
