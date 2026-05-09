import { useEffect, useState, useMemo } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { api } from '../../services/api';
import type { Strategy, StrategyFormData } from '../../types/strategy';
import type { Account } from '../../types';

const schema = z.object({
  account_id: z.number().min(1, '请选择账户'),
  direction: z.enum(['long', 'short']),
  symbol: z.string().min(1, '请选择交易对'),
  base_qty_type: z.enum(['margin_pct', 'usdt']),
  base_qty_value: z.number().min(0.01),
  max_layers: z.number().min(1).max(50),
  leverage: z.number().min(1).max(125),
  tp_pct: z.number().min(0.1).max(50),
  grid_drop_base_pct: z.number().min(0.1).max(100),
  grid_interval_multiplier: z.number().min(1).max(10),
  position_multiplier: z.number().min(1).max(10),
  cumulative_loss_threshold_u: z.number().min(0),
  reopen_after_close: z.coerce.boolean(),
});

interface Props {
  accounts: Account[];
  initialData: Strategy | null;
  onSubmit: (data: StrategyFormData) => void;
  onCancel: () => void;
}

function toFormDefaults(accounts: Account[]): StrategyFormData {
  return {
    account_id: accounts[0]?.id || 0,
    direction: 'long',
    symbol: '',
    base_qty_type: 'margin_pct',
    base_qty_value: 6,
    max_layers: 8,
    leverage: 20,
    tp_pct: 1,
    grid_drop_base_pct: 1,
    grid_interval_multiplier: 1.5,
    position_multiplier: 1.5,
    cumulative_loss_threshold_u: 0,
    reopen_after_close: true,
  };
}

export default function StrategyForm({ accounts, initialData, onSubmit, onCancel }: Props) {
  const { register, handleSubmit, watch, setValue, formState: { errors } } = useForm<StrategyFormData>({
    resolver: zodResolver(schema),
    defaultValues: toFormDefaults(accounts),
  });

  const [symbols, setSymbols] = useState<string[]>([]);
  const [strategyCounts, setStrategyCounts] = useState<Record<string, number>>({});
  const [strategyDirs, setStrategyDirs] = useState<Record<string, string[]>>({});
  const [search, setSearch] = useState('');
  const [showDropdown, setShowDropdown] = useState(false);

  const selectedAccountId = watch('account_id');
  const selectedSymbol = watch('symbol');
  const direction = watch('direction');

  useEffect(() => {
    const acct = accounts.find(a => a.id === selectedAccountId);
    const ex = (acct as any)?.exchange || 'binance';
    api.getMarkets(ex).then(r => setSymbols(r.symbols)).catch(() => {});
  }, [selectedAccountId, accounts]);

  useEffect(() => {
    if (selectedAccountId) {
      api.getStrategyCounts(selectedAccountId).then(r => {
        setStrategyCounts(r.counts);
        setStrategyDirs(r.directions);
      }).catch(() => {});
    }
  }, [selectedAccountId]);

  const filteredSymbols = useMemo(() => {
    const q = search.toUpperCase();
    return symbols.filter(sym => {
      if (q && !sym.includes(q)) return false;
      const count = strategyCounts[sym] || 0;
      if (count >= 2) return false;
      if (count === 1) {
        const dirs = strategyDirs[sym] || [];
        if (dirs.includes(direction)) return false;
      }
      return true;
    });
  }, [symbols, search, strategyCounts, strategyDirs, direction]);

  const inputClass = 'w-full bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-white focus:border-blue-500 focus:outline-none';
  const labelClass = 'block text-xs text-gray-400 mb-0.5';
  const errorClass = 'text-red-400 text-xs';

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="font-semibold mb-4">新建策略</h3>
      <form onSubmit={handleSubmit(onSubmit)} className="space-y-3">
        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className={labelClass}>交易账户</label>
            <select {...register('account_id', { valueAsNumber: true })} className={inputClass}>
              {accounts.map((a) => <option key={a.id} value={a.id}>{a.name} ({(a as any).exchange || 'binance'}) {a.testnet ? '(测试网)' : '(实盘)'}</option>)}
            </select>
            {errors.account_id && <p className={errorClass}>{errors.account_id.message}</p>}
          </div>
          <div>
            <label className={labelClass}>交易方向</label>
            <select {...register('direction')} className={inputClass}>
              <option value="long">做多</option>
              <option value="short">做空</option>
            </select>
          </div>
          <div className="relative">
            <label className={labelClass}>
              交易对
              <span className="text-gray-600 ml-1">每币种最多一多一空</span>
            </label>
            <input type="hidden" {...register('symbol')} />
            <input
              type="text"
              value={search}
              onChange={(e) => { setSearch(e.target.value); setShowDropdown(true); }}
              onFocus={() => setShowDropdown(true)}
              onBlur={() => setTimeout(() => setShowDropdown(false), 200)}
              placeholder="搜索交易对..."
              className={inputClass}
              autoComplete="off"
            />
            {showDropdown && (
              <div className="absolute z-50 w-full mt-0.5 max-h-48 overflow-y-auto bg-gray-800 border border-gray-700 rounded shadow-lg">
                {filteredSymbols.slice(0, 100).map(sym => {
                  const dirs = strategyDirs[sym] || [];
                  const takenLong = dirs.includes('long');
                  const takenShort = dirs.includes('short');
                  return (
                    <div
                      key={sym}
                      className={`px-3 py-1.5 text-sm cursor-pointer flex items-center justify-between ${
                        selectedSymbol === sym ? 'bg-blue-600/30 text-blue-300' : 'text-gray-300 hover:bg-gray-700'
                      }`}
                      onMouseDown={(e) => {
                        e.preventDefault();
                        setValue('symbol', sym);
                        setSearch(sym);
                        setShowDropdown(false);
                      }}
                    >
                      <span className="font-mono">{sym}</span>
                      <span className="text-xs text-gray-500">
                        {takenLong && <span className="text-green-500 mr-1">多</span>}
                        {takenShort && <span className="text-red-500">空</span>}
                      </span>
                    </div>
                  );
                })}
                {filteredSymbols.length === 0 && (
                  <div className="px-3 py-2 text-sm text-gray-500">
                    {search ? '无匹配交易对' : '该账户交易对已全部使用'}
                  </div>
                )}
              </div>
            )}
            {errors.symbol && <p className={errorClass}>{errors.symbol.message}</p>}
          </div>
        </div>

        <div className="border-t border-gray-800 my-3" />

        <h4 className="text-sm font-semibold text-gray-300">首单开仓设置</h4>
        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className={labelClass}>仓位类型</label>
            <select {...register('base_qty_type')} className={inputClass}>
              <option value="margin_pct">保证金百分比</option>
              <option value="usdt">固定USDT金额</option>
            </select>
          </div>
          <div>
            <label className={labelClass}>首单仓位数值</label>
            <input type="number" step="0.01" {...register('base_qty_value', { valueAsNumber: true })} className={inputClass} />
          </div>
          <div>
            <label className={labelClass}>合约杠杆</label>
            <input type="number" {...register('leverage', { valueAsNumber: true })} className={inputClass} />
          </div>
        </div>

        <div className="border-t border-gray-800 my-3" />

        <h4 className="text-sm font-semibold text-gray-300">马丁网格加仓设置</h4>
        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className={labelClass}>首层加仓跌幅 (%)</label>
            <input type="number" step="0.1" {...register('grid_drop_base_pct', { valueAsNumber: true })} className={inputClass} />
            <span className="text-xs text-gray-600">默认1%</span>
          </div>
          <div>
            <label className={labelClass}>跌幅间隔倍数</label>
            <input type="number" step="0.1" {...register('grid_interval_multiplier', { valueAsNumber: true })} className={inputClass} />
            <span className="text-xs text-gray-600">后续每层=上层×倍数</span>
          </div>
          <div>
            <label className={labelClass}>仓位递增倍数</label>
            <input type="number" step="0.1" {...register('position_multiplier', { valueAsNumber: true })} className={inputClass} />
            <span className="text-xs text-gray-600">每层仓位=上层×倍数</span>
          </div>
          <div>
            <label className={labelClass}>最大加仓层数</label>
            <input type="number" {...register('max_layers', { valueAsNumber: true })} className={inputClass} />
          </div>
        </div>

        <div className="border-t border-gray-800 my-3" />

        <h4 className="text-sm font-semibold text-gray-300">出场设置</h4>
        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className={labelClass}>止盈比例 (%)</label>
            <input type="number" step="0.1" {...register('tp_pct', { valueAsNumber: true })} className={inputClass} />
            <span className="text-xs text-gray-600">默认1%，限价止盈</span>
          </div>
          <div>
            <label className={labelClass}>累计亏损阈值 (U)</label>
            <input type="number" step="0.01" {...register('cumulative_loss_threshold_u', { valueAsNumber: true })} className={inputClass} />
            <span className="text-xs text-gray-600">0=禁用止损</span>
          </div>
        </div>

        <div>
          <label className={`${labelClass} flex items-center gap-2`}>
            <span>平仓后自动重开</span>
            <label className="relative inline-flex items-center cursor-pointer">
              <input type="checkbox" {...register('reopen_after_close')} className="sr-only peer" />
              <div className="w-9 h-5 bg-gray-600 peer-checked:bg-blue-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
            </label>
            <span className="text-xs text-gray-500">止盈/止损后立即开新首单</span>
          </label>
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onCancel} className="px-4 py-1.5 text-sm bg-gray-700 hover:bg-gray-600 rounded-lg">取消</button>
          <button type="submit" className="px-4 py-1.5 text-sm bg-blue-600 hover:bg-blue-700 rounded-lg font-medium">创建策略</button>
        </div>
      </form>
    </div>
  );
}
