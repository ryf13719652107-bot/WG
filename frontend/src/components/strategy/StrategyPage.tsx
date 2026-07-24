import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';
import type { Strategy } from '../../types/strategy';
import type { Account } from '../../types';
import StrategyForm from './StrategyForm';
import DeclineRankAutoForm from './DeclineRankAutoForm';
import { Play, Square, AlertTriangle, Trash2, Plus, Eye, Edit3, Hand, TrendingDown } from 'lucide-react';

type CreateMode = null | 'choose' | 'manual' | 'auto';

export default function StrategyPage() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [createMode, setCreateMode] = useState<CreateMode>(null);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [bulkBusy, setBulkBusy] = useState<'start' | 'stop' | 'panic' | null>(null);
  const selectedAccountId = useDashboardStore((s) => s.selectedAccountId);

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
  };

  useEffect(() => { load(); }, [selectedAccountId]);

  const closeCreate = () => {
    setCreateMode(null);
    setEditingId(null);
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

  const handleBulkStart = async () => {
    if (!confirm('确认一键启动当前账户下全部已停止的策略？')) return;
    setBulkBusy('start');
    try {
      const r = await api.bulkStartStrategies(selectedAccountId ?? undefined);
      alert(`启动完成：成功 ${r.started}，失败 ${r.failed}，跳过 ${r.skipped}${r.errors?.length ? '\n' + r.errors.join('\n') : ''}`);
      load();
    } catch (e: unknown) {
      alert('一键启动失败: ' + (e instanceof Error ? e.message : String(e)));
    }
    setBulkBusy(null);
  };

  const handleBulkStop = async () => {
    if (!confirm('确认一键停止当前账户下全部运行中的策略？')) return;
    setBulkBusy('stop');
    try {
      const r = await api.bulkStopStrategies(selectedAccountId ?? undefined);
      alert(`已停止 ${r.stopped} 个策略`);
      load();
    } catch (e: unknown) {
      alert('一键停止失败: ' + (e instanceof Error ? e.message : String(e)));
    }
    setBulkBusy(null);
  };

  const handleBulkPanicClose = async () => {
    if (!confirm('确认一键紧急平仓当前账户下全部策略？将市价平仓并撤单。')) return;
    setBulkBusy('panic');
    try {
      const r = await api.bulkPanicClose(selectedAccountId ?? undefined);
      alert(`平仓完成：成功 ${r.closed}，无仓 ${r.no_position}，失败 ${r.failed}`);
      load();
    } catch (e: unknown) {
      alert('一键平仓失败: ' + (e instanceof Error ? e.message : String(e)));
    }
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

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <button
          type="button"
          disabled={bulkBusy !== null}
          onClick={() => void handleBulkStart()}
          className="flex items-center justify-center gap-2 px-4 py-3 rounded-lg bg-green-600 hover:bg-green-700 disabled:opacity-50 text-sm font-semibold transition-colors"
        >
          <Play size={18} />
          {bulkBusy === 'start' ? '启动中…' : '一键启动全部'}
        </button>
        <button
          type="button"
          disabled={bulkBusy !== null}
          onClick={() => void handleBulkStop()}
          className="flex items-center justify-center gap-2 px-4 py-3 rounded-lg bg-amber-600 hover:bg-amber-700 disabled:opacity-50 text-sm font-semibold transition-colors"
        >
          <Square size={18} />
          {bulkBusy === 'stop' ? '停止中…' : '一键停止全部'}
        </button>
        <button
          type="button"
          disabled={bulkBusy !== null}
          onClick={() => void handleBulkPanicClose()}
          className="flex items-center justify-center gap-2 px-4 py-3 rounded-lg bg-red-600 hover:bg-red-700 disabled:opacity-50 text-sm font-semibold transition-colors"
        >
          <AlertTriangle size={18} />
          {bulkBusy === 'panic' ? '平仓中…' : '一键平仓全部'}
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
          onSaved={() => load()}
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
        {strategies.map((s) => (
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
                {s.source === 'decline_rank' && (
                  <span className="text-xs px-2 py-0.5 rounded bg-cyan-600/20 text-cyan-400">
                    跌幅榜自动
                  </span>
                )}
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
        {strategies.length === 0 && (
          <div className="col-span-full text-center text-gray-600 py-8">暂无策略</div>
        )}
      </div>
    </div>
  );
}
