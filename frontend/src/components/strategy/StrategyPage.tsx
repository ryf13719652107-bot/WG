import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';
import type { Strategy } from '../../types/strategy';
import type { Account } from '../../types';
import StrategyForm from './StrategyForm';
import { Play, Square, AlertTriangle, Edit, Trash2, Plus, Eye } from 'lucide-react';

export default function StrategyPage() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<Strategy | null>(null);
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
      // silently ignore load errors — data stays stale, user can retry
    }
  };

  useEffect(() => { load(); }, [selectedAccountId]);

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
      alert(`平仓完成: ${result.closed} 成功, ${result.failed || 0} 失败`);
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

  const handleSubmit = async (data: any) => {
    try {
      if (editing) {
        await api.updateStrategy(editing.id, data);
      } else {
        await api.createStrategy(data);
      }
      setShowForm(false);
      setEditing(null);
      load();
    } catch (e: any) {
      alert((editing ? '更新' : '创建') + '策略失败: ' + (e.message || '未知错误'));
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold">策略管理</h2>
        <button
          onClick={() => { setEditing(null); setShowForm(true); }}
          className="flex items-center gap-1.5 bg-blue-600 hover:bg-blue-700 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors"
        >
          <Plus size={16} />
          新建策略
        </button>
      </div>

      {showForm && (
        <StrategyForm
          accounts={accounts}
          initialData={editing}
          onSubmit={handleSubmit}
          onCancel={() => { setShowForm(false); setEditing(null); }}
        />
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {strategies.map((s) => (
          <div key={s.id} className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="flex items-center justify-between mb-3">
              <div>
                <Link to={`/strategies/${s.id}`} className="font-semibold hover:text-blue-400 transition-colors">{s.name}</Link>
                <span className={`ml-2 text-xs px-2 py-0.5 rounded ${
                  s.direction === 'long' ? 'bg-green-600/20 text-green-400' : 'bg-red-600/20 text-red-400'
                }`}>
                  {s.direction === 'long' ? '做多' : '做空'}
                </span>
                <span className={`ml-2 text-xs px-2 py-0.5 rounded ${
                  s.status === 'running' ? 'bg-green-600/20 text-green-400' :
                  s.status === 'error' ? 'bg-red-600/20 text-red-400' :
                  'bg-gray-700 text-gray-400'
                }`}>
                  {s.status === 'running' ? '运行中' : s.status === 'error' ? '异常' : '已停止'}
                </span>
              </div>
              <div className="flex items-center gap-1">
                <Link to={`/strategies/${s.id}`} className="p-1.5 text-blue-400 hover:bg-blue-600/20 rounded" title="查看详情">
                  <Eye size={16} />
                </Link>
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
                <button onClick={() => { setEditing(s); setShowForm(true); }} className="p-1.5 text-gray-400 hover:bg-gray-700 rounded" title="编辑">
                  <Edit size={16} />
                </button>
                <button onClick={() => handleDelete(s.id)} className="p-1.5 text-gray-400 hover:bg-red-600/20 rounded" title="删除">
                  <Trash2 size={16} />
                </button>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-2 text-xs text-gray-400">
              <div>交易对: <span className="text-gray-200">{s.symbol}</span></div>
              <div>首单: <span className="text-gray-200">{s.base_qty_type === 'margin_pct' ? `${s.base_qty_value}%保证金` : `${s.base_qty_value}U`}</span></div>
              <div>杠杆: <span className="text-gray-200">{s.leverage}x</span></div>
              <div>首层跌幅: <span className="text-gray-200">{s.grid_drop_base_pct}%</span></div>
              <div>层级倍数: <span className="text-gray-200">x{s.grid_interval_multiplier} / x{s.position_multiplier}</span></div>
              <div>最大层数: <span className="text-gray-200">{s.max_layers}</span></div>
              <div>止盈: <span className="text-gray-200">{s.tp_pct}%</span></div>
              <div>止损阈值: <span className="text-gray-200">{s.cumulative_loss_threshold_u > 0 ? `${s.cumulative_loss_threshold_u}U` : '禁用'}</span></div>
              <div>平仓重开: <span className="text-gray-200">{s.reopen_after_close ? '是' : '否'}</span></div>
            </div>
          </div>
        ))}
        {strategies.length === 0 && (
          <div className="col-span-2 text-center text-gray-600 py-8">暂无策略</div>
        )}
      </div>
    </div>
  );
}
