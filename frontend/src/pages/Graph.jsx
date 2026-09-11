import { useEffect, useState, useRef, useLayoutEffect } from 'react';
import { memoryApi } from '../api/memory';
import { NodeDetail } from '../components/NodeDetail';
import { Graph3D } from '../components/Graph3D';
import { useModelStatus } from '../hooks/useModelStatus';

export default function Graph() {
  const containerRef = useRef(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [data, setData] = useState({ nodes: [], links: [] });
  const [selectedNode, setSelectedNode] = useState(null);
  const { chat_configured: chatConfigured } = useModelStatus();

  useEffect(() => {
    memoryApi.getGraph().then((r) => setData(r));
  }, []);

  // 量取容器真实尺寸，显式传给 3D 画布（填满可视区、无滚动条；窗口变化时跟随）
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const update = () => {
      const rect = el.getBoundingClientRect();
      setSize({ width: rect.width, height: rect.height });
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const handleNodeClick = (node) => {
    setSelectedNode(node);
  };

  const handleDeleteNode = async (nodeId, password) => {
    await memoryApi.deleteNode(nodeId, password);
    // 兼容 source/target 为 id 字符串或节点对象两种形态
    const srcId = (l) => (typeof l.source === 'object' ? l.source?.id : l.source);
    const tgtId = (l) => (typeof l.target === 'object' ? l.target?.id : l.target);
    setData((prev) => ({
      nodes: prev.nodes.filter((n) => n.id !== nodeId),
      links: prev.links.filter((l) => srcId(l) !== nodeId && tgtId(l) !== nodeId),
    }));
    setSelectedNode(null);
  };

  return (
    <div ref={containerRef} className="absolute inset-0 bg-white">
      <Graph3D data={data} onNodeClick={handleNodeClick} width={size.width} height={size.height} />
      {!chatConfigured && (
        <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 px-4 py-2 bg-amber-50 border border-amber-200 text-amber-700 text-sm rounded-lg shadow-card">
          您还没有配置对话模型，请到
          <a href="/settings" className="mx-1 underline font-medium">设置页</a>
          配置后再开始对话。
        </div>
      )}
      {selectedNode && (
        <NodeDetail
          node={selectedNode}
          onClose={() => setSelectedNode(null)}
          onDelete={handleDeleteNode}
        />
      )}
    </div>
  );
}
