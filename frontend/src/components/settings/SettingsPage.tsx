import { useEffect, useState } from 'react';
import { api } from '../../services/api';
import type { Account, WebUiPasswordStatus } from '../../types';
import type { DeclineRankAutoConfig, DeclineRankAutoStatus } from '../../types/declineRank';
import { defaultDeclineRankConfig } from '../../types/declineRank';
import StrategyParamFieldsForm from '../strategy/StrategyParamFieldsForm';
import { Key, Trash2, Plus, Shield, AlertCircle, Lock, Wallet, TrendingDown } from 'lucide-react';

export default function SettingsPage() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: '', exchange: 'binance', api_key: '', api_secret: '', okx_passphrase: '', testnet: true, hedge_mode: true });
  const [error, setError] = useState('');
  const [loadingAccounts, setLoadingAccounts] = useState(false);
  const [saveError, setSaveError] = useState('');

  const [webUiPw, setWebUiPw] = useState<WebUiPasswordStatus | null>(null);
  const [loadingWebUi, setLoadingWebUi] = useState(false);
  const [webUiDraft, setWebUiDraft] = useState('');
  const [webUiSaveErr, setWebUiSaveErr] = useState('');
  const [webUiSaving, setWebUiSaving] = useState(false);
  const [equityDraft, setEquityDraft] = useState<Record<number, string>>({});
  const [equitySaving, setEquitySaving] = useState<number | null>(null);
  const [equityErr, setEquityErr] = useState('');

  const [declineCfg, setDeclineCfg] = useState<DeclineRankAutoConfig>(defaultDeclineRankConfig());
  const [declineStatus, setDeclineStatus] = useState<DeclineRankAutoStatus | null>(null);
  const [declineLoading, setDeclineLoading] = useState(false);
  const [declineSaving, setDeclineSaving] = useState(false);
  const [declineErr, setDeclineErr] = useState('');
  const [declineOk, setDeclineOk] = useState('');

  const loadAccounts = async () => {
    setLoadingAccounts(true);
    setError('');
    try {
      const result = await api.listAccounts();
      setAccounts(result);
      const drafts: Record<number, string> = {};
      for (const a of result) {
        drafts[a.id] = String(a.equity_stop_floor_u ?? 0);
      }
      setEquityDraft(drafts);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(`加载账户失败: ${msg}`);
      setAccounts([]);
    }
    setLoadingAccounts(false);
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

  const loadDeclineRank = async () => {
    setDeclineLoading(true);
    setDeclineErr('');
    try {
      const [cfg, st] = await Promise.all([
        api.getDeclineRankConfig(),
        api.getDeclineRankStatus(),
      ]);
      setDeclineCfg({
        ...defaultDeclineRankConfig(),
        ...cfg,
        params: { ...defaultDeclineRankConfig().params, ...(cfg.params || {}) },
      });
      setDeclineStatus(st);
    } catch (e: unknown) {
      setDeclineErr(e instanceof Error ? e.message : String(e));
    }
    setDeclineLoading(false);
  };

  const load = async () => {
    await Promise.all([loadAccounts(), loadWebUi(), loadDeclineRank()]);
  };

  useEffect(() => {
    load();
  }, []);

  const handleSaveDeclineRank = async () => {
    setDeclineSaving(true);
    setDeclineErr('');
    setDeclineOk('');
    try {
      if (declineCfg.enabled && !declineCfg.account_id) {
        setDeclineErr('启用自动策略时必须选择绑定账户');
        setDeclineSaving(false);
        return;
      }
      if (declineCfg.refresh_interval_min < 1) {
        setDeclineErr('刷新间隔至少 1 分钟');
        setDeclineSaving(false);
        return;
      }
      if (declineCfg.top_n < 1 || declineCfg.top_n > 100) {
        setDeclineErr('跌幅榜前 N 须在 1–100');
        setDeclineSaving(false);
        return;
      }
      const saved = await api.saveDeclineRankConfig(declineCfg);
      setDeclineCfg({
        ...defaultDeclineRankConfig(),
        ...saved,
        params: { ...defaultDeclineRankConfig().params, ...(saved.params || {}) },
      });
      const st = await api.getDeclineRankStatus();
      setDeclineStatus(st);
      setDeclineOk('已保存跌幅榜自动策略配置');
    } catch (e: unknown) {
      setDeclineErr(e instanceof Error ? e.message : String(e));
    }
    setDeclineSaving(false);
  };

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

  const handleSaveEquityGuard = async (accountId: number) => {
    const raw = (equityDraft[accountId] ?? '0').trim();
    const floor = parseFloat(raw);
    if (Number.isNaN(floor) || floor < 0) {
      setEquityErr('止损下限须为 ≥0 的数字');
      return;
    }
    setEquitySaving(accountId);
    setEquityErr('');
    try {
      await api.updateAccountEquityGuard(accountId, floor);
      await loadAccounts();
    } catch (e: unknown) {
      setEquityErr(e instanceof Error ? e.message : String(e));
    }
    setEquitySaving(null);
  };

  const handleResetEquityGuard = async (accountId: number) => {
    if (!confirm('重置后将清除「已触发」标记与初始总权益记录；下次启动策略时会重新记入。确定？')) return;
    setEquitySaving(accountId);
    setEquityErr('');
    try {
      await api.resetAccountEquityGuard(accountId);
      await loadAccounts();
    } catch (e: unknown) {
      setEquityErr(e instanceof Error ? e.message : String(e));
    }
    setEquitySaving(null);
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
          <Key size={16} className="text-yellow-400" />
          交易所API密钥管理
        </h3>

        {error && (
          <div className="flex items-center gap-2 text-red-400 text-sm mb-3 bg-red-900/20 rounded p-2">
            <AlertCircle size={14} /> {error}
          </div>
        )}

        {loadingAccounts && <p className="text-gray-500 text-sm py-4">加载中...</p>}

        {equityErr && (
          <div className="flex items-center gap-2 text-red-400 text-xs mb-2 bg-red-900/20 rounded px-2 py-1.5">
            <AlertCircle size={14} /> {equityErr}
          </div>
        )}

        {!loadingAccounts && accounts.map((a) => (
          <div key={a.id} className="py-3 border-b border-gray-800 space-y-2">
            <div className="flex items-center justify-between">
              <div>
                <span className="font-medium">{a.name}</span>
                <span className={`ml-2 text-xs px-2 py-0.5 rounded ${a.testnet ? 'bg-yellow-600/20 text-yellow-400' : 'bg-green-600/20 text-green-400'}`}>
                  {a.testnet ? '测试网' : '实盘'}
                </span>
                <span className="ml-1 text-xs px-2 py-0.5 rounded bg-purple-600/20 text-purple-400">
                  {a.exchange === 'okx' ? 'OKX' : '币安'}
                </span>
                <span className={`ml-1 text-xs px-2 py-0.5 rounded ${a.hedge_mode ? 'bg-blue-600/20 text-blue-400' : 'bg-purple-600/20 text-purple-400'}`}>
                  {a.hedge_mode ? '双向持仓' : '单向持仓'}
                </span>
                {a.equity_stop_triggered && (
                  <span className="ml-1 text-xs px-2 py-0.5 rounded bg-red-600/30 text-red-300">
                    总资产止损已触发
                  </span>
                )}
                <div className="text-xs text-gray-500 mt-0.5">
                  API密钥: {a.masked_key}
                </div>
              </div>
              <button onClick={() => handleDelete(a.id)} className="p-1.5 text-red-400 hover:bg-red-600/20 rounded shrink-0">
                <Trash2 size={16} />
              </button>
            </div>
            <div className="pl-0.5 space-y-1.5 bg-gray-800/50 rounded-lg p-2.5">
              <div className="flex items-center gap-2 text-xs text-gray-400">
                <Wallet size={14} className="text-orange-400 shrink-0" />
                <span className="font-medium text-gray-300">总资产止损（账户级）</span>
              </div>
              <p className="text-xs text-gray-500 leading-relaxed">
                填 <strong className="text-gray-400">0</strong> 表示关闭。非 0 时：本账户<strong className="text-gray-400">第一个</strong>策略启动会记入当时总权益；
                之后每分钟检查，若合约账户总权益（USDT）&lt; 下限，则<strong className="text-gray-400">立即市价平仓各策略持仓、停止本账户全部运行中策略</strong>并禁止再启动，直至重置。
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <label className="text-xs text-gray-500">止损下限 (USDT)</label>
                <input
                  type="number"
                  min={0}
                  step="0.01"
                  value={equityDraft[a.id] ?? '0'}
                  onChange={(e) => setEquityDraft((d) => ({ ...d, [a.id]: e.target.value }))}
                  className="w-28 bg-gray-700 border border-gray-600 rounded px-2 py-1 text-sm"
                />
                <button
                  type="button"
                  disabled={equitySaving === a.id}
                  onClick={() => void handleSaveEquityGuard(a.id)}
                  className="px-2 py-1 text-xs bg-orange-600/80 hover:bg-orange-600 rounded disabled:opacity-50"
                >
                  {equitySaving === a.id ? '…' : '保存'}
                </button>
                <button
                  type="button"
                  disabled={equitySaving === a.id}
                  onClick={() => void handleResetEquityGuard(a.id)}
                  className="px-2 py-1 text-xs border border-gray-600 rounded hover:bg-gray-700 disabled:opacity-50"
                >
                  重置状态
                </button>
              </div>
              {(a.equity_baseline_u != null && a.equity_baseline_u > 0) && (
                <p className="text-xs text-gray-500">
                  已记入初始总权益: <span className="text-gray-300">{a.equity_baseline_u.toFixed(4)} USDT</span>
                  {a.equity_baseline_at ? ` · ${a.equity_baseline_at.replace('T', ' ').slice(0, 19)}` : ''}
                </p>
              )}
            </div>
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
          <TrendingDown size={16} className="text-cyan-400" />
          跌幅榜自动策略
        </h3>
        <p className="text-xs text-gray-500 mb-3 leading-relaxed">
          北京时间窗口内按绑定账户所属交易所定时获取 USDT 永续合约 24h 跌幅榜前 N，
          为尚未创建的币种自动建策略并启动（同一币种不重复）。窗口结束后仅清理本功能创建的策略（撤单+市价平仓+删除），手动策略不受影响。
          另需顶栏总开关处于「运行中」才会执行扫描。
        </p>

        {declineErr && (
          <div className="flex items-center gap-2 text-red-400 text-xs mb-2 bg-red-900/20 rounded px-2 py-1.5">
            <AlertCircle size={14} /> {declineErr}
          </div>
        )}
        {declineOk && (
          <div className="text-green-400 text-xs mb-2 bg-green-900/20 rounded px-2 py-1.5">{declineOk}</div>
        )}

        {declineLoading ? (
          <p className="text-gray-500 text-sm">加载中…</p>
        ) : (
          <div className="space-y-3 text-sm">
            {declineStatus && (
              <div className="text-xs text-gray-400 bg-gray-800/60 rounded p-2.5 space-y-1">
                <div>
                  状态：
                  <span className={declineStatus.in_window ? 'text-cyan-300' : 'text-gray-300'}>
                    {declineStatus.enabled
                      ? (declineStatus.in_window ? '运行窗口内' : '等待下一窗口')
                      : '未启用'}
                  </span>
                  {' · '}自动策略数：<span className="text-gray-200">{declineStatus.auto_strategy_count}</span>
                </div>
                <div>
                  上次刷新：{declineStatus.last_refresh_at || '—'}
                  {' · '}下次：{declineStatus.next_refresh_at || '—'}
                </div>
                {declineStatus.current_symbols?.length > 0 && (
                  <div className="text-gray-500 break-all">
                    当前榜：{declineStatus.current_symbols.join(', ')}
                  </div>
                )}
                {declineStatus.last_error && (
                  <div className="text-amber-400">最近错误：{declineStatus.last_error}</div>
                )}
              </div>
            )}

            <label className="flex items-center gap-2 text-gray-300">
              <input
                type="checkbox"
                checked={declineCfg.enabled}
                onChange={(e) => setDeclineCfg({ ...declineCfg, enabled: e.target.checked })}
              />
              启用跌幅榜自动策略
            </label>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-gray-400 mb-1">绑定账户</label>
                <select
                  value={declineCfg.account_id ?? ''}
                  onChange={(e) =>
                    setDeclineCfg({
                      ...declineCfg,
                      account_id: e.target.value ? Number(e.target.value) : null,
                    })
                  }
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm"
                >
                  <option value="">请选择账户</option>
                  {accounts.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name} ({a.exchange === 'okx' ? 'OKX' : '币安'})
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">方向</label>
                <select
                  value={declineCfg.direction}
                  onChange={(e) =>
                    setDeclineCfg({
                      ...declineCfg,
                      direction: e.target.value as 'long' | 'short',
                    })
                  }
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm"
                >
                  <option value="short">做空</option>
                  <option value="long">做多</option>
                </select>
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">开始时间（北京时间）</label>
                <input
                  type="time"
                  value={declineCfg.start_time}
                  onChange={(e) => setDeclineCfg({ ...declineCfg, start_time: e.target.value })}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">结束时间（北京时间）</label>
                <input
                  type="time"
                  value={declineCfg.end_time}
                  onChange={(e) => setDeclineCfg({ ...declineCfg, end_time: e.target.value })}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm"
                />
                <span className="text-xs text-gray-600">例：03:00→00:00 表示跨日至午夜</span>
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">刷新间隔（分钟）</label>
                <input
                  type="number"
                  min={1}
                  value={declineCfg.refresh_interval_min}
                  onChange={(e) =>
                    setDeclineCfg({
                      ...declineCfg,
                      refresh_interval_min: Number(e.target.value),
                    })
                  }
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">跌幅榜前 N 名</label>
                <input
                  type="number"
                  min={1}
                  max={100}
                  value={declineCfg.top_n}
                  onChange={(e) =>
                    setDeclineCfg({ ...declineCfg, top_n: Number(e.target.value) })
                  }
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm"
                />
              </div>
            </div>

            <div className="border-t border-gray-800 pt-3">
              <StrategyParamFieldsForm
                value={declineCfg.params}
                onChange={(params) => setDeclineCfg({ ...declineCfg, params })}
              />
            </div>

            <button
              type="button"
              disabled={declineSaving}
              onClick={() => void handleSaveDeclineRank()}
              className="px-4 py-2 bg-cyan-600 hover:bg-cyan-700 disabled:opacity-50 rounded text-sm"
            >
              {declineSaving ? '保存中…' : '保存跌幅榜配置'}
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
