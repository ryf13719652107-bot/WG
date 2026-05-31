export interface WebUiPasswordStatus {
  auth_required_effective: boolean;
  environment_has_password: boolean;
  database_has_password: boolean;
}

export interface FeishuNotifySettings {
  webhook_masked: string;
  webhook_source: 'database' | 'environment' | 'none';
  keyword_prefix: string;
  has_database_webhook_override: boolean;
  has_database_prefix_override: boolean;
}

export interface Account {
  id: number;
  name: string;
  exchange: string;
  masked_key: string;
  testnet: boolean;
  hedge_mode: boolean;
  /** 总资产止损下限 USDT；0=关闭。当前总权益低于该值时停止本账户全部策略 */
  equity_stop_floor_u: number;
  /** 策略启动时记入的初始总权益 */
  equity_baseline_u: number | null;
  equity_baseline_at: string | null;
  equity_stop_triggered: boolean;
  created_at: string;
  updated_at: string;
}

export interface Position {
  id: number;
  strategy_id: number | null;
  account_id: number;
  symbol: string;
  side: 'long' | 'short';
  quantity: number;
  entry_price: number;
  mark_price: number | null;
  unrealized_pnl: number | null;
  layer: number;
  grid_level: number;
  grid_trigger_price: number | null;
  take_profit_price: number | null;
  exchange_order_id: string | null;
  tp_limit_order_id: string | null;
  add_limit_order_id: string | null;
  opened_at: string;
  closed_at: string | null;
}

export interface Trade {
  id: number;
  strategy_id: number | null;
  account_id: number;
  symbol: string;
  side: 'long' | 'short';
  quantity: number;
  entry_price: number;
  exit_price: number;
  realized_pnl: number;
  pnl_pct: number;
  entry_time: string;
  exit_time: string;
  layer: number;
  grid_level: number;
  close_reason: string;
}

export interface DashboardData {
  total_balance: number;
  available_balance: number;
  unrealized_pnl: number;
  unrealized_pnl_long: number;
  unrealized_pnl_short: number;
  daily_pnl: number;
  daily_pnl_long: number;
  daily_pnl_short: number;
  daily_pnl_pct: number;
  active_strategies: number;
  open_positions: number;
  daily_trades: number;
  win_rate_pct: number;
  total_realized_pnl: number;
  total_trades: number;
  total_win_rate_pct: number;
  total_pnl_long: number;
  total_pnl_short: number;
  leverage_multiplier: number;
  master_switch: boolean;
  account_name: string;
  balance_status: string;
  exchange_positions: Array<{
    symbol: string;
    side: string;
    usdt: number;
    contracts?: number;
    entry_price: number;
    mark_price: number;
    unrealized_pnl: number;
    pnl_pct: number;
  }>;
  strategy_stats: Array<{
    strategy_id: number;
    symbol: string;
    direction: string;
    status: string;
    tp_total: number;
    tp_today: number;
    sl_events: Array<{
      time: string;
      exit_price: number;
      quantity: number;
    }>;
  }>;
  special_sl_restarts: Array<{
    strategy_id: number;
    symbol: string;
    direction: string;
    time: string;
    exit_price: number;
    quantity: number;
    realized_pnl: number;
  }>;
  trading_window?: {
    enabled: boolean;
    start_hm: string;
    end_hm: string;
    within_window: boolean;
  };
}

export interface TradingScheduleConfig {
  enabled: boolean;
  start_hm: string;
  end_hm: string;
  within_window: boolean;
}
