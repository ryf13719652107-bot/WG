import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';
import type { Strategy } from '../../types/strategy';
import type { Account } from '../../types';
import StrategyForm from './StrategyForm';
import { Play, Square, AlertTriangle, Trash2, Plus, Eye, Edit3 } from 'lucide-react';

export default function StrategyPage() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
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

  const handleEdit = (id: number) => {
    setEditingId(id);
    setShowForm(true);
  };

  const handleSubmit = async (data: any) => {
    try {
      await api.createStrategy(data);
      setShowForm(false);
      load();
    } catch (e: any) {
      alert('创建策略失败: ' + (e.message || '未知错误'));
    }
  };

  const handleSubmitEdit = async (data: any) => {
    if (!editingId) return;
    try {
      await api.updateStrategy(editingId, data);
      setShowForm(false);
      setEditingId(null);
      load();
    } catch (e: any) {
      alert('更新策略失败: ' + (e.message || '未知错误'));
    }
  };

  const editingStrategy = editingId ? strategies.find(s => s.id === editingId) || null : null;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold">策略管理</h2>
        <button
          onClick={() => { setEditingId(null); setShowForm(true); }}
          className="flex items-center gap-1.5 bg-blue-600 hover:bg-blue-700 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors"
        >
          <Plus size={16} />
          新建策略
        </button>
      </div>

      {showForm && editingId === null && (
        <StrategyForm
          accounts={accounts}
          initialData={null}
          onSubmit={handleSubmit}
          onCancel={() => setShowForm(false)}
        />
      )}

      {showForm && editingId !== null && editingStrategy && (
        <StrategyForm
          accounts={accounts}
          initialData={editingStrategy}
          onSubmit={handleSubmitEdit}
          onCancel={() => { setShowForm(false); setEditingId(null); }}
        />
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
        {strategies.map((s) => (
          <div key={s.id} className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
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
        {strategies.length === 0 && (
          <div className="col-span-full text-center text-gray-600 py-8">暂无策略</div>
        )}
      </div>
    </div>
  );
}
