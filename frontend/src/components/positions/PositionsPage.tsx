import { useEffect, useState, useRef, useMemo } from 'react';
import { api } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';
import type { DashboardData, Position } from '../../types';
import { Check, Minus } from 'lucide-react';

type ExchangePos = DashboardData['exchange_positions'][number];

type DisplayRow = {
  key: string;
  symbol: string;
  side: 'long' | 'short';
  notional_usdt: number;
  entry_price: number;
  unrealized_pnl: number;
  layer: string;
  tp_has_order: boolean;
  tp_target_only: boolean;
  opened_at_label: string;
  /** 可调用 POST /api/positions/:id/close（交易所已平时会仅清本地） */
  positionId?: number;
};

/** 与后端 exchange_base._norm_sym 等价 */
function normSym(s: string): string {
  return (s || '').replace(/\//g, '').replace(':USDT', '').replace('-SWAP', '').toUpperCase();
}

function exchangeNotionalUsdt(ep: ExchangePos): number {
  let u = typeof ep.usdt === 'number' ? ep.usdt : 0;
  if (u > 1e-8) return u;
  const c = ep.contracts;
  const m = ep.mark_price;
  if (typeof c === 'number' && typeof m === 'number' && c > 0 && m > 0) {
    return Math.abs(c * m);
  }
  return 0;
}

/** 仅展示本地「未平仓」持仓；交易所数据只用于合并盈亏/名义。无本地记录时不显示孤儿交易所腿。 */
function buildRows(dbPositions: Position[], exchangePositions: ExchangePos[]): DisplayRow[] {
  if (dbPositions.length === 0) {
    return [];
  }

  const exMap = new Map<string, ExchangePos>();
  for (const ep of exchangePositions || []) {
    const side = (ep.side || '').toLowerCase();
    const key = `${normSym(ep.symbol)}-${side}`;
    exMap.set(key, ep);
  }

  const groupMap = new Map<string, Position[]>();
  for (const p of dbPositions) {
    const key = `${normSym(p.symbol)}-${p.side}`;
    if (!groupMap.has(key)) groupMap.set(key, []);
    groupMap.get(key)!.push(p);
  }

  const rows: DisplayRow[] = [];
  for (const [key, match] of groupMap) {
    match.sort((a, b) => a.layer - b.layer);
    const ep = exMap.get(key);

    let layer = '-';
    if (match.length === 1) layer = `L${match[0].layer}`;
    else if (match.length > 1) {
      const layers = [...new Set(match.map((m) => m.layer))].sort((a, b) => a - b);
      layer = `L${layers[0]}-L${layers[layers.length - 1]}（${match.length}层）`;
    }
    const tpId = match.some((m) => !!m.tp_limit_order_id);
    const hasTpPrice = match.some((m) => m.take_profit_price != null);
    const opened = match
      .map((m) => m.opened_at)
      .filter(Boolean)
      .sort()[0];

    if (ep) {
      rows.push({
        key,
        symbol: match[0].symbol,
        side: match[0].side,
        notional_usdt: exchangeNotionalUsdt(ep),
        entry_price: ep.entry_price,
        unrealized_pnl: ep.unrealized_pnl,
        layer,
        tp_has_order: tpId,
        tp_target_only: hasTpPrice && !tpId,
        opened_at_label: opened ? new Date(opened as string).toLocaleString() : '-',
      });
    } else {
      for (const p of match) {
        const px = p.mark_price ?? p.entry_price;
        rows.push({
          key: String(p.id),
          symbol: p.symbol,
          side: p.side,
          notional_usdt: px * p.quantity,
          entry_price: p.entry_price,
          unrealized_pnl: p.unrealized_pnl ?? 0,
          layer: `L${p.layer}`,
          tp_has_order: !!p.tp_limit_order_id,
          tp_target_only: p.take_profit_price != null && !p.tp_limit_order_id,
          opened_at_label: p.opened_at ? new Date(p.opened_at).toLocaleString() : '-',
          positionId: p.id,
        });
      }
    }
  }

  return rows.sort((a, b) => normSym(a.symbol).localeCompare(normSym(b.symbol)));
}

export default function PositionsPage() {
  const { selectedAccountId } = useDashboardStore();
  const [dbPositions, setDbPositions] = useState<Position[]>([]);
  const [exchangePositions, setExchangePositions] = useState<ExchangePos[]>([]);
  const loadRef = useRef<() => void>(() => {});

  const load = async () => {
    const acc = selectedAccountId ?? undefined;
    try {
      const [positions, dash] = await Promise.all([
        api.listPositions({ account_id: acc }),
        api.getDashboard(acc),
      ]);
      setDbPositions(positions);
      setExchangePositions(dash.exchange_positions || []);
      useDashboardStore.getState().setData(dash);
    } catch {
      try {
        const positions = await api.listPositions({ account_id: acc });
        setDbPositions(positions);
        setExchangePositions([]);
      } catch {
        setDbPositions([]);
      }
    }
  };
  loadRef.current = load;

  useEffect(() => {
    load();
  }, [selectedAccountId]);

  useEffect(() => {
    const timer = setInterval(() => loadRef.current(), 60000);
    return () => clearInterval(timer);
  }, []);

  const rows = useMemo(
    () => buildRows(dbPositions, exchangePositions),
    [dbPositions, exchangePositions],
  );

  const [clearBusyId, setClearBusyId] = useState<number | null>(null);

  const clearLocalRow = async (positionId: number) => {
    setClearBusyId(positionId);
    try {
      await api.closePosition(positionId);
      await loadRef.current();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      window.alert(`清除失败：${msg}`);
    } finally {
      setClearBusyId(null);
    }
  };

  /** 交易所有仓但机器人库无未平仓——与删除策略后的空表区分开，仍提示对账 */
  const hasExchangeHint =
    (exchangePositions?.length ?? 0) > 0 && dbPositions.length === 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-3">
        <h2 className="text-xl font-bold">当前持仓</h2>
        <span className="text-xs text-gray-500">
          仅展示<strong className="text-gray-400">本地策略未平仓</strong>记录；名义/浮盈亏优先取交易所；每 60 秒刷新
          <span className="ml-2 text-gray-600 font-mono" title="每次 npm run build 更新；若与执行时间不符说明浏览器或 CDN 仍在用旧包">
            build:{__FRONTEND_BUILD_STAMP__}
          </span>
        </span>
      </div>

      {hasExchangeHint && (
        <p className="text-xs text-amber-500/90 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2">
          交易所有持仓，但本地暂无对应未平仓记录（例如策略已删除并完成平仓、或非本机器人开仓）。若刚删策略，此处应为空；若交易所仍有仓请自行在交易所核对。
        </p>
      )}

      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-gray-500 text-left border-b border-gray-800">
              <th className="p-3">交易对</th>
              <th className="p-3">方向</th>
              <th className="p-3">持仓(USDT)</th>
              <th className="p-3">层数</th>
              <th className="p-3">入场价</th>
              <th className="p-3">限价止盈</th>
              <th className="p-3">未实现盈亏</th>
              <th className="p-3">开仓时间</th>
              <th className="p-3 w-28">操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.key}
                className="border-b border-gray-800/50 hover:bg-gray-800/30"
              >
                <td className="p-3 font-medium font-mono">
                  {row.symbol}
                </td>
                <td className={`p-3 ${row.side === 'long' ? 'text-green-400' : 'text-red-400'}`}>
                  {row.side === 'long' ? '做多' : '做空'}
                </td>
                <td className="p-3 font-mono text-gray-200">{row.notional_usdt.toFixed(2)}</td>
                <td className="p-3 text-gray-400">{row.layer}</td>
                <td className="p-3 font-mono">{row.entry_price?.toFixed(8)}</td>
                <td className="p-3">
                  {row.tp_has_order ? (
                    <span className="inline-flex items-center gap-1 text-green-400" title="已挂限价止盈单">
                      <Check size={16} strokeWidth={2.5} />
                      <span className="text-xs">已挂单</span>
                    </span>
                  ) : row.tp_target_only ? (
                    <span className="inline-flex items-center gap-1 text-amber-400/90" title="策略有止盈目标，当前无未完成限价单">
                      <Minus size={16} />
                      <span className="text-xs">未挂单</span>
                    </span>
                  ) : (
                    <span className="text-gray-600 text-xs">-</span>
                  )}
                </td>
                <td className={`p-3 font-mono ${row.unrealized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {row.unrealized_pnl >= 0 ? '+' : ''}
                  {row.unrealized_pnl.toFixed(2)} USDT
                </td>
                <td className="p-3 text-gray-500 text-xs">{row.opened_at_label}</td>
                <td className="p-3">
                  {row.positionId != null ? (
                    <button
                      type="button"
                      disabled={clearBusyId === row.positionId}
                      onClick={() => void clearLocalRow(row.positionId!)}
                      className="text-xs px-2 py-1 rounded border border-amber-500/40 text-amber-400 hover:bg-amber-500/10 disabled:opacity-50"
                      title="交易所已无该持仓时写入平仓并清除本地未完成记录（若仍能查到实盘持仓会先提示失败）"
                    >
                      {clearBusyId === row.positionId ? '…' : '清除本地记录'}
                    </button>
                  ) : (
                    <span className="text-gray-600 text-xs">—</span>
                  )}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={9} className="p-8 text-center text-gray-600">
                  暂无持仓
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
