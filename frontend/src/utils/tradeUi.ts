/** 后端 close_reason → 简短中文展示 */
export function formatCloseReason(cr: string | undefined | null): string {
  if (cr == null || cr === '') return '—';
  const labels: Record<string, string> = {
    take_profit: '止盈',
    stop_loss: '止损',
    panic_close: '紧急平仓',
    panic_loss: '恐慌止损',
    sync: '同步平仓',
    margin_stop: '保证金止损',
    equity_stop: '总资产止损',
    schedule_stop: '时段收市',
    manual: '手动平仓',
    strategy_deleted: '策略删除',
    策略删除: '策略删除',
    exchange_already_flat: '交易所已平仓',
  };
  return labels[cr] ?? cr;
}
