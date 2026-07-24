import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';
import type { Strategy } from '../../types/strategy';
import type { Account } from '../../types';
import type { DeclineRankAutoStatus } from '../../types/declineRank';
import StrategyForm from './StrategyForm';
import DeclineRankAutoForm from './DeclineRankAutoForm';
import { Play, Square, AlertTriangle, Trash2, Plus, Eye, Edit3, Hand, TrendingDown, Pause, RefreshCw } from 'lucide-react';

type CreateMode = null | 'choose' | 'manual' | 'auto';

export default function StrategyPage() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [createMode, setCreateMode] = useState<CreateMode>(null);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [bulkBusy, setBulkBusy] = useState<'start' | 'stop' | 'panic' | null>(null);
  const [autoStatus, setAutoStatus] = useState<DeclineRankAutoStatus | null>(null);
  const [autoBusy, setAutoBusy] = useState<'pause' | 'refresh' | null>(null);
  const [autoExpanded, setAutoExpanded] = useState(false);
  const selectedAccountId = useDashboardStore((s) => s.selectedAccountId);

  const loadAutoStatus = async () => {
    try {
      const st = await api.getDeclineRankStatus();
      setAutoStatus(st);
    } catch {
      setAutoStatus(null);
    }
  };

  const load = async () => {
    try {
      const [s, a] = await Promise.all([
        api.listStrategies(undefined, selectedAccountId ?? undefined),
        api.listAccounts(),
      ]);
      setStrategies(s);
      setAccounts(a);
    } catch {
    }
    await loadAutoStatus();
  };

  useEffect(() => { load(); }, [selectedAccountId]);

  useEffect(() => {
    if (!autoStatus?.enabled) return;
    const t = setInterval(() => { void load(); }, 30000);
    return () => clearInterval(t);
  }, [autoStatus?.enabled, selectedAccountId]);

  const closeCreate = () => {
    setCreateMode(null);
    setEditingId(null);
  };

  const handlePauseAuto = async () => {
    const n = autoStatus?.auto_strategy_count ?? 0;
    const msg = n > 0
      ? `确认暂停跌幅榜自动策略？将关闭自动模式，并对已创建的 ${n} 个自动策略执行撤单+市价平仓+删除。手动策略不受影响。`
      : '确认暂停跌幅榜自动策略？将关闭自动模式，不再按跌幅榜建仓。';
    if (!confirm(msg)) return;
    setAutoBusy('pause');
    try {
      await api.pauseDeclineRank(true);
      alert('已暂停自动策略');
      closeCreate();
      await load();
    } catch (e: unknown) {
      alert('暂停失败: ' + (e instanceof Error ? e.message : String(e)));
    }
    setAutoBusy(null);
  };

  const handleRefreshAuto = async () => {
    setAutoBusy('refresh');
    try {
      const r = await api.refreshDeclineRank();
      const created = Number(r.created ?? 0);
      const skipped = Number(r.skipped ?? 0);
      const failed = Number(r.failed ?? 0);
      alert(`刷新完成：新建 ${created}，跳过 ${skipped}，失败 ${failed}`);
      await load();
    } catch (e: unknown) {
      alert('刷新失败: ' + (e instanceof Error ? e.message : String(e)));
      await loadAutoStatus();
    }
    setAutoBusy(null);
  };

  const handleStart = async (id: number) => {
    try {
      await api.startStrategy(id);
    } catch (e: any) {
      alert('启动失败: ' + (e.message || '未知错误'));
    }
    load();
  };

  const handleStop = async (id: number) => {
    try {
      await api.stopStrategy(id);
    } catch (e: any) {
      alert('停止失败: ' + (e.message || '未知错误'));
    }
    load();
  };

  const handlePanicClose = async (id: number) => {
    if (!confirm('确认紧急平仓？将以市价单平掉该策略对应账户的所有交易所持仓。')) return;
    try {
      const result = await api.panicCloseStrategy(id);
      const failedList = (result.results || [])
        .filter((r: any) => r.status === 'failed')
        .map((r: any) => `${r.symbol}(${r.side}): ${r.error}`)
        .join('; ');
      const msg = `平仓完成: ${result.closed} 成功, ${result.failed || 0} 失败`;
      alert(failedList ? msg + '\n失败详情: ' + failedList : msg);
    } catch (e: any) {
      alert('平仓失败: ' + (e.message || e));
    }
    load();
  };

  const handleDelete = async (id: number) => {
    if (!confirm('确定要删除该策略吗？')) return;
    try {
      await api.deleteStrategy(id);
    } catch (e: any) {
      alert(e.message);
    }
    load();
  };

  const handleEdit = (id: number) => {
    setCreateMode(null);
    setEditingId(id);
  };

  const handleSubmit = async (data: any) => {
    try {
      await api.createStrategy(data);
      closeCreate();
      load();
    } catch (e: any) {
      alert('创建策略失败: ' + (e.message || '未知错误'));
    }
  };

  const handleSubmitEdit = async (data: any) => {
    if (!editingId) return;
    try {
      await api.updateStrategy(editingId, data);
      closeCreate();
      load();
    } catch (e: any) {
      alert('更新策略失败: ' + (e.message || '未知错误'));
    }
  };

  const editingStrategy = editingId ? strategies.find(s => s.id === editingId) || null : null;

  const manualStrategies = strategies.filter((s) => s.source !== 'decline_rank');
  const autoStrategies = strategies.filter((s) => s.source === 'decline_rank');
  const autoRunning = autoStrategies.filter((s) => s.status === 'running').length;
  const autoStopped = autoStrategies.filter((s) => s.status !== 'running').length;
  const showAutoCard = Boolean(autoStatus?.enabled || autoStrategies.length > 0);

  const handleBulkStart = async () => {
    const targets = manualStrategies.filter((s) => s.status === 'stopped' || s.status === 'error');
    if (targets.length === 0) {
      alert('没有可启动的手动策略（自动策略请用上方合并卡片管理）');
      return;
    }
    if (!confirm(`确认启动 ${targets.length} 个已停止的手动策略？`)) return;
    setBulkBusy('start');
    let started = 0;
    const errors: string[] = [];
    for (const s of targets) {
      try {
        await api.startStrategy(s.id);
        started += 1;
      } catch (e: unknown) {
        errors.push(`${s.symbol}: ${e instanceof Error ? e.message : String(e)}`);
      }
    }
    alert(`启动完成：成功 ${started}${errors.length ? `\n失败:\n${errors.slice(0, 10).join('\n')}` : ''}`);
    await load();
    setBulkBusy(null);
  };

  const handleBulkStop = async () => {
    const targets = manualStrategies.filter((s) => s.status === 'running');
    if (targets.length === 0) {
      alert('没有运行中的手动策略');
      return;
    }
    if (!confirm(`确认停止 ${targets.length} 个运行中的手动策略？`)) return;
    setBulkBusy('stop');
    for (const s of targets) {
      try {
        await api.stopStrategy(s.id);
      } catch {
      }
    }
    alert(`已停止手动策略`);
    await load();
    setBulkBusy(null);
  };

  const handleBulkPanicClose = async () => {
    const targets = manualStrategies;
    if (targets.length === 0) {
      alert('没有手动策略可平仓（自动策略请用「暂停自动」）');
      return;
    }
    if (!confirm(`确认对 ${targets.length} 个手动策略紧急平仓？自动策略不受影响。`)) return;
    setBulkBusy('panic');
    let closed = 0;
    let failed = 0;
    for (const s of targets) {
      try {
        const r = await api.panicCloseStrategy(s.id);
        if ((r.failed || 0) > 0) failed += 1;
        else closed += 1;
      } catch {
        failed += 1;
      }
    }
    alert(`手动策略平仓：处理 ${closed}，失败 ${failed}`);
    await load();
    setBulkBusy(null);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-xl font-bold">策略管理</h2>
        <button
          type="button"
          onClick={() => { setEditingId(null); setCreateMode('choose'); }}
          className="flex items-center gap-1.5 bg-blue-600 hover:bg-blue-700 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors shrink-0"
        >
          <Plus size={16} />
          新建策略
        </button>
      </div>

      {showAutoCard && (
        <div className="bg-gray-900 border border-cyan-800/50 rounded-lg p-4 space-y-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="space-y-1.5 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <TrendingDown size={18} className="text-cyan-400 shrink-0" />
                <span className="font-semibold text-white text-base">跌幅榜自动策略</span>
                <span className={`text-xs px-2 py-0.5 rounded ${
                  autoStatus?.enabled ? 'bg-cyan-600/20 text-cyan-300' : 'bg-gray-700 text-gray-400'
                }`}>
                  {autoStatus?.enabled ? '已启用' : '已暂停（残留仓位）'}
                </span>
                <span className={`text-xs px-2 py-0.5 rounded ${
                  autoStatus?.enabled
                    ? (autoStatus.in_window
                      ? 'bg-green-600/20 text-green-400'
                      : autoStatus.waiting_next_start
                        ? 'bg-amber-600/20 text-amber-300'
                        : 'bg-gray-700 text-gray-400')
                    : 'bg-gray-700 text-gray-400'
                }`}>
                  {!autoStatus?.enabled
                    ? '已暂停'
                    : autoStatus.in_window
                      ? '运行中（已开盘）'
                      : autoStatus.waiting_next_start
                        ? `等待开盘 ${autoStatus.next_session_at || ''}`.trim()
                        : '等待中'}
                </span>
                <span className="text-xs px-2 py-0.5 rounded bg-cyan-600/10 text-cyan-400/90">
                  {autoStrategies.length} 币 · 运行 {autoRunning} · 停止 {autoStopped}
                </span>
              </div>
              <div className="text-xs text-gray-500">
                上次刷新：{autoStatus?.last_refresh_at || '尚未刷新'}
                {autoStatus?.next_refresh_at ? ` · 下次：${autoStatus.next_refresh_at}` : ''}
              </div>
              {(autoStatus?.last_ranked_count || 0) > 0 && (
                <div className="text-xs text-gray-500">
                  最近一次榜单 {autoStatus?.last_ranked_count} 个币
                  {' · '}新建 {autoStatus?.last_created ?? 0}
                  {' · '}跳过 {autoStatus?.last_skipped ?? 0}
                  {' · '}失败 {autoStatus?.last_failed ?? 0}
                  {(autoStatus?.last_skipped || 0) > 0 && '（跳过=同币同向已有策略，不会重复开）'}
                </div>
              )}
              {autoStatus?.last_error && (
                <div className="text-xs text-amber-300">最近错误：{autoStatus.last_error}</div>
              )}
              {(autoStatus?.last_skip_reasons?.length || 0) > 0 && (
                <div className="text-xs text-gray-500 break-all">
                  跳过原因：{autoStatus?.last_skip_reasons?.slice(0, 5).join('；')}
                </div>
              )}
            </div>
            <div className="flex flex-wrap gap-2 shrink-0">
              <button
                type="button"
                disabled={autoBusy !== null}
                onClick={() => { setCreateMode('auto'); setEditingId(null); }}
                className="px-2.5 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 text-gray-200 disabled:opacity-50"
              >
                编辑配置
              </button>
              <button
                type="button"
                disabled={autoBusy !== null || !autoStatus?.enabled || !autoStatus.in_window}
                onClick={() => void handleRefreshAuto()}
                className="inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded bg-cyan-700 hover:bg-cyan-600 text-white disabled:opacity-50"
              >
                <RefreshCw size={12} />
                {autoBusy === 'refresh' ? '刷新中…' : '立即刷新'}
              </button>
              {(autoStatus?.enabled || autoStrategies.length > 0) && (
                <button
                  type="button"
                  disabled={autoBusy !== null}
                  onClick={() => void handlePauseAuto()}
                  className="inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded bg-amber-700 hover:bg-amber-600 text-white disabled:opacity-50"
                >
                  <Pause size={12} />
                  {autoBusy === 'pause' ? '处理中…' : '暂停并清理'}
                </button>
              )}
            </div>
          </div>

          <button
            type="button"
            onClick={() => setAutoExpanded((v) => !v)}
            className="text-xs text-cyan-400/90 hover:text-cyan-300"
          >
            {autoExpanded ? '收起币种列表 ▲' : `展开币种列表（${autoStrategies.length}）▼`}
          </button>

          {autoExpanded && (
            <div className="rounded-lg border border-gray-800 bg-gray-950/50 p-2 max-h-56 overflow-y-auto">
              {autoStrategies.length === 0 ? (
                <p className="text-xs text-gray-600 px-1 py-2">暂无已创建的自动策略币种</p>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
                  {autoStrategies.map((s) => (
                    <div
                      key={s.id}
                      className="flex items-center justify-between gap-2 px-2 py-1.5 rounded bg-gray-900/80 text-xs"
                    >
                      <div className="flex items-center gap-1.5 min-w-0">
                        <span className="text-gray-200 font-medium truncate">{s.symbol}</span>
                        <span className={s.direction === 'long' ? 'text-green-400' : 'text-red-400'}>
                          {s.direction === 'long' ? '多' : '空'}
                        </span>
                        <span className={
                          s.status === 'running' ? 'text-green-400' :
                          s.status === 'error' ? 'text-red-400' : 'text-gray-500'
                        }>
                          {s.status === 'running' ? '运行' : s.status === 'error' ? '异常' : '停止'}
                        </span>
                      </div>
                      <Link
                        to={`/strategies/${s.id}`}
                        className="text-blue-400 hover:text-blue-300 shrink-0"
                        title="详情"
                      >
                        <Eye size={14} />
                      </Link>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {!autoExpanded && (autoStatus?.current_symbols?.length || 0) > 0 && (
            <div className="text-xs text-gray-500 break-all">
              当前榜：{autoStatus?.current_symbols.join(', ')}
            </div>
          )}
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <button
          type="button"
          disabled={bulkBusy !== null}
          onClick={() => void handleBulkStart()}
          className="flex items-center justify-center gap-2 px-4 py-3 rounded-lg bg-green-600 hover:bg-green-700 disabled:opacity-50 text-sm font-semibold transition-colors"
        >
          <Play size={18} />
          {bulkBusy === 'start' ? '启动中…' : '一键启动手动'}
        </button>
        <button
          type="button"
          disabled={bulkBusy !== null}
          onClick={() => void handleBulkStop()}
          className="flex items-center justify-center gap-2 px-4 py-3 rounded-lg bg-amber-600 hover:bg-amber-700 disabled:opacity-50 text-sm font-semibold transition-colors"
        >
          <Square size={18} />
          {bulkBusy === 'stop' ? '停止中…' : '一键停止手动'}
        </button>
        <button
          type="button"
          disabled={bulkBusy !== null}
          onClick={() => void handleBulkPanicClose()}
          className="flex items-center justify-center gap-2 px-4 py-3 rounded-lg bg-red-600 hover:bg-red-700 disabled:opacity-50 text-sm font-semibold transition-colors"
        >
          <AlertTriangle size={18} />
          {bulkBusy === 'panic' ? '平仓中…' : '一键平仓手动'}
        </button>
      </div>

      {createMode === 'choose' && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 max-w-2xl space-y-3">
          <h3 className="text-base font-semibold text-white">选择策略类型</h3>
          <p className="text-xs text-gray-500">手动：自选币种创建单个策略。自动：按跌幅榜定时批量创建并在窗口结束时清理。</p>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <button
              type="button"
              onClick={() => setCreateMode('manual')}
              className="flex flex-col items-start gap-2 p-4 rounded-lg border border-gray-700 hover:border-blue-500 bg-gray-800/50 text-left transition-colors"
            >
              <span className="flex items-center gap-2 text-sm font-semibold text-white">
                <Hand size={18} className="text-blue-400" />
                手动策略
              </span>
              <span className="text-xs text-gray-500">选择账户、方向、交易对与网格参数，创建单个策略。</span>
            </button>
            <button
              type="button"
              onClick={() => setCreateMode('auto')}
              className="flex flex-col items-start gap-2 p-4 rounded-lg border border-gray-700 hover:border-cyan-500 bg-gray-800/50 text-left transition-colors"
            >
              <span className="flex items-center gap-2 text-sm font-semibold text-white">
                <TrendingDown size={18} className="text-cyan-400" />
                自动策略（跌幅榜）
              </span>
              <span className="text-xs text-gray-500">配置时间窗口与统一参数，按跌幅榜前 N 自动建仓。</span>
            </button>
          </div>
          <div className="flex justify-end">
            <button
              type="button"
              onClick={closeCreate}
              className="px-4 py-1.5 text-sm bg-gray-700 hover:bg-gray-600 rounded-lg"
            >
              取消
            </button>
          </div>
        </div>
      )}

      {createMode === 'manual' && (
        <StrategyForm
          accounts={accounts}
          initialData={null}
          onSubmit={handleSubmit}
          onCancel={closeCreate}
        />
      )}

      {createMode === 'auto' && (
        <DeclineRankAutoForm
          accounts={accounts}
          onCancel={closeCreate}
          onSaved={async () => {
            await load();
            try {
              await api.refreshDeclineRank();
              await load();
            } catch {
              // 窗口外或总开关关闭时刷新会失败，状态条会提示
            }
          }}
        />
      )}

      {editingId !== null && editingStrategy && (
        <StrategyForm
          accounts={accounts}
          initialData={editingStrategy}
          onSubmit={handleSubmitEdit}
          onCancel={closeCreate}
        />
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
        {manualStrategies.map((s) => (
          <div key={s.id} className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-semibold text-white text-base">{s.symbol}</span>
                <span className={`text-xs px-2 py-0.5 rounded font-medium ${
                  s.direction === 'long' ? 'bg-green-600/20 text-green-400' : 'bg-red-600/20 text-red-400'
                }`}>
                  {s.direction === 'long' ? '做多' : '做空'}
                </span>
                <span className={`text-xs px-2 py-0.5 rounded ${
                  s.status === 'running' ? 'bg-green-600/20 text-green-400' :
                  s.status === 'error' ? 'bg-red-600/20 text-red-400' :
                  'bg-gray-700 text-gray-400'
                }`}>
                  {s.status === 'running' ? '运行中' : s.status === 'error' ? '异常' : '已停止'}
                </span>
              </div>
            </div>

            <div className="flex items-center gap-1">
              <Link to={`/strategies/${s.id}`} className="p-1.5 text-blue-400 hover:bg-blue-600/20 rounded" title="查看详情">
                <Eye size={16} />
              </Link>
              <button onClick={() => handleEdit(s.id)} className="p-1.5 text-purple-400 hover:bg-purple-600/20 rounded" title="编辑参数">
                <Edit3 size={16} />
              </button>
              {s.status === 'stopped' || s.status === 'error' ? (
                <button onClick={() => handleStart(s.id)} className="p-1.5 text-green-400 hover:bg-green-600/20 rounded" title="启动">
                  <Play size={16} />
                </button>
              ) : (
                <button onClick={() => handleStop(s.id)} className="p-1.5 text-yellow-400 hover:bg-yellow-600/20 rounded" title="停止">
                  <Square size={16} />
                </button>
              )}
              <button onClick={() => handlePanicClose(s.id)} className="p-1.5 text-red-400 hover:bg-red-600/20 rounded" title="紧急平仓">
                <AlertTriangle size={16} />
              </button>
              <button onClick={() => handleDelete(s.id)} className="p-1.5 text-gray-400 hover:bg-red-600/20 rounded" title="删除">
                <Trash2 size={16} />
              </button>
            </div>
          </div>
        ))}
        {manualStrategies.length === 0 && !showAutoCard && (
          <div className="col-span-full text-center text-gray-600 py-8">暂无策略</div>
        )}
        {manualStrategies.length === 0 && showAutoCard && (
          <div className="col-span-full text-center text-gray-600 py-4 text-sm">暂无手动策略</div>
        )}
      </div>
    </div>
  );
}
