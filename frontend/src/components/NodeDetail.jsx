import { useState } from 'react';

export function NodeDetail({ node, onClose, onDelete }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');

  return (
    <div className="absolute top-4 right-4 w-80 bg-white p-5 rounded-xl shadow-lift border border-slate-200">
      <h3 className="font-bold text-slate-800 mb-2">{node.name}</h3>
      <div className="text-sm text-slate-600 space-y-1">
        <p>类型：<span className="text-slate-800">{node.label}</span></p>
        <p>别名：{node.properties?.aliases?.join(', ') || '无'}</p>
        <p>描述：{node.properties?.summary || '暂无'}</p>
      </div>
      <div className="mt-4">
        <p className="text-sm text-slate-500 mb-2">删除节点需要输入密码：</p>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="input-base mb-2"
        />
        {error && <p className="text-red-500 text-sm mb-2">{error}</p>}
        <button
          onClick={async () => {
            try {
              await onDelete(node.id, password);
            } catch (e) {
              setError(e.response?.data?.detail || '删除失败');
            }
          }}
          className="bg-red-500 text-white px-3 py-1.5 rounded-lg text-sm mr-2 hover:bg-red-600"
        >
          删除节点
        </button>
        <button onClick={onClose} className="btn-secondary">
          关闭
        </button>
      </div>
    </div>
  );
}
