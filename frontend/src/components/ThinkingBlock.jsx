import { useEffect, useRef, useState } from 'react';

// 思考过程折叠块（05 §12）：顶部「深度思考」标题 + 折叠箭头。
// 收起时只显示前几行（line-clamp），点击展开看完整思考过程；流式中自动滚到底部。
export function ThinkingBlock({ thinking, streaming = false }) {
  const [expanded, setExpanded] = useState(false);
  const bodyRef = useRef(null);

  // 流式思考输出时自动展开 + 跟随到底部，让用户看到思考过程逐字往下走；
  // 思考结束后保持展开，用户可手动收起。
  useEffect(() => {
    if (streaming) setExpanded(true);
  }, [streaming]);

  // 流式思考输出时自动跟随到底部，让用户看到思考过程一直在往下走
  useEffect(() => {
    if (streaming && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [thinking, streaming]);

  return (
    <div className="mb-2 rounded-xl border border-amber-200 bg-amber-50/70 overflow-hidden">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center justify-between px-3 py-2 text-xs font-medium text-amber-800 bg-amber-100/60 hover:bg-amber-100"
      >
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
          深度思考
          {streaming && <span className="font-normal text-amber-500">思考中…</span>}
        </span>
        <span className="text-amber-600">{expanded ? '收起 ▲' : '展开 ▼'}</span>
      </button>
      <div
        ref={bodyRef}
        className={`px-3 py-2 text-xs text-slate-600 whitespace-pre-wrap ${
          expanded ? 'max-h-96 overflow-y-auto' : 'line-clamp-3 overflow-hidden'
        }`}
      >
        {thinking || '正在思考…'}
      </div>
    </div>
  );
}
