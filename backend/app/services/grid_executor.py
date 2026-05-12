"""Grid strategy executor — state machine that drives the martingale grid lifecycle."""
import logging
from typing import Optional

from ..config import now_beijing
from ..models.position import Position
from ..models.trade import Trade
from .exchange_base import BaseExchangeService
from .grid_engine import GridStrategyEngine, GridLevel
from .order_tracker import order_tracker, OrderState
from .log_service import strategy_log_service

logger = logging.getLogger(__name__)


class GridExecutor:
    """Per-symbol grid strategy lifecycle executor.

    Flow:
    1. No position → market open initial + place TP limit + place grid add limits + place SL order
    2. Has positions → check SL order fills / TP fills / grid add fills
    3. TP fill → close all + reopen initial
    4. Grid add fill → update avg entry + replace TP + update SL
    5. SL fill → close all + reopen initial
    """

    @staticmethod
    def _order_symbol_matches(tracker_symbol: str, strategy_symbol: str) -> bool:
        """order_tracker 里存的 symbol 与 strategy.symbol 可能格式不同（如 OKX ccxt 写法）。"""
        return BaseExchangeService._norm_sym(tracker_symbol) == BaseExchangeService._norm_sym(
            strategy_symbol
        )

    MAX_CONSECUTIVE_FAILURES = 3
    MAX_ORDER_QTY = 1000000.0

    def __init__(self, engine: GridStrategyEngine):
        self.engine = engine
        self._consecutive_failures = 0

    @staticmethod
    def _check_order_qty(qty: float, symbol: str) -> bool:
        if qty <= 0:
            return False
        if qty > GridExecutor.MAX_ORDER_QTY:
            logger.warning("Order qty %.2f exceeds max limit for %s", qty, symbol)
            return False
        return True

    @staticmethod
    def _round_qty(qty: float) -> float:
        """Round quantity to reasonable precision for exchange order."""
        if qty <= 0:
            return 0.0
        if qty >= 1:
            return round(qty, 4)
        if qty >= 0.01:
            return round(qty, 6)
        return round(qty, 8)

    async def _purge_exchange_open_orders(
        self, exchange, symbol: str, strategy_id: int, direction: str,
    ) -> int:
        """止盈/止损等平仓记账后：扫尾撤销该策略持仓腿在该交易对上的挂单（双向下同币种对向策略互不撤单）。"""
        n = 0
        hedge = getattr(exchange, "hedge_mode", True)
        dir_low = (direction or "").strip().lower()
        ex_id = getattr(exchange, "exchange_id", "") or ""
        pos_filter = dir_low if hedge else None
        if ex_id == "okx" and hasattr(exchange, "cancel_all_pending_orders_for_symbol"):
            try:
                n += await exchange.cancel_all_pending_orders_for_symbol(symbol, pos_filter)
            except Exception as e:
                logger.warning("purge OKX bulk %s strategy=%d: %s", symbol, strategy_id, e)

        try:
            oo = await exchange.fetch_open_orders(symbol)
            for row in oo or []:
                if not BaseExchangeService.open_order_matches_strategy_scope(row, symbol, dir_low, hedge):
                    continue
                algo_raw = row.get("algoId")
                plain = str(row.get("id") or row.get("orderId") or "").strip()
                if algo_raw:
                    aid = str(algo_raw).strip()
                    try:
                        await exchange.cancel_algo_order(aid, symbol)
                        n += 1
                        continue
                    except Exception as e:
                        logger.debug("purge cancel_algo_order %s: %s", aid, e)
                    if aid and aid != plain:
                        try:
                            await exchange.cancel_order(aid, symbol)
                            n += 1
                            continue
                        except Exception as e2:
                            logger.debug("purge cancel_order(algoId as id) %s: %s", aid, e2)
                if plain:
                    try:
                        await exchange.cancel_order(plain, symbol)
                        n += 1
                    except Exception as e:
                        logger.debug("purge cancel_order %s: %s", plain, e)
        except Exception as e:
            logger.warning("purge fetch_open_orders %s strategy=%d: %s", symbol, strategy_id, e)

        if hasattr(exchange, "cancel_all_open_algo_orders"):
            try:
                n += await exchange.cancel_all_open_algo_orders(symbol, pos_filter)
            except Exception as e:
                logger.debug("purge cancel_all_open_algo_orders %s: %s", symbol, e)
        return n

    @staticmethod
    def _avg_price_from_order_dict(raw: dict | None) -> float:
        """见 ``BaseExchangeService.avg_fill_price_from_order``。"""
        return BaseExchangeService.avg_fill_price_from_order(raw)

    async def _filled_exit_price(self, exchange, order_id: str, sym: str) -> float:
        """从交易所拉取已成交订单的均价/成交价，用于交易记录 exit_price。"""
        try:
            raw = await exchange.fetch_order(order_id, sym)
            return GridExecutor._avg_price_from_order_dict(raw)
        except Exception as e:
            logger.debug("filled_exit_price fetch_order %s: %s", order_id, e)
            return 0.0

    async def _resolve_entry_fill(
        self,
        exchange,
        symbol: str,
        order: dict,
        current_price: float,
        *,
        qty_fallback: float,
    ) -> tuple[float, float]:
        """市价开仓：优先用 fetch_order 与交易所一致的成交均价与成交量；失败则退回本地解析。"""
        oid = str(order.get("id") or order.get("orderId") or "").strip()
        filled_qty = float(order.get("filled", 0) or qty_fallback or 0) or qty_fallback
        entry_price = GridExecutor._avg_price_from_order_dict(order)
        if oid:
            try:
                fresh = await exchange.fetch_order(oid, symbol)
                px = GridExecutor._avg_price_from_order_dict(fresh)
                fq = float(fresh.get("filled", 0) or 0)
                if px > 0:
                    entry_price = px
                if fq > 1e-12:
                    filled_qty = fq
            except Exception as e:
                logger.debug("resolve_entry_fill fetch_order %s: %s", oid, e)
        if entry_price <= 0:
            entry_price = float(current_price or 0)
        return entry_price, filled_qty

    @staticmethod
    def _position_row_ct_val_hint(rp: dict, contracts_abs: float) -> float:
        """从 ccxt 持仓行取每张合约基础数量（OKX 线性永续 PnL ∝ 张数×ctVal×价差）。"""
        cs = float(rp.get("contractSize") or 0)
        if cs > 0:
            return cs
        info = rp.get("info")
        if isinstance(info, dict):
            try:
                v = float(info.get("ctVal") or 0)
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
        c = float(contracts_abs)
        mark = float(rp.get("markPrice") or rp.get("mark_price") or 0)
        notional = abs(float(rp.get("notional") or 0))
        if c > 1e-12 and mark > 1e-12 and notional > 1e-12:
            return notional / (c * mark)
        return 0.0

    async def _avg_qty_for_stop_loss_basis(
        self,
        exchange,
        symbol: str,
        direction: str,
        db_positions: list,
    ) -> tuple[float, float, str, float]:
        """止损用加权均价与张数：优先使用交易所实时持仓（与浮亏口径一致），否则退回 DB。

        第四项为从持仓行解析的 ctVal 提示（>0 时优先于单独拉 instruments），避免 market 失败时退回 1.0 导致止损价算到 0 以下。
        """
        side = direction.lower()
        qty_db = sum(float(p.quantity) for p in db_positions if float(p.quantity) > 0)
        avg_db = self.engine.calculate_avg_entry(db_positions) if qty_db > 0 else 0.0

        want = BaseExchangeService._norm_sym(symbol)
        raw: list = []
        try:
            raw = await exchange.fetch_positions([symbol])
        except Exception as e:
            logger.warning("fetch_positions for SL basis (%s): %s", symbol, e)

        ex_qty = 0.0
        ex_cost = 0.0
        ex_ct_hint = 0.0
        sum_notional = 0.0
        sum_mc = 0.0
        for rp in raw or []:
            rsym = BaseExchangeService._norm_sym(str(rp.get("symbol") or ""))
            if rsym != want:
                continue
            p_side = BaseExchangeService.position_row_side_lower(rp)
            if p_side != side:
                continue
            c = BaseExchangeService.position_row_contracts_abs(rp)
            if c <= 0:
                continue
            ep = float(rp.get("entryPrice") or rp.get("entry_price") or 0)
            if ep <= 0:
                continue
            ex_qty += c
            ex_cost += ep * c
            sum_notional += abs(float(rp.get("notional") or 0))
            mpx = float(rp.get("markPrice") or rp.get("mark_price") or 0)
            if mpx > 0:
                sum_mc += mpx * c
            if ex_ct_hint <= 0:
                h = GridExecutor._position_row_ct_val_hint(rp, c)
                if h > 0:
                    ex_ct_hint = h

        if ex_qty > 1e-12:
            avg_ex = ex_cost / ex_qty
            if ex_ct_hint <= 0 and sum_notional > 1e-12:
                wmark = sum_mc / ex_qty if ex_qty > 1e-12 else 0.0
                if wmark > 1e-12:
                    ex_ct_hint = sum_notional / (ex_qty * wmark)
            if qty_db > 1e-12 and (
                abs(ex_qty - qty_db) / qty_db > 0.015
                or abs(avg_ex - avg_db) / max(avg_db, 1e-12) > 0.015
            ):
                logger.info(
                    "SL basis prefers exchange avg=%.6f qty=%.8f over DB avg=%.6f qty=%.8f",
                    avg_ex, ex_qty, avg_db, qty_db,
                )
            return avg_ex, ex_qty, "exchange", ex_ct_hint

        return avg_db, qty_db, "db", 0.0

    async def _compute_and_place_stop_loss(
        self,
        *,
        strategy,
        symbol: str,
        exchange,
        db_positions: list,
        position_side: str,
        log_label: str,
    ) -> None:
        """按当前持仓加权均价与张数推算触发价（张数会先按交易所步长取整），使名义浮亏≈阈值U."""
        loss_u = self.engine.loss_threshold
        if loss_u <= 0:
            return

        avg_raw, qty_raw, basis, ct_from_pos = await self._avg_qty_for_stop_loss_basis(
            exchange, symbol, strategy.direction, db_positions
        )
        qty_sl = await exchange.normalize_order_amount(symbol, qty_raw)
        ct_val = await exchange.linear_contract_ct_val(symbol)
        if ct_from_pos > 0:
            ct_val = ct_from_pos
        sl_price = self.engine.stop_loss_price_for_fixed_usdt_loss(
            avg_raw, qty_sl, loss_u, strategy.direction, ct_val=ct_val,
        )
        sl_side = "sell" if strategy.direction == "long" else "buy"

        if sl_price <= 0 or qty_sl <= 0 or not self._check_order_qty(qty_sl, symbol):
            strategy_log_service.warning(
                strategy.id,
                f"{log_label}跳过: 无效价或量 basis={basis} avg={avg_raw:.6f} raw_qty={qty_raw:.8f} "
                f"step_qty={qty_sl:.8f} ct_val={ct_val:.8f} loss_u={loss_u:.6f} sl_price={sl_price:.8f}",
            )
            return

        try:
            sl_order = await exchange.create_stop_loss_order(
                symbol, sl_side, qty_sl, sl_price,
                reduce_only=True, position_side=position_side,
            )
            sl_order_id = str(sl_order.get("algoId") or sl_order.get("id", ""))
            order_tracker.add(
                sl_order_id, symbol, sl_side, "stop",
                qty_sl, sl_price, strategy.id, "stop_loss",
            )
            strategy_log_service.success(
                strategy.id,
                f"{log_label}操作成功: basis={basis} avg={avg_raw:.6f} step_qty={qty_sl:.8f} "
                f"止损价={sl_price:.6f} (阈值约{loss_u:.2f}U) 订单ID={sl_order_id or '-'}",
            )
            self._schedule_feishu(
                strategy,
                title=f"{log_label}（市价条件单）",
                body_lines=[
                    f"basis={basis} 加权均价≈{avg_raw:.6f}",
                    f"下单数量(step)={qty_sl:.8f}",
                    f"触发价≈{sl_price:.6f}",
                    f"阈值约 {loss_u:.2f} U",
                    f"algoId={sl_order_id}" if sl_order_id else "",
                ],
            )
            logger.info(
                "%s SL placed sym=%s basis=%s avg=%s qty_sl=%s sl_price=%s loss_u=%s",
                log_label,
                symbol,
                basis,
                avg_raw,
                qty_sl,
                sl_price,
                loss_u,
            )
        except Exception as e:
            logger.error("%s SL failed strategy=%s: %s", log_label, strategy.id, e)
            strategy_log_service.warning(
                strategy.id,
                f"{log_label}失败: {e}（请检查持仓与保证金；下一周期或加仓更新时会再尝试挂止损）",
            )

    def _schedule_feishu(self, strategy, title: str, body_lines: list[str]) -> None:
        """非阻塞推送飞书（未配置 webhook 时自动跳过）。"""
        from .feishu_notify import schedule_trade_notify

        lines = [ln for ln in body_lines if ln]
        schedule_trade_notify(
            strategy_id=strategy.id,
            account_id=strategy.account_id,
            symbol=strategy.symbol or "",
            direction=strategy.direction or "",
            title=title,
            body_lines=lines,
        )

    async def process_symbol(
        self, session, strategy, symbol: str, exchange, current_price: float,
    ) -> None:
        """Main per-symbol processing entry point."""
        from sqlalchemy import select
        stmt = select(Position).where(
            Position.strategy_id == strategy.id,
            Position.symbol == symbol,
            Position.closed_at.is_(None),
        ).order_by(Position.grid_level)
        result = await session.execute(stmt)
        open_positions = list(result.scalars().all())

        # OKX 曾仅依赖 REST 轮询时 order 状态更新滞后；每 tick 拉一次挂单状态与 WS 互补（请求量仍低）
        if getattr(exchange, "exchange_id", "") == "okx":
            await order_tracker.check_all_pending(exchange, strategy.id)

        if not open_positions:
            await self._open_initial(session, strategy, symbol, exchange, current_price)
            return

        for pos in open_positions:
            pos.mark_price = current_price
            if pos.side == "long":
                pos.unrealized_pnl = (current_price - pos.entry_price) * pos.quantity
            else:
                pos.unrealized_pnl = (pos.entry_price - current_price) * pos.quantity

        sl_filled = await self._check_stop_loss_fills(session, strategy, symbol, exchange, open_positions, current_price)
        if sl_filled:
            return

        tp_filled = await self._check_tp_fills(session, strategy, symbol, exchange, open_positions, current_price)
        if tp_filled:
            return

        add_filled = await self._check_grid_add_fills(session, strategy, symbol, exchange, open_positions, current_price)
        if add_filled:
            return

        await session.commit()

    async def _open_initial(self, session, strategy, symbol, exchange, current_price):
        """Open initial position at market + place TP limit + first grid add limit."""
        from sqlalchemy import select

        if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            logger.error("Strategy %d: consecutive failures (%d) exceeded limit, stopping", strategy.id, self._consecutive_failures)
            strategy_log_service.error(strategy.id, f"连续开仓失败{self._consecutive_failures}次,策略自动停止")
            strategy.status = "stopped"
            await session.commit()
            return

        side_raw = "buy" if strategy.direction == "long" else "sell"
        position_side = "LONG" if strategy.direction == "long" else "SHORT"

        qty = 0.0
        if strategy.base_qty_type == "margin_pct":
            try:
                balance = await exchange.fetch_balance()
                total_usdt = float(balance.get("total", {}).get("USDT", 0) or 0)
                usdt_amount = total_usdt * (strategy.base_qty_value / 100.0)
                if current_price > 0:
                    qty = await exchange.quote_usdt_to_order_amount(symbol, usdt_amount, float(current_price))
            except Exception as e:
                logger.warning("Failed to calculate margin-based qty: %s", e)
                strategy_log_service.error(strategy.id, f"开仓失败: 获取余额异常 - {e}")
                self._consecutive_failures += 1
                return
        else:
            if current_price > 0:
                qty = await exchange.quote_usdt_to_order_amount(
                    symbol, float(strategy.base_qty_value), float(current_price),
                )
            else:
                logger.error("Cannot calculate qty: current_price is 0")
                strategy_log_service.error(strategy.id, "开仓失败: 无法获取当前价格")
                self._consecutive_failures += 1
                return

        if qty <= 0:
            logger.error("Calculated qty is 0 for strategy %d", strategy.id)
            strategy_log_service.error(strategy.id, "开仓失败: 计算数量为0")
            self._consecutive_failures += 1
            return

        qty = self._round_qty(qty)
        qty = await exchange.normalize_order_amount(symbol, qty)

        try:
            order = await exchange.create_market_order(
                symbol, side_raw, qty, reduce_only=False, position_side=position_side,
            )
        except Exception as e:
            logger.error("Failed to open initial position for %s %s: %s", strategy.id, symbol, e)
            strategy_log_service.error(strategy.id, f"开仓失败: {symbol} {strategy.direction} - {e}")
            self._consecutive_failures += 1
            return

        self._consecutive_failures = 0

        entry_price, filled_qty = await self._resolve_entry_fill(
            exchange, symbol, order, current_price, qty_fallback=qty,
        )

        strategy_log_service.success(
            strategy.id,
            f"开仓成功: {symbol} {strategy.direction} 数量={filled_qty:.4f} 价格={entry_price:.4f}",
        )
        self._schedule_feishu(
            strategy,
            title="市价开仓成交",
            body_lines=[
                f"方向: {'开多(long)' if strategy.direction == 'long' else '开空(short)'}",
                f"成交数量≈{filled_qty:.8f}",
                f"成交价≈{entry_price:.8f}",
                f"订单ID: {order.get('id', '')}",
            ],
        )

        # Record position in DB
        pos = Position(
            strategy_id=strategy.id,
            account_id=strategy.account_id,
            symbol=symbol,
            side=strategy.direction,
            quantity=filled_qty,
            entry_price=entry_price,
            mark_price=current_price,
            layer=0,
            grid_level=0,
            exchange_order_id=str(order.get("id", "")),
        )
        session.add(pos)
        await session.commit()
        await session.refresh(pos)

        # Track the entry order
        order_tracker.add(
            str(order.get("id", "")), symbol, side_raw, "market",
            filled_qty, entry_price, strategy.id, "initial_entry",
        )

        # 2. Place TP limit order
        tp_price = self.engine.calculate_tp_price(entry_price, strategy.direction)
        tp_side = "sell" if strategy.direction == "long" else "buy"
        if self._check_order_qty(filled_qty, symbol):
            try:
                tp_order = await exchange.create_limit_order(
                    symbol, tp_side, filled_qty, tp_price,
                    reduce_only=True, position_side=position_side,
                )
                pos.tp_limit_order_id = str(tp_order.get("id", ""))
                pos.take_profit_price = tp_price
                order_tracker.add(
                    pos.tp_limit_order_id, symbol, tp_side, "limit",
                    filled_qty, tp_price, strategy.id, "tp",
                )
                strategy_log_service.success(
                    strategy.id,
                    f"挂单止盈操作成功: 数量={filled_qty:.4f} 止盈价={tp_price:.4f} ({strategy.tp_pct}%) "
                    f"订单ID={tp_order.get('id', '')}",
                )
                self._schedule_feishu(
                    strategy,
                    title="挂单止盈（限价 reduce-only）",
                    body_lines=[
                        f"数量≈{filled_qty:.8f}",
                        f"止盈价={tp_price:.8f}（{strategy.tp_pct}%）",
                        f"订单ID: {tp_order.get('id', '')}",
                    ],
                )
            except Exception as e:
                logger.error("Failed to place TP order for %s %s: %s", strategy.id, symbol, e)
                strategy_log_service.error(strategy.id, f"挂单止盈失败: {e}")
        else:
            strategy_log_service.warning(strategy.id, f"止盈数量超限({filled_qty:.2f}),跳过挂单,请手动止盈")

        await session.commit()

        if self.engine.loss_threshold > 0:
            await self._compute_and_place_stop_loss(
                strategy=strategy,
                symbol=symbol,
                exchange=exchange,
                db_positions=[pos],
                position_side=position_side,
                log_label="挂单止损",
            )

        grid_levels = self.engine.calculate_grid_levels(entry_price, strategy.direction)
        placed = 0
        total = len(grid_levels)
        for gl in grid_levels:
            result = await self._place_grid_add(session, strategy, symbol, exchange, pos, gl)
            if result:
                placed += 1
        if placed == total:
            strategy_log_service.success(
                strategy.id,
                f"挂单加仓: {placed}/{total} 层已挂单",
            )
            self._schedule_feishu(
                strategy,
                title="挂单加仓（限价挂单汇总）",
                body_lines=[
                    f"已成功挂出 {placed}/{total} 层加仓限价单",
                    f"基准入场价≈{entry_price:.8f}",
                ],
            )
        else:
            strategy_log_service.warning(
                strategy.id,
                f"挂单加仓: {placed}/{total} 层已挂单 (失败{total-placed}层可能因交易所订单/持仓限制, 建议降低max_layers)",
            )
            self._schedule_feishu(
                strategy,
                title="挂单加仓（部分失败）",
                body_lines=[
                    f"成功 {placed}/{total} 层",
                    f"失败 {total - placed} 层，请留意交易所限额或持仓上限",
                    f"基准入场价≈{entry_price:.8f}",
                ],
            )

        logger.info(
            "Grid initial open: %s %s qty=%.4f entry=%.4f tp=%.4f grid_placed=%d/%d",
            strategy.direction, symbol, filled_qty, entry_price, tp_price, placed, total,
        )

    async def _place_grid_add(self, session, strategy, symbol, exchange, position, grid_level: GridLevel):
        """Place a limit add order for a grid level."""
        add_side = "buy" if strategy.direction == "long" else "sell"
        position_side = "LONG" if strategy.direction == "long" else "SHORT"

        raw_size = grid_level.quantity
        trigger_price = grid_level.trigger_price
        if trigger_price <= 0:
            logger.error("Grid add trigger_price is 0 for strategy %d level %d", strategy.id, grid_level.level)
            strategy_log_service.error(strategy.id, f"挂单加仓 Lv{grid_level.level} 失败: trigger_price=0")
            return None

        qty = 0.0
        if strategy.base_qty_type == "margin_pct":
            try:
                balance = await exchange.fetch_balance()
                total_usdt = float(balance.get("total", {}).get("USDT", 0) or 0)
                usdt_amount = total_usdt * (raw_size / 100.0)
                qty = await exchange.quote_usdt_to_order_amount(symbol, usdt_amount, float(trigger_price))
            except Exception as e:
                logger.warning("Failed to calculate margin-based grid add qty: %s", e)
                strategy_log_service.error(strategy.id, f"挂单加仓 Lv{grid_level.level} 失败: 余额查询异常")
                return None
        else:
            qty = await exchange.quote_usdt_to_order_amount(symbol, float(raw_size), float(trigger_price))

        if qty <= 0:
            logger.error("Grid add qty is 0 for strategy %d level %d", strategy.id, grid_level.level)
            strategy_log_service.error(strategy.id, f"挂单加仓 Lv{grid_level.level} 失败: 计算数量为0")
            return None

        qty = self._round_qty(qty)
        qty = await exchange.normalize_order_amount(symbol, qty)

        try:
            order = await exchange.create_limit_order(
                symbol, add_side, qty, trigger_price,
                reduce_only=False, position_side=position_side,
            )
            order_id = str(order.get("id", ""))
            order_tracker.add(
                order_id, symbol, add_side, "limit",
                qty, trigger_price, strategy.id, "grid_add",
            )
            strategy_log_service.success(
                strategy.id,
                f"挂单加仓 Lv{grid_level.level}: 数量={qty:.4f} 触发价={trigger_price:.4f} (累计跌幅{grid_level.drop_pct}%)",
            )
            logger.info(
                "Grid add placed: %s lv=%d qty=%.4f trigger=%.4f (raw_size=%.4f)",
                symbol, grid_level.level, qty, trigger_price, raw_size,
            )
            return order_id
        except Exception as e:
            err_str = str(e)
            logger.error("Failed to place grid add order for %s lv=%d: %s", symbol, grid_level.level, e)
            if "-2027" in err_str:
                strategy_log_service.warning(
                    strategy.id,
                    f"挂单加仓 Lv{grid_level.level} 被交易所拒绝(持仓/订单超限,可尝试降低加仓层数)",
                )
            else:
                strategy_log_service.error(strategy.id, f"挂单加仓 Lv{grid_level.level} 失败: {e}")
            return None

    async def _check_tp_fills(self, session, strategy, symbol, exchange, positions, current_price) -> bool:
        """Check if TP limit orders have filled. If so, close all and reopen."""
        tp_orders = order_tracker.get_pending_by_purpose(strategy.id, "tp")
        filled_orders = order_tracker.get_filled(strategy.id, "tp")
        by_id: dict = {o.order_id: o for o in tp_orders + filled_orders}

        # 本地 DB 中的止盈单 ID（服务重启后 order_tracker 为空时仍能轮询所侧）
        tp_side_hint = "sell" if strategy.direction == "long" else "buy"
        for p in positions:
            oid = (p.tp_limit_order_id or "").strip()
            if not oid or oid in by_id:
                continue
            if not GridExecutor._order_symbol_matches(p.symbol or symbol, symbol):
                continue
            if not order_tracker.get(oid):
                order_tracker.add(
                    oid,
                    symbol,
                    tp_side_hint,
                    "limit",
                    float(p.quantity or 0),
                    float(p.take_profit_price or 0) or 0.0,
                    strategy.id,
                    "tp",
                )
            co = order_tracker.get(oid)
            if co:
                by_id[oid] = co

        ref_tp = None
        for o in by_id.values():
            if not GridExecutor._order_symbol_matches(o.symbol, symbol):
                continue
            if o.status == OrderState.FILLED:
                ref_tp = o
                break
            updated = await order_tracker.check_order(exchange, o.order_id, symbol)
            if updated and updated.status == OrderState.FILLED:
                ref_tp = updated
                break

        if ref_tp is None:
            return False

        tp_exit = float(getattr(ref_tp, "price", 0) or 0)
        if tp_exit <= 0:
            tp_exit = await self._filled_exit_price(exchange, ref_tp.order_id, ref_tp.symbol)
        if tp_exit <= 0:
            tp_exit = current_price

        strategy_log_service.success(
            strategy.id,
            f"止盈触发: {symbol} 止盈成交价≈{tp_exit:.6f}（记账用），开始平仓并记录交易",
        )
        self._schedule_feishu(
            strategy,
            title="止盈触发（止盈单已成交）",
            body_lines=[
                f"止盈成交价≈{tp_exit:.8f}",
                "后续：记录平仓并按配置重开或未重开",
            ],
        )

        # TP filled → close all positions (skip market-close, TP already closed on exchange)
        await self._close_all(
            session, strategy, symbol, exchange, positions, current_price, "take_profit",
            exit_price_override=tp_exit,
            positions_already_closed=True,
        )

        # Refresh strategy (status may be stale after commit)
        await session.refresh(strategy)

        # Reopen initial if configured and strategy still running; otherwise stop
        if strategy.reopen_after_close and strategy.status == "running":
            strategy_log_service.success(strategy.id, f"止盈后续操作成功: {symbol} 已按配置自动重开首单")
            self._schedule_feishu(
                strategy,
                title="止盈完成 · 自动重开",
                body_lines=["已按市价重新开首单并挂止盈/加仓/止损（若启用）"],
            )
            try:
                again = await self._purge_exchange_open_orders(exchange, symbol, strategy.id, strategy.direction)
                if again > 0:
                    strategy_log_service.info(
                        strategy.id,
                        f"重开首单前再次撤销 {symbol} 残留挂单约 {again} 笔，避免与上一轮订单重叠",
                    )
            except Exception as e:
                logger.warning("purge before reopen after TP strategy=%d: %s", strategy.id, e)
            await self._open_initial(session, strategy, symbol, exchange, current_price)
        else:
            strategy_log_service.success(strategy.id, f"止盈后续操作成功: {symbol} 策略已停止 (reopen_after_close=否)")
            strategy.status = "stopped"
            await session.commit()
            self._schedule_feishu(
                strategy,
                title="止盈完成 · 策略已停止",
                body_lines=["reopen_after_close=否，策略标记为 stopped"],
            )

        return True

    async def _check_grid_add_fills(self, session, strategy, symbol, exchange, positions, current_price) -> bool:
        """Check if grid add limit orders have filled. Update positions and re-place orders."""
        add_orders = order_tracker.get_pending_by_purpose(strategy.id, "grid_add")
        filled_orders = order_tracker.get_filled(strategy.id, "grid_add")
        all_orders = {o.order_id: o for o in add_orders + filled_orders}
        any_filled = False
        for o in all_orders.values():
            if not GridExecutor._order_symbol_matches(o.symbol, symbol):
                continue
            if o.status != OrderState.FILLED:
                updated = await order_tracker.check_order(exchange, o.order_id, o.symbol)
                if not updated or updated.status != OrderState.FILLED:
                    continue
            side = strategy.direction
            position_side = "LONG" if side == "long" else "SHORT"

            filled_qty = float(o.filled) if o.filled > 0 else float(o.amount)
            fill_price = float(o.price) if o.price > 0 else 0.0

            if filled_qty <= 0 or fill_price <= 0:
                logger.warning("Grid add filled but invalid qty/price: %s qty=%.4f price=%.4f", o.order_id, filled_qty, fill_price)
                continue

            max_level = max((p.grid_level for p in positions), default=0)
            next_level = max_level + 1

            existing = [p for p in positions if p.exchange_order_id == o.order_id]
            if existing:
                continue

            pos = Position(
                strategy_id=strategy.id,
                account_id=strategy.account_id,
                symbol=symbol,
                side=side,
                quantity=filled_qty,
                entry_price=fill_price,
                mark_price=current_price,
                layer=next_level,
                grid_level=next_level,
                exchange_order_id=o.order_id,
                grid_trigger_price=fill_price,
            )
            session.add(pos)
            await session.commit()
            await session.refresh(pos)
            positions.append(pos)
            any_filled = True

            strategy_log_service.success(
                strategy.id,
                f"加仓成交记录成功: Lv{next_level} 数量={filled_qty:.4f} 价格={fill_price:.4f} "
                f"订单ID={o.order_id}（已写入本地持仓）",
            )
            self._schedule_feishu(
                strategy,
                title=f"加仓成交 Lv{next_level}",
                body_lines=[
                    f"成交数量≈{filled_qty:.8f}",
                    f"成交价≈{fill_price:.8f}",
                    f"备注: 当前价≈{current_price:.8f}",
                ],
            )
            logger.info("Grid add filled: %s lv=%d qty=%.4f price=%.4f", symbol, next_level, filled_qty, fill_price)

            # Cancel old TP order
            await self._cancel_tp_orders(session, strategy, symbol, exchange, positions)

            # Place new TP order for combined position
            avg_entry = self.engine.calculate_avg_entry(positions)
            tp_price = self.engine.calculate_tp_price(avg_entry, side)
            tp_side = "sell" if side == "long" else "buy"
            total_qty = sum(float(p.quantity) for p in positions)
            if self._check_order_qty(total_qty, symbol):
                try:
                    tp_order = await exchange.create_limit_order(
                        symbol, tp_side, total_qty, tp_price,
                        reduce_only=True, position_side=position_side,
                    )
                    tp_order_id = str(tp_order.get("id", ""))
                    for p in positions:
                        p.tp_limit_order_id = tp_order_id
                        p.take_profit_price = tp_price
                    order_tracker.add(
                        tp_order_id, symbol, tp_side, "limit",
                        total_qty, tp_price, strategy.id, "tp",
                    )
                    strategy_log_service.success(
                        strategy.id,
                        f"重新挂单止盈操作成功: 合并数量={total_qty:.4f} 均价={avg_entry:.4f} 止盈价={tp_price:.4f} "
                        f"订单ID={tp_order_id}",
                    )
                    self._schedule_feishu(
                        strategy,
                        title="重新挂单止盈（加仓后合并仓位）",
                        body_lines=[
                            f"合并数量≈{total_qty:.8f}",
                            f"加权均价≈{avg_entry:.8f}",
                            f"止盈价={tp_price:.8f}",
                            f"订单ID: {tp_order_id}",
                        ],
                    )
                except Exception as e:
                    logger.error("Failed to place new TP after grid add: %s", e)
                    strategy_log_service.error(strategy.id, f"重新挂单止盈失败: {e}")
            else:
                strategy_log_service.warning(strategy.id, f"止盈数量超限({total_qty:.2f}),跳过挂单,请手动止盈")

            if self.engine.loss_threshold > 0:
                await self._cancel_sl_orders(session, strategy, symbol, exchange)
                await self._compute_and_place_stop_loss(
                    strategy=strategy,
                    symbol=symbol,
                    exchange=exchange,
                    db_positions=positions,
                    position_side=position_side,
                    log_label="更新止损",
                )

            await session.commit()
            # 所有加仓单已在首单开仓时一次性挂出，成交后无需再挂下一层

        return any_filled

    async def _cancel_tp_orders(self, session, strategy, symbol, exchange, positions):
        """Cancel all existing TP limit orders for this strategy+symbol."""
        tp_orders = order_tracker.get_pending_by_purpose(strategy.id, "tp")
        for o in tp_orders:
            if GridExecutor._order_symbol_matches(o.symbol, symbol):
                try:
                    await exchange.cancel_order(o.order_id, o.symbol)
                except Exception as e:
                    logger.debug("Cancel TP order %s: %s", o.order_id, e)

    async def _cancel_sl_orders(self, session, strategy, symbol, exchange):
        """Cancel all existing stop loss orders for this strategy+symbol."""
        sl_orders = order_tracker.get_pending_by_purpose(strategy.id, "stop_loss")
        for o in sl_orders:
            if GridExecutor._order_symbol_matches(o.symbol, symbol):
                try:
                    await exchange.cancel_algo_order(o.order_id, symbol)
                    logger.info("Cancelled SL order %s for strategy %d", o.order_id, strategy.id)
                except Exception as e:
                    logger.warning(
                        "Cancel SL order %s failed strategy=%d: %s",
                        o.order_id, strategy.id, e,
                    )

    async def _check_stop_loss_fills(self, session, strategy, symbol, exchange, positions, current_price) -> bool:
        """Check if stop loss orders have filled. If so, close all and reopen."""
        if self.engine.loss_threshold <= 0:
            return False

        sl_orders = order_tracker.get_pending_by_purpose(strategy.id, "stop_loss")
        filled_orders = order_tracker.get_filled(strategy.id, "stop_loss")
        all_orders = {o.order_id: o for o in sl_orders + filled_orders}
        ref_sl = None
        for o in all_orders.values():
            if not GridExecutor._order_symbol_matches(o.symbol, symbol):
                continue
            if o.status == OrderState.FILLED:
                ref_sl = o
                break
            updated = await order_tracker.check_order(exchange, o.order_id, o.symbol)
            if updated and updated.status == OrderState.FILLED:
                ref_sl = updated
                break

        if ref_sl is None:
            return False

        sl_exit = float(getattr(ref_sl, "price", 0) or 0)
        if sl_exit <= 0:
            sl_exit = await self._filled_exit_price(exchange, ref_sl.order_id, ref_sl.symbol)
        if sl_exit <= 0:
            sl_exit = current_price

        strategy_log_service.success(
            strategy.id,
            f"止损触发: {symbol} 止损成交价≈{sl_exit:.6f}（记账用），开始平仓并记录交易",
        )
        self._schedule_feishu(
            strategy,
            title="止损触发（Algo 条件市价单已成交）",
            body_lines=[
                f"止损成交价≈{sl_exit:.8f}",
                "后续：平仓记录并按配置自动重开或未重开",
            ],
        )

        await self._close_all(
            session, strategy, symbol, exchange, positions, current_price, "stop_loss",
            exit_price_override=sl_exit,
            positions_already_closed=True,
        )

        await session.refresh(strategy)

        if strategy.reopen_after_close and strategy.status == "running":
            strategy_log_service.success(strategy.id, f"止损后续操作成功: {symbol} 已按配置自动重开首单")
            self._schedule_feishu(
                strategy,
                title="止损完成 · 自动重开",
                body_lines=["市价止损后已重新开首单并挂新单（若启用止损单等）"],
            )
            try:
                again = await self._purge_exchange_open_orders(exchange, symbol, strategy.id, strategy.direction)
                if again > 0:
                    strategy_log_service.info(
                        strategy.id,
                        f"重开首单前再次撤销 {symbol} 残留挂单约 {again} 笔，避免与上一轮订单重叠",
                    )
            except Exception as e:
                logger.warning("purge before reopen after SL strategy=%d: %s", strategy.id, e)
            await self._open_initial(session, strategy, symbol, exchange, current_price)
        else:
            strategy_log_service.success(strategy.id, f"止损后续操作成功: {symbol} 策略已停止 (reopen_after_close=否)")
            strategy.status = "stopped"
            await session.commit()
            self._schedule_feishu(
                strategy,
                title="止损完成 · 策略已停止",
                body_lines=["reopen_after_close=否"],
            )

        return True

    async def _close_all(
        self,
        session,
        strategy,
        symbol,
        exchange,
        positions,
        current_price,
        reason: str,
        *,
        exit_price_override: float | None = None,
        positions_already_closed: bool = False,
    ):
        """Close all positions for this strategy+symbol via market order. Record trades."""
        # 先按交易所全量扫单（避免 tracker 与所侧不一致时残留止盈/止损/加仓限价）
        try:
            pre_purge = await self._purge_exchange_open_orders(exchange, symbol, strategy.id, strategy.direction)
            if pre_purge > 0:
                strategy_log_service.info(
                    strategy.id,
                    f"平仓前已撤销 {symbol} 交易所挂单约 {pre_purge} 笔（止盈/止损/加仓等）",
                )
        except Exception as e:
            logger.warning("purge before close_all strategy=%d: %s", strategy.id, e)

        # Cancel all pending orders first
        pending = order_tracker.get_active_for_strategy(strategy.id)
        for o in pending:
            if GridExecutor._order_symbol_matches(o.symbol, symbol):
                try:
                    if o.purpose == "stop_loss":
                        await exchange.cancel_algo_order(o.order_id, o.symbol)
                    else:
                        await exchange.cancel_order(o.order_id, o.symbol)
                except Exception:
                    pass

        # 止盈/止损条件单已在交易所平掉持仓时跳过 close；其余原因仍走 close_position
        close_success = True
        market_avg = 0.0
        if positions_already_closed:
            logger.info(
                "Skipping close_position for strategy=%d: positions already closed on exchange (%s)",
                strategy.id,
                reason,
            )
        else:
            close_success = False
            try:
                result = await exchange.close_position(symbol, strategy.direction)
                if result:
                    close_success = True
                    market_avg = GridExecutor._avg_price_from_order_dict(result if isinstance(result, dict) else {})
            except Exception as e:
                logger.error("Failed to close position for strategy=%d %s: %s", strategy.id, symbol, e)
                strategy_log_service.error(strategy.id, f"交易所平仓失败: {e}")

        if not close_success and not positions_already_closed:
            logger.warning("Exchange close failed for strategy=%d %s — still recording DB close", strategy.id, symbol)
            strategy_log_service.warning(strategy.id, "交易所平仓失败，本地记录已关闭，请手动检查交易所持仓")

        resolved_exit = float(current_price)
        if exit_price_override is not None and exit_price_override > 0:
            resolved_exit = float(exit_price_override)
        elif market_avg > 0:
            resolved_exit = market_avg

        # Record trades for each position
        now = now_beijing()
        for pos in positions:
            exit_price = resolved_exit
            if pos.side == "long":
                pnl = (exit_price - pos.entry_price) * pos.quantity
                pct = (exit_price - pos.entry_price) / pos.entry_price * 100 if pos.entry_price > 0 else 0
            else:
                pnl = (pos.entry_price - exit_price) * pos.quantity
                pct = (pos.entry_price - exit_price) / pos.entry_price * 100 if pos.entry_price > 0 else 0

            trade = Trade(
                strategy_id=strategy.id,
                account_id=strategy.account_id,
                symbol=pos.symbol,
                side=pos.side,
                quantity=pos.quantity,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                realized_pnl=round(pnl, 4),
                pnl_pct=round(pct, 4),
                entry_time=pos.opened_at or now,
                exit_time=now,
                layer=pos.layer,
                grid_level=pos.grid_level,
                close_reason=reason,
            )
            session.add(trade)
            pos.closed_at = now

        await session.commit()
        n_pos = len(positions)
        if n_pos > 0:
            reason_label = {
                "take_profit": "止盈",
                "stop_loss": "止损",
                "panic_close": "紧急平仓",
                "margin_stop": "保证金止损",
                "manual": "手动/其他",
                "strategy_deleted": "策略删除平仓",
            }.get(reason, reason)
            strategy_log_service.success(
                strategy.id,
                f"{reason_label}平仓记录成功: {symbol} 已写入 {n_pos} 笔交易历史 (close_reason={reason})，"
                f"平仓价≈{resolved_exit:.6f}",
            )
        order_tracker.clear_strategy(strategy.id)
        try:
            purged = await self._purge_exchange_open_orders(exchange, symbol, strategy.id, strategy.direction)
            if purged > 0:
                strategy_log_service.info(
                    strategy.id,
                    f"已撤销交易所残留挂单: {symbol} 约 {purged} 笔（平仓记账后扫尾，便于重新开仓挂单）",
                )
        except Exception as e:
            logger.warning("purge after close_all strategy=%d: %s", strategy.id, e)
        logger.info("Closed all %s positions for strategy=%d reason=%s exchange_ok=%s", symbol, strategy.id, reason, close_success)
