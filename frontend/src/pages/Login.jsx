import { useState, useRef, useEffect } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { authApi } from '../api/auth';
import { useAuth } from '../hooks/useAuth';

export default function Login() {
  const [mode, setMode] = useState('password'); // 'password' | 'code'
  const [phone, setPhone] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [countdown, setCountdown] = useState(0);
  const [error, setError] = useState('');
  const navigate = useNavigate();
  const { login } = useAuth();
  const timerRef = useRef(null);   // 验证码倒计时定时器（卸载清理，防 setInterval 泄漏）

  // 组件卸载时清理倒计时定时器（切页/离开后不再 setState）
  useEffect(() => () => clearInterval(timerRef.current), []);

  const sendCode = async () => {
    if (!phone) return setError('请填写手机号');
    try {
      const res = await authApi.sendCode(phone);
      if (res?.code) {
        setCode(res.code);
        setError(`演示模式，验证码已自动填入：${res.code}`);
      }
      setCountdown(60);
      clearInterval(timerRef.current);   // 防连续点击叠加多个倒计时定时器
      timerRef.current = setInterval(() => {
        setCountdown((c) => {
          if (c <= 1) { clearInterval(timerRef.current); return 0; }
          return c - 1;
        });
      }, 1000);
    } catch (e) {
      setError(e.response?.data?.detail || '验证码发送失败');
    }
  };

  const handleSubmit = async () => {
    setError('');
    try {
      const res = mode === 'password'
        ? await authApi.login({ phone, password })
        : await authApi.login({ phone, code });
      login(res.token, res.user);
      navigate('/');
    } catch (e) {
      setError(e.response?.data?.detail || '登录失败');
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 via-white to-violet-50 p-4">
      <div className="bg-white p-8 rounded-2xl shadow-lift w-96 space-y-5 border border-slate-100">
        <div className="text-center">
          <h1 className="text-3xl font-bold bg-gradient-to-r from-indigo-600 to-violet-600 bg-clip-text text-transparent mb-1">
            XIAONA
          </h1>
          <p className="text-sm text-slate-500">欢迎回来，登录以继续</p>
        </div>
        <div className="flex gap-2 p-1 bg-slate-100 rounded-lg">
          <button
            onClick={() => setMode('password')}
            className={`flex-1 py-2 rounded-md text-sm font-medium transition ${
              mode === 'password' ? 'bg-white shadow text-indigo-600' : 'text-slate-500 hover:text-slate-700'
            }`}
          >
            密码登录
          </button>
          <button
            onClick={() => setMode('code')}
            className={`flex-1 py-2 rounded-md text-sm font-medium transition ${
              mode === 'code' ? 'bg-white shadow text-indigo-600' : 'text-slate-500 hover:text-slate-700'
            }`}
          >
            验证码登录
          </button>
        </div>
        <input
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
          placeholder="手机号"
          className="input-base"
        />
        {mode === 'password' ? (
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSubmit()}
            placeholder="密码"
            className="input-base"
          />
        ) : (
          <div className="flex gap-2">
            <input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSubmit()}
              placeholder="验证码"
              className="input-base"
            />
            <button
              onClick={sendCode}
              disabled={countdown > 0}
              className="shrink-0 px-3 py-2 rounded-lg border border-slate-300 text-sm text-slate-600 hover:bg-slate-50 disabled:opacity-50"
            >
              {countdown > 0 ? `${countdown}s` : '获取验证码'}
            </button>
          </div>
        )}
        {error && <p className="text-red-500 text-sm">{error}</p>}
        <button onClick={handleSubmit} className="btn-primary w-full">
          登录
        </button>
        <Link to="/register" className="block text-center text-sm text-indigo-600 hover:text-indigo-700">
          还没有账号？注册
        </Link>
      </div>
    </div>
  );
}
