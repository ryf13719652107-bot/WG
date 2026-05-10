import { useEffect, useState, useRef, useCallback } from 'react';
import { api } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';
import type { Trade } from '../../types';
import { Download, Trash2, Search, X } from 'lucide-react';
import { formatCloseReason } from '../../utils/tradeUi';

export default function TradesPage() {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [sideFilter, setSideFilter] = useState<'' | 'long' | 'short'>('');
  const [closeReasonFilter, setCloseReasonFilter] = useState<'' | 'tp_sl' | 'take_profit' | 'stop_loss'>('');
  const [symbolSearch, setSymbolSearch] = useState('');
  const limit = 50;
  const selectedAccountId = useDashboardStore((s) => s.selectedAccountId);
  const loadRef = useRef<() => void>(() => {});

  const load = useCallback(async () => {
    const data = await api.listTrades({
      limit, offset: page * limit,
      account_id: selectedAccountId ?? undefined,
      side: sideFilter || undefined,
      symbol: symbolSearch.trim() || undefined,
      close_reason: closeReasonFilter || undefined,
    });
    setTrades(data.trades);
    setTotal(data.total);
  }, [page, selectedAccountId, sideFilter, symbolSearch, closeReasonFilter]);
  loadRef.current = load;

  useEffect(() => { setPage(0); }, [selectedAccountId, sideFilter, symbolSearch, closeReasonFilter]);
  useEffect(() => { load(); }, [page, selectedAccountId, sideFilter, symbolSearch, closeReasonFilter]);

  useEffect(() => {
    const timer = setInterval(() => loadRef.current(), 30000);
    return () => clearInterval(timer);
  }, []);

  const handleDeleteOne = async (id: number) => {
    if (!confirm('确定要删除这条交易记录吗？')) return;
    await api.deleteTrade(id);
    load();
  };

  const handleDeleteFiltered = async () => {
    const desc = [
      symbolSearch && `币种=${symbolSearch}`,
      sideFilter && `方向=${sideFilter === 'long' ? '多' : '空'}`,
      closeReasonFilter === 'tp_sl' && '平仓原因=止盈或止损',
      closeReasonFilter === 'take_profit' && '平仓原因=止盈',
      closeReasonFilter === 'stop_loss' && '平仓原因=止损',
    ].filter(Boolean).join(', ');
    if (!confirm(`确定要删除当前筛选的所有交易记录吗？\n筛选条件：${desc || '全部'}\n此操作不可恢复。`)) return;
    await api.deleteFilteredTrades({
      symbol: symbolSearch.trim() || undefined,
      side: sideFilter || undefined,
      account_id: selectedAccountId ?? undefined,
    });
    setPage(0);
    load();
  };

  const exportCsv = () => {
    window.open('/api/trades/export', '_blank');
  };

  const clearFilters = () => {
    setSymbolSearch('');
    setSideFilter('');
    setCloseReasonFilter('');
    setPage(0);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h2 className="text-xl font-bold">交易历史</h2>
        <div className="flex items-center gap-2">
          <button onClick={handleDeleteFiltered} className="flex items-center gap-1.5 bg-red-600/20 hover:bg-red-600/40 text-red-400 px-3 py-1.5 rounded-lg text-sm">
            <Trash2 size={16} />
            删除筛选
          </button>
          <button onClick={exportCsv} className="flex items-center gap-1.5 bg-gray-700 hover:bg-gray-600 px-3 py-1.5 rounded-lg text-sm">
            <Download size={16} />
            导出CSV
          </button>
        </div>
      </div>

      {/* Search & Filter Bar */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="relative">
          <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            type="text"
            placeholder="模糊搜索 ETH、BTC …"
            value={symbolSearch}
            onChange={(e) => setSymbolSearch(e.target.value)}
            className="w-44 bg-gray-800 border border-gray-700 rounded pl-8 pr-3 py-1.5 text-sm text-white placeholder-gray-500 focus:border-blue-500 focus:outline-none"
          />
        </div>
        <div className="flex bg-gray-800 rounded-lg p-0.5">
          {(['', 'long', 'short'] as const).map((s) => (
            <button
              key={s}
              onClick={() => setSideFilter(s)}
              className={`px-3 py-1 text-sm rounded-md transition-colors ${
                sideFilter === s
                  ? 'bg-blue-600 text-white'
                  : 'text-gray-400 hover:text-gray-200'
              }`}
            >
              {s === '' ? '全部' : s === 'long' ? '做多' : '做空'}
            </button>
          ))}
        </div>
        <select
          value={closeReasonFilter}
          onChange={(e) =>
            setCloseReasonFilter(e.target.value as '' | 'tp_sl' | 'take_profit' | 'stop_loss')
          }
          className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white focus:border-blue-500 focus:outline-none"
          title="按平仓原因筛选"
        >
          <option value="">平仓原因·全部</option>
          <option value="tp_sl">止盈或止损</option>
          <option value="take_profit">仅止盈</option>
          <option value="stop_loss">仅止损</option>
        </select>
        {(symbolSearch || sideFilter || closeReasonFilter) && (
          <button onClick={clearFilters} className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200">
            <X size={12} /> 清除筛选
          </button>
        )}
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-gray-500 text-left border-b border-gray-800">
              <th className="p-3">平仓时间</th>
              <th className="p-3">交易对</th>
              <th className="p-3">方向</th>
              <th className="p-3">成本(USDT)</th>
              <th className="p-3">入场价</th>
              <th className="p-3">出场价</th>
              <th className="p-3">盈亏</th>
              <th className="p-3">盈亏%</th>
              <th className="p-3">平仓原因</th>
              <th className="p-3 w-10"></th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t) => (
              <tr key={t.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td className="p-3 text-gray-400 text-xs">{new Date(t.exit_time).toLocaleString()}</td>
                <td className="p-3 font-medium font-mono">{t.symbol}</td>
                <td className={`p-3 ${t.side === 'long' ? 'text-green-400' : 'text-red-400'}`}>
                  {t.side === 'long' ? '做多' : '做空'}
                </td>
                <td className="p-3 font-mono">{(t.quantity * t.entry_price).toFixed(2)}</td>
                <td className="p-3 font-mono text-xs">{t.entry_price?.toFixed(6)}</td>
                <td className="p-3 font-mono text-xs">{t.exit_price?.toFixed(6)}</td>
                <td className={`p-3 font-mono ${t.realized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {t.realized_pnl >= 0 ? '+' : ''}{t.realized_pnl.toFixed(4)}
                </td>
                <td className={`p-3 ${t.pnl_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {t.pnl_pct >= 0 ? '+' : ''}{t.pnl_pct.toFixed(2)}%
                </td>
                <td className="p-3">
                  <span className={`px-2 py-0.5 rounded text-xs ${
                    t.close_reason === 'take_profit' ? 'bg-green-600/20 text-green-400' :
                    t.close_reason === 'stop_loss' ? 'bg-red-600/20 text-red-400' :
                    t.close_reason === 'panic_close' || t.close_reason === 'panic_loss' ? 'bg-yellow-600/20 text-yellow-400' :
                    t.close_reason === 'exchange_already_flat' ? 'bg-sky-600/20 text-sky-300' :
                    t.close_reason === 'sync' ? 'bg-blue-600/20 text-blue-400' :
                    t.close_reason === 'strategy_deleted' || t.close_reason === '策略删除' ? 'bg-slate-600/30 text-slate-300' :
                    'bg-gray-700 text-gray-400'
                  }`}>
                    {formatCloseReason(t.close_reason)}
                  </span>
                </td>
                <td className="p-3">
                  <button onClick={() => handleDeleteOne(t.id)} className="p-1 text-gray-500 hover:text-red-400 hover:bg-red-600/20 rounded" title="删除">
                    <Trash2 size={14} />
                  </button>
                </td>
              </tr>
            ))}
            {trades.length === 0 && (
              <tr><td colSpan={10} className="p-8 text-center text-gray-600">暂无交易记录</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {total > limit && (
        <div className="flex items-center justify-center gap-3">
          <button
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
            className="px-3 py-1 bg-gray-800 rounded text-sm disabled:opacity-50"
          >
            上一页
          </button>
          <span className="text-sm text-gray-400">
            第 {page + 1} 页 / 共 {Math.ceil(total / limit)} 页 ({total} 条)
          </span>
          <button
            onClick={() => setPage((p) => p + 1)}
            disabled={(page + 1) * limit >= total}
            className="px-3 py-1 bg-gray-800 rounded text-sm disabled:opacity-50"
          >
            下一页
          </button>
        </div>
      )}
    </div>
  );
}
