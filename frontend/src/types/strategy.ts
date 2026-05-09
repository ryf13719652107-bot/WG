export interface Strategy {
  id: number;
  account_id: number;
  name: string;
  direction: 'long' | 'short';
  symbol: string;
  base_qty_type: 'margin_pct' | 'usdt';
  base_qty_value: number;
  max_layers: number;
  leverage: number;
  tp_pct: number;
  grid_drop_base_pct: number;
  grid_interval_multiplier: number;
  position_multiplier: number;
  cumulative_loss_threshold_u: number;
  reopen_after_close: boolean;
  status: 'running' | 'stopped' | 'error';
  started_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface StrategyFormData {
  account_id: number;
  name: string;
  direction: 'long' | 'short';
  symbol: string;
  base_qty_type: 'margin_pct' | 'usdt';
  base_qty_value: number;
  max_layers: number;
  leverage: number;
  tp_pct: number;
  grid_drop_base_pct: number;
  grid_interval_multiplier: number;
  position_multiplier: number;
  cumulative_loss_threshold_u: number;
  reopen_after_close: boolean;
}
