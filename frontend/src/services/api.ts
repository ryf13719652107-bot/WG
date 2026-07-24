import type {
  Account,
  Position,
  Trade,
  DashboardData,
  WebUiPasswordStatus,
} from '../types';
import type { Strategy, StrategyFormData, StrategyParamTemplate, StrategyParamFields } from '../types/strategy';

const BASE = '/api';

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const path = url.startsWith('http') ? new URL(url).pathname : `${BASE}${url}`;
  const authPath = path.includes('/auth/');
  const res = await fetch(`${BASE}${url}`, {
    credentials: 'include',
    ...options,
    ...(authPath ? { cache: 'no-store' as RequestInit['cache'] } : {}),
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
    const detail = err.detail || res.statusText || 'Request failed';
    throw new Error(
      typeof detail === 'string'
        ? (res.status >= 500 ? `${detail}（HTTP ${res.status}，请确认后端已重启且为最新代码）` : detail)
        : 'Request failed',
    );
  }
  if (res.status === 204) return undefined as unknown as T;
  const contentType = res.headers.get('content-type') || '';
  const text = await res.text();
  if (!text) return undefined as unknown as T;
  if (contentType.includes('application/json') || text.trimStart().startsWith('{') || text.trimStart().startsWith('[')) {
    try {
      return JSON.parse(text) as T;
    } catch (e) {
      throw new Error(`接口返回无法解析的 JSON（${url}）`);
    }
  }
  if (text.trimStart().startsWith('<!DOCTYPE') || text.trimStart().startsWith('<html')) {
    throw new Error(
      `接口 ${url} 返回了网页而非 JSON，通常是后端未重启或未更新到含该接口的版本。请在服务器重启后端进程后再试。`,
    );
  }
  throw new Error(`接口 ${url} 返回了非 JSON 内容`);
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
  updateAccountEquityGuard: (id: number, equity_stop_floor_u: number): Promise<Account> =>
    request<Account>(`/accounts/${id}/equity-guard`, {
      method: 'PATCH',
      body: JSON.stringify({ equity_stop_floor_u }),
    }),
  resetAccountEquityGuard: (id: number): Promise<Account> =>
    request<Account>(`/accounts/${id}/equity-guard/reset`, { method: 'POST' }),

  // Strategies
  listParamTemplates: (): Promise<StrategyParamTemplate[]> =>
    request<StrategyParamTemplate[]>('/strategies/param-templates'),
  saveParamTemplate: (name: string, params: StrategyParamFields): Promise<StrategyParamTemplate> =>
    request<StrategyParamTemplate>('/strategies/param-templates', {
      method: 'POST',
      body: JSON.stringify({ name, params }),
    }),
  deleteParamTemplate: (id: string): Promise<{ ok: boolean }> =>
    request<{ ok: boolean }>(`/strategies/param-templates/${id}`, { method: 'DELETE' }),
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
  bulkStartStrategies: (accountId?: number): Promise<{
    started: number; failed: number; skipped: number; total: number; errors: string[];
  }> => request(`/strategies/bulk/start${accountId != null ? `?account_id=${accountId}` : ''}`, { method: 'POST' }),
  bulkStopStrategies: (accountId?: number): Promise<{ stopped: number; total: number }> =>
    request(`/strategies/bulk/stop${accountId != null ? `?account_id=${accountId}` : ''}`, { method: 'POST' }),
  bulkPanicClose: (accountId?: number): Promise<{
    closed: number; failed: number; no_position: number; total: number;
    results: Array<{ strategy_id: number; symbol: string; direction: string; status: string; error?: string }>;
  }> => request(`/strategies/bulk/panic-close${accountId != null ? `?account_id=${accountId}` : ''}`, { method: 'POST' }),
  panicCloseStrategy: (id: number): Promise<{
    closed: number; failed: number;
    results: Array<{ symbol: string; side: string; status: string; exit_price?: number; error?: string }>;
  }> => request(`/strategies/${id}/panic-close`, { method: 'POST' }),
  getStrategyLogs: (id: number, limit?: number): Promise<{ time: string; level: string; message: string }[]> =>
    request(`/strategies/${id}/logs${limit ? `?limit=${limit}` : ''}`),
  getStrategyStats: (id: number): Promise<{
    tp_total: number;
    tp_today: number;
    sl_events: Array<{ time: string; exit_price: number; quantity: number }>;
  }> => request(`/strategies/${id}/stats`),
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
    strategy_id?: number;
    symbol?: string;
    side?: string;
    account_id?: number;
    close_reason?: string;
    limit?: number;
    offset?: number;
  }): Promise<{ trades: Trade[]; total: number }> => {
    const qs = new URLSearchParams();
    if (params?.strategy_id) qs.set('strategy_id', String(params.strategy_id));
    if (params?.symbol) qs.set('symbol', params.symbol);
    if (params?.side) qs.set('side', params.side);
    if (params?.account_id != null) qs.set('account_id', String(params.account_id));
    if (params?.close_reason) qs.set('close_reason', params.close_reason);
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
  getMarkets: (exchange?: string, accountId?: number): Promise<{ symbols: string[] }> => {
    const params = new URLSearchParams();
    if (exchange) params.set('exchange', exchange);
    if (accountId) params.set('account_id', String(accountId));
    const q = params.toString();
    return request(`/markets${q ? `?${q}` : ''}`);
  },

  searchMarkets: (query: string, exchange?: string, accountId?: number): Promise<{ symbols: string[] }> => {
    const params = new URLSearchParams({ q: query });
    if (exchange) params.set('exchange', exchange);
    if (accountId) params.set('account_id', String(accountId));
    return request(`/markets/search?${params.toString()}`);
  },
  getStrategyCounts: (accountId?: number): Promise<{
    counts: Record<string, number>;
    directions: Record<string, string[]>;
  }> => request(`/markets/strategy-counts${accountId ? `?account_id=${accountId}` : ''}`),

  // Bot toggle
  toggleBot: (enabled: boolean): Promise<{ master_switch: boolean }> =>
    request('/bot/toggle', { method: 'POST', body: JSON.stringify({ enabled }) }),

  getWebUiPasswordStatus: (): Promise<WebUiPasswordStatus> => request('/bot/web-ui-password'),

  updateWebUiPassword: (password: string): Promise<WebUiPasswordStatus> =>
    request('/bot/web-ui-password', {
      method: 'PUT',
      body: JSON.stringify({ password }),
    }),

  getDeclineRankConfig: (): Promise<import('../types/declineRank').DeclineRankAutoConfig> =>
    request('/bot/decline-rank-config'),

  saveDeclineRankConfig: (
    config: import('../types/declineRank').DeclineRankAutoConfig,
  ): Promise<import('../types/declineRank').DeclineRankAutoConfig> =>
    request('/bot/decline-rank-config', {
      method: 'PUT',
      body: JSON.stringify(config),
    }),

  getDeclineRankStatus: (): Promise<import('../types/declineRank').DeclineRankAutoStatus> =>
    request('/bot/decline-rank-status'),

  refreshDeclineRank: (): Promise<Record<string, unknown>> =>
    request('/bot/decline-rank-refresh', { method: 'POST' }),

  /** 暂停：优先 POST /pause；若 405（旧后端未加载路由）则回退 PUT config + cleanup */
  pauseDeclineRank: async (cleanup = true): Promise<Record<string, unknown>> => {
    try {
      return await request(`/bot/decline-rank-pause?cleanup=${cleanup ? 'true' : 'false'}`, {
        method: 'POST',
      });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      if (!/405|Method Not Allowed|非 JSON|网页而非 JSON/i.test(msg)) {
        throw e;
      }
      const cfg = await request<import('../types/declineRank').DeclineRankAutoConfig>(
        '/bot/decline-rank-config',
      );
      await request('/bot/decline-rank-config?cleanup=true', {
        method: 'PUT',
        body: JSON.stringify({ ...cfg, enabled: false }),
      });
      return { enabled: false, cleanup, via: 'put-config-fallback' };
    }
  },
};
