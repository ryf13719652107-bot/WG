import { Component, ReactNode, useCallback, useEffect, useRef, useState } from 'react';
import { Routes, Route } from 'react-router-dom';
import AppShell from './components/layout/AppShell';
import DashboardPage from './components/dashboard/DashboardPage';
import StrategyPage from './components/strategy/StrategyPage';
import StrategyDetailPage from './components/strategy/StrategyDetailPage';
import PositionsPage from './components/positions/PositionsPage';
import TradesPage from './components/trades/TradesPage';
import SettingsPage from './components/settings/SettingsPage';
import LoginScreen from './components/auth/LoginScreen';
import { api } from './services/api';

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  constructor(props: { children: ReactNode }) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div style={{ color: '#fff', padding: '40px', fontFamily: 'monospace', background: '#0a0a0a', minHeight: '100vh' }}>
          <h1 style={{ color: '#ef4444', fontSize: '24px' }}>渲染错误</h1>
          <pre style={{ color: '#f87171', marginTop: '16px', whiteSpace: 'pre-wrap', fontSize: '14px' }}>
            {this.state.error.message}
          </pre>
          <pre style={{ color: '#6b7280', marginTop: '16px', whiteSpace: 'pre-wrap', fontSize: '12px', maxHeight: '400px', overflow: 'auto' }}>
            {this.state.error.stack}
          </pre>
        </div>
      );
    }
    return this.props.children;
  }
}

type Gate = 'loading' | 'login' | 'app';

export default function App() {
  const [gate, setGate] = useState<Gate>('loading');
  const [authRequired, setAuthRequired] = useState(false);
  const authRequiredRef = useRef(false);
  authRequiredRef.current = authRequired;

  const bootstrap = useCallback(async () => {
    setGate('loading');
    try {
      const s = await api.authStatus();
      setAuthRequired(s.auth_required);
      if (!s.auth_required || s.authenticated) setGate('app');
      else setGate('login');
    } catch {
      setGate('login');
      setAuthRequired(true);
    }
  }, []);

  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  useEffect(() => {
    const fn = () => {
      setGate('login');
      setAuthRequired(true);
    };
    window.addEventListener('wg-auth-required', fn);
    return () => window.removeEventListener('wg-auth-required', fn);
  }, []);

  /** 门禁开启时：切回前台后复检，禁止未登录继续操作 SPA */
  useEffect(() => {
    if (gate !== 'app') return;
    let t: ReturnType<typeof setTimeout> | undefined;
    const verify = async () => {
      if (!authRequiredRef.current) return;
      try {
        const s = await api.authStatus();
        if (s.auth_required && !s.authenticated) setGate('login');
      } catch {
        /* 网络抖动不踢下线，避免误判 */
      }
    };
    const onFocus = () => {
      clearTimeout(t);
      t = setTimeout(verify, 200);
    };
    const onVis = () => {
      if (document.visibilityState === 'visible') onFocus();
    };
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVis);
    return () => {
      clearTimeout(t);
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVis);
    };
  }, [gate]);

  const handleLogout = async () => {
    try {
      await api.logout();
    } catch {
      /* ignore */
    }
    if (authRequired) setGate('login');
    else await bootstrap();
  };

  if (gate === 'loading') {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-950 text-gray-500 text-sm">
        校验访问权限…
      </div>
    );
  }

  if (gate === 'login') {
    return <LoginScreen onSuccess={() => setGate('app')} />;
  }

  return (
    <ErrorBoundary>
      <AppShell showLogout={authRequired} onLogout={handleLogout}>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/strategies" element={<StrategyPage />} />
          <Route path="/strategies/:id" element={<StrategyDetailPage />} />
          <Route path="/positions" element={<PositionsPage />} />
          <Route path="/trades" element={<TradesPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </AppShell>
    </ErrorBoundary>
  );
}
