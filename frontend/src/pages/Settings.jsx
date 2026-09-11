import { useEffect, useState, useRef } from 'react';
import { settingsApi } from '../api/settings';
import { authApi } from '../api/auth';
import { api } from '../api/client';
import { useAuth } from '../hooks/useAuth';
import { ModelCard } from '../components/ModelCard';
import { DeleteConfirm } from '../components/DeleteConfirm';

// 模型厂商下拉：市面上主流 AI 厂商（统一走 OpenAI 兼容协议），末尾 custom 自定义。
// 2026-08-15 简化：Base URL / 模型名 / API Key 全部由用户手填，不再自动填充或扫描。
const PROVIDERS = [
  { value: 'openai', label: 'OpenAI' },
  { value: 'anthropic', label: 'Claude / Anthropic' },
  { value: 'google', label: 'Google Gemini' },
  { value: 'ollama', label: 'Ollama（本地）' },
  { value: 'moonshot', label: 'Kimi / Moonshot' },
  { value: 'deepseek', label: 'DeepSeek' },
  { value: 'zhipu', label: '智谱 GLM' },
  { value: 'qwen', label: '通义千问 Qwen' },
  { value: 'baidu', label: '文心 ERNIE' },
  { value: 'doubao', label: '豆包' },
  { value: 'mistral', label: 'Mistral' },
  { value: 'groq', label: 'Groq' },
  { value: 'xai', label: 'Grok / xAI' },
  { value: 'openrouter', label: 'OpenRouter' },
  { value: 'together', label: 'Together AI' },
  { value: 'siliconflow', label: '硅基流动' },
  { value: 'huggingface', label: 'Hugging Face' },
  { value: 'azure', label: 'Azure OpenAI' },
  { value: 'localai', label: 'LocalAI' },
];
const CUSTOM_PROVIDER = 'custom';   // 下拉找不到时自定义厂商标识

export default function Settings() {
  const { setUser } = useAuth();   // 绑定手机号成功后刷新全局用户信息
  const [models, setModels] = useState([]);
  const [showForm, setShowForm] = useState(false);
  const [editingModel, setEditingModel] = useState(null);   // 正在编辑的模型对象（null=新增模式）
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [newModel, setNewModel] = useState({
    name: '',
    provider: 'openai',
    base_url: '',   // Base URL 由用户手填（不自动填充）
    api_key: '',
    model: '',
    temperature: 0.7,
    max_tokens: 2048,
    capabilities: [],   // 能力由后端 test 时自动探测写回，添加时无需选择类型
    customProvider: '',   // provider 选 custom 时自定义的厂商标识
  });
  const [functionModels, setFunctionModels] = useState({
    chat_model_id: '',
    extract_model_id: '',
    embedding_model_id: '',
    executor_model_id: '',
    reviewer_model_id: '',
    chat_configured: false,
    extract_configured: false,
    embedding_configured: false,
    executor_configured: false,
    reviewer_configured: false,
  });
  // 电商数据源 API key（02 爬取 §7.1，2026-08-19 起）：Amazon 内置无 key；Etsy/Pinterest 等按用户填写
  const [dataSources, setDataSources] = useState({});
  const [etsyKey, setEtsyKey] = useState('');
  const [etsySaving, setEtsySaving] = useState(false);
  const [pinterestKey, setPinterestKey] = useState('');
  const [pinterestSaving, setPinterestSaving] = useState(false);

  // 账号绑定（为账号补录手机号 + 密码；真正的飞书绑定在飞书对话框内三步完成，10 §5.1）
  const [bindPhone, setBindPhone] = useState('');
  const [bindCode, setBindCode] = useState('');
  const [bindPassword, setBindPassword] = useState('');
  const [bindCountdown, setBindCountdown] = useState(0);
  const bindTimerRef = useRef(null);   // 绑定验证码倒计时定时器（卸载清理，防 setInterval 泄漏）
  // 飞书绑定状态：设置页展示已绑定身份列表 + 解绑/改密（需手机号+验证码确认）
  const [feishuBindStatus, setFeishuBindStatus] = useState({ phone_masked: '', bound: [] });
  const [confirmMode, setConfirmMode] = useState(null);    // 'unbind' | 'change_pwd'
  const [unbindTarget, setUnbindTarget] = useState(null);  // 正在解绑的 open_id_short
  const [confirmPhone, setConfirmPhone] = useState('');
  const [confirmCode, setConfirmCode] = useState('');
  const [confirmCountdown, setConfirmCountdown] = useState(0);
  const [newPassword, setNewPassword] = useState('');
  const confirmTimerRef = useRef(null);  // 解绑/改密验证码倒计时定时器
  useEffect(
    () => () => {
      clearInterval(bindTimerRef.current);
      clearInterval(confirmTimerRef.current);
    },
    []
  );

  useEffect(() => {
    settingsApi.getModels().then((r) => setModels(r.models));
    settingsApi.getFunctionModels().then((r) => setFunctionModels(r));
    settingsApi.getFeishuBindStatus().then((r) => setFeishuBindStatus(r)).catch(() => {});
    settingsApi.getDataSources().then((r) => setDataSources(r.datasources || {})).catch(() => {});
  }, []);

  const emptyModel = () => ({
    name: '',
    provider: 'openai',
    base_url: '',
    api_key: '',
    model: '',
    temperature: 0.7,
    max_tokens: 2048,
    capabilities: [],   // 能力由后端 test 时自动探测写回，添加时无需选择类型
    customProvider: '',
  });

  // 打开编辑表单（预填当前模型字段；provider 不在已知列表时落到 custom 并回填）
  const openEdit = (model) => {
    setEditingModel(model);
    const known = PROVIDERS.some((p) => p.value === model.provider);
    setNewModel({
      name: model.name,
      provider: known ? model.provider : CUSTOM_PROVIDER,
      base_url: model.base_url || '',
      api_key: model.api_key || '',
      model: model.model || '',
      temperature: model.temperature ?? 0.7,
      max_tokens: model.max_tokens ?? 2048,
      capabilities: model.capabilities || [],
      customProvider: known ? '' : (model.provider || ''),
    });
    setShowForm(true);
  };

  // 保存（新增或修改共用表单）。保存后自动调用测试接口探测能力写回（前端无需手动选类型）
  const saveModel = async () => {
    if (!newModel.name) return alert('请填写模型名称');
    if (!newModel.model) return alert('请填写模型名');
    // provider 选 custom 时用自定义厂商标识（未填则默认 custom）
    const { customProvider, ...rest } = newModel;
    const payload = {
      ...rest,
      provider:
        rest.provider === CUSTOM_PROVIDER ? (customProvider.trim() || CUSTOM_PROVIDER) : rest.provider,
    };
    let saved;
    if (editingModel) {
      saved = await settingsApi.updateModel(editingModel.id, payload);
    } else {
      saved = await settingsApi.addModel(payload);
    }
    // 保存后自动探测能力（test_model 内部写回 capabilities 并返回），刷新模型列表
    try {
      const t = await settingsApi.testModel(saved.id);
      saved = { ...saved, capabilities: t.capabilities || saved.capabilities || [] };
    } catch (e) {
      // 探测失败不阻断保存（能力留空，功能分配时会再探测）
    }
    setModels((prev) => {
      const exists = prev.some((m) => m.id === saved.id);
      return exists ? prev.map((m) => (m.id === saved.id ? saved : m)) : [...prev, saved];
    });
    setEditingModel(null);
    setNewModel(emptyModel());
    setShowForm(false);
  };

  // 删除模型（若被功能分配引用，后端会同步置空分配）
  const handleDeleteModel = async (model) => {
    await settingsApi.deleteModel(model.id);
    setModels((prev) => prev.filter((m) => m.id !== model.id));
    // 删除后重拉功能分配，同步刷新 configured 提示状态
    const r = await settingsApi.getFunctionModels();
    setFunctionModels(r);
  };

  const assignModel = async (func, modelId) => {
    // 乐观更新：选择/取消立即更新下拉与 configured 提示行（不等待后端能力探测）
    const prev = functionModels;
    // func 形如 chat_model_id / extract_model_id / embedding_model_id，去 _model_id 得
    // chat / extract / embedding 才是 configured 字段前缀（此前用 replace('_id','') 会把
    // chat_model_id 错拼成 chat_model_configured，提示行永不刷新，2026-08-17 修）
    const flag = `${func.replace('_model_id', '')}_configured`;
    setFunctionModels((p) => ({ ...p, [func]: modelId, [flag]: !!modelId }));
    try {
      await settingsApi.assignFunctionModel(func, modelId);
    } catch (e) {
      // 后端校验拒绝（如能力不匹配 400）→ 回滚旧值 + 提示原因
      setFunctionModels(prev);
      alert(e.response?.data?.detail || '分配失败，请重试');
    }
  };

  // 功能分配下拉过滤：聊天/记忆抽取只列对话模型，向量化只列向量化模型。
  // 能力未确认（添加后没测过）的模型允许选（分配时后端会再探测校验）；当前已选值始终保留。
  const filterByCapability = (list, cap, currentId) =>
    list.filter((m) => !m.capabilities?.length || m.capabilities.includes(cap) || m.id === currentId);

  const clearData = async (type, password) => {
    await settingsApi.clearData(type, password);
    alert('已清空');
  };

  const sendBindCode = async () => {
    if (!bindPhone) return alert('请填写手机号');
    const res = await authApi.sendCode(bindPhone);
    // mock 模式（SMS_MOCK=1）后端直接把验证码随响应返回：自动填入 + 提示，与 Login/Register
    // 一致，否则 mock 下用户看不到验证码无法完成绑定（2026-08-17 修）
    if (res?.code) {
      setBindCode(res.code);
      alert(`演示模式，验证码已自动填入：${res.code}`);
    }
    setBindCountdown(60);
    clearInterval(bindTimerRef.current);   // 防连续点击叠加多个倒计时定时器
    bindTimerRef.current = setInterval(() => {
      setBindCountdown((c) => {
        if (c <= 1) { clearInterval(bindTimerRef.current); return 0; }
        return c - 1;
      });
    }, 1000);
  };

  const bindAccount = async () => {
    if (!bindPhone || !bindCode || !bindPassword) return alert('请填写手机号、验证码和新密码');
    await settingsApi.bindPhone({ phone: bindPhone, code: bindCode, password: bindPassword });
    setUser(await authApi.me());   // 刷新全局用户（昵称/手机号），顶部导航同步更新
    alert('绑定成功，可用手机号+密码登录 Web/CLI');
    setBindPhone('');
    setBindCode('');
    setBindPassword('');
    const r = await settingsApi.getFeishuBindStatus();
    setFeishuBindStatus(r);
  };

  // 解绑/改密确认表单的验证码（手机号 + 验证码双重确认，防误操作）
  const sendConfirmCode = async () => {
    if (!confirmPhone) return alert('请填写确认手机号');
    const res = await authApi.sendCode(confirmPhone);
    if (res?.code) {
      setConfirmCode(res.code);
      alert(`演示模式，验证码已自动填入：${res.code}`);
    }
    setConfirmCountdown(60);
    clearInterval(confirmTimerRef.current);   // 防连续点击叠加多个倒计时定时器
    confirmTimerRef.current = setInterval(() => {
      setConfirmCountdown((c) => {
        if (c <= 1) { clearInterval(confirmTimerRef.current); return 0; }
        return c - 1;
      });
    }, 1000);
  };

  const confirmAction = async () => {
    if (!confirmPhone || !confirmCode) return alert('请填写手机号和验证码');
    try {
      if (confirmMode === 'unbind') {
        if (!unbindTarget) return;
        await settingsApi.unbindFeishu({ openId: unbindTarget, phone: confirmPhone, code: confirmCode });
        alert('已解绑该飞书身份');
      } else {
        if (!newPassword || newPassword.length < 6) return alert('新密码至少 6 位');
        await settingsApi.changePassword({ phone: confirmPhone, code: confirmCode, newPassword });
        alert('密码修改成功');
      }
      setConfirmMode(null);
      setUnbindTarget(null);
      setNewPassword('');
      setConfirmPhone('');
      setConfirmCode('');
      const r = await settingsApi.getFeishuBindStatus();
      setFeishuBindStatus(r);
    } catch (e) {
      alert(e.response?.data?.detail || '操作失败，请重试');
    }
  };

  // 导出全部数据（09 §4.1）：下载 xiaona_backup.zip
  const exportAll = async () => {
    const res = await fetch('/api/data/export', {
      headers: { Authorization: `Bearer ${localStorage.getItem('token')}` },
    });
    if (!res.ok) return alert('导出失败');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'xiaona_backup.zip';
    a.click();
    URL.revokeObjectURL(url);
  };

  // 导入备份（09 §5）：需当前用户本人备份
  const importBackup = async (file) => {
    const form = new FormData();
    form.append('file', file);
    try {
      await api.post('/api/data/import', form);
      alert('导入成功');
    } catch (e) {
      alert(e.response?.data?.detail || '导入失败');
    }
  };

  // 保存 Etsy API key（02 爬取 §7.1，2026-08-19 起）：按当前用户存数据库
  const saveEtsyKey = async () => {
    if (!etsyKey.trim()) return alert('请填写 Etsy API key');
    setEtsySaving(true);
    try {
      const r = await settingsApi.saveDataSource({ source: 'etsy', apiKey: etsyKey.trim() });
      setDataSources((prev) => ({ ...prev, etsy: r }));
      setEtsyKey('');
      alert(r.configured ? '已保存 Etsy API key' : '已清除 Etsy API key');
    } catch (e) {
      alert(e.response?.data?.detail || '保存失败，请重试');
    } finally {
      setEtsySaving(false);
    }
  };

  // 保存 Pinterest API token（02 爬取 §7.1，2026-08-24 起）：按当前用户存数据库
  const savePinterestKey = async () => {
    if (!pinterestKey.trim()) return alert('请填写 Pinterest API token');
    setPinterestSaving(true);
    try {
      const r = await settingsApi.saveDataSource({ source: 'pinterest', apiKey: pinterestKey.trim() });
      setDataSources((prev) => ({ ...prev, pinterest: r }));
      setPinterestKey('');
      alert(r.configured ? '已保存 Pinterest API token' : '已清除 Pinterest API token');
    } catch (e) {
      alert(e.response?.data?.detail || '保存失败，请重试');
    } finally {
      setPinterestSaving(false);
    }
  };

  return (
    <div className="p-6 max-w-3xl mx-auto">
      <h1 className="text-2xl font-bold mb-4">设置</h1>

      <section className="bg-white border border-slate-200 rounded-xl p-6 mb-6 shadow-card">
        <h2 className="font-bold mb-3">模型库</h2>
        {models.map((m) => (
          <ModelCard
            key={m.id}
            model={m}
            onEdit={openEdit}
            onDelete={(model) => {
              if (window.confirm(`确定删除模型「${model.name}」吗？删除后其功能分配将被清空。`)) {
                handleDeleteModel(model);
              }
            }}
          />
        ))}
        {!showForm && (
          <button onClick={() => setShowForm(true)} className="btn-secondary">
            + 添加模型
          </button>
        )}
        {showForm && (
          <div className="border p-4 mt-2 space-y-2">
            <div className="font-semibold text-sm mb-1">{editingModel ? `修改模型：${editingModel.name}` : '添加模型'}</div>
            <input
              className="input-base"
              placeholder="模型名称"
              value={newModel.name}
              onChange={(e) => setNewModel({ ...newModel, name: e.target.value })}
            />
            <select
              className="input-base"
              value={newModel.provider}
              onChange={(e) => {
                const provider = e.target.value;
                setNewModel({
                  ...newModel,
                  provider,
                  customProvider: provider === CUSTOM_PROVIDER ? newModel.customProvider : '',
                });
              }}
            >
              {PROVIDERS.map((p) => (
                <option key={p.value} value={p.value}>{p.label}</option>
              ))}
              <option value={CUSTOM_PROVIDER}>自定义（下拉未列出）</option>
            </select>
            {newModel.provider === CUSTOM_PROVIDER && (
              <input
                className="input-base"
                placeholder="自定义厂商标识（英文，如 ollama-free）"
                value={newModel.customProvider}
                onChange={(e) => setNewModel({ ...newModel, customProvider: e.target.value })}
              />
            )}
            <input
              className="input-base"
              placeholder="Base URL（如 https://api.deepseek.com/v1）"
              value={newModel.base_url}
              onChange={(e) => setNewModel({ ...newModel, base_url: e.target.value })}
            />
            <input
              className="input-base"
              placeholder="模型名（如 deepseek-chat / qwen2.5）"
              value={newModel.model}
              onChange={(e) => setNewModel({ ...newModel, model: e.target.value })}
            />
            <input
              className="input-base"
              placeholder="API Key（可选，本地 Ollama 可留空）"
              type="password"
              value={newModel.api_key}
              onChange={(e) => setNewModel({ ...newModel, api_key: e.target.value })}
            />
            <button onClick={saveModel} className="btn-primary px-4">保存</button>
            <button
              onClick={() => {
                setShowForm(false);
                setEditingModel(null);
              }}
              className="btn-secondary ml-2"
            >
              取消
            </button>
          </div>
        )}
      </section>

      <section className="bg-white border border-slate-200 rounded-xl p-6 mb-6 shadow-card">
        <h2 className="font-bold mb-3">功能模型分配</h2>
        <div className="space-y-2">
          <div>
            <label className="block text-sm">聊天模型</label>
            <select
              className="input-base"
              value={functionModels.chat_model_id}
              onChange={(e) => assignModel('chat_model_id', e.target.value)}
            >
              <option value="">请选择</option>
              {filterByCapability(models, 'chat', functionModels.chat_model_id).map((m) => (
                <option key={m.id} value={m.id}>{m.name}</option>
              ))}
            </select>
            {!functionModels.chat_configured && (
              <p className="mt-1 text-xs text-amber-600">
                未配置对话模型：聊天、知识图谱、桌面助手将无法对话，请先配置。
              </p>
            )}
          </div>
          <div>
            <label className="block text-sm">记忆抽取模型</label>
            <select
              className="input-base"
              value={functionModels.extract_model_id}
              onChange={(e) => assignModel('extract_model_id', e.target.value)}
            >
              <option value="">请选择</option>
              {filterByCapability(models, 'chat', functionModels.extract_model_id).map((m) => (
                <option key={m.id} value={m.id}>{m.name}</option>
              ))}
            </select>
            {!functionModels.extract_configured && (
              <p className="mt-1 text-xs text-amber-600">
                未配置记忆抽取模型：对话内容将不提炼到知识图谱，长期记忆减弱。
              </p>
            )}
          </div>
          <div>
            <label className="block text-sm">向量化模型</label>
            <select
              className="input-base"
              value={functionModels.embedding_model_id}
              onChange={(e) => assignModel('embedding_model_id', e.target.value)}
            >
              <option value="">请选择</option>
              {filterByCapability(models, 'embedding', functionModels.embedding_model_id).map((m) => (
                <option key={m.id} value={m.id}>{m.name}</option>
              ))}
            </select>
            {!functionModels.embedding_configured && (
              <p className="mt-1 text-xs text-amber-600">
                未配置向量化模型：对话内容将不做向量化存储，语义检索减弱。
              </p>
            )}
          </div>
          <div className="pt-2 border-t border-slate-100">
            <label className="block text-sm">执行者模型（多 Agent 任务）</label>
            <select
              className="input-base"
              value={functionModels.executor_model_id}
              onChange={(e) => assignModel('executor_model_id', e.target.value)}
            >
              <option value="">请选择</option>
              {filterByCapability(models, 'chat', functionModels.executor_model_id).map((m) => (
                <option key={m.id} value={m.id}>{m.name}</option>
              ))}
            </select>
            {!functionModels.executor_configured && (
              <p className="mt-1 text-xs text-amber-600">
                未配置执行者模型：执行任务时将回退使用聊天模型。
              </p>
            )}
          </div>
          <div>
            <label className="block text-sm">审核者模型（多 Agent 任务）</label>
            <select
              className="input-base"
              value={functionModels.reviewer_model_id}
              onChange={(e) => assignModel('reviewer_model_id', e.target.value)}
            >
              <option value="">请选择</option>
              {filterByCapability(models, 'chat', functionModels.reviewer_model_id).map((m) => (
                <option key={m.id} value={m.id}>{m.name}</option>
              ))}
            </select>
            {!functionModels.reviewer_configured && (
              <p className="mt-1 text-xs text-amber-600">
                未配置审核者模型：验收产物时将回退使用执行者模型（建议分配不同模型，独立把关）。
              </p>
            )}
          </div>
        </div>
      </section>

      <section className="bg-white border border-slate-200 rounded-xl p-6 mb-6 shadow-card">
        <h2 className="font-bold mb-3">数据源 API</h2>
        <p className="text-sm text-gray-500 mb-4">
          配置电商选品数据源的 API key。配置后可让「北美商品榜单/选品」类任务同时用多个数据源交叉验证；
          未配置的 key 只会影响对应数据源（任务自动改用已配置的数据源）。
        </p>
        <div className="space-y-4">
          <div className="flex items-center justify-between border border-slate-200 rounded-lg px-3 py-2">
            <div>
              <div className="text-sm font-medium">Amazon 北美榜单</div>
              <div className="text-xs text-gray-400">内置数据源，无需 API key，保底可用</div>
            </div>
            <span className="text-xs bg-emerald-50 text-emerald-600 border border-emerald-200 rounded-full px-2.5 py-1">无需配置</span>
          </div>

          <div className="border border-slate-200 rounded-lg px-3 py-2">
            <div className="flex items-center justify-between mb-2">
              <div>
                <div className="text-sm font-medium">Pinterest 趋势</div>
                <div className="text-xs text-gray-400">
                  收藏种草/前瞻趋势词。官方 API v5 免费，用于判断品类在 Pinterest 的趋势
                </div>
              </div>
              {dataSources.pinterest?.configured ? (
                <span className="text-xs bg-emerald-50 text-emerald-600 border border-emerald-200 rounded-full px-2.5 py-1">
                  已配置（{dataSources.pinterest.api_key_masked}）
                </span>
              ) : (
                <span className="text-xs bg-gray-100 text-gray-500 rounded-full px-2.5 py-1">未配置</span>
              )}
            </div>
            <div className="flex gap-2">
              <input
                type="password"
                className="input-base"
                placeholder="Pinterest API token（到 Pinterest 开发者后台创建应用获取）"
                value={pinterestKey}
                onChange={(e) => setPinterestKey(e.target.value)}
              />
              <button onClick={savePinterestKey} disabled={pinterestSaving} className="btn-primary px-4 shrink-0 disabled:opacity-50">
                {pinterestSaving ? '保存中…' : '保存'}
              </button>
              {dataSources.pinterest?.configured && (
                <button
                  onClick={async () => {
                    await settingsApi.saveDataSource({ source: 'pinterest', apiKey: '' });
                    setDataSources((prev) => ({ ...prev, pinterest: { configured: false, api_key_masked: '' } }));
                    alert('已清除 Pinterest API token');
                  }}
                  className="btn-secondary shrink-0"
                >
                  清除
                </button>
              )}
            </div>
            <p className="mt-2 text-xs text-gray-400">
              获取指引：访问
              <a className="text-indigo-600" href="https://developers.pinterest.com/" target="_blank" rel="noreferrer"> Pinterest 开发者后台 </a>
              → 创建应用 → OAuth 授权 scope user_accounts:read → 复制 Bearer token。token 仅保存在当前账号，不会明文回显。
            </p>
          </div>

          <div className="border border-slate-200 rounded-lg px-3 py-2">
            <div className="flex items-center justify-between mb-2">
              <div>
                <div className="text-sm font-medium">Etsy 官方 API</div>
                <div className="text-xs text-gray-400">
                  手作/饰品趋势数据。免费额度 10000 请求/天，用于与 Amazon 榜交叉验证
                </div>
              </div>
              {dataSources.etsy?.configured ? (
                <span className="text-xs bg-emerald-50 text-emerald-600 border border-emerald-200 rounded-full px-2.5 py-1">
                  已配置（{dataSources.etsy.api_key_masked}）
                </span>
              ) : (
                <span className="text-xs bg-gray-100 text-gray-500 rounded-full px-2.5 py-1">未配置</span>
              )}
            </div>
            <div className="flex gap-2">
              <input
                type="password"
                className="input-base"
                placeholder="Etsy API key（到 Etsy 开发者后台注册 Open API v3）"
                value={etsyKey}
                onChange={(e) => setEtsyKey(e.target.value)}
              />
              <button onClick={saveEtsyKey} disabled={etsySaving} className="btn-primary px-4 shrink-0 disabled:opacity-50">
                {etsySaving ? '保存中…' : '保存'}
              </button>
              {dataSources.etsy?.configured && (
                <button
                  onClick={async () => {
                    await settingsApi.saveDataSource({ source: 'etsy', apiKey: '' });
                    setDataSources((prev) => ({ ...prev, etsy: { configured: false, api_key_masked: '' } }));
                    alert('已清除 Etsy API key');
                  }}
                  className="btn-secondary shrink-0"
                >
                  清除
                </button>
              )}
            </div>
            <p className="mt-2 text-xs text-gray-400">
              获取指引：访问
              <a className="text-indigo-600" href="https://www.etsy.com/developers/" target="_blank" rel="noreferrer"> Etsy 开发者后台 </a>
              → 创建应用（OAuth 方式选 Key-only）→ 复制 API Key。key 仅保存在当前账号，不会明文回显。
            </p>
          </div>
        </div>
      </section>

      <section className="bg-white border border-slate-200 rounded-xl p-6 mb-6 shadow-card">
        <h2 className="font-bold mb-3">账号绑定</h2>
        {feishuBindStatus.bound.length > 0 ? (
          <>
            <p className="text-sm text-gray-500 mb-3">
              已连接手机号 <span className="font-semibold text-indigo-600">{feishuBindStatus.phone_masked}</span>，
              一个账号可绑定多个飞书身份（共享同一账号的三层记忆）。
            </p>
            <div className="space-y-2 mb-4">
              {feishuBindStatus.bound.map((b) => (
                <div key={b.open_id_short} className="flex items-center justify-between border border-slate-200 rounded-lg px-3 py-2">
                  <div>
                    <div className="text-sm font-medium">飞书身份 …{b.open_id_short}</div>
                    <div className="text-xs text-gray-400">
                      绑定于 {b.bound_at ? new Date(b.bound_at).toLocaleString() : '-'}
                      {b.last_active_at ? ` · 最近活跃 ${new Date(b.last_active_at).toLocaleString()}` : ''}
                    </div>
                  </div>
                  <div className="flex gap-2">
                    <button onClick={() => { setConfirmMode('unbind'); setUnbindTarget(b.open_id_short); }} className="text-xs border border-slate-300 rounded-lg px-2.5 py-1 text-slate-600 hover:bg-slate-50">解绑</button>
                    <button onClick={() => { setConfirmMode('change_pwd'); }} className="text-xs border border-slate-300 rounded-lg px-2.5 py-1 text-slate-600 hover:bg-slate-50">修改密码</button>
                  </div>
                </div>
              ))}
            </div>
            {confirmMode && (
              <div className="border border-amber-200 bg-amber-50 rounded-lg p-3 mb-2">
                <p className="text-xs text-amber-700 mb-2">
                  {confirmMode === 'unbind'
                    ? `确认解绑飞书身份 …${unbindTarget}？解绑后该飞书账号将无法与小娜对话（三层记忆保留，重新绑定即可恢复）。`
                    : '修改密码将同步影响飞书绑定侧的密码校验，请用当前绑定手机号验证。'}
                </p>
                <div className="flex gap-2 mb-2">
                  <input value={confirmPhone} onChange={(e) => setConfirmPhone(e.target.value)} placeholder="确认手机号" className="input-base max-w-[160px]" />
                  <input value={confirmCode} onChange={(e) => setConfirmCode(e.target.value)} placeholder="验证码" className="input-base w-28" />
                  <button onClick={sendConfirmCode} disabled={confirmCountdown > 0} className="shrink-0 px-3 py-2 rounded-lg border border-slate-300 text-sm text-slate-600 hover:bg-slate-50 disabled:opacity-50">
                    {confirmCountdown > 0 ? `${confirmCountdown}s` : '获取验证码'}
                  </button>
                </div>
                {confirmMode === 'change_pwd' && (
                  <input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} placeholder="新密码（至少 6 位）" className="input-base max-w-[200px] mb-2" />
                )}
                <div className="flex gap-2">
                  <button onClick={confirmAction} className="btn-primary px-4 text-sm">确认{confirmMode === 'unbind' ? '解绑' : '修改'}</button>
                  <button onClick={() => { setConfirmMode(null); setUnbindTarget(null); setNewPassword(''); }} className="btn-secondary px-3 text-sm">取消</button>
                </div>
              </div>
            )}
          </>
        ) : (
          <>
            <p className="text-sm text-gray-500 mb-2">
              当前未绑定飞书身份。真正的飞书绑定在 <b>飞书对话框内</b> 完成：回复「绑定」→ 依次输入 手机号 → 密码 → 短信验证码。
            </p>
            <p className="text-sm text-gray-500 mb-2">
              下方三个输入框用于为当前账号补录真实手机号与密码（注册账号的初始账号没有真实手机号/密码，补录后才能作为飞书绑定的目标账号）：
            </p>
            <ul className="text-xs text-gray-400 list-disc pl-5 mb-3 space-y-1">
              <li>手机号：确定要绑定的目标账号；</li>
              <li>验证码：证明该手机号归你所有；</li>
              <li>密码：飞书侧绑定时用于身份确认。</li>
            </ul>
            <div className="flex gap-2 mb-2">
              <input value={bindPhone} onChange={(e) => setBindPhone(e.target.value)} placeholder="手机号" className="input-base max-w-[200px]" />
              <input value={bindCode} onChange={(e) => setBindCode(e.target.value)} placeholder="验证码" className="input-base w-28" />
              <button onClick={sendBindCode} disabled={bindCountdown > 0} className="shrink-0 px-3 py-2 rounded-lg border border-slate-300 text-sm text-slate-600 hover:bg-slate-50 disabled:opacity-50">
                {bindCountdown > 0 ? `${bindCountdown}s` : '获取验证码'}
              </button>
            </div>
            <div className="flex gap-2">
              <input type="password" value={bindPassword} onChange={(e) => setBindPassword(e.target.value)} placeholder="新密码" className="input-base max-w-[200px]" />
              <button onClick={bindAccount} className="btn-primary px-4">绑定</button>
            </div>
          </>
        )}
      </section>

      <section className="bg-white border border-slate-200 rounded-xl p-6 mb-6 shadow-card">
        <h2 className="font-bold mb-3">数据管理</h2>
        <div className="flex gap-2 mb-4">
          <button onClick={exportAll} className="btn-secondary">导出全部数据</button>
          <label className="btn-secondary cursor-pointer">
            导入备份
            <input
              type="file"
              accept=".zip"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) importBackup(f);
                e.target.value = '';
              }}
            />
          </label>
        </div>
        <button onClick={() => setDeleteTarget('sql')} className="bg-red-50 text-red-600 border border-red-200 rounded-lg px-3 py-1.5 mr-2 hover:bg-red-100">清空 SQL 对话</button>
        <button onClick={() => setDeleteTarget('chroma')} className="bg-red-50 text-red-600 border border-red-200 rounded-lg px-3 py-1.5 mr-2 hover:bg-red-100">清空 Chroma 向量</button>
        <button onClick={() => setDeleteTarget('kg')} className="bg-red-50 text-red-600 border border-red-200 rounded-lg px-3 py-1.5 mr-2 hover:bg-red-100">清空知识图谱</button>
        <button onClick={() => setDeleteTarget('all')} className="bg-red-500 text-white rounded-lg px-3 py-1.5 hover:bg-red-600">清空全部数据</button>

        <DeleteConfirm
          open={deleteTarget !== null}
          onClose={() => setDeleteTarget(null)}
          onConfirm={async (password) => {
            await clearData(deleteTarget, password);
            setDeleteTarget(null);
          }}
        />
      </section>
    </div>
  );
}
