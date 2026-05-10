import { useEffect, useState } from 'react';
import { api } from '../../services/api';
import type { Account, FeishuNotifySettings } from '../../types';
import { Key, Trash2, Plus, Shield, AlertCircle, MessageSquare } from 'lucide-react';

export default function SettingsPage() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: '', exchange: 'binance', api_key: '', api_secret: '', okx_passphrase: '', testnet: true, hedge_mode: true });
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [feishu, setFeishu] = useState<FeishuNotifySettings | null>(null);
  const [webhookDraft, setWebhookDraft] = useState('');
  const [keywordDraft, setKeywordDraft] = useState('');
  const [keywordUseEnvDefault, setKeywordUseEnvDefault] = useState(false);
  const [clearWebhookDb, setClearWebhookDb] = useState(false);
  const [feishuSaveError, setFeishuSaveError] = useState('');
  const [feishuSaving, setFeishuSaving] = useState(false);

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const [result, fd] = await Promise.all([
        api.listAccounts(),
        api.getFeishuNotify().catch(() => null),
      ]);
      setAccounts(result);
      if (fd) {
        setFeishu(fd);
        setKeywordDraft(fd.keyword_prefix);
        setKeywordUseEnvDefault(!fd.has_database_prefix_override);
      }
      setWebhookDraft('');
      setClearWebhookDb(false);
      setFeishuSaveError('');
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(`加载账户失败: ${msg}`);
      setAccounts([]);
    }
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  const handleAdd = async () => {
    if (!form.name.trim()) { setSaveError('请输入账户名称'); return; }
    if (!form.api_key.trim()) { setSaveError('请输入API Key'); return; }
    if (!form.api_secret.trim()) { setSaveError('请输入API Secret'); return; }

    setSaveError('');
    try {
      await api.createAccount(form);
      setShowForm(false);
      setForm({ name: '', exchange: 'binance', api_key: '', api_secret: '', okx_passphrase: '', testnet: true, hedge_mode: true });
      await load();
    } catch (e: any) {
      setSaveError(`保存失败: ${e.message}`);
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm('确定要删除该账户吗？相关的策略也会被删除。')) return;
    try {
      await api.deleteAccount(id);
      await load();
    } catch (e: any) {
      setError(`删除失败: ${e.message}`);
    }
  };

  const handleSaveFeishu = async () => {
    setFeishuSaving(true);
    setFeishuSaveError('');
    try {
      const body: Parameters<typeof api.updateFeishuNotify>[0] = {};
      if (clearWebhookDb) body.webhook_url = '';
      else if (webhookDraft.trim()) body.webhook_url = webhookDraft.trim();
      if (keywordUseEnvDefault) body.keyword_prefix_use_env_default = true;
      else body.keyword_prefix = keywordDraft;
      const updated = await api.updateFeishuNotify(body);
      setFeishu(updated);
      setKeywordDraft(updated.keyword_prefix);
      setKeywordUseEnvDefault(!updated.has_database_prefix_override);
      setWebhookDraft('');
      setClearWebhookDb(false);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setFeishuSaveError(`保存失败: ${msg}`);
    }
    setFeishuSaving(false);
  };

  const sourceLabel =
    feishu?.webhook_source === 'database'
      ? '数据库'
      : feishu?.webhook_source === 'environment'
        ? '环境变量'
        : '未配置';

  return (
    <div className="space-y-6 max-w-2xl">
      <h2 className="text-xl font-bold">系统设置</h2>

      <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
          <Key size={16} className="text-yellow-400" />
          交易所API密钥管理
        </h3>

        {error && (
          <div className="flex items-center gap-2 text-red-400 text-sm mb-3 bg-red-900/20 rounded p-2">
            <AlertCircle size={14} /> {error}
          </div>
        )}

        {loading && <p className="text-gray-500 text-sm py-4">加载中...</p>}

        {!loading && accounts.map((a) => (
          <div key={a.id} className="flex items-center justify-between py-2 border-b border-gray-800">
            <div>
              <span className="font-medium">{a.name}</span>
              <span className={`ml-2 text-xs px-2 py-0.5 rounded ${a.testnet ? 'bg-yellow-600/20 text-yellow-400' : 'bg-green-600/20 text-green-400'}`}>
                {a.testnet ? '测试网' : '实盘'}
              </span>
              <span className="ml-1 text-xs px-2 py-0.5 rounded bg-purple-600/20 text-purple-400">
                {(a as any).exchange === 'okx' ? 'OKX' : '币安'}
              </span>
              <span className={`ml-1 text-xs px-2 py-0.5 rounded ${a.hedge_mode ? 'bg-blue-600/20 text-blue-400' : 'bg-purple-600/20 text-purple-400'}`}>
                {a.hedge_mode ? '双向持仓' : '单向持仓'}
              </span>
              <div className="text-xs text-gray-500 mt-0.5">
                API密钥: {a.masked_key}
              </div>
            </div>
            <button onClick={() => handleDelete(a.id)} className="p-1.5 text-red-400 hover:bg-red-600/20 rounded">
              <Trash2 size={16} />
            </button>
          </div>
        ))}

        {!loading && accounts.length === 0 && !error && (
          <p className="text-gray-600 text-sm py-2">暂无账户，请添加交易所API密钥</p>
        )}

        {!showForm && (
          <button
            onClick={() => setShowForm(true)}
            className="flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 mt-3"
          >
            <Plus size={16} /> 添加账户
          </button>
        )}

        {showForm && (
          <div className="mt-3 space-y-2 p-3 bg-gray-800 rounded-lg">
            {saveError && (
              <div className="flex items-center gap-2 text-red-400 text-sm bg-red-900/20 rounded p-2">
                <AlertCircle size={14} /> {saveError}
              </div>
            )}
            <select
              value={form.exchange}
              onChange={(e) => setForm({ ...form, exchange: e.target.value })}
              className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-1.5 text-sm"
            >
              <option value="binance">币安 Binance</option>
              <option value="okx">OKX</option>
            </select>
            <input
              placeholder="账户名称"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-1.5 text-sm"
            />
            <input
              type="password"
              placeholder="API Key"
              value={form.api_key}
              onChange={(e) => setForm({ ...form, api_key: e.target.value })}
              className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-1.5 text-sm"
            />
            <input
              type="password"
              placeholder="API Secret"
              value={form.api_secret}
              onChange={(e) => setForm({ ...form, api_secret: e.target.value })}
              className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-1.5 text-sm"
            />
            {form.exchange === 'okx' && (
              <input
                type="password"
                placeholder="OKX Passphrase（OKX必填）"
                value={form.okx_passphrase}
                onChange={(e) => setForm({ ...form, okx_passphrase: e.target.value })}
                className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-1.5 text-sm"
              />
            )}
            <label className="flex items-center gap-2 text-sm text-gray-400">
              <input
                type="checkbox"
                checked={form.testnet}
                onChange={(e) => setForm({ ...form, testnet: e.target.checked })}
              />
              使用测试网 (建议先在测试网验证策略)
            </label>
            <label className="flex items-center gap-2 text-sm text-gray-400">
              <input
                type="checkbox"
                checked={form.hedge_mode}
                onChange={(e) => setForm({ ...form, hedge_mode: e.target.checked })}
              />
              双向持仓模式 (Hedge Mode)
            </label>
            <div className="flex gap-2">
              <button onClick={handleAdd} className="px-3 py-1 bg-blue-600 hover:bg-blue-700 rounded text-sm">保存</button>
              <button onClick={() => { setShowForm(false); setSaveError(''); }} className="px-3 py-1 bg-gray-700 hover:bg-gray-600 rounded text-sm">取消</button>
            </div>
          </div>
        )}
      </section>

      <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
          <MessageSquare size={16} className="text-teal-400" />
          飞书通知（Webhook）
        </h3>

        {!feishu && !loading && (
          <p className="text-gray-500 text-sm">暂无法加载飞书配置。</p>
        )}

        {feishu && (
          <div className="space-y-3 text-sm">
            {feishuSaveError && (
              <div className="flex items-center gap-2 text-red-400 text-sm bg-red-900/20 rounded p-2">
                <AlertCircle size={14} /> {feishuSaveError}
              </div>
            )}

            <div className="text-gray-400">
              当前生效地址{' '}
              <span className="text-gray-200 font-mono text-xs">{feishu.webhook_masked || '—'}</span>
              <span
                className={`ml-2 text-xs px-2 py-0.5 rounded ${
                  feishu.webhook_source === 'database'
                    ? 'bg-purple-600/25 text-purple-300'
                    : feishu.webhook_source === 'environment'
                      ? 'bg-blue-600/25 text-blue-300'
                      : 'bg-gray-700 text-gray-500'
                }`}
              >
                {sourceLabel}
              </span>
            </div>

            <p className="text-xs text-gray-600">
              优先级：以下为「写入数据库」的地址与环境变量<code className="text-gray-400"> FEISHU_WEBHOOK_URL</code>{' '}
              并存时，数据库优先。
            </p>

            <p className="text-xs text-amber-700/90">
              Webhook 保存在 SQLite（明文）。请保护好数据库文件与服务器访问权限。
            </p>

            <label className="block text-gray-400 text-xs">新 Webhook URL（HTTPS）</label>
            <input
              type="password"
              autoComplete="off"
              placeholder="填写则保存到数据库（脱敏不回显）；不填则沿用当前来源"
              value={webhookDraft}
              onChange={(e) => setWebhookDraft(e.target.value)}
              className="w-full bg-gray-800 border border-gray-600 rounded px-3 py-1.5 text-sm font-mono"
            />

            <label className="flex items-center gap-2 text-gray-400">
              <input
                type="checkbox"
                checked={clearWebhookDb}
                onChange={(e) => {
                  const v = e.target.checked;
                  setClearWebhookDb(v);
                  if (v) setWebhookDraft('');
                }}
              />
              清除数据库里的 Webhook（改回仅使用环境变量）
            </label>

            <label className="block text-gray-400 text-xs mt-2">
              消息关键词前缀（飞书自定义机器人安全设置时需要与关键词一致）
            </label>
            <input
              type="text"
              placeholder="默认 [WG]"
              value={keywordDraft}
              onChange={(e) => setKeywordDraft(e.target.value)}
              disabled={keywordUseEnvDefault}
              className="w-full bg-gray-800 border border-gray-600 rounded px-3 py-1.5 text-sm disabled:opacity-50"
            />
            <label className="flex items-center gap-2 text-gray-400">
              <input
                type="checkbox"
                checked={keywordUseEnvDefault}
                onChange={(e) => setKeywordUseEnvDefault(e.target.checked)}
              />
              前缀使用环境变量（不在数据库单独保存）
            </label>

            <button
              type="button"
              disabled={feishuSaving}
              onClick={handleSaveFeishu}
              className="px-3 py-1.5 bg-teal-600 hover:bg-teal-700 disabled:opacity-50 rounded text-sm"
            >
              {feishuSaving ? '保存中…' : '保存通知设置'}
            </button>
          </div>
        )}
      </section>

      <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
          <Shield size={16} className="text-blue-400" />
          安全说明
        </h3>
        <p className="text-sm text-gray-500">
          API密钥使用AES-128-CBC Fernet加密后存储，前端仅展示脱敏后的密钥。
          所有敏感操作（删除策略、紧急平仓等）均需要二次确认。
          建议先使用币安测试网验证所有策略和功能，确认无误后再切换到实盘环境。
        </p>
      </section>
    </div>
  );
}
