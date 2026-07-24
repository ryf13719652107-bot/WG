import { useEffect, useState } from 'react';
import { api } from '../../services/api';
import type { Account } from '../../types';
import type { DeclineRankAutoConfig, DeclineRankAutoStatus } from '../../types/declineRank';
import { defaultDeclineRankConfig } from '../../types/declineRank';
import StrategyParamFieldsForm from './StrategyParamFieldsForm';
import { AlertCircle, TrendingDown } from 'lucide-react';

interface Props {
  accounts: Account[];
  onCancel: () => void;
  onSaved?: () => void;
}

export default function DeclineRankAutoForm({ accounts, onCancel, onSaved }: Props) {
  const [cfg, setCfg] = useState<DeclineRankAutoConfig>(defaultDeclineRankConfig());
  const [status, setStatus] = useState<DeclineRankAutoStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState('');
  const [ok, setOk] = useState('');

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setErr('');
      try {
        const [c, st] = await Promise.all([
          api.getDeclineRankConfig(),
          api.getDeclineRankStatus(),
        ]);
        if (cancelled) return;
        setCfg({
          ...defaultDeclineRankConfig(),
          ...c,
          params: { ...defaultDeclineRankConfig().params, ...(c.params || {}) },
        });
        setStatus(st);
      } catch (e: unknown) {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      }
      if (!cancelled) setLoading(false);
    })();
    return () => { cancelled = true; };
  }, []);

  const handleSave = async () => {
    setSaving(true);
    setErr('');
    setOk('');
    try {
      if (cfg.enabled && !cfg.account_id) {
        setErr('启用自动策略时必须选择绑定账户');
        setSaving(false);
        return;
      }
      if (cfg.refresh_interval_min < 1) {
        setErr('刷新间隔至少 1 分钟');
        setSaving(false);
        return;
      }
      if (cfg.top_n < 1 || cfg.top_n > 100) {
        setErr('跌幅榜前 N 须在 1–100');
        setSaving(false);
        return;
      }
      const saved = await api.saveDeclineRankConfig(cfg);
      setCfg({
        ...defaultDeclineRankConfig(),
        ...saved,
        params: { ...defaultDeclineRankConfig().params, ...(saved.params || {}) },
      });
      const st = await api.getDeclineRankStatus();
      setStatus(st);
      setOk('已保存跌幅榜自动策略配置');
      onSaved?.();
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    }
    setSaving(false);
  };

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-3 max-w-2xl">
      <div className="flex items-center gap-2">
        <TrendingDown size={18} className="text-cyan-400" />
        <h3 className="text-base font-semibold text-white">跌幅榜自动策略</h3>
      </div>
      <p className="text-xs text-gray-500 leading-relaxed">
        北京时间窗口内按绑定账户所属交易所定时获取 USDT 永续合约 24h 跌幅榜前 N，
        为尚未创建的币种自动建策略并启动（同一币种不重复）。窗口结束后仅清理本功能创建的策略（撤单+市价平仓+删除），手动策略不受影响。
        另需顶栏总开关处于「运行中」才会执行扫描建仓。
      </p>

      {err && (
        <div className="flex items-start gap-2 text-red-400 text-xs bg-red-900/20 rounded px-2 py-1.5">
          <AlertCircle size={14} className="shrink-0 mt-0.5" />
          <span className="break-all">{err}</span>
        </div>
      )}
      {ok && (
        <div className="text-green-400 text-xs bg-green-900/20 rounded px-2 py-1.5">{ok}</div>
      )}

      {loading ? (
        <p className="text-gray-500 text-sm">加载中…</p>
      ) : (
        <div className="space-y-3 text-sm">
          {status && (
            <div className="text-xs text-gray-400 bg-gray-800/60 rounded p-2.5 space-y-1">
              <div>
                状态：
                <span className={status.in_window ? 'text-cyan-300' : 'text-gray-300'}>
                  {status.enabled
                    ? (status.in_window ? '运行窗口内' : '等待下一窗口')
                    : '未启用'}
                </span>
                {' · '}自动策略数：<span className="text-gray-200">{status.auto_strategy_count}</span>
              </div>
              <div>
                上次刷新：{status.last_refresh_at || '—'}
                {' · '}下次：{status.next_refresh_at || '—'}
              </div>
              {status.current_symbols?.length > 0 && (
                <div className="text-gray-500 break-all">
                  当前榜：{status.current_symbols.join(', ')}
                </div>
              )}
              {status.last_error && (
                <div className="text-amber-400">最近错误：{status.last_error}</div>
              )}
            </div>
          )}

          <label className="flex items-center gap-2 text-gray-300">
            <input
              type="checkbox"
              checked={cfg.enabled}
              onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })}
            />
            启用跌幅榜自动策略
          </label>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-gray-400 mb-1">绑定账户</label>
              <select
                value={cfg.account_id ?? ''}
                onChange={(e) =>
                  setCfg({
                    ...cfg,
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
                value={cfg.direction}
                onChange={(e) =>
                  setCfg({ ...cfg, direction: e.target.value as 'long' | 'short' })
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
                value={cfg.start_time}
                onChange={(e) => setCfg({ ...cfg, start_time: e.target.value })}
                className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">结束时间（北京时间）</label>
              <input
                type="time"
                value={cfg.end_time}
                onChange={(e) => setCfg({ ...cfg, end_time: e.target.value })}
                className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm"
              />
              <span className="text-xs text-gray-600">例：03:00→00:00 表示跨日至午夜</span>
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">刷新间隔（分钟）</label>
              <input
                type="number"
                min={1}
                value={cfg.refresh_interval_min}
                onChange={(e) =>
                  setCfg({ ...cfg, refresh_interval_min: Number(e.target.value) })
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
                value={cfg.top_n}
                onChange={(e) => setCfg({ ...cfg, top_n: Number(e.target.value) })}
                className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm"
              />
            </div>
          </div>

          <div className="border-t border-gray-800 pt-3">
            <StrategyParamFieldsForm
              value={cfg.params}
              onChange={(params) => setCfg({ ...cfg, params })}
            />
          </div>

          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={onCancel}
              className="px-4 py-1.5 text-sm bg-gray-700 hover:bg-gray-600 rounded-lg"
            >
              取消
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={() => void handleSave()}
              className="px-4 py-1.5 text-sm bg-cyan-600 hover:bg-cyan-700 disabled:opacity-50 rounded-lg font-medium"
            >
              {saving ? '保存中…' : '保存并启用配置'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
