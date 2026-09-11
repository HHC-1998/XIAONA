import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider } from './contexts/AuthContext';
import { useAuth } from './hooks/useAuth';
import { AuthRoute } from './components/AuthRoute';
import Navbar from './components/Navbar';
import Login from './pages/Login';
import Register from './pages/Register';
import Chat from './pages/Chat';
import Graph from './pages/Graph';
import DesktopAssistant from './pages/DesktopAssistant';
import Settings from './pages/Settings';

function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <AppLayout />
      </BrowserRouter>
    </AuthProvider>
  );
}

function AppLayout() {
  const { user } = useAuth();

  return (
    <div className="h-screen flex flex-col">
      {user && <Navbar />}
      <main className="flex-1 relative overflow-auto">
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />
          <Route path="/" element={<AuthRoute><Chat /></AuthRoute>} />
          <Route path="/graph" element={<AuthRoute><Graph /></AuthRoute>} />
          <Route path="/desktop" element={<AuthRoute><DesktopAssistant /></AuthRoute>} />
          <Route path="/settings" element={<AuthRoute><Settings /></AuthRoute>} />
        </Routes>
      </main>
    </div>
  );
}

export default App;
