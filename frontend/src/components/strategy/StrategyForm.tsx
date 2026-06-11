import { useEffect, useState, useMemo } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { api } from '../../services/api';
import type { Strategy, StrategyFormData, StrategyParamTemplate, StrategyParamFields } from '../../types/strategy';
import type { Account } from '../../types';

const schema = z.object({
  account_id: z.number().min(1, '请选择账户'),
  direction: z.enum(['long', 'short']),
  symbol: z.string().min(1, '请选择交易对'),
  base_qty_value: z.number().min(0.01),
  max_layers: z.number().min(1).max(99999),
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

function toFormDefaults(accounts: Account[], initial?: Strategy | null): StrategyFormData {
  if (initial) {
    return {
      account_id: initial.account_id,
      direction: initial.direction,
      symbol: initial.symbol,
      base_qty_value: initial.base_qty_value,
      max_layers: initial.max_layers,
      tp_pct: initial.tp_pct,
      grid_drop_base_pct: initial.grid_drop_base_pct,
      grid_interval_multiplier: initial.grid_interval_multiplier,
      position_multiplier: initial.position_multiplier,
      cumulative_loss_threshold_u: initial.cumulative_loss_threshold_u,
      reopen_after_close: initial.reopen_after_close,
    };
  }
  return {
    account_id: accounts[0]?.id || 0,
    direction: 'long',
    symbol: '',
    base_qty_value: 6,
    max_layers: 6,
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
    defaultValues: toFormDefaults(accounts, initialData),
  });

  const [symbols, setSymbols] = useState<string[]>([]);
  const [symbolsLoading, setSymbolsLoading] = useState(false);
  const [symbolsError, setSymbolsError] = useState('');
  const [remoteSymbols, setRemoteSymbols] = useState<string[]>([]);
  const [remoteSearching, setRemoteSearching] = useState(false);
  const [strategyCounts, setStrategyCounts] = useState<Record<string, number>>({});
  const [strategyDirs, setStrategyDirs] = useState<Record<string, string[]>>({});
  const [search, setSearch] = useState(initialData?.symbol || '');
  const [showDropdown, setShowDropdown] = useState(false);
  const [templates, setTemplates] = useState<StrategyParamTemplate[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState('');
  const isEdit = initialData !== null;

  const applyTemplateParams = (params: StrategyParamFields) => {
    (Object.keys(params) as (keyof StrategyParamFields)[]).forEach((key) => {
      setValue(key, params[key] as never, { shouldDirty: true });
    });
  };

  useEffect(() => {
    api.listParamTemplates().then(setTemplates).catch(() => {});
  }, []);

  const selectedAccountId = watch('account_id');
  const selectedSymbol = watch('symbol');
  const direction = watch('direction');

  useEffect(() => {
    if (!selectedAccountId) {
      setSymbols([]);
      setSymbolsError('');
      return;
    }
    const acct = accounts.find(a => a.id === selectedAccountId);
    const ex = acct?.exchange || 'binance';
    setSymbolsLoading(true);
    setSymbolsError('');
    api.getMarkets(ex, selectedAccountId)
      .then((r) => {
        setSymbols(r.symbols || []);
        if (!r.symbols?.length) setSymbolsError('未获取到交易对，请检查网络或代理');
      })
      .catch((e: Error) => {
        setSymbols([]);
        setSymbolsError(e.message || '加载交易对失败');
      })
      .finally(() => setSymbolsLoading(false));
  }, [selectedAccountId, accounts]);

  useEffect(() => {
    if (selectedAccountId) {
      api.getStrategyCounts(selectedAccountId).then(r => {
        setStrategyCounts(r.counts);
        setStrategyDirs(r.directions);
      }).catch(() => {});
    }
  }, [selectedAccountId]);

  useEffect(() => {
    const q = search.trim();
    if (!selectedAccountId || q.length < 2) {
      setRemoteSymbols([]);
      setRemoteSearching(false);
      return;
    }
    const timer = setTimeout(() => {
      const qq = q.toUpperCase();
      const hasLocal = symbols.some((sym) => sym.includes(qq));
      if (hasLocal) {
        setRemoteSymbols([]);
        setRemoteSearching(false);
        return;
      }
      const acct = accounts.find((a) => a.id === selectedAccountId);
      const ex = acct?.exchange || 'binance';
      setRemoteSearching(true);
      api.searchMarkets(q, ex, selectedAccountId)
        .then((r) => setRemoteSymbols(r.symbols || []))
        .catch(() => setRemoteSymbols([]))
        .finally(() => setRemoteSearching(false));
    }, 350);
    return () => clearTimeout(timer);
  }, [search, symbols, selectedAccountId, accounts]);

  const symbolPool = useMemo(
    () => [...new Set([...symbols, ...remoteSymbols])],
    [symbols, remoteSymbols],
  );

  type SymbolAvailability = 'ok' | 'dir_taken' | 'full';

  const symbolAvailability = (sym: string): SymbolAvailability => {
    if (isEdit && sym === initialData?.symbol) return 'ok';
    const count = strategyCounts[sym] || 0;
    if (count >= 2) return 'full';
    if (count === 1) {
      const dirs = strategyDirs[sym] || [];
      if (dirs.includes(direction)) return 'dir_taken';
    }
    return 'ok';
  };

  const matchedSymbols = useMemo(() => {
    const q = search.toUpperCase();
    return symbolPool.filter((sym) => !q || sym.includes(q));
  }, [symbolPool, search]);

  const selectableSymbols = useMemo(
    () => matchedSymbols.filter((sym) => symbolAvailability(sym) === 'ok'),
    [matchedSymbols, strategyCounts, strategyDirs, direction, isEdit, initialData],
  );

  const inputClass = 'w-full bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-white focus:border-blue-500 focus:outline-none';
  const labelClass = 'block text-xs text-gray-400 mb-0.5';
  const errorClass = 'text-red-400 text-xs';

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="font-semibold mb-4">{isEdit ? '编辑策略' : '新建策略'}</h3>

      <div className="mb-4 p-3 rounded-lg bg-gray-800/60 border border-gray-700 space-y-2">
        <div className="text-xs text-gray-400">参数模版（不含账户/方向/交易对）</div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={selectedTemplateId}
            onChange={(e) => setSelectedTemplateId(e.target.value)}
            className="flex-1 min-w-[140px] bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm"
          >
            <option value="">选择模版…</option>
            {templates.map((t) => (
              <option key={t.id} value={t.id}>{t.name}</option>
            ))}
          </select>
          <button
            type="button"
            disabled={!selectedTemplateId}
            onClick={() => {
              const t = templates.find((x) => x.id === selectedTemplateId);
              if (t) applyTemplateParams(t.params);
            }}
            className="px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-700 disabled:opacity-40 text-sm font-medium"
          >
            一键应用
          </button>
          <button
            type="button"
            onClick={async () => {
              const name = prompt('模版名称');
              if (!name?.trim()) return;
              const values = watch();
              const params: StrategyParamFields = {
                base_qty_value: values.base_qty_value,
                max_layers: values.max_layers,
                tp_pct: values.tp_pct,
                grid_drop_base_pct: values.grid_drop_base_pct,
                grid_interval_multiplier: values.grid_interval_multiplier,
                position_multiplier: values.position_multiplier,
                cumulative_loss_threshold_u: values.cumulative_loss_threshold_u,
                reopen_after_close: values.reopen_after_close,
              };
              try {
                const saved = await api.saveParamTemplate(name.trim(), params);
                setTemplates((prev) => [...prev, saved]);
                setSelectedTemplateId(saved.id);
                alert('模版已保存');
              } catch (e: unknown) {
                alert('保存失败: ' + (e instanceof Error ? e.message : String(e)));
              }
            }}
            className="px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 text-sm"
          >
            保存当前为模版
          </button>
          <button
            type="button"
            disabled={!selectedTemplateId}
            onClick={async () => {
              if (!selectedTemplateId || !confirm('确定删除该模版？')) return;
              try {
                await api.deleteParamTemplate(selectedTemplateId);
                setTemplates((prev) => prev.filter((t) => t.id !== selectedTemplateId));
                setSelectedTemplateId('');
              } catch (e: unknown) {
                alert('删除失败: ' + (e instanceof Error ? e.message : String(e)));
              }
            }}
            className="px-3 py-1.5 rounded bg-gray-700 hover:bg-red-900/50 text-sm disabled:opacity-40"
          >
            删除模版
          </button>
        </div>
      </div>

      <form onSubmit={handleSubmit(onSubmit)} className="space-y-3">
        {!isEdit && (
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
                  {matchedSymbols.slice(0, 100).map(sym => {
                    const dirs = strategyDirs[sym] || [];
                    const takenLong = dirs.includes('long');
                    const takenShort = dirs.includes('short');
                    const avail = symbolAvailability(sym);
                    const disabled = avail !== 'ok';
                    const reason = avail === 'full'
                      ? '已满(删旧策略后可建)'
                      : avail === 'dir_taken'
                        ? `${direction === 'long' ? '多' : '空'}向已占用`
                        : '';
                    return (
                      <div
                        key={sym}
                        className={`px-3 py-1.5 text-sm flex items-center justify-between ${
                          disabled
                            ? 'text-gray-500 cursor-not-allowed opacity-70'
                            : selectedSymbol === sym
                              ? 'bg-blue-600/30 text-blue-300 cursor-pointer'
                              : 'text-gray-300 hover:bg-gray-700 cursor-pointer'
                        }`}
                        onMouseDown={(e) => {
                          if (disabled) return;
                          e.preventDefault();
                          setValue('symbol', sym);
                          setSearch(sym);
                          setShowDropdown(false);
                        }}
                      >
                        <span className="font-mono">{sym}</span>
                        <span className="text-xs text-gray-500 ml-2 shrink-0">
                          {reason ? (
                            <span className="text-amber-500/90">{reason}</span>
                          ) : (
                            <>
                              {takenLong && <span className="text-green-500 mr-1">多</span>}
                              {takenShort && <span className="text-red-500">空</span>}
                            </>
                          )}
                        </span>
                      </div>
                    );
                  })}
                  {matchedSymbols.length === 0 && (
                    <div className="px-3 py-2 text-sm text-gray-500">
                      {symbolsLoading || remoteSearching
                        ? '加载交易对中…'
                        : symbolsError
                          ? symbolsError
                          : symbols.length === 0 && remoteSymbols.length === 0
                            ? '交易对列表为空，请检查网络或代理'
                            : search
                              ? '无匹配交易对（该币种可能未上架永续）'
                              : '暂无交易对'}
                    </div>
                  )}
                  {matchedSymbols.length > 0 && selectableSymbols.length === 0 && search && (
                    <div className="px-3 py-2 text-xs text-amber-500/90 border-t border-gray-700">
                      匹配到的币种本账户均已占用；请在策略管理中删除已停止的旧策略后再创建。
                    </div>
                  )}
                </div>
              )}
              {errors.symbol && <p className={errorClass}>{errors.symbol.message}</p>}
            </div>
          </div>
        )}

        {isEdit && (
          <div className="flex items-center gap-2 mb-2">
            <span className="font-mono text-white">{initialData?.symbol}</span>
            <span className={`text-xs px-2 py-0.5 rounded ${initialData?.direction === 'long' ? 'bg-green-600/20 text-green-400' : 'bg-red-600/20 text-red-400'}`}>
              {initialData?.direction === 'long' ? '做多' : '做空'}
            </span>
          </div>
        )}

        <div className="border-t border-gray-800 my-3" />

        <h4 className="text-sm font-semibold text-gray-300">首单开仓设置</h4>
        <div>
          <label className={labelClass}>首单仓位 (USDT)</label>
          <input type="number" step="0.01" {...register('base_qty_value', { valueAsNumber: true })} className={inputClass} />
          <span className="text-xs text-gray-600">按固定 USDT 名义计算开仓数量</span>
        </div>

        <div className="border-t border-gray-800 my-3" />

        <h4 className="text-sm font-semibold text-gray-300">马丁网格加仓设置</h4>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelClass}>首层加仓跌幅 (%)</label>
            <input type="number" step="0.1" {...register('grid_drop_base_pct', { valueAsNumber: true })} className={inputClass} />
            <span className="text-xs text-gray-600">默认1%</span>
          </div>
          <div>
            <label className={labelClass}>跌幅间隔倍数</label>
            <input type="number" step="0.1" {...register('grid_interval_multiplier', { valueAsNumber: true })} className={inputClass} />
          </div>
          <div>
            <label className={labelClass}>仓位递增倍数</label>
            <input type="number" step="0.1" {...register('position_multiplier', { valueAsNumber: true })} className={inputClass} />
          </div>
          <div>
            <label className={labelClass}>最大加仓层数</label>
            <input type="number" {...register('max_layers', { valueAsNumber: true })} className={inputClass} />
            <span className="text-xs text-gray-600">链式挂单：同时仅一单下一层加仓限价，成交后再挂下一层</span>
          </div>
        </div>

        <div className="border-t border-gray-800 my-3" />

        <h4 className="text-sm font-semibold text-gray-300">出场设置</h4>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelClass}>止盈比例 (%)</label>
            <input type="number" step="0.1" {...register('tp_pct', { valueAsNumber: true })} className={inputClass} />
            <span className="text-xs text-gray-600">默认1%，限价止盈</span>
          </div>
          <div>
            <label className={labelClass}>止损触发亏损 (USDT)</label>
            <input type="number" step="0.01" {...register('cumulative_loss_threshold_u', { valueAsNumber: true })} className={inputClass} />
            <span className="text-xs text-gray-600">按整仓推算触发价；0=不挂止损</span>
          </div>
          <div className="col-span-2">
            <span className="text-xs text-gray-500">止损触发后全部平仓并自动重开首单（与下方「止盈重开」开关无关）</span>
          </div>
        </div>

        <div>
          <label className={`${labelClass} flex items-center gap-2`}>
            <span>止盈全平后自动重开</span>
            <label className="relative inline-flex items-center cursor-pointer">
              <input type="checkbox" {...register('reopen_after_close')} className="sr-only peer" />
              <div className="w-9 h-5 bg-gray-600 peer-checked:bg-blue-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all"></div>
            </label>
            <span className="text-xs text-gray-500">仅止盈全部平仓后生效</span>
          </label>
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onCancel} className="px-4 py-1.5 text-sm bg-gray-700 hover:bg-gray-600 rounded-lg">取消</button>
          <button type="submit" className="px-4 py-1.5 text-sm bg-blue-600 hover:bg-blue-700 rounded-lg font-medium">
            {isEdit ? '保存修改' : '创建策略'}
          </button>
        </div>
      </form>
    </div>
  );
}
