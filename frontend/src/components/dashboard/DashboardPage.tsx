import { useMemo, useState } from 'react';
import { useDashboardStore } from '../../store/dashboardStore';
import { api } from '../../services/api';
import { TrendingDown, Layers, Activity, Wallet, PiggyBank, Gauge, TrendingUp, AlertTriangle, ArrowUpDown, Clock } from 'lucide-react';

type TpSortKey = 'tp_total' | 'tp_today';
type SortDir = 'asc' | 'desc';

function SortableTh({
  label,
  active,
  dir,
  onClick,
}: {
  label: string;
  active: boolean;
  dir: SortDir;
  onClick: () => void;
}) {
  return (
    <th className="pb-2">
      <button
        type="button"
        onClick={onClick}
        className={`inline-flex items-center gap-1 hover:text-gray-300 ${active ? 'text-gray-200' : 'text-gray-500'}`}
      >
        {label}
        <ArrowUpDown size={12} className={active ? 'text-cyan-400' : 'opacity-40'} />
        {active && <span className="text-[10px] text-cyan-400/80">{dir === 'desc' ? '↓' : '↑'}</span>}
      </button>
    </th>
  );
}

function StrategyToggle({
  strategyId,
  running,
  disabled,
  onDone,
}: {
  strategyId: number;
  running: boolean;
  disabled?: boolean;
  onDone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const toggle = async () => {
    setBusy(true);
    setErr('');
    try {
      if (running) {
        await api.stopStrategy(strategyId);
      } else {
        await api.startStrategy(strategyId);
      }
      onDone();
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : '操作失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col items-center gap-0.5">
      <button
        type="button"
        role="switch"
        aria-checked={running}
        disabled={disabled || busy}
        onClick={toggle}
        title={running ? '点击停止策略' : '点击启动策略'}
        className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ${
          running ? 'bg-green-600' : 'bg-gray-700'
        } ${disabled || busy ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
      >
        <span
          className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
            running ? 'translate-x-5' : ''
          }`}
        />
      </button>
      {err && <span className="text-[10px] text-red-400 max-w-[88px] text-center leading-tight">{err}</span>}
    </div>
  );
}

export default function DashboardPage() {
  const { data, selectedAccountId } = useDashboardStore();
  const strategyStats = data.strategy_stats || [];
  const specialRestarts = data.special_sl_restarts || [];
  const tw = data.trading_window;

  const [tpSortKey, setTpSortKey] = useState<TpSortKey>('tp_total');
  const [tpSortDir, setTpSortDir] = useState<SortDir>('desc');
  const [refreshKey, setRefreshKey] = useState(0);

  const toggleSort = (key: TpSortKey) => {
    if (tpSortKey === key) {
      setTpSortDir((d) => (d === 'desc' ? 'asc' : 'desc'));
    } else {
      setTpSortKey(key);
      setTpSortDir('desc');
    }
  };

  const sortedStats = useMemo(() => {
    const mul = tpSortDir === 'desc' ? -1 : 1;
    return [...strategyStats].sort((a, b) => mul * (a[tpSortKey] - b[tpSortKey]));
  }, [strategyStats, tpSortKey, tpSortDir, refreshKey]);

  const refetchDashboard = () => {
    setRefreshKey((k) => k + 1);
    const aid = selectedAccountId ?? undefined;
    api.getDashboard(aid).then((d) => useDashboardStore.getState().setData(d)).catch(() => {});
  };

  const leverageColor = data.leverage_multiplier > 5 ? 'text-red-400' : data.leverage_multiplier > 2 ? 'text-yellow-400' : 'text-green-400';
  const canStartBySchedule = !tw?.enabled || tw?.within_window;

  const mainStats = [
    { label: '钱包余额', value: `${data.total_balance.toFixed(2)} USDT`, icon: Wallet, color: 'text-blue-400' },
    { label: '可用余额', value: `${data.available_balance.toFixed(2)} USDT`, icon: PiggyBank, color: 'text-green-400' },
    { label: '杠杆倍数', value: `${data.leverage_multiplier.toFixed(2)}x`, icon: Gauge, color: leverageColor },
    {
      label: '未实现盈亏',
      value: `${data.unrealized_pnl >= 0 ? '+' : ''}${data.unrealized_pnl.toFixed(2)} USDT`,
      icon: TrendingDown,
      color: data.unrealized_pnl >= 0 ? 'text-green-400' : 'text-red-400',
    },
    { label: '活跃策略', value: String(data.active_strategies), icon: Activity, color: 'text-yellow-400' },
    { label: '当前持仓', value: String(data.open_positions), icon: Layers, color: 'text-purple-400' },
  ];

  return (
    <div className="space-y-4">
      <h2 className="text-xl font-bold">仪表盘</h2>

      {tw?.enabled && (
        <div
          className={`flex items-center gap-2 text-xs px-3 py-2 rounded-lg border ${
            tw.within_window
              ? 'bg-green-900/20 border-green-800/50 text-green-300'
              : 'bg-amber-900/20 border-amber-800/50 text-amber-200'
          }`}
        >
          <Clock size={14} />
          {tw.within_window ? (
            <span>
              交易时段内（北京时间 {tw.start_hm}–{tw.end_hm}），策略可运行
            </span>
          ) : (
            <span>
              当前为盘外时段（允许时段 {tw.start_hm}–{tw.end_hm}），{tw.end_hm} 已收市全平；{tw.start_hm} 将自动恢复被时段停止的策略
            </span>
          )}
        </div>
      )}

      <div className="flex flex-col xl:flex-row gap-4 items-start">
        <div className="flex-1 min-w-0 space-y-4 w-full">
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
            {mainStats.map(({ label, value, icon: Icon, color }) => (
              <div key={label} className="bg-gray-900 border border-gray-800 rounded-lg p-3">
                <div className="flex items-center gap-2 text-gray-500 text-xs mb-1">
                  <Icon size={14} className={color} />
                  {label}
                </div>
                <div className={`text-lg font-semibold ${color}`}>{value}</div>
              </div>
            ))}
          </div>

          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <h3 className="text-sm font-semibold text-gray-300 mb-3 flex items-center gap-2">
              <TrendingUp size={14} className="text-green-400" />
              策略止盈/止损概览
            </h3>
            {sortedStats.length === 0 ? (
              <div className="text-gray-600 text-sm py-4 text-center">暂无策略数据</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-gray-500 text-left">
                      <th className="pb-2">策略</th>
                      <th className="pb-2">方向</th>
                      <th className="pb-2">状态</th>
                      <SortableTh
                        label="总止盈"
                        active={tpSortKey === 'tp_total'}
                        dir={tpSortDir}
                        onClick={() => toggleSort('tp_total')}
                      />
                      <SortableTh
                        label="今日止盈"
                        active={tpSortKey === 'tp_today'}
                        dir={tpSortDir}
                        onClick={() => toggleSort('tp_today')}
                      />
                      <th className="pb-2">止损</th>
                      <th className="pb-2 text-center w-20">运行</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedStats.map((s) => {
                      const isRunning = s.status === 'running';
                      const startBlocked = !isRunning && !canStartBySchedule;
                      return (
                        <tr key={s.strategy_id} className="border-t border-gray-800">
                          <td className="py-2 font-mono">{s.symbol}</td>
                          <td className={s.direction === 'long' ? 'text-green-400' : 'text-red-400'}>
                            {s.direction === 'long' ? '做多' : '做空'}
                          </td>
                          <td>
                            <span
                              className={`text-xs px-1.5 py-0.5 rounded ${
                                s.status === 'running'
                                  ? 'bg-green-600/20 text-green-400'
                                  : s.status === 'error'
                                    ? 'bg-red-600/20 text-red-400'
                                    : 'bg-gray-700 text-gray-400'
                              }`}
                            >
                              {s.status === 'running' ? '运行' : s.status === 'error' ? '异常' : '停止'}
                            </span>
                          </td>
                          <td className="font-mono text-green-400">{s.tp_total}</td>
                          <td className="font-mono text-green-400">{s.tp_today}</td>
                          <td className="text-gray-500 text-xs">
                            {s.sl_events.length === 0 ? (
                              <span className="text-gray-600">-</span>
                            ) : (
                              <span
                                className="text-red-400"
                                title={s.sl_events.map((e) => `${e.time} @${e.exit_price}`).join('\n')}
                              >
                                触发 {s.sl_events.length}次
                              </span>
                            )}
                          </td>
                          <td className="py-2 text-center">
                            <StrategyToggle
                              strategyId={s.strategy_id}
                              running={isRunning}
                              disabled={startBlocked}
                              onDone={refetchDashboard}
                            />
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {strategyStats.some((s) => s.sl_events.length > 0) && (
              <details className="mt-3 text-xs text-gray-500">
                <summary className="cursor-pointer hover:text-gray-300">止损明细</summary>
                <div className="mt-2 space-y-2">
                  {strategyStats
                    .filter((s) => s.sl_events.length > 0)
                    .map((s) => (
                      <div key={s.strategy_id} className="pl-2 border-l border-red-500/30">
                        <span className="font-mono text-gray-300">{s.symbol}</span>
                        {s.sl_events.map((e, i) => (
                          <div key={i} className="ml-3 text-gray-500">
                            {e.time} 成交价={e.exit_price?.toFixed(6)} 数量={e.quantity?.toFixed(4)}
                          </div>
                        ))}
                      </div>
                    ))}
                </div>
              </details>
            )}
          </div>
        </div>

        <aside className="w-full xl:w-80 shrink-0">
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="flex items-center gap-2 text-amber-400/90 text-sm font-semibold mb-2 border-b border-gray-800 pb-2">
              <AlertTriangle size={16} />
              止损减仓过小 · 全平重启
            </div>
            <p className="text-xs text-gray-500 mb-3">
              加仓层数≥3 后触发止损，若减仓量低于交易所最小下单量，系统会全平并按策略配置重开。
            </p>
            {specialRestarts.length === 0 ? (
              <div className="text-gray-600 text-sm py-6 text-center">暂无记录</div>
            ) : (
              <ul className="space-y-2 max-h-[420px] overflow-y-auto text-xs">
                {specialRestarts.map((e, i) => (
                  <li key={`${e.strategy_id}-${e.time}-${i}`} className="border-b border-gray-800/80 pb-2 last:border-0">
                    <div className="flex justify-between gap-2">
                      <span className="font-mono text-gray-200">{e.symbol}</span>
                      <span className={e.direction === 'long' ? 'text-green-400' : 'text-red-400'}>
                        {e.direction === 'long' ? '多' : '空'}
                      </span>
                    </div>
                    <div className="text-gray-500 mt-0.5">{e.time}</div>
                    <div className="text-gray-400 mt-0.5 font-mono">
                      价 {e.exit_price?.toFixed(4)} · 量 {e.quantity?.toFixed(4)} · 盈亏{' '}
                      <span className={e.realized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}>
                        {e.realized_pnl >= 0 ? '+' : ''}
                        {e.realized_pnl?.toFixed(2)}
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
