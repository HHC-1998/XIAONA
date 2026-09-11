import { useMemo, useRef } from 'react';
import ForceGraph3D from 'react-force-graph-3d';
import * as THREE from 'three';

// 3D 力导向图包装组件（对标 3d-force-graph 官方风格，vasturiano/3d-force-graph）
// 白底浅色主题：节点按 label 自动着色、半透明球、hover 放大并显示标签、自转/拖拽/缩放；
// 节点大小按关联度（度数）决定；自我节点（is_self）用渐变发光球高亮。
// width/height 由父组件显式传入（Graph 页用 ResizeObserver 量取），保证画布填满可视区。
export function Graph3D({ data, onNodeClick, width, height }) {
  const fgRef = useRef();
  const hoveredNodeRef = useRef(null);

  // 度数（关联度）统计：决定 nodeVal 与节点大小
  const degreeMap = useMemo(() => {
    const m = {};
    (data.links || []).forEach((l) => {
      const s = typeof l.source === 'object' ? l.source.id : l.source;
      const t = typeof l.target === 'object' ? l.target.id : l.target;
      m[s] = (m[s] || 0) + 1;
      m[t] = (m[t] || 0) + 1;
    });
    return m;
  }, [data]);

  const isSelf = (node) => node?.properties?.is_self === true;

  // 布局预处理：自我节点固定在原点（引力中心），其余节点分布在球壳上，
  // 保证打开关系网时 HHC 恒在屏幕中心、其它节点围绕它（03 §9）。
  const graphData = useMemo(() => {
    const self = (data.nodes || []).find((n) => n.properties?.is_self === true);
    const nodes = (data.nodes || []).map((n) => {
      if (self && n.id === self.id) {
        return { ...n, fx: 0, fy: 0, fz: 0, val: 40 };
      }
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      const r = 60 + Math.random() * 40;
      return {
        ...n,
        x: r * Math.sin(phi) * Math.cos(theta),
        y: r * Math.sin(phi) * Math.sin(theta),
        z: r * Math.cos(phi),
      };
    });
    return { nodes, links: data.links || [] };
  }, [data]);

  // 普通节点：按度数定大小（关联度越高略大）；自我节点保持大质量作为引力中心
  const nodeVal = (node) => {
    if (isSelf(node)) return 40;
    return Math.max(0.6, Math.sqrt(degreeMap[node.id] || 0) * 0.35 + 0.3);
  };

  // hover 放大节点（3d-force-graph 官方交互）
  const handleNodeHover = (node) => {
    if (hoveredNodeRef.current && hoveredNodeRef.current.__threeObj) {
      hoveredNodeRef.current.__threeObj.scale.set(1, 1, 1);
    }
    if (node && node.__threeObj) {
      node.__threeObj.scale.set(1.6, 1.6, 1.6);
    }
    hoveredNodeRef.current = node;
  };

  // 自我节点：渐变发光球（半透明外层 + 高亮内核）
  const nodeThreeObject = (node) => {
    if (!isSelf(node)) return undefined; // 非自我节点走默认球体（按 label 着色）
    const group = new THREE.Group();
    const halo = new THREE.Mesh(
      new THREE.SphereGeometry(2.4, 32, 32),
      new THREE.MeshBasicMaterial({ color: 0x818cf8, transparent: true, opacity: 0.18 }),
    );
    const core = new THREE.Mesh(
      new THREE.SphereGeometry(1.7, 32, 32),
      new THREE.MeshPhongMaterial({
        color: 0x6366f1,
        emissive: 0x4f46e5,
        emissiveIntensity: 0.45,
        transparent: true,
        opacity: 0.95,
      }),
    );
    group.add(halo, core);
    return group;
  };

  const isEmpty = !data.nodes || data.nodes.length === 0;

  return (
    <div className="absolute inset-0">
      <ForceGraph3D
        ref={fgRef}
        graphData={graphData}
        width={width}
        height={height}
        backgroundColor="#ffffff"
        nodeAutoColorBy="label"
        nodeLabel="name"
        nodeVal={nodeVal}
        nodeRelSize={1.6}
        nodeOpacity={0.85}
        nodeResolution={24}
        nodeThreeObject={nodeThreeObject}
        // 边：简洁静态连线（无箭头/粒子/曲率），悬停显示关系类型（03 §9）
        linkLabel={(l) => (l.type ? `关系：${l.type}` : '关系')}
        linkColor={() => 'rgba(99, 102, 241, 0.55)'}
        linkWidth={1.5}
        linkHoverPrecision={10}
        onNodeHover={handleNodeHover}
        onNodeClick={onNodeClick}
        // 相机对准原点：打开即从中心看自我节点，autoRotate 绕它自转
        cameraPosition={{ x: 0, y: 0, z: 150 }}
        onReady={() => {
          fgRef.current?.centerAt([0, 0, 0], 0);
          fgRef.current?.camera()?.lookAt(0, 0, 0);
        }}
        autoRotate
        autoRotateSpeed={0.6}
        warmupTicks={60}
      />
      {isEmpty && (
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
          <p className="text-sm text-slate-400">图谱还没有节点，先和 XIAONA 聊几句吧</p>
        </div>
      )}
    </div>
  );
}
