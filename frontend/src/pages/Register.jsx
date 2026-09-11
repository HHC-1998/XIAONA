import { useState, useRef, useEffect } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { authApi } from '../api/auth';
import { useAuth } from '../hooks/useAuth';

export default function Register() {
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [nickname, setNickname] = useState('');
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
    if (password !== confirmPassword) return setError('两次密码不一致');
    try {
      const res = await authApi.register({ phone, code, password, nickname });
      login(res.token, res.user);
      navigate('/');
    } catch (e) {
      setError(e.response?.data?.detail || '注册失败');
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 via-white to-violet-50 p-4">
      <div className="bg-white p-8 rounded-2xl shadow-lift w-96 space-y-4 border border-slate-100">
        <div className="text-center">
          <h1 className="text-3xl font-bold bg-gradient-to-r from-indigo-600 to-violet-600 bg-clip-text text-transparent mb-1">
            注册账号
          </h1>
          <p className="text-sm text-slate-500">创建你的 XIAONA 账号</p>
        </div>
        <input
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
          placeholder="手机号"
          className="input-base"
        />
        <div className="flex gap-2">
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
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
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="设置密码（必填）"
          className="input-base"
        />
        <input
          type="password"
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          placeholder="确认密码"
          className="input-base"
        />
        <input
          value={nickname}
          onChange={(e) => setNickname(e.target.value)}
          placeholder="昵称（选填）"
          className="input-base"
        />
        {error && <p className="text-red-500 text-sm">{error}</p>}
        <button onClick={handleSubmit} className="btn-primary w-full">
          注册
        </button>
        <Link to="/login" className="block text-center text-sm text-indigo-600 hover:text-indigo-700">
          已有账号？登录
        </Link>
      </div>
    </div>
  );
}
