import { useEffect, useState } from 'react';
import { settingsApi } from '../api/settings';

// 模型卡片：显示模型信息 + 能力标签 + 测试连通性（绿勾✓/红叉✗）+ 修改/删除
export function ModelCard({ model, onEdit, onDelete }) {
  const [testState, setTestState] = useState(null); // { loading } | { ok, detail }
  const [testing, setTesting] = useState(false);
  const [caps, setCaps] = useState(model.capabilities || []);

  // 编辑保存后 model 变化时同步能力标签
  useEffect(() => {
    setCaps(model.capabilities || []);
  }, [model]);

  const capsLabel = caps.length === 0
    ? '未确认'
    : [caps.includes('chat') ? '对话' : null, caps.includes('embedding') ? '嵌入' : null]
        .filter(Boolean)
        .join(' + ');

  const handleTest = async () => {
    setTesting(true);
    setTestState(null);
    try {
      const res = await settingsApi.testModel(model.id);
      setTestState({ ok: res.ok, detail: res.detail });
      // 后端探测能力写回并返回，同步能力标签
      if (Array.isArray(res.capabilities)) setCaps(res.capabilities);
    } catch (e) {
      setTestState({ ok: false, detail: e.response?.data?.detail || '测试请求失败' });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="border border-slate-200 rounded-xl p-4 mb-2 shadow-card hover:shadow-lift transition">
      <div className="flex items-center justify-between">
        <span className="font-semibold text-slate-800">{model.name}</span>
        <span className="flex items-center gap-2">
          <span className={`text-xs px-2 py-0.5 rounded-full border ${caps.length ? 'bg-indigo-50 text-indigo-600 border-indigo-200' : 'bg-slate-50 text-slate-400 border-slate-200'}`}>
            {capsLabel}
          </span>
          <span className="text-sm text-slate-500">{model.provider}</span>
        </span>
      </div>
      <p className="text-sm text-slate-500">{model.model}</p>
      <div className="flex items-center gap-2 mt-3">
        <button
          onClick={handleTest}
          disabled={testing}
          className="btn-secondary !py-1 text-xs"
        >
          {testing ? '测试中…' : '测试'}
        </button>
        <button
          onClick={() => onEdit(model)}
          className="btn-secondary !py-1 text-xs"
        >
          修改
        </button>
        <button
          onClick={() => onDelete(model)}
          className="bg-red-50 text-red-600 border border-red-200 rounded-lg px-3 py-1 text-xs hover:bg-red-100"
        >
          删除
        </button>
      </div>
      {testState && (
        <p className={`text-sm mt-2 flex items-start gap-1 ${testState.ok ? 'text-green-600' : 'text-red-500'}`}>
          <span>{testState.ok ? '✓' : '✗'}</span>
          <span className="leading-snug break-words">{testState.detail}</span>
        </p>
      )}
    </div>
  );
}
