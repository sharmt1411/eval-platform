import React, { useEffect, useRef, useState } from 'react';
import mermaid from 'mermaid';
import { Button, Space, Tag, Typography, Tooltip } from 'antd';
import {
  PlayCircleOutlined,
  PauseCircleOutlined,
  StepForwardOutlined,
  ReloadOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
  FullscreenExitOutlined,
} from '@ant-design/icons';

const { Text } = Typography;

mermaid.initialize({
  startOnLoad: false,
  suppressErrorRendering: true,
  theme: 'default',
  securityLevel: 'loose',
    flowchart: {
    useMaxWidth: true,
    htmlLabels: true,
    curve: 'basis',
  },
});

interface MermaidViewerProps {
  graphDefinition: string;
  trajectory?: string[];
  topologyValid?: boolean;
  topologyError?: string;
  autoPlay?: boolean;
  activeNodeId?: string;
  onNodeClick?: (nodeId: string) => void;
}

export const MermaidViewer: React.FC<MermaidViewerProps> = ({
  graphDefinition,
  trajectory = [],
  topologyValid = true,
  topologyError,
  autoPlay = false,
  activeNodeId,
  onNodeClick,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const [svgContent, setSvgContent] = useState<string>('');
  const [currentStep, setCurrentStep] = useState<number>(-1);
  const [isPlaying, setIsPlaying] = useState<boolean>(false);
  const timerRef = useRef<any>(null);

  // 缩放与平移状态
  const [scale, setScale] = useState<number>(1);
  const [pan, setPan] = useState<{ x: number; y: number }>({ x: 0, y: 0 });
  const [isDragging, setIsDragging] = useState<boolean>(false);
  const dragStartRef = useRef<{ x: number; y: number }>({ x: 0, y: 0 });

  // 渲染 Mermaid
  useEffect(() => {
    let isMounted = true;
    const renderGraph = async () => {
      if (!graphDefinition || !graphDefinition.trim()) {
        setSvgContent('');
        return;
      }
        const id = `mermaid-svg-${Math.random().toString(36).substring(2, 9)}`;
      try {
        const { svg } = await mermaid.render(id, graphDefinition);
        if (isMounted) {
          setSvgContent(svg);
        }
      } catch (err) {
        console.error('Mermaid render error:', err);
        const errEl = document.getElementById('d' + id) || document.getElementById(id);
        if (errEl) errEl.remove();
        if (isMounted) {
          setSvgContent(`<div class="text-red-500 p-4 text-sm font-mono bg-red-50 rounded">流程图解析异常: ${String(err)}</div>`);
        }
      }
    };

    renderGraph();
    // 图定义变化时重置缩放与位置
    setScale(1);
    setPan({ x: 0, y: 0 });

    return () => {
      isMounted = false;
    };
  }, [graphDefinition]);

  // 高亮节点
  useEffect(() => {
    if (!containerRef.current || !svgContent) return;

    // 清除之前的高亮并绑定点击交互
    const allNodes = containerRef.current.querySelectorAll('.node');
    allNodes.forEach((node) => {
      const rect = node.querySelector('rect, circle, polygon, path');
      if (rect) {
        rect.removeAttribute('style');
        rect.classList.remove('highlight-node', 'error-node', 'visited-node');
      }
      (node as HTMLElement).style.cursor = 'pointer';
      const cleanMatch = (node.textContent || node.id || '').match(/S[1-8]_\d+/);
      const nodeId = cleanMatch ? cleanMatch[0] : (node.id || '').replace(/^flowchart-/, '').split('-')[0];
      if (nodeId) {
        (node as HTMLElement).onclick = (e) => {
          e.stopPropagation();
          onNodeClick?.(nodeId);
        };
      }
    });

    if (trajectory.length === 0 || currentStep < 0) return;

    // 高亮已走过的节点 (浅绿)
    for (let i = 0; i <= currentStep && i < trajectory.length; i++) {
      const rawNode = trajectory[i];
      const cleanIdMatch = String(rawNode).match(/S[1-8]_\d+/);
      const cleanId = cleanIdMatch ? cleanIdMatch[0] : String(rawNode).trim();
      const isCurrent = i === currentStep;
      const isLastStep = i === trajectory.length - 1;

      // 匹配 svg 中的节点：优先按 cleanId 匹配节点 ID 或节点文本
      const targetNode = Array.from(allNodes).find((el) => {
        const textContent = el.textContent || '';
        const id = el.id || '';
        return (
          id.includes('flowchart-' + cleanId + '-') ||
          id === cleanId ||
          id.includes(cleanId) ||
          textContent.includes(cleanId) ||
          (rawNode && id.includes(rawNode))
        );
      });

      if (targetNode) {
        const shape = targetNode.querySelector('rect, circle, polygon, path') as HTMLElement;
        if (shape) {
          if (isCurrent) {
            // 当前激活节点：高亮翠绿脉冲
            shape.style.stroke = !topologyValid && isLastStep ? '#ef4444' : '#10b981';
            shape.style.strokeWidth = '3.5px';
            shape.style.fill = !topologyValid && isLastStep ? '#fee2e2' : '#d1fae5';
            shape.style.filter = 'drop-shadow(0 4px 6px rgba(16, 185, 129, 0.4))';
          } else {
            // 已走过的节点：温和背景
            shape.style.stroke = '#059669';
            shape.style.strokeWidth = '2px';
            shape.style.fill = '#ecfdf5';
          }
        }
      }
    }
  
    // 如果外部指定了 activeNodeId，给予强聚焦高亮
    if (activeNodeId) {
      const activeEl = Array.from(allNodes).find((el) => {
        const textContent = el.textContent || '';
        const id = el.id || '';
        return (
          id.includes('flowchart-' + activeNodeId + '-') ||
          id === activeNodeId ||
          id.includes(activeNodeId) ||
          textContent.includes(activeNodeId)
        );
      });
      if (activeEl) {
        const shape = activeEl.querySelector('rect, circle, polygon, path') as HTMLElement;
        if (shape) {
          shape.style.stroke = '#2563eb';
          shape.style.strokeWidth = '4px';
          shape.style.fill = '#dbeafe';
          shape.style.filter = 'drop-shadow(0 0 10px rgba(37, 99, 235, 0.7))';
        }
      }
    }
  }, [svgContent, currentStep, trajectory, topologyValid, activeNodeId, onNodeClick]);

  // 自动播放与步进定时器
  useEffect(() => {
    if (isPlaying) {
      timerRef.current = setInterval(() => {
        setCurrentStep((prev) => {
          if (prev >= trajectory.length - 1) {
            setIsPlaying(false);
            return prev;
          }
          return prev + 1;
        });
      }, 1000);
    } else {
      if (timerRef.current) clearInterval(timerRef.current);
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [isPlaying, trajectory.length]);

  useEffect(() => {
    if (autoPlay && trajectory.length > 0) {
      setCurrentStep(0);
      setIsPlaying(true);
    } else if (trajectory.length > 0) {
      // 默认展示全部轨迹收口
      setCurrentStep(trajectory.length - 1);
    } else {
      setCurrentStep(-1);
    }
  }, [autoPlay, trajectory]);

  // 全局拖拽事件监听（保证鼠标移出容器仍能丝滑拖拽）
  useEffect(() => {
    if (!isDragging) return;

    const handleWindowMouseMove = (e: MouseEvent) => {
      setPan({
        x: e.clientX - dragStartRef.current.x,
        y: e.clientY - dragStartRef.current.y,
      });
    };

    const handleWindowMouseUp = () => {
      setIsDragging(false);
    };

    window.addEventListener('mousemove', handleWindowMouseMove);
    window.addEventListener('mouseup', handleWindowMouseUp);

    return () => {
      window.removeEventListener('mousemove', handleWindowMouseMove);
      window.removeEventListener('mouseup', handleWindowMouseUp);
    };
  }, [isDragging]);

  // 鼠标滚轮缩放监听 (passive: false 拦截页面联动滚动)
  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;

    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const zoomFactor = e.deltaY < 0 ? 1.12 : 0.88;
      setScale((prev) => Math.min(Math.max(Number((prev * zoomFactor).toFixed(2)), 0.2), 3.0));
    };

    el.addEventListener('wheel', onWheel, { passive: false });
    return () => {
      el.removeEventListener('wheel', onWheel);
    };
  }, []);

  const handleMouseDown = (e: React.MouseEvent) => {
    if (e.button !== 0) return; // 仅左键拖拽
    setIsDragging(true);
    dragStartRef.current = {
      x: e.clientX - pan.x,
      y: e.clientY - pan.y,
    };
  };

  const handleResetView = () => {
    setScale(1);
    setPan({ x: 0, y: 0 });
  };

  const handleZoomIn = () => {
    setScale((prev) => Math.min(Number((prev + 0.15).toFixed(2)), 3.0));
  };

  const handleZoomOut = () => {
    setScale((prev) => Math.max(Number((prev - 0.15).toFixed(2)), 0.2));
  };

  const handleReset = () => {
    setIsPlaying(false);
    setCurrentStep(0);
  };

  const handleStepNext = () => {
    setIsPlaying(false);
    setCurrentStep((prev) => Math.min(prev + 1, trajectory.length - 1));
  };

  return (
    <div className="flex flex-col h-full bg-white border border-slate-200 rounded-lg shadow-sm overflow-hidden select-none">
      {/* 顶部控制栏 */}
      <div className="flex items-center justify-between px-3 py-2 bg-slate-50 border-b border-slate-200 gap-2 shrink-0">
        <div className="flex items-center gap-2 overflow-hidden">
          <Text strong className="text-slate-700 text-xs uppercase tracking-wide shrink-0">
            SOP 状态机拓扑
          </Text>
          {trajectory.length > 0 && (
            <Tag color={topologyValid ? 'success' : 'error'} className="shrink-0">
              {topologyValid ? '拓扑合法' : '拓扑断路/违规'}
            </Tag>
          )}
          {topologyError && (
            <Text type="danger" className="text-xs truncate max-w-[160px]" title={topologyError}>
              {topologyError}
            </Text>
          )}
        </div>

        <div className="flex items-center gap-2">
          {/* SOP 状态机节点推进轨迹动态回放控制栏 */}
          {trajectory.length > 0 ? (
            <div className="flex items-center gap-1.5 bg-blue-50/70 border border-blue-200 px-2 py-0.5 rounded text-xs shrink-0">
              <Tooltip title="【SOP节点流转动态回放】点击播放/暂停，按秒级动画在流程图上高亮此通对话经历的状态机节点，可直观检查是否跳步或断路">
                <span className="font-medium text-blue-900 cursor-help">
                  SOP轨迹回放:
                </span>
              </Tooltip>
              <Tag color="processing" className="m-0 text-3xs font-mono">
                {currentStep >= 0 && trajectory[currentStep] ? `${currentStep + 1}/${trajectory.length} (${trajectory[currentStep]})` : `0/${trajectory.length}`}
              </Tag>
              <Tooltip title={isPlaying ? '暂停流程图轨迹动画' : '播放流程图节点推进动画'}>
                <Button
                  size="small"
                  type="primary"
                  ghost
                  icon={isPlaying ? <PauseCircleOutlined /> : <PlayCircleOutlined />}
                  onClick={() => setIsPlaying(!isPlaying)}
                  className="text-xs h-6 px-1.5"
                >
                  {isPlaying ? '暂停' : '播放'}
                </Button>
              </Tooltip>
              <Tooltip title="单步推进到下一个状态节点">
                <Button
                  size="small"
                  icon={<StepForwardOutlined />}
                  onClick={handleStepNext}
                  disabled={currentStep >= trajectory.length - 1}
                  className="text-xs h-6 px-1.5"
                />
              </Tooltip>
              <Tooltip title="重置到初始开场节点">
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  onClick={handleReset}
                  className="text-xs h-6 px-1.5"
                />
              </Tooltip>
            </div>
          ) : (
            <Tooltip title="对弈完成后，法官将自动提取整通对话走过的 SOP 状态节点并支持动态步进回放">
              <span className="text-3xs text-slate-400 bg-slate-100 px-2 py-1 rounded cursor-help">
                待对弈生成 SOP 节点流转轨迹
              </span>
            </Tooltip>
          )}

          {/* 画布缩放平移与复位工具栏 */}
          <div className="flex items-center gap-1 bg-white px-1.5 py-0.5 rounded border border-slate-200 shadow-2xs shrink-0">
            <Tooltip title="缩小画布 (滚轮向后滑)">
              <Button
                size="small"
                type="text"
                icon={<ZoomOutOutlined />}
                onClick={handleZoomOut}
                disabled={scale <= 0.2}
                className="text-slate-600 hover:text-slate-900"
              />
            </Tooltip>
            <span className="text-3xs font-mono text-slate-500 w-9 text-center select-none">
              {Math.round(scale * 100)}%
            </span>
            <Tooltip title="放大画布 (滚轮向前滑)">
              <Button
                size="small"
                type="text"
                icon={<ZoomInOutlined />}
                onClick={handleZoomIn}
                disabled={scale >= 3.0}
                className="text-slate-600 hover:text-slate-900"
              />
            </Tooltip>
            <Tooltip title="复位视角 (可双击画布快速复位)">
              <Button
                size="small"
                type="text"
                icon={<FullscreenExitOutlined />}
                onClick={handleResetView}
                className="text-slate-600 hover:text-slate-900"
              />
            </Tooltip>
          </div>
        </div>
      </div>

      {/* Mermaid SVG 画布容器：支持鼠标按住拖拽、双击复位、滚轮缩放 */}
      <div
        ref={viewportRef}
        className="flex-1 relative overflow-hidden flex items-center justify-center bg-slate-50/40"
        style={{ cursor: isDragging ? 'grabbing' : 'grab' }}
        onMouseDown={handleMouseDown}
        onDoubleClick={handleResetView}
      >
        <div
          ref={containerRef}
          className="transition-transform duration-75 flex items-center justify-center"
          style={{
            transform: `translate(${pan.x}px, ${pan.y}px) scale(${scale})`,
            transformOrigin: 'center center',
            pointerEvents: isDragging ? 'none' : 'auto',
          }}
          dangerouslySetInnerHTML={{ __html: svgContent }}
        />

        {/* 提示微字 */}
        <div className="absolute bottom-2 right-2 text-3xs text-slate-400 bg-white/80 px-2 py-0.5 rounded pointer-events-none border border-slate-200/60 shadow-2xs">
          按住鼠标拖拽 · 滚轮缩放 · 双击复位
        </div>
      </div>

      {/* 底部轨迹展示 */}
      {trajectory.length > 0 && (
        <div className="px-3 py-1.5 bg-slate-50 border-t border-slate-200 text-xs flex items-center gap-1.5 flex-wrap shrink-0">
          <span className="text-slate-500 font-medium shrink-0">执行轨迹:</span>
          {trajectory.map((node, idx) => (
            <React.Fragment key={idx}>
              <span
                className={`px-1.5 py-0.5 rounded cursor-pointer transition-all text-xs ${
                  idx === currentStep
                    ? 'bg-emerald-600 text-white font-bold shadow-sm'
                    : idx < currentStep
                    ? 'bg-emerald-100 text-emerald-800'
                    : 'bg-slate-200 text-slate-600'
                }`}
                onClick={() => {
                  setIsPlaying(false);
                  setCurrentStep(idx);
                }}
              >
                {node}
              </span>
              {idx < trajectory.length - 1 && <span className="text-slate-400">➔</span>}
            </React.Fragment>
          ))}
        </div>
      )}
    </div>
  );
};
