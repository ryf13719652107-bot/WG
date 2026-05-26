import { useDashboardStore } from '../../store/dashboardStore';
import { TrendingDown, Layers, BarChart3, Activity, Target, Wallet, PiggyBank, Gauge, TrendingUp } from 'lucide-react';

function PanelRow({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="flex justify-between items-baseline gap-3 py-1.5 border-b border-gray-800/80 last:border-0">
      <span className="text-gray-500 text-xs shrink-0">{label}</span>
      <span className={`text-sm font-mono font-medium text-right ${valueClass ?? 'text-gray-200'}`}>{value}</span>
    </div>
  );
}

export default function DashboardPage() {
  const { data } = useDashboardStore();
  const strategyStats = data.strategy_stats || [];

  const leverageColor = data.leverage_multiplier > 5 ? 'text-red-400' : data.leverage_multiplier > 2 ? 'text-yellow-400' : 'text-green-400';

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

  const fmtPnl = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(2)} USDT`;

  return (
    <div className="space-y-4">
      <h2 className="text-xl font-bold">仪表盘</h2>

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
            {strategyStats.length === 0 ? (
              <div className="text-gray-600 text-sm py-4 text-center">暂无策略数据</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-gray-500 text-left">
                      <th className="pb-2">策略</th>
                      <th className="pb-2">方向</th>
                      <th className="pb-2">状态</th>
                      <th className="pb-2">总止盈</th>
                      <th className="pb-2">今日止盈</th>
                      <th className="pb-2">止损</th>
                    </tr>
                  </thead>
                  <tbody>
                    {strategyStats.map((s) => (
                      <tr key={s.strategy_id} className="border-t border-gray-800">
                        <td className="py-2 font-mono">{s.symbol}</td>
                        <td className={s.direction === 'long' ? 'text-green-400' : 'text-red-400'}>
                          {s.direction === 'long' ? '做多' : '做空'}
                        </td>
                        <td>
                          <span className={`text-xs px-1.5 py-0.5 rounded ${
                            s.status === 'running' ? 'bg-green-600/20 text-green-400' :
                            s.status === 'error' ? 'bg-red-600/20 text-red-400' :
                            'bg-gray-700 text-gray-400'
                          }`}>
                            {s.status === 'running' ? '运行' : s.status === 'error' ? '异常' : '停止'}
                          </span>
                        </td>
                        <td className="font-mono text-green-400">{s.tp_total}</td>
                        <td className="font-mono text-green-400">{s.tp_today}</td>
                        <td className="text-gray-500 text-xs">
                          {s.sl_events.length === 0 ? (
                            <span className="text-gray-600">-</span>
                          ) : (
                            s.sl_events.slice(0, 1).map((e, i) => (
                              <span key={i} className="text-red-400" title={`${e.time} 成交价=${e.exit_price} 数量=${e.quantity}`}>
                                触发 {s.sl_events.length}次
                              </span>
                            ))
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {strategyStats.some((s) => s.sl_events.length > 0) && (
              <details className="mt-3 text-xs text-gray-500">
                <summary className="cursor-pointer hover:text-gray-300">止损明细</summary>
                <div className="mt-2 space-y-2">
                  {strategyStats.filter((s) => s.sl_events.length > 0).map((s) => (
                    <div key={s.strategy_id} className="pl-2 border-l border-red-500/30">
                      <span className="font-mono text-gray-300">{s.symbol}</span>
                      <span className={`ml-2 ${s.direction === 'long' ? 'text-green-400' : 'text-red-400'}`}>
                        {s.direction === 'long' ? '多' : '空'}
                      </span>
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

        <aside className="w-full xl:w-72 shrink-0 space-y-3">
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="flex items-center gap-2 text-emerald-400/90 text-sm font-semibold mb-2 border-b border-gray-800 pb-2">
              <Target size={16} />
              累计数据
            </div>
            <div>
              <PanelRow
                label="累计已实现"
                value={fmtPnl(data.total_realized_pnl)}
                valueClass={data.total_realized_pnl >= 0 ? 'text-emerald-400' : 'text-orange-400'}
              />
              <PanelRow label="累计交易" value={`${data.total_trades} 笔`} />
              <PanelRow label="累计胜率" value={`${data.total_win_rate_pct.toFixed(1)}%`} valueClass="text-indigo-400" />
              <PanelRow
                label="多单盈亏(累计)"
                value={fmtPnl(data.total_pnl_long)}
                valueClass={data.total_pnl_long >= 0 ? 'text-green-400' : 'text-red-400'}
              />
              <PanelRow
                label="空单盈亏(累计)"
                value={fmtPnl(data.total_pnl_short)}
                valueClass={data.total_pnl_short >= 0 ? 'text-green-400' : 'text-red-400'}
              />
            </div>
          </div>

          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="flex items-center gap-2 text-cyan-400/90 text-sm font-semibold mb-2 border-b border-gray-800 pb-2">
              <BarChart3 size={16} />
              当日数据
            </div>
            <div>
              <PanelRow
                label="当日盈亏"
                value={fmtPnl(data.daily_pnl)}
                valueClass={data.daily_pnl >= 0 ? 'text-green-400' : 'text-red-400'}
              />
              <PanelRow label="当日交易" value={`${data.daily_trades} 笔`} />
              <PanelRow label="当日胜率" value={`${data.win_rate_pct.toFixed(1)}%`} valueClass="text-blue-400" />
              <PanelRow
                label="当日盈亏/余额"
                value={`${data.daily_pnl_pct >= 0 ? '+' : ''}${data.daily_pnl_pct.toFixed(2)}%`}
                valueClass={data.daily_pnl_pct >= 0 ? 'text-green-400' : 'text-red-400'}
              />
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
