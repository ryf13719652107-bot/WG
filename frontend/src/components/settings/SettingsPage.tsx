import { useEffect, useState } from 'react';
import { api } from '../../services/api';
import type { Account, FeishuNotifySettings, WebUiPasswordStatus } from '../../types';
import { Key, Trash2, Plus, Shield, AlertCircle, MessageSquare, Lock } from 'lucide-react';

const FEISHU_DEFAULT: FeishuNotifySettings = {
  webhook_masked: '',
  webhook_source: 'none',
  keyword_prefix: '[WG]',
  has_database_webhook_override: false,
  has_database_prefix_override: false,
};

export default function SettingsPage() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: '', exchange: 'binance', api_key: '', api_secret: '', okx_passphrase: '', testnet: true, hedge_mode: true });
  const [error, setError] = useState('');
  const [loadingAccounts, setLoadingAccounts] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [feishu, setFeishu] = useState<FeishuNotifySettings>(FEISHU_DEFAULT);
  const [feishuLoadFailed, setFeishuLoadFailed] = useState(false);
  const [loadingFeishu, setLoadingFeishu] = useState(false);
  const [webhookDraft, setWebhookDraft] = useState('');
  const [keywordDraft, setKeywordDraft] = useState('');
  const [keywordUseEnvDefault, setKeywordUseEnvDefault] = useState(false);
  const [clearWebhookDb, setClearWebhookDb] = useState(false);
  const [feishuSaveError, setFeishuSaveError] = useState('');
  const [feishuSaving, setFeishuSaving] = useState(false);

  const [webUiPw, setWebUiPw] = useState<WebUiPasswordStatus | null>(null);
  const [loadingWebUi, setLoadingWebUi] = useState(false);
  const [webUiDraft, setWebUiDraft] = useState('');
  const [webUiSaveErr, setWebUiSaveErr] = useState('');
  const [webUiSaving, setWebUiSaving] = useState(false);

  const loadAccounts = async () => {
    setLoadingAccounts(true);
    setError('');
    try {
      const result = await api.listAccounts();
      setAccounts(result);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(`加载账户失败: ${msg}`);
      setAccounts([]);
    }
    setLoadingAccounts(false);
  };

  const loadFeishu = async () => {
    setFeishuLoadFailed(false);
    setLoadingFeishu(true);
    try {
      const fd = await api.getFeishuNotify();
      setFeishu(fd);
      setKeywordDraft(fd.keyword_prefix);
      setKeywordUseEnvDefault(!fd.has_database_prefix_override);
    } catch {
      setFeishu(FEISHU_DEFAULT);
      setKeywordDraft(FEISHU_DEFAULT.keyword_prefix);
      setKeywordUseEnvDefault(true);
      setFeishuLoadFailed(true);
    }
    setWebhookDraft('');
    setClearWebhookDb(false);
    setFeishuSaveError('');
    setLoadingFeishu(false);
  };

  const loadWebUi = async () => {
    setLoadingWebUi(true);
    try {
      const s = await api.getWebUiPasswordStatus();
      setWebUiPw(s);
      setWebUiSaveErr('');
    } catch {
      setWebUiPw(null);
    }
    setLoadingWebUi(false);
  };

  const load = async () => {
    await Promise.all([loadAccounts(), loadFeishu(), loadWebUi()]);
  };

  useEffect(() => {
    load();
  }, []);

  const handleAdd = async () => {
    if (!form.name.trim()) { setSaveError('请输入账户名称'); return; }
    if (!form.api_key.trim()) { setSaveError('请输入API Key'); return; }
    if (!form.api_secret.trim()) { setSaveError('请输入API Secret'); return; }
    if (form.exchange === 'okx' && !form.okx_passphrase.trim()) {
      setSaveError('OKX 必须填写 Passphrase（创建 API 时自定义的口令），否则无法拉取余额');
      return;
    }

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
      setFeishuLoadFailed(false);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setFeishuSaveError(`保存失败: ${msg}`);
    }
    setFeishuSaving(false);
  };

  const handleSaveWebUiPassword = async () => {
    setWebUiSaving(true);
    setWebUiSaveErr('');
    try {
      const prev = webUiPw?.auth_required_effective ?? false;
      const next = await api.updateWebUiPassword(webUiDraft);
      setWebUiPw(next);
      setWebUiDraft('');
      const becameEnabled = next.auth_required_effective && !prev;
      const becameDisabled = !next.auth_required_effective && prev;
      if (becameEnabled || becameDisabled) {
        alert(
          becameEnabled
            ? '已开启登录门禁。若当前未登录，将刷新页面后要求输入密码。'
            : '已关闭登录门禁（需环境变量与数据库均未设置密码）。刷新页面后直接进控制台。',
        );
        window.location.reload();
      }
    } catch (e: unknown) {
      setWebUiSaveErr(e instanceof Error ? e.message : String(e));
    }
    setWebUiSaving(false);
  };

  return (
    <div className="space-y-6 max-w-2xl">
      <h2 className="text-xl font-bold">系统设置</h2>

      <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
          <Lock size={16} className="text-amber-400" />
          Web 控制台登录密码
        </h3>
        {loadingWebUi ? (
          <p className="text-gray-500 text-sm">加载中…</p>
        ) : (
          <div className="space-y-3 text-sm text-gray-400">
            {webUiSaveErr && (
              <div className="flex items-center gap-2 text-red-400 text-xs bg-red-900/20 rounded px-2 py-1.5">
                <AlertCircle size={14} /> {webUiSaveErr}
              </div>
            )}
            <p className="text-xs leading-relaxed">
              当前门禁状态：<span className="text-gray-200">{webUiPw?.auth_required_effective ? '已启用（需登录）' : '未启用'}</span>
              {' · '}
              环境变量：<span className="text-gray-300">{webUiPw?.environment_has_password ? '已设置 WEB_UI_PASSWORD' : '未设置'}</span>
              {' · '}
              数据库：<span className="text-gray-300">{webUiPw?.database_has_password ? '已保存密码' : '无'}</span>
            </p>
            <p className="text-xs text-gray-500">
              若 Docker / systemd 不方便配环境变量，可在此写入密码（存入服务器数据库）。
              <strong className="text-gray-600">优先级</strong>：进程环境变量 <code className="text-gray-500">WEB_UI_PASSWORD</code> 高于此处保存的值。
              环境变量仍有值时「留空保存」只会清数据库条目，门禁可能仍为启用。
            </p>
            <input
              type="password"
              autoComplete="new-password"
              placeholder="输入新登录密码…"
              value={webUiDraft}
              onChange={(e) => setWebUiDraft(e.target.value)}
              className="w-full bg-gray-800 border border-gray-600 rounded px-3 py-2 text-sm"
            />
            <button
              type="button"
              disabled={webUiSaving}
              onClick={() => void handleSaveWebUiPassword()}
              className="px-4 py-2 bg-amber-600 hover:bg-amber-700 disabled:opacity-50 rounded text-sm mr-2"
            >
              {webUiSaving ? '保存中…' : '保存密码'}
            </button>
          </div>
        )}
      </section>

      <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
          <MessageSquare size={16} className="text-teal-400" />
          飞书
        </h3>

        {loadingFeishu ? (
          <p className="text-gray-500 text-sm">加载中…</p>
        ) : (
          <div className="space-y-3 text-sm">
            {feishuSaveError && (
              <div className="flex items-center gap-2 text-red-400 text-xs bg-red-900/20 rounded px-2 py-1.5">
                <AlertCircle size={14} /> {feishuSaveError}
              </div>
            )}
            {feishuLoadFailed && (
              <p className="text-xs text-amber-500/80">未加载到配置，仍可填写后保存</p>
            )}

            <div>
              <label className="block text-xs text-gray-500 mb-1">Webhook 链接</label>
              <input
                type="text"
                inputMode="url"
                autoComplete="off"
                spellCheck={false}
                placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…"
                value={webhookDraft}
                onChange={(e) => setWebhookDraft(e.target.value)}
                className="w-full bg-gray-800 border border-gray-600 rounded px-3 py-2 text-sm font-mono"
              />
            </div>

            <label className="flex items-center gap-2 text-xs text-gray-400 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={clearWebhookDb}
                onChange={(e) => {
                  const v = e.target.checked;
                  setClearWebhookDb(v);
                  if (v) setWebhookDraft('');
                }}
              />
              删除已保存的链接
            </label>

            <div>
              <label className="block text-xs text-gray-500 mb-1">关键词前缀</label>
              <input
                type="text"
                placeholder="[WG]"
                value={keywordDraft}
                onChange={(e) => setKeywordDraft(e.target.value)}
                disabled={keywordUseEnvDefault}
                className="w-full bg-gray-800 border border-gray-600 rounded px-3 py-2 text-sm disabled:opacity-45"
              />
            </div>

            <label className="flex items-center gap-2 text-xs text-gray-400 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={keywordUseEnvDefault}
                onChange={(e) => setKeywordUseEnvDefault(e.target.checked)}
              />
              关键词用环境变量
            </label>

            <button
              type="button"
              disabled={feishuSaving}
              onClick={handleSaveFeishu}
              className="px-4 py-2 bg-teal-600 hover:bg-teal-700 disabled:opacity-50 rounded text-sm"
            >
              {feishuSaving ? '保存中…' : '保存'}
            </button>
          </div>
        )}
      </section>

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

        {loadingAccounts && <p className="text-gray-500 text-sm py-4">加载中...</p>}

        {!loadingAccounts && accounts.map((a) => (
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

        {!loadingAccounts && accounts.length === 0 && !error && (
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
