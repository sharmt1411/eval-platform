import React from 'react';
import ReactECharts from 'echarts-for-react';
import type { BenchmarkSummary } from '../types';

interface MetricRadarChartProps {
  summaries: Record<string, BenchmarkSummary>;
  height?: number | string;
}

export const MetricRadarChart: React.FC<MetricRadarChartProps> = ({ summaries, height = 360 }) => {
  const modelIds = Object.keys(summaries);

  if (modelIds.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 text-slate-400 text-sm">
        暂无评测数据，请选择任务进行对比
      </div>
    );
  }

  // 找最大 TPS / Latency 做相对归一化打分
  const maxTps = Math.max(...modelIds.map((m) => summaries[m].avg_tps || 1), 60);

  const seriesData = modelIds.map((mid) => {
    const s = summaries[mid];
    // 归一化得分 (0~100)
    // Latency 越低得分越高 (假设 1000ms 为基准)
    const latencyScore = Math.max(0, Math.min(100, Math.round(100 - (s.avg_latency_ms / 2000) * 100)));
    // TTFT 越低得分越高 (假设 500ms 为基准)
    const ttftScore = Math.max(0, Math.min(100, Math.round(100 - (s.avg_ttft_ms / 1000) * 100)));
    // TPS 归一化
    const tpsScore = Math.min(100, Math.round((s.avg_tps / maxTps) * 100));
    // 思考Token占比
    const thinkingRatio = Math.round((s.thinking_token_ratio || 0) * 100);
    // 客观通过率
    const passRate = Math.round((s.pass_rate || 0) * 100);
    // 裁判均分
    const judgeScore = Math.round(s.avg_judge_score || 0);

    return {
      name: mid,
      value: [latencyScore, ttftScore, tpsScore, passRate, judgeScore, thinkingRatio],
    };
  });

  const option = {
    tooltip: {
      trigger: 'item',
    },
    legend: {
      data: modelIds,
      bottom: 0,
      textStyle: {
        fontSize: 12,
        color: '#475569',
      },
    },
    radar: {
      indicator: [
        { name: '总延时得分 (低延时优)', max: 100 },
        { name: '首字响应 (低TTFT优)', max: 100 },
        { name: '吞吐能力 (TPS)', max: 100 },
        { name: '客观通过率 (%)', max: 100 },
        { name: '裁判均分 (0-100)', max: 100 },
        { name: '思考Token深度 (%)', max: 100 },
      ],
      radius: '65%',
      splitArea: {
        areaStyle: {
          color: ['#f8fafc', '#f1f5f9', '#e2e8f0', '#cbd5e1'].reverse(),
        },
      },
    },
    series: [
      {
        name: '多模型多维表现',
        type: 'radar',
        data: seriesData,
        symbolSize: 4,
        lineStyle: {
          width: 2,
        },
        areaStyle: {
          opacity: 0.15,
        },
      },
    ],
  };

  return <ReactECharts option={option} style={{ height }} />;
};
