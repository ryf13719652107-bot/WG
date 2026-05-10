import React, { FormEvent, useEffect, useState } from 'react';
import { api } from '../../services/api';

export default function LoginScreen({ onSuccess }: { onSuccess: () => void }) {
  const [password, setPassword] = useState('');
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const el = document.getElementById('wg-login-password');
    el?.focus();
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const p = password.trim();
    if (!p) return;
    setErr('');
    setLoading(true);
    try {
      await api.login(p);
      const s = await api.authStatus();
      if (s.auth_required && !s.authenticated) {
        throw new Error('登录后会话未生效，请重试或清空浏览器本站 Cookie');
      }
      setPassword('');
      onSuccess();
    } catch (x: unknown) {
      const msg = x instanceof Error ? x.message : String(x);
      setErr(msg === '密码错误' || msg.includes('401') ? '密码错误' : msg);
    }
    setLoading(false);
  }

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-gray-950/95 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="wg-login-title"
    >
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-4 p-6 rounded-xl border border-gray-800 bg-gray-900 shadow-xl"
      >
        <div>
          <h1 id="wg-login-title" className="text-lg font-semibold text-gray-100">
            进入控制台前请先登录
          </h1>
          <p className="text-xs text-gray-500 mt-1">后端已启用访问密码时必须验证通过后才能操作本系统</p>
        </div>
        {err && (
          <p className="text-sm text-red-400 bg-red-950/40 border border-red-900/50 rounded px-2 py-1.5">
            {err}
          </p>
        )}
        <input
          id="wg-login-password"
          type="password"
          autoComplete="current-password"
          placeholder="请输入访问密码"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="w-full rounded-lg bg-gray-800 border border-gray-600 px-3 py-2 text-sm text-white placeholder-gray-500 focus:border-blue-500 focus:outline-none"
        />
        <button
          type="submit"
          disabled={loading || !password.trim()}
          className="w-full rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-45 disabled:pointer-events-none py-2 text-sm font-medium text-white"
        >
          {loading ? '验证中…' : '登录并进入'}
        </button>
      </form>
    </div>
  );
}
