import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../hooks/useAuth';

// 顶部导航栏：历史会话已并入聊天侧边栏，导航只保留核心入口
const navItems = [
  { to: '/', label: '聊天' },
  { to: '/graph', label: '关系网' },
  { to: '/desktop', label: '桌面助手' },
  { to: '/settings', label: '设置' },
];

export default function Navbar() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  return (
    <nav className="bg-white border-b border-slate-200 px-4 h-14 flex items-center justify-between shrink-0">
      <div className="flex items-center gap-6">
        <span className="text-lg font-bold bg-gradient-to-r from-indigo-600 to-violet-600 bg-clip-text text-transparent">
          XIAONA
        </span>
        <div className="flex items-center gap-1">
          {navItems.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              className="px-3 py-1.5 rounded-lg text-sm text-slate-600 hover:text-indigo-600 hover:bg-indigo-50"
            >
              {item.label}
            </Link>
          ))}
        </div>
      </div>
      <div className="flex items-center gap-3">
        <span className="text-sm text-slate-500">{user?.nickname || `用户${user?.phone}`}</span>
        <button
          onClick={() => { logout(); navigate('/login'); }}
          className="px-3 py-1.5 rounded-lg text-sm text-slate-600 hover:bg-slate-100"
        >
          退出
        </button>
      </div>
    </nav>
  );
}
