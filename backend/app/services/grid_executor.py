"""Grid strategy executor — state machine that drives the martingale grid lifecycle."""
import logging
from typing import Optional

from ..config import now_beijing
from ..models.position import Position
from ..models.trade import Trade
from .grid_engine import GridStrategyEngine, GridLevel
from .order_tracker import order_tracker, OrderState
from .stop_loss_manager import StopLossManager, StopLossLevel
from .log_service import strategy_log_service

logger = logging.getLogger(__name__)


class GridExecutor:
    """Per-symbol grid strategy lifecycle executor.

    Flow:
    1. No position → market open initial + place TP limit + place first grid add limit
    2. Has positions → check TP fills / grid add fills / stop loss
    3. TP fill → close all + reopen initial
    4. Grid add fill → update avg entry + replace TP + place next grid add
    5. Stop loss:
       - SOFT (80% threshold): alert only, continue trading
       - HARD (100% threshold): close all + reopen initial
       - PANIC (single layer >50% loss): close all + reopen initial
    """

    def __init__(self, engine: GridStrategyEngine):
        self.engine = engine
        self.sl_manager = StopLossManager(threshold_u=engine.loss_threshold)

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

    async def process_symbol(
        self, session, strategy, symbol: str, exchange, current_price: float,
    ) -> None:
        """Main per-symbol processing entry point."""
        # Check existing open positions
        from sqlalchemy import select
        stmt = select(Position).where(
            Position.strategy_id == strategy.id,
            Position.symbol == symbol,
            Position.closed_at.is_(None),
        ).order_by(Position.grid_level)
        result = await session.execute(stmt)
        open_positions = list(result.scalars().all())

        if not open_positions:
            await self._open_initial(session, strategy, symbol, exchange, current_price)
            return

        # Update mark prices and calculate cumulative unrealized PnL
        cumulative_u = 0.0
        for pos in open_positions:
            pos.mark_price = current_price
            if pos.side == "long":
                pos.unrealized_pnl = (current_price - pos.entry_price) * pos.quantity
            else:
                pos.unrealized_pnl = (pos.entry_price - current_price) * pos.quantity
            cumulative_u += pos.unrealized_pnl

        if self.engine.loss_threshold > 0:
            strategy_log_service.info(
                strategy.id,
                f"浮亏监控: 累计 {cumulative_u:.2f}U / 阈值 {self.engine.loss_threshold:.2f}U "
                f"({abs(cumulative_u) / self.engine.loss_threshold * 100:.0f}%)"
                if cumulative_u < 0 else
                f"浮盈监控: 累计 {cumulative_u:.2f}U",
            )

        # Check TP order fills
        tp_filled = await self._check_tp_fills(session, strategy, symbol, exchange, open_positions, current_price)
        if tp_filled:
            return  # positions were closed and reopened

        # Check grid add order fills
        add_filled = await self._check_grid_add_fills(session, strategy, symbol, exchange, open_positions, current_price)
        if add_filled:
            return  # positions updated

        # Check cumulative stop loss
        sl_triggered = await self._check_stop_loss(session, strategy, symbol, exchange, open_positions, current_price, cumulative_u)
        if sl_triggered:
            return

    async def _open_initial(self, session, strategy, symbol, exchange, current_price):
        """Open initial position at market + place TP limit + first grid add limit."""
        from sqlalchemy import select

        side_raw = "buy" if strategy.direction == "long" else "sell"
        position_side = "LONG" if strategy.direction == "long" else "SHORT"

        # Calculate position quantity (USDT notional / price, no leverage)
        qty = 0.0
        if strategy.base_qty_type == "margin_pct":
            try:
                balance = await exchange.fetch_balance()
                total_usdt = float(balance.get("total", {}).get("USDT", 0) or 0)
                usdt_amount = total_usdt * (strategy.base_qty_value / 100.0)
                if current_price > 0:
                    qty = usdt_amount / current_price
            except Exception as e:
                logger.warning("Failed to calculate margin-based qty: %s", e)
                strategy_log_service.error(strategy.id, f"开仓失败: 获取余额异常 - {e}")
                return
        else:
            if current_price > 0:
                qty = strategy.base_qty_value / current_price
            else:
                logger.error("Cannot calculate qty: current_price is 0")
                strategy_log_service.error(strategy.id, "开仓失败: 无法获取当前价格")
                return

        if qty <= 0:
            logger.error("Calculated qty is 0 for strategy %d", strategy.id)
            strategy_log_service.error(strategy.id, "开仓失败: 计算数量为0")
            return

        qty = self._round_qty(qty)

        # 1. Market open initial position
        try:
            order = await exchange.create_market_order(
                symbol, side_raw, qty, reduce_only=False, position_side=position_side,
            )
        except Exception as e:
            logger.error("Failed to open initial position for %s %s: %s", strategy.id, symbol, e)
            strategy_log_service.error(strategy.id, f"开仓失败: {symbol} {strategy.direction} - {e}")
            return

        entry_price = float(order.get("average", 0) or order.get("price", 0) or current_price)
        filled_qty = float(order.get("filled", qty) or qty)

        strategy_log_service.success(
            strategy.id,
            f"开仓成功: {symbol} {strategy.direction} 数量={filled_qty:.4f} 价格={entry_price:.4f}",
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
                f"挂单止盈: 数量={filled_qty:.4f} 止盈价={tp_price:.4f} ({strategy.tp_pct}%)",
            )
        except Exception as e:
            logger.error("Failed to place TP order for %s %s: %s", strategy.id, symbol, e)
            strategy_log_service.error(strategy.id, f"挂单止盈失败: {e}")

        await session.commit()

        # 3. Place all grid add limit orders
        grid_levels = self.engine.calculate_grid_levels(entry_price, strategy.direction)
        placed = 0
        total = len(grid_levels)
        for gl in grid_levels:
            result = await self._place_grid_add(session, strategy, symbol, exchange, pos, gl)
            if result:
                placed += 1
        strategy_log_service.success(
            strategy.id,
            f"挂单加仓: {placed}/{total} 层已挂单 (共{total}层加仓限价单, max_layers={strategy.max_layers})",
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
                qty = usdt_amount / trigger_price
            except Exception as e:
                logger.warning("Failed to calculate margin-based grid add qty: %s", e)
                strategy_log_service.error(strategy.id, f"挂单加仓 Lv{grid_level.level} 失败: 余额查询异常")
                return None
        else:
            qty = raw_size / trigger_price

        if qty <= 0:
            logger.error("Grid add qty is 0 for strategy %d level %d", strategy.id, grid_level.level)
            strategy_log_service.error(strategy.id, f"挂单加仓 Lv{grid_level.level} 失败: 计算数量为0")
            return None

        qty = self._round_qty(qty)

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
            logger.error("Failed to place grid add order for %s lv=%d: %s", symbol, grid_level.level, e)
            strategy_log_service.error(strategy.id, f"挂单加仓 Lv{grid_level.level} 失败: {e}")
            return None

    async def _check_tp_fills(self, session, strategy, symbol, exchange, positions, current_price) -> bool:
        """Check if TP limit orders have filled. If so, close all and reopen."""
        tp_orders = order_tracker.get_pending_by_purpose(strategy.id, "tp")
        filled = False
        for o in tp_orders:
            if o.symbol == symbol:
                updated = await order_tracker.check_order(exchange, o.order_id, o.symbol)
                if updated and updated.status == OrderState.FILLED:
                    filled = True
                    break

        if not filled:
            return False

        strategy_log_service.success(
            strategy.id,
            f"止盈触发: {symbol} 当前价={current_price:.4f} 平仓+重开",
        )

        # TP filled → close all positions
        await self._close_all(session, strategy, symbol, exchange, positions, current_price, "take_profit")

        # Reopen initial if configured and strategy still running; otherwise stop
        if strategy.reopen_after_close and strategy.status == "running":
            await self._open_initial(session, strategy, symbol, exchange, current_price)
        else:
            strategy.status = "stopped"
            await session.commit()

        return True

    async def _check_grid_add_fills(self, session, strategy, symbol, exchange, positions, current_price) -> bool:
        """Check if grid add limit orders have filled. Update positions and re-place orders."""
        add_orders = order_tracker.get_pending_by_purpose(strategy.id, "grid_add")
        any_filled = False
        for o in add_orders:
            if o.symbol != symbol:
                continue
            updated = await order_tracker.check_order(exchange, o.order_id, o.symbol)
            if not updated or updated.status != OrderState.FILLED:
                continue

            # Grid add filled → record new position
            side = strategy.direction
            position_side = "LONG" if side == "long" else "SHORT"

            filled_qty = float(updated.filled) if updated.filled > 0 else float(o.amount)
            fill_price = float(updated.price) if updated.price > 0 else float(o.price)

            max_level = max((p.grid_level for p in positions), default=0)
            next_level = max_level + 1

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
                f"加仓成交: Lv{next_level} 数量={filled_qty:.4f} 价格={fill_price:.4f}",
            )
            logger.info("Grid add filled: %s lv=%d qty=%.4f price=%.4f", symbol, next_level, filled_qty, fill_price)

            # Cancel old TP order
            await self._cancel_tp_orders(session, strategy, symbol, exchange, positions)

            # Place new TP order for combined position
            avg_entry = self.engine.calculate_avg_entry(positions)
            tp_price = self.engine.calculate_tp_price(avg_entry, side)
            tp_side = "sell" if side == "long" else "buy"
            total_qty = sum(float(p.quantity) for p in positions)
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
                    f"重新挂单止盈: 合并数量={total_qty:.4f} 均价={avg_entry:.4f} 止盈价={tp_price:.4f}",
                )
            except Exception as e:
                logger.error("Failed to place new TP after grid add: %s", e)
                strategy_log_service.error(strategy.id, f"重新挂单止盈失败: {e}")

            await session.commit()
            # 所有加仓单已在首单开仓时一次性挂出，成交后无需再挂下一层

        return any_filled

    async def _cancel_tp_orders(self, session, strategy, symbol, exchange, positions):
        """Cancel all existing TP limit orders for this strategy+symbol."""
        tp_orders = order_tracker.get_pending_by_purpose(strategy.id, "tp")
        for o in tp_orders:
            if o.symbol == symbol:
                try:
                    await exchange.cancel_order(o.order_id, o.symbol)
                except Exception as e:
                    logger.debug("Cancel TP order %s: %s", o.order_id, e)

    async def _check_stop_loss(self, session, strategy, symbol, exchange, positions, current_price, cumulative_u: float) -> bool:
        """Check per-strategy cumulative unrealized loss with multi-level stop loss.

        Calculates total floating PnL across all layers of this strategy.
        SOFT (80%): alert only
        HARD (100%): close all + reopen
        PANIC (single layer >50% loss): close all + reopen
        """
        decision = self.sl_manager.evaluate(cumulative_u, positions, current_price)

        if decision.level == StopLossLevel.NONE:
            return False

        if decision.level == StopLossLevel.SOFT:
            logger.warning(
                "SL SOFT: strategy=%d %s cumulative_u=%.2f threshold=%.2f",
                strategy.id, symbol, cumulative_u, self.engine.loss_threshold,
            )
            strategy_log_service.warning(
                strategy.id,
                f"止损预警: 累计浮亏 {abs(cumulative_u):.2f}U (阈值 {self.engine.loss_threshold:.2f}U 的80%)",
            )
            return False

        close_reason = "stop_loss"
        if decision.level == StopLossLevel.PANIC:
            close_reason = "panic_loss"
            layer = decision.affected_layer
            logger.warning(
                "SL PANIC: strategy=%d %s layer=%d single loss >50%% entry value",
                strategy.id, symbol, layer,
            )
            strategy_log_service.error(
                strategy.id,
                f"恐慌止损: 第{layer}层单层亏损超50%，立即平仓",
            )
        else:
            logger.warning(
                "SL HARD: strategy=%d %s cumulative_u=%.2f threshold=%.2f",
                strategy.id, symbol, cumulative_u, self.engine.loss_threshold,
            )
            strategy_log_service.error(
                strategy.id,
                f"硬止损触发: 累计浮亏 {abs(cumulative_u):.2f}U 达到阈值 {self.engine.loss_threshold:.2f}U",
            )

        await self._close_all(session, strategy, symbol, exchange, positions, current_price, close_reason)

        if strategy.reopen_after_close and strategy.status == "running":
            await self._open_initial(session, strategy, symbol, exchange, current_price)
        else:
            strategy.status = "stopped"
            await session.commit()

        return True

    async def _close_all(self, session, strategy, symbol, exchange, positions, current_price, reason: str):
        """Close all positions for this strategy+symbol via market order. Record trades."""
        # Cancel all pending orders first
        pending = order_tracker.get_active_for_strategy(strategy.id)
        for o in pending:
            if o.symbol == symbol:
                try:
                    await exchange.cancel_order(o.order_id, o.symbol)
                except Exception:
                    pass

        # Market close all
        close_success = False
        try:
            result = await exchange.close_position(symbol, strategy.direction)
            if result:
                close_success = True
        except Exception as e:
            logger.error("Failed to close position for strategy=%d %s: %s", strategy.id, symbol, e)
            strategy_log_service.error(strategy.id, f"交易所平仓失败: {e}")

        if not close_success:
            logger.warning("Exchange close failed for strategy=%d %s — still recording DB close", strategy.id, symbol)
            strategy_log_service.warning(strategy.id, "交易所平仓失败，本地记录已关闭，请手动检查交易所持仓")

        # Record trades for each position
        now = now_beijing()
        for pos in positions:
            exit_price = current_price
            if pos.side == "long":
                pnl = (exit_price - pos.entry_price) * pos.quantity
                pct = (exit_price - pos.entry_price) / pos.entry_price * 100 if pos.entry_price > 0 else 0
            else:
                pnl = (pos.entry_price - exit_price) * pos.quantity
                pct = (pos.entry_price - exit_price) / pos.entry_price * 100 if pos.entry_price > 0 else 0

            trade = Trade(
                strategy_id=strategy.id,
                account_id=strategy.account_id,
                symbol=symbol,
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
        order_tracker.clear_strategy(strategy.id)
        logger.info("Closed all %s positions for strategy=%d reason=%s exchange_ok=%s", symbol, strategy.id, reason, close_success)
