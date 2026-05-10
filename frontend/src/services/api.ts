import type { Account, Position, Trade, DashboardData, FeishuNotifySettings, WebUiPasswordStatus } from '../types';
import type { Strategy, StrategyFormData } from '../types/strategy';

const BASE = '/api';

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const path = url.startsWith('http') ? new URL(url).pathname : `${BASE}${url}`;
  const res = await fetch(`${BASE}${url}`, {
    credentials: 'include',
    ...options,
    headers: { 'Content-Type': 'application/json', ...options?.headers },
  });
  if (res.status === 401) {
    if (!path.endsWith('/auth/login') && !path.endsWith('/auth/status')) {
      window.dispatchEvent(new CustomEvent('wg-auth-required'));
    }
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(typeof err.detail === 'string' ? err.detail : 'Request failed');
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || 'Request failed');
  }
  if (res.status === 204) return undefined as unknown as T;
  return res.json() as Promise<T>;
}

export const api = {
  authStatus: (): Promise<{ auth_required: boolean; authenticated: boolean }> =>
    request('/auth/status'),

  login: (password: string): Promise<{ ok: boolean; auth_required?: boolean }> =>
    request('/auth/login', { method: 'POST', body: JSON.stringify({ password }) }),

  logout: (): Promise<{ ok: boolean }> =>
    request('/auth/logout', { method: 'POST', body: JSON.stringify({}) }),

  // Accounts
  createAccount: (data: {
    name: string; exchange: string; api_key: string; api_secret: string;
    okx_passphrase?: string; testnet: boolean; hedge_mode: boolean;
  }): Promise<Account> =>
    request<Account>('/accounts', { method: 'POST', body: JSON.stringify(data) }),
  listAccounts: (): Promise<Account[]> => request<Account[]>('/accounts'),
  deleteAccount: (id: number): Promise<void> => request<void>(`/accounts/${id}`, { method: 'DELETE' }),

  // Strategies
  createStrategy: (data: StrategyFormData): Promise<Strategy> =>
    request<Strategy>('/strategies', { method: 'POST', body: JSON.stringify(data) }),
  listStrategies: (status?: string, accountId?: number, symbol?: string): Promise<Strategy[]> => {
    const qs = new URLSearchParams();
    if (status) qs.set('status', status);
    if (accountId != null) qs.set('account_id', String(accountId));
    if (symbol) qs.set('symbol', symbol);
    const q = qs.toString();
    return request<Strategy[]>(`/strategies${q ? `?${q}` : ''}`);
  },
  getStrategy: (id: number): Promise<Strategy> => request<Strategy>(`/strategies/${id}`),
  updateStrategy: (id: number, data: Partial<StrategyFormData>): Promise<Strategy> =>
    request<Strategy>(`/strategies/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteStrategy: (id: number): Promise<void> => request<void>(`/strategies/${id}`, { method: 'DELETE' }),
  startStrategy: (id: number): Promise<{ status: string }> =>
    request(`/strategies/${id}/start`, { method: 'POST' }),
  stopStrategy: (id: number): Promise<{ status: string }> =>
    request(`/strategies/${id}/stop`, { method: 'POST' }),
  panicCloseStrategy: (id: number): Promise<{
    closed: number; failed: number;
    results: Array<{ symbol: string; side: string; status: string; exit_price?: number; error?: string }>;
  }> => request(`/strategies/${id}/panic-close`, { method: 'POST' }),
  getStrategyLogs: (id: number, limit?: number): Promise<{ time: string; level: string; message: string }[]> =>
    request(`/strategies/${id}/logs${limit ? `?limit=${limit}` : ''}`),
  getExchangePositions: (id: number): Promise<{
    symbol: string; side: string; usdt: number;
    entry_price: number; mark_price: number; unrealized_pnl: number; pnl_pct: number;
  }[]> => request(`/strategies/${id}/exchange-positions`),

  // Positions
  listPositions: (params?: { strategy_id?: number; symbol?: string; account_id?: number }): Promise<Position[]> => {
    const qs = new URLSearchParams();
    if (params?.strategy_id) qs.set('strategy_id', String(params.strategy_id));
    if (params?.symbol) qs.set('symbol', params.symbol);
    if (params?.account_id != null) qs.set('account_id', String(params.account_id));
    const q = qs.toString();
    return request<Position[]>(`/positions${q ? `?${q}` : ''}`);
  },
  closePosition: (id: number): Promise<{ status: string }> =>
    request(`/positions/${id}/close`, { method: 'POST' }),

  // Trades
  listTrades: (params?: {
    strategy_id?: number; symbol?: string; side?: string; account_id?: number; limit?: number; offset?: number;
  }): Promise<{ trades: Trade[]; total: number }> => {
    const qs = new URLSearchParams();
    if (params?.strategy_id) qs.set('strategy_id', String(params.strategy_id));
    if (params?.symbol) qs.set('symbol', params.symbol);
    if (params?.side) qs.set('side', params.side);
    if (params?.account_id != null) qs.set('account_id', String(params.account_id));
    if (params?.limit) qs.set('limit', String(params.limit));
    if (params?.offset) qs.set('offset', String(params.offset));
    const q = qs.toString();
    return request(`/trades${q ? `?${q}` : ''}`);
  },
  deleteTrade: (id: number): Promise<void> => request(`/trades/${id}`, { method: 'DELETE' }),
  deleteFilteredTrades: (params?: { symbol?: string; side?: string; account_id?: number }): Promise<void> => {
    const qs = new URLSearchParams();
    if (params?.symbol) qs.set('symbol', params.symbol);
    if (params?.side) qs.set('side', params.side);
    if (params?.account_id != null) qs.set('account_id', String(params.account_id));
    const q = qs.toString();
    return request(`/trades${q ? `?${q}` : ''}`, { method: 'DELETE' });
  },

  // Dashboard
  getDashboard: (accountId?: number): Promise<DashboardData> =>
    request<DashboardData>(`/dashboard${accountId ? `?account_id=${accountId}` : ''}`),

  // Markets
  getMarkets: (exchange?: string): Promise<{ symbols: string[] }> =>
    request(`/markets${exchange ? `?exchange=${exchange}` : ''}`),
  getStrategyCounts: (accountId?: number): Promise<{
    counts: Record<string, number>;
    directions: Record<string, string[]>;
  }> => request(`/markets/strategy-counts${accountId ? `?account_id=${accountId}` : ''}`),

  // Bot toggle
  toggleBot: (enabled: boolean): Promise<{ master_switch: boolean }> =>
    request('/bot/toggle', { method: 'POST', body: JSON.stringify({ enabled }) }),

  getFeishuNotify: (): Promise<FeishuNotifySettings> => request('/bot/feishu-notify'),

  updateFeishuNotify: (data: {
    webhook_url?: string;
    keyword_prefix?: string;
    keyword_prefix_use_env_default?: boolean;
  }): Promise<FeishuNotifySettings> =>
    request('/bot/feishu-notify', { method: 'PUT', body: JSON.stringify(data) }),

  getWebUiPasswordStatus: (): Promise<WebUiPasswordStatus> => request('/bot/web-ui-password'),

  updateWebUiPassword: (password: string): Promise<WebUiPasswordStatus> =>
    request('/bot/web-ui-password', {
      method: 'PUT',
      body: JSON.stringify({ password }),
    }),
};
