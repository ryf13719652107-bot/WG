import type { StrategyParamFields } from '../../types/strategy';

const inputClass = 'w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500';
const labelClass = 'block text-xs text-gray-400 mb-1';

interface Props {
  value: StrategyParamFields;
  onChange: (next: StrategyParamFields) => void;
}

/** 共享马丁网格参数区块（手动建策略 / 跌幅榜自动模板）。 */
export default function StrategyParamFieldsForm({ value, onChange }: Props) {
  const set = <K extends keyof StrategyParamFields>(key: K, v: StrategyParamFields[K]) => {
    onChange({ ...value, [key]: v });
  };

  return (
    <div className="space-y-3">
      <h4 className="text-sm font-semibold text-gray-300">统一策略参数模板</h4>
      <div>
        <label className={labelClass}>首单仓位 (USDT)</label>
        <input
          type="number"
          step="0.01"
          value={value.base_qty_value}
          onChange={(e) => set('base_qty_value', Number(e.target.value))}
          className={inputClass}
        />
        <span className="text-xs text-gray-600">按固定 USDT 名义计算开仓数量</span>
      </div>

      <div className="border-t border-gray-800 my-1" />

      <h4 className="text-sm font-semibold text-gray-300">马丁网格加仓设置</h4>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className={labelClass}>首层加仓跌幅 (%)</label>
          <input
            type="number"
            step="0.1"
            value={value.grid_drop_base_pct}
            onChange={(e) => set('grid_drop_base_pct', Number(e.target.value))}
            className={inputClass}
          />
        </div>
        <div>
          <label className={labelClass}>跌幅间隔倍数</label>
          <input
            type="number"
            step="0.1"
            value={value.grid_interval_multiplier}
            onChange={(e) => set('grid_interval_multiplier', Number(e.target.value))}
            className={inputClass}
          />
        </div>
        <div>
          <label className={labelClass}>仓位递增倍数</label>
          <input
            type="number"
            step="0.1"
            value={value.position_multiplier}
            onChange={(e) => set('position_multiplier', Number(e.target.value))}
            className={inputClass}
          />
        </div>
        <div>
          <label className={labelClass}>最大加仓层数</label>
          <input
            type="number"
            value={value.max_layers}
            onChange={(e) => set('max_layers', Number(e.target.value))}
            className={inputClass}
          />
        </div>
      </div>

      <div className="border-t border-gray-800 my-1" />

      <h4 className="text-sm font-semibold text-gray-300">出场设置</h4>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className={labelClass}>止盈比例 (%)</label>
          <input
            type="number"
            step="0.1"
            value={value.tp_pct}
            onChange={(e) => set('tp_pct', Number(e.target.value))}
            className={inputClass}
          />
        </div>
        <div>
          <label className={labelClass}>止损触发亏损 (USDT)</label>
          <input
            type="number"
            step="0.01"
            value={value.cumulative_loss_threshold_u}
            onChange={(e) => set('cumulative_loss_threshold_u', Number(e.target.value))}
            className={inputClass}
          />
          <span className="text-xs text-gray-600">0=不挂止损</span>
        </div>
      </div>

      <div>
        <label className={`${labelClass} flex items-center gap-2`}>
          <span>止盈全平后自动重开</span>
          <label className="relative inline-flex items-center cursor-pointer">
            <input
              type="checkbox"
              checked={value.reopen_after_close}
              onChange={(e) => set('reopen_after_close', e.target.checked)}
              className="sr-only peer"
            />
            <div className="w-9 h-5 bg-gray-600 peer-checked:bg-blue-600 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all" />
          </label>
        </label>
      </div>
    </div>
  );
}
