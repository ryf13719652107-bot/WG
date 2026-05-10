/**
 * 与后端 `BaseExchangeService._norm_sym` 一致：统一为 BASEQUOTE（如 SUIUSDT），
 * 避免 SUI-USDT 与 SUI/USDT:USDT 在持仓合并时无法对齐。
 */
export function normSym(s: string): string {
  let x = (s || '').trim().toUpperCase();
  x = x.replace(/\//g, '').replace(':USDT', '');
  x = x.replace(/-SWAP/gi, '');
  x = x.replace(/-/g, '');
  return x;
}

/** 合并持仓行用的多空；兼容大写或交易所原始字段 */
export function normExchangeLegSide(side: string | undefined | null): 'long' | 'short' | null {
  const s = (side || '').trim().toLowerCase();
  if (s === 'long' || s === 'short') return s;
  return null;
}
