import React, { FormEvent, useState } from 'react';
import { api } from '../../services/api';

export default function LoginScreen({ onSuccess }: { onSuccess: () => void }) {
  const [password, setPassword] = useState('');
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setErr('');
    setLoading(true);
    try {
      await api.login(password);
      setPassword('');
      onSuccess();
    } catch (x: unknown) {
      const msg = x instanceof Error ? x.message : String(x);
      setErr(msg === '密码错误' || msg.includes('401') ? '密码错误' : msg);
    }
    setLoading(false);
  }

  return (
    <div className="min-h-screen bg-gray-950 flex items-center justify-center p-4">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-4 p-6 rounded-xl border border-gray-800 bg-gray-900 shadow-xl"
      >
        <div>
          <h1 className="text-lg font-semibold text-gray-100">马丁网格</h1>
          <p className="text-xs text-gray-500 mt-1">请输入访问密码</p>
        </div>
        {err && <p className="text-sm text-red-400 bg-red-950/40 border border-red-900/50 rounded px-2 py-1.5">{err}</p>}
        <input
          type="password"
          autoComplete="current-password"
          placeholder="密码"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="w-full rounded-lg bg-gray-800 border border-gray-600 px-3 py-2 text-sm text-white placeholder-gray-500 focus:border-blue-500 focus:outline-none"
        />
        <button
          type="submit"
          disabled={loading || !password.trim()}
          className="w-full rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-45 py-2 text-sm font-medium text-white"
        >
          {loading ? '验证中…' : '登录'}
        </button>
      </form>
    </div>
  );
}
