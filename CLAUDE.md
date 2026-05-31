# CLAUDE.md

本文件为 Claude Code 在此仓库中工作提供指导。

## 项目概述

马丁网格交易系统 — 纯马丁格尔网格策略的币安/OKX USDT-M 永续合约交易机器人。支持双向持仓模式（一个交易所账户同时运行做多和做空策略）。

**核心策略**：策略启动时立即市价开仓 → 挂限价止盈单(1%) → 单层链式加仓限价 → 止盈全平后可配置自动重开首单；交易所止损触发后按持仓比例减仓（减仓后刷新止盈/加仓），止损路径永不自动重开，清仓则停止策略。

**技术栈**：FastAPI (Python 3.11) + SQLite (aiosqlite) + React/TypeScript (Vite) + ccxt (币安 USDM / OKX 合约)

## 构建与运行

```bash
# 后端（开发）
cd backend && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 前端（开发，Vite 代理到后端）
cd frontend && npm run dev

# 前端（生产构建）
cd frontend && npm run build    # 输出到 frontend/dist/

# TypeScript 类型检查
cd frontend && npx tsc --noEmit

# 测试
cd backend && python -m pytest tests/ -v

# 部署
bash deploy.sh
```

## 架构

### 调度时序
每个策略在 APScheduler 中有一个定时任务（30秒间隔）：
- 获取实时价格（WebSocket 优先，REST 兜底）
- 检查止盈单成交 → 检查加仓单成交 → 检查止损
- 最大 100 策略并发（`_STRATEGY_SEMAPHORE`）

### 马丁网格交易流程
1. 调度器每 30 秒触发策略执行
2. **无持仓** → 市价开首单 + 挂限价止盈单 + 挂第一笔加仓限价单（同一时间交易所侧仅一单加仓挂单）
3. **有持仓** → 更新标记价格和浮亏 → 依次检查：
   - 止盈单是否成交 → 成交则平仓记录 +（若开启）止盈全平后自动重开首单
   - 加仓单是否成交 → 成交则记录新持仓 + 取消旧止盈 + 挂新止盈 + （未达上限时）再挂单笔下一层加仓限价
   - 交易所止损条件单是否成交 → 按配置比例减仓记账；有剩余则刷新止盈与止损单；若无剩余持仓则停止策略（不自动重开）

### 马丁网格参数
- **止盈比例** `tp_pct`：默认 1%，限价止盈
- **首层加仓跌幅** `grid_drop_base_pct`：默认 1%
- **跌幅间隔倍数** `grid_interval_multiplier`：默认 1.5（第2层=1.5%，第3层=2.25%...）
- **仓位递增倍数** `position_multiplier`：默认 1.5（每层仓位=上层×1.5）
- **最大加仓层数** `max_layers`：API 校验上限 99999（实际仍受交易所挂单/持仓限额约束）
- **止损触发亏损** `cumulative_loss_threshold_u`：按「当前整仓」推算交易所止损触发价（名义亏损达到约 U）；**0=不挂止损**
- **止损平仓比例** `stop_loss_close_pct`：触发后在持仓数量维度平仓的比例 **0–100%**；**0=不挂止损**；**100** 等价于单次触发减满仓（旧行为）；减仓后若有剩余会自动重挂止盈与下一笔加仓链
- **止盈全平后重开** `reopen_after_close`：仅当 **止盈限价全部平仓** 后是否自动市价重开首单；**止损路径永不自动重开**
- **账户总资产止损**（`Account.equity_stop_floor_u`）：0=关闭；非 0 时首个策略启动记入 `equity_baseline_u`，每分钟 `account_equity_guard` 检查合约总权益(USDT)，低于下限则对本账户各策略撤单+市价全平（`close_reason=equity_stop`）并停止全部策略
- **交易时段控制**（`bot_config`：`trading_window_*`，北京时间）：须在系统设置启用；仅 `schedule_participate=true` 的策略参与——06:00 自动恢复 `stopped_by_schedule`，21:00 收市市价全平（`schedule_stop`）；仪表盘「时段」开关绑定 `POST /schedule-participate`（开=参与时段，关=正常连续运行）；策略页启停不受时段约束（除非该策略已开「时段」且盘外）

### 多级止损机制
- **SOFT（80%阈值）**：仅预警，继续交易，记录策略日志
- **HARD（100%阈值）**：立即市价全平 + 自动重开首单
- **PANIC（单层>50%亏损）**：立即市价全平 + 自动重开首单

### 核心服务
- **`grid_engine.py`**：纯计算引擎，无副作用无I/O。计算网格层级、止盈价、均价、累计浮亏、止损判定。
- **`grid_executor.py`**：策略状态机，驱动马丁网格生命周期。每个策略一个实例。集成多级止损管理器。
- **`stop_loss_manager.py`**：多级止损评估器。SOFT/HARD/PANIC 三级判定。
- **`scheduler.py`**：策略生命周期管理、100 并发信号量、WebSocket 行情订阅。`lifespan` 启动时恢复运行中策略。
- **`price_stream.py`**：统一 WebSocket 行情管理。每交易所一个 `watch_tickers` 订阅覆盖所有交易对。自动重连(指数退避1s→30s)、心跳监测(30s)、REST 兜底(WS断开>60s)。
- **`order_tracker.py`**：内存订单缓存，按策略索引。减少交易所 API 调用，每 tick 批量检查挂单状态。
- **`health_monitor.py`**：策略健康监控。连续失败、心跳延迟、订单延迟检测。支持告警回调。
- **`log_service.py`**：策略日志服务。内存缓冲 + SQLite 持久化 + 90天保留。
- **`sync_service.py`**：每 60 秒对账 DB ↔ 交易所。交易所无仓位但 DB 有开仓记录时自动关闭并记录交易。
- **`binance_service.py`**：币安 USDM 合约 ccxt 封装（REST + WebSocket）。TTL 缓存(30min)自动重建。
- **`okx_service.py`**：OKX 合约 ccxt 封装（REST + WebSocket）。支持 passphrase。
- **`exchange_base.py`**：交易所抽象基类 + `retry_with_backoff()` 指数退避重试(3次)。
- **`exchange_factory.py`**：交易所实例工厂，按账户缓存(10min TTL)。
- **`websocket_manager.py`**：前端 WebSocket 管理。dashboard 频道 60s 广播快照。

### 数据库
- SQLite + aiosqlite，**NullPool**（按需短连接）+ WAL + `TickDbSession`（策略 tick 仅在 commit 时占库）+ 调度错峰（`strategy_id % 30s`）
- 环境变量 `TICK_DB_CONCURRENCY`（默认 48）控制单进程同时 tick 写库并发，适配单账户 50–100 策略
- 启动时 `Base.metadata.create_all()` + `init_db()` 内联 ALTER TABLE 迁移
- 模型：Strategy（含网格参数）、Position（含 grid_level/grid_trigger_price/tp_limit_order_id）、Trade（含 grid_level/close_reason）、Account（含 exchange/okx_passphrase）、BotConfig
- 所有时间存储为无时区的北京时间（`now_beijing()`）

### 前端
- React + TypeScript + Vite + TailwindCSS + Zustand
- 生产环境：FastAPI 在 8000 端口直接托管 `frontend/dist/`，`index.html` 禁止缓存
- 页面：仪表盘、策略管理、当前持仓、交易历史、系统设置
- 策略表单：首单开仓设置 + 马丁网格加仓设置 + 出场设置

## 关键约定

- **双向持仓模式**：每笔订单含 `positionSide`（"LONG"/"SHORT"），平仓加 `reduceOnly`。单向模式账户不发送这些参数。`_order_params()` 通过 `self.hedge_mode` 控制。
- **限价止盈**：开仓/加仓后立即挂限价止盈单，加仓成交后取消旧止盈单并挂新止盈单（基于加权均价）。
- **加仓限价单**：同一时间仅一单下一层限价加仓；每层成交后在挂好新止盈/止损后再挂单笔下一层。使用首单入场价计算所有层级触发价，避免网格漂移。
- **交易所止损**：触发价由亏损阈值 U 与整仓推算；条件单数量为持仓×平仓比例%；触发后按比例在各 DB 持仓腿上减仓记账，剩余持仓重挂止盈/止损与加仓链；止损永不自动重开首单。
- **成交量**：开仓和加仓用 `order.get("filled")` 实际成交量。
- **符号标准化**：比较时统一去 `/`、`:USDT`、`-SWAP`、大写。函数：`_norm_sym()`（exchange_base）、`_norm_leg_symbol()`（sync_service）、`_panic_symbol_key()`（strategies）。
- **策略隔离**：每个策略独立 executor/engine，异常不扩散到其他策略。
- **新增数据库列**：同步添加 model + schema + 前端 types + `init_db()` 迁移 + NULL 兜底。
- **Python 命令**：Windows 环境使用 `python`（非 `python3.11`）。
