import type { StrategyParamFields } from './strategy';

export interface DeclineRankAutoConfig {
  enabled: boolean;
  account_id: number | null;
  direction: 'long' | 'short';
  start_time: string;
  end_time: string;
  refresh_interval_min: number;
  top_n: number;
  params: StrategyParamFields;
}

export interface DeclineRankAutoStatus {
  enabled: boolean;
  in_window: boolean;
  window_id: string | null;
  last_refresh_at: string | null;
  next_refresh_at: string | null;
  current_symbols: string[];
  active_symbols?: string[];
  auto_strategy_count: number;
  last_error: string | null;
  cleaned_for_window: string | null;
  last_ranked_count?: number;
  last_created?: number;
  last_skipped?: number;
  last_failed?: number;
  last_skip_reasons?: string[];
}

export const defaultDeclineRankParams = (): StrategyParamFields => ({
  base_qty_value: 6,
  max_layers: 6,
  tp_pct: 1,
  grid_drop_base_pct: 1,
  grid_interval_multiplier: 1.5,
  position_multiplier: 1.5,
  cumulative_loss_threshold_u: 0,
  reopen_after_close: true,
});

export const defaultDeclineRankConfig = (): DeclineRankAutoConfig => ({
  enabled: false,
  account_id: null,
  direction: 'short',
  start_time: '03:00',
  end_time: '00:00',
  refresh_interval_min: 15,
  top_n: 10,
  params: defaultDeclineRankParams(),
});
