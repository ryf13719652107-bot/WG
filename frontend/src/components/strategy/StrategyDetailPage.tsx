import { useEffect, useState, useCallback, useRef } from 'react';
import { useParams, Link } from 'react-router-dom';
import { api } from '../../services/api';
import type { Strategy } from '../../types/strategy';
import type { Trade } from '../../types';
import { ArrowLeft, Terminal } from 'lucide-react';
import { formatCloseReason } from '../../utils/tradeUi';

interface LogEntry { time: string; level: string; message: string; }

function logColor(level: string) {
  switch (level) {
    case 'success': return 'text-green-400';
    case 'error': return 'text-red-400';
    case 'warning': return 'text-yellow-400';
    default: return 'text-gray-300';
  }
}

function fmtTime(s: string | null) {
  if (!s) return '-';
  return new Date(s).toLocaleString();
}

function pnlColor(v: number | null) {
  if (v == null) return 'text-gray-400';
  return v >= 0 ? 'text-green-400' : 'text-red-400';
}

export default function StrategyDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [strategy, setStrategy] = useState<Strategy | null>(null);
  const [positions, setPositions] = useState<any[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(true);

  const loadRef = useRef<() => void>(() => {});

  const load = useCallback(async () => {
    if (!id) return;
    try {
      const s = await api.getStrategy(Number(id));
      setStrategy(s);
      setLoading(false);

      try {
        const ep = await api.getExchangePositions(Number(id));
        setPositions(ep);
      } catch { setPositions([]); }

      try {
        const tr = await api.listTrades({ strategy_id: Number(id), limit: 50 });
        setTrades(tr.trades);
      } catch { setTrades([]); }

      try {
        const l = await api.getStrategyLogs(Number(id), 100);
        setLogs(l);
      } catch { setLogs([]); }
    } catch {
      setStrategy(null);
      setLoading(false);
    }
  }, [id]);
  loadRef.current = load;

  useEffect(() => { load(); }, [id]);

  useEffect(() => {
    const timer = setInterval(() => loadRef.current(), 10000);
    return () => clearInterval(timer);
  }, []);

  if (loading) {
    return <div className="text-center text-gray-400 py-20">加载中...</div>;
  }

  if (!strategy) {
    return <div className="text-center text-gray-400 py-20">策略不存在</div>;
  }

  const labelClass = 'text-xs text-gray-500';
  const valClass = 'text-sm text-gray-200';

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Link to="/strategies" className="text-gray-400 hover:text-white transition-colors">
          <ArrowLeft size={20} />
        </Link>
        <h2 className="text-xl font-bold">{strategy.symbol}</h2>
        <span className={`text-xs px-2 py-0.5 rounded ${
          strategy.direction === 'long' ? 'bg-green-600/20 text-green-400' : 'bg-red-600/20 text-red-400'
        }`}>
          {strategy.direction === 'long' ? '做多' : '做空'}
        </span>
        <span className={`text-xs px-2 py-0.5 rounded ${
          strategy.status === 'running' ? 'bg-green-600/20 text-green-400' :
          strategy.status === 'error' ? 'bg-red-600/20 text-red-400' :
          'bg-gray-700 text-gray-400'
        }`}>
          {strategy.status === 'running' ? '运行中' : strategy.status === 'error' ? '异常' : '已停止'}
        </span>
      </div>

      {/* Strategy parameters */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="font-semibold mb-3 text-sm">网格策略参数</h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div>
            <span className={labelClass}>交易对</span>
            <div className={valClass}>{strategy.symbol}</div>
          </div>
          <div>
            <span className={labelClass}>启动时间</span>
            <div className={valClass}>{fmtTime(strategy.started_at)}</div>
          </div>
          <div>
            <span className={labelClass}>首单仓位</span>
            <div className={valClass}>{strategy.base_qty_type === 'margin_pct' ? `保证金${strategy.base_qty_value}%` : `${strategy.base_qty_value} USDT`}</div>
          </div>
          <div>
            <span className={labelClass}>止盈比例</span>
            <div className={valClass}>{strategy.tp_pct}% (限价单)</div>
          </div>
          <div>
            <span className={labelClass}>首层跌幅</span>
            <div className={valClass}>{strategy.grid_drop_base_pct}%</div>
          </div>
          <div>
            <span className={labelClass}>跌幅间隔倍数</span>
            <div className={valClass}>x{strategy.grid_interval_multiplier}</div>
          </div>
          <div>
            <span className={labelClass}>仓位递增倍数</span>
            <div className={valClass}>x{strategy.position_multiplier}</div>
          </div>
          <div>
            <span className={labelClass}>最大层数</span>
            <div className={valClass}>{strategy.max_layers}</div>
          </div>
          <div>
            <span className={labelClass}>累计亏损阈值</span>
            <div className={valClass}>{strategy.cumulative_loss_threshold_u > 0 ? `${strategy.cumulative_loss_threshold_u} U` : '已禁用'}</div>
          </div>
          <div>
            <span className={labelClass}>平仓重开</span>
            <div className={valClass}>{strategy.reopen_after_close ? '是' : '否'}</div>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Exchange positions */}
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="font-semibold mb-3 text-sm">
            当前持仓
            <span className="text-gray-500 ml-2 text-xs">({positions.length} 个)</span>
          </h3>
          {positions.length === 0 ? (
            <div className="text-gray-600 text-sm py-4 text-center">暂无持仓</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-gray-500 border-b border-gray-800">
                    <th className="text-left py-1.5 px-2">币种</th>
                    <th className="text-left py-1.5 px-2">方向</th>
                    <th className="text-right py-1.5 px-2">USDT</th>
                    <th className="text-right py-1.5 px-2">入场价</th>
                    <th className="text-right py-1.5 px-2">未实现盈亏</th>
                    <th className="text-right py-1.5 px-2">盈亏%</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((p: any, i: number) => (
                    <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                      <td className="py-1.5 px-2 text-gray-200 font-mono">{p.symbol}</td>
                      <td className="py-1.5 px-2">
                        <span className={`px-1.5 py-0.5 rounded text-xs ${p.side === 'long' ? 'bg-green-600/20 text-green-400' : 'bg-red-600/20 text-red-400'}`}>
                          {p.side === 'long' ? '多' : '空'}
                        </span>
                      </td>
                      <td className="py-1.5 px-2 text-right text-gray-200 font-mono">{p.usdt?.toFixed(2)}</td>
                      <td className="py-1.5 px-2 text-right text-gray-200 font-mono">{p.entry_price?.toFixed(6)}</td>
                      <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(p.unrealized_pnl)}`}>
                        {p.unrealized_pnl >= 0 ? '+' : ''}{p.unrealized_pnl?.toFixed(2)}
                      </td>
                      <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(p.pnl_pct)}`}>
                        {p.pnl_pct >= 0 ? '+' : ''}{p.pnl_pct?.toFixed(2)}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {/* Trade history */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="font-semibold mb-3 text-sm">
          交易记录
          <span className="text-gray-500 ml-2 text-xs">({trades.length} 条)</span>
        </h3>
        {trades.length === 0 ? (
          <div className="text-gray-600 text-sm py-4 text-center">暂无交易记录</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800">
                  <th className="text-left py-1.5 px-2">币种</th>
                  <th className="text-left py-1.5 px-2">方向</th>
                  <th className="text-right py-1.5 px-2">入场价</th>
                  <th className="text-right py-1.5 px-2">出场价</th>
                  <th className="text-right py-1.5 px-2">盈亏</th>
                  <th className="text-right py-1.5 px-2">盈亏%</th>
                  <th className="text-right py-1.5 px-2">层/网格</th>
                  <th className="text-right py-1.5 px-2">原因</th>
                  <th className="text-right py-1.5 px-2">时间</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => (
                  <tr key={t.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                    <td className="py-1.5 px-2 text-gray-200 font-mono">{t.symbol}</td>
                    <td className="py-1.5 px-2">
                      <span className={`px-1.5 py-0.5 rounded text-xs ${t.side === 'long' ? 'bg-green-600/20 text-green-400' : 'bg-red-600/20 text-red-400'}`}>
                        {t.side === 'long' ? '多' : '空'}
                      </span>
                    </td>
                    <td className="py-1.5 px-2 text-right text-gray-200 font-mono">{t.entry_price?.toFixed(8)}</td>
                    <td className="py-1.5 px-2 text-right text-gray-200 font-mono">{t.exit_price?.toFixed(8)}</td>
                    <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(t.realized_pnl)}`}>
                      {t.realized_pnl >= 0 ? '+' : ''}{t.realized_pnl?.toFixed(2)}
                    </td>
                    <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(t.pnl_pct)}`}>
                      {t.pnl_pct >= 0 ? '+' : ''}{t.pnl_pct?.toFixed(2)}%
                    </td>
                    <td className="py-1.5 px-2 text-right text-gray-400">L{t.layer}/G{t.grid_level ?? 0}</td>
                    <td className="py-1.5 px-2 text-right text-gray-400">{formatCloseReason(t.close_reason)}</td>
                    <td className="py-1.5 px-2 text-right text-gray-500">{fmtTime(t.exit_time)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Logs */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="font-semibold mb-3 text-sm flex items-center gap-2">
          <Terminal size={14} />
          交易日志
          <span className="text-gray-500 text-xs">({logs.length} 条)</span>
        </h3>
        {logs.length === 0 ? (
          <div className="text-gray-600 text-sm py-4 text-center">暂无日志</div>
        ) : (
          <div className="max-h-80 overflow-y-auto">
            <table className="w-full text-xs font-mono">
              <tbody>
                {logs.map((l, i) => (
                  <tr key={i} className="border-b border-gray-800/30">
                    <td className="py-1 pr-3 text-gray-600 whitespace-nowrap align-top w-16">{l.time}</td>
                    <td className={`py-1 pr-3 whitespace-nowrap align-top w-16 ${logColor(l.level)}`}>
                      [{l.level === 'success' ? 'OK' : l.level === 'error' ? 'ERR' : l.level === 'warning' ? 'WARN' : 'INFO'}]
                    </td>
                    <td className={`py-1 ${logColor(l.level)}`}>{l.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
