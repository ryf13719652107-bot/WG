import { useEffect, useState, useCallback, useRef } from 'react';
import { useParams, Link } from 'react-router-dom';
import { api } from '../../services/api';
import type { Strategy } from '../../types/strategy';
import { ArrowLeft, Terminal, TrendingUp, AlertTriangle } from 'lucide-react';

interface LogEntry { time: string; level: string; message: string; }

interface StrategyStats {
  tp_total: number;
  tp_today: number;
  sl_events: Array<{ time: string; exit_price: number; quantity: number }>;
}

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

export default function StrategyDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [strategy, setStrategy] = useState<Strategy | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [stats, setStats] = useState<StrategyStats>({ tp_total: 0, tp_today: 0, sl_events: [] });
  const [loading, setLoading] = useState(true);

  const loadRef = useRef<() => void>(() => {});

  const load = useCallback(async () => {
    if (!id) return;
    try {
      const s = await api.getStrategy(Number(id));
      setStrategy(s);
      setLoading(false);

      try {
        const l = await api.getStrategyLogs(Number(id), 100);
        setLogs(l);
      } catch { setLogs([]); }

      try {
        const st = await api.getStrategyStats(Number(id));
        setStats(st);
      } catch { setStats({ tp_total: 0, tp_today: 0, sl_events: [] }); }
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
            <span className={labelClass}>止损触发亏损 (U)</span>
            <div className={valClass}>{strategy.cumulative_loss_threshold_u > 0 ? `${strategy.cumulative_loss_threshold_u} U` : '已禁用'}</div>
          </div>
          <div>
            <span className={labelClass}>止损平仓比例</span>
            <div className={valClass}>{strategy.stop_loss_close_pct ?? 100}%（0=不挂止损）</div>
          </div>
          <div>
            <span className={labelClass}>止盈全平后重开</span>
            <div className={valClass}>{strategy.reopen_after_close ? '是' : '否'}</div>
          </div>
        </div>
      </div>

      {/* TP & SL stats */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="font-semibold mb-3 text-sm flex items-center gap-2">
            <TrendingUp size={14} className="text-green-400" />
            止盈统计
          </h3>
          <div className="flex gap-6">
            <div className="flex flex-col">
              <span className="text-xs text-gray-500">本次运行</span>
              <span className="text-2xl font-bold text-green-400">{stats.tp_total}</span>
              <span className="text-xs text-gray-500">次止盈</span>
            </div>
            <div className="flex flex-col">
              <span className="text-xs text-gray-500">今日</span>
              <span className="text-2xl font-bold text-green-400">{stats.tp_today}</span>
              <span className="text-xs text-gray-500">次止盈</span>
            </div>
          </div>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="font-semibold mb-3 text-sm flex items-center gap-2">
            <AlertTriangle size={14} className="text-red-400" />
            止损记录
            <span className="text-gray-500 ml-2 text-xs">(本次运行)</span>
          </h3>
          {stats.sl_events.length === 0 ? (
            <div className="text-gray-600 text-sm py-2">暂无止损触发</div>
          ) : (
            <div className="max-h-40 overflow-y-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-gray-500 border-b border-gray-800">
                    <th className="text-left py-1 px-2">时间</th>
                    <th className="text-right py-1 px-2">成交价</th>
                    <th className="text-right py-1 px-2">数量</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.sl_events.map((e, i) => (
                    <tr key={i} className="border-b border-gray-800/50">
                      <td className="py-1 px-2 text-gray-300">{e.time}</td>
                      <td className="py-1 px-2 text-right text-red-400 font-mono">{e.exit_price?.toFixed(6)}</td>
                      <td className="py-1 px-2 text-right text-gray-300 font-mono">{e.quantity?.toFixed(4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
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
