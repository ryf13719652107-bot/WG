"""Grid strategy executor — state machine that drives the martingale grid lifecycle."""
import logging
from typing import Optional

from ..config import now_beijing
from ..models.position import Position
from ..models.trade import Trade
from .exchange_base import BaseExchangeService
from .grid_engine import GridStrategyEngine, GridLevel
from .order_tracker import order_tracker, OrderState
from .stop_loss_manager import StopLossManager, StopLossLevel
from .log_service import strategy_log_service

logger = logging.getLogger(__name__)


class GridExecutor:
    """Per-symbol grid strategy lifecycle executor.

    Flow:
    1. No position → market open initial + place TP limit + place grid add limits + place SL order
    2. Has positions → check TP fills / grid add fills / stop loss fills
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
        self.sl_manager = StopLossManager(threshold_u=engine.loss_threshold)
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

    async def _avg_qty_for_stop_loss_basis(
        self,
        exchange,
        symbol: str,
        direction: str,
        db_positions: list,
    ) -> tuple[float, float, str]:
        """止损用加权均价与张数：优先使用交易所实时持仓（与浮亏口径一致），否则退回 DB."""
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

        if ex_qty > 1e-12:
            avg_ex = ex_cost / ex_qty
            if qty_db > 1e-12 and (
                abs(ex_qty - qty_db) / qty_db > 0.015
                or abs(avg_ex - avg_db) / max(avg_db, 1e-12) > 0.015
            ):
                logger.info(
                    "SL basis prefers exchange avg=%.6f qty=%.8f over DB avg=%.6f qty=%.8f",
                    avg_ex, ex_qty, avg_db, qty_db,
                )
            return avg_ex, ex_qty, "exchange"

        return avg_db, qty_db, "db"

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

        avg_raw, qty_raw, basis = await self._avg_qty_for_stop_loss_basis(
            exchange, symbol, strategy.direction, db_positions
        )
        qty_sl = await exchange.normalize_order_amount(symbol, qty_raw)
        sl_price = self.engine.stop_loss_price_for_fixed_usdt_loss(
            avg_raw, qty_sl, loss_u, strategy.direction,
        )
        sl_side = "sell" if strategy.direction == "long" else "buy"

        if sl_price <= 0 or qty_sl <= 0 or not self._check_order_qty(qty_sl, symbol):
            strategy_log_service.warning(
                strategy.id,
                f"{log_label}跳过: 无效价或量 basis={basis} avg={avg_raw:.6f} raw_qty={qty_raw:.8f} step_qty={qty_sl:.8f}",
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
                f"{log_label}: basis={basis} avg={avg_raw:.6f} step_qty={qty_sl:.8f} "
                f"止损价={sl_price:.6f} (阈值约{loss_u:.2f}U)",
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
            strategy_log_service.warning(strategy.id, f"{log_label}失败: {e} (将使用浮亏监控止损)")

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
                    qty = usdt_amount / current_price
            except Exception as e:
                logger.warning("Failed to calculate margin-based qty: %s", e)
                strategy_log_service.error(strategy.id, f"开仓失败: 获取余额异常 - {e}")
                self._consecutive_failures += 1
                return
        else:
            if current_price > 0:
                qty = strategy.base_qty_value / current_price
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

        entry_price = float(order.get("average", 0) or order.get("price", 0) or current_price)
        filled_qty = float(order.get("filled", qty) or qty)

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
                    f"挂单止盈: 数量={filled_qty:.4f} 止盈价={tp_price:.4f} ({strategy.tp_pct}%)",
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
        all_orders = {o.order_id: o for o in tp_orders + filled_orders}
        filled = False
        for o in all_orders.values():
            if not GridExecutor._order_symbol_matches(o.symbol, symbol):
                continue
            if o.status == OrderState.FILLED:
                filled = True
                break
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
        self._schedule_feishu(
            strategy,
            title="止盈触发（止盈单已成交）",
            body_lines=[
                f"当前价≈{current_price:.8f}",
                "后续：记录平仓并按配置重开或未重开",
            ],
        )

        # TP filled → close all positions (skip market-close, TP already closed on exchange)
        await self._close_all(session, strategy, symbol, exchange, positions, current_price, "take_profit")

        # Refresh strategy (status may be stale after commit)
        await session.refresh(strategy)

        # Reopen initial if configured and strategy still running; otherwise stop
        if strategy.reopen_after_close and strategy.status == "running":
            strategy_log_service.success(strategy.id, f"止盈成功,自动重开: {symbol}")
            self._schedule_feishu(
                strategy,
                title="止盈完成 · 自动重开",
                body_lines=["已按市价重新开首单并挂止盈/加仓/止损（若启用）"],
            )
            await self._open_initial(session, strategy, symbol, exchange, current_price)
        else:
            strategy_log_service.success(strategy.id, f"止盈成功,策略停止: {symbol}")
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
                f"加仓成交: Lv{next_level} 数量={filled_qty:.4f} 价格={fill_price:.4f}",
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
                        f"重新挂单止盈: 合并数量={total_qty:.4f} 均价={avg_entry:.4f} 止盈价={tp_price:.4f}",
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
                    await exchange.cancel_algo_order(o.order_id, o.symbol)
                    logger.debug("Cancelled SL algo order %s", o.order_id)
                except Exception as e:
                    logger.debug("Cancel SL algo order %s: %s", o.order_id, e)

    async def _check_stop_loss_fills(self, session, strategy, symbol, exchange, positions, current_price) -> bool:
        """Check if stop loss orders have filled. If so, close all and reopen."""
        if self.engine.loss_threshold <= 0:
            return False

        sl_orders = order_tracker.get_pending_by_purpose(strategy.id, "stop_loss")
        filled_orders = order_tracker.get_filled(strategy.id, "stop_loss")
        all_orders = {o.order_id: o for o in sl_orders + filled_orders}
        filled = False
        for o in all_orders.values():
            if not GridExecutor._order_symbol_matches(o.symbol, symbol):
                continue
            if o.status == OrderState.FILLED:
                filled = True
                break
            updated = await order_tracker.check_order(exchange, o.order_id, o.symbol)
            if updated and updated.status == OrderState.FILLED:
                filled = True
                break

        if not filled:
            return False

        strategy_log_service.success(
            strategy.id,
            f"止损触发: {symbol} 当前价={current_price:.4f} 平仓+重开",
        )
        self._schedule_feishu(
            strategy,
            title="止损触发（Algo 条件市价单已成交）",
            body_lines=[
                f"当前价≈{current_price:.8f}",
                "后续：平仓记录并按配置自动重开或未重开",
            ],
        )

        await self._close_all(session, strategy, symbol, exchange, positions, current_price, "stop_loss")

        await session.refresh(strategy)

        if strategy.reopen_after_close and strategy.status == "running":
            strategy_log_service.success(strategy.id, f"止损成功,自动重开: {symbol}")
            self._schedule_feishu(
                strategy,
                title="止损完成 · 自动重开",
                body_lines=["市价止损后已重新开首单并挂新单（若启用止损单等）"],
            )
            await self._open_initial(session, strategy, symbol, exchange, current_price)
        else:
            strategy_log_service.success(strategy.id, f"止损成功,策略停止: {symbol}")
            strategy.status = "stopped"
            await session.commit()
            self._schedule_feishu(
                strategy,
                title="止损完成 · 策略已停止",
                body_lines=["reopen_after_close=否"],
            )

        return True

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
            self._schedule_feishu(
                strategy,
                title="止损预警（SOFT 80%）",
                body_lines=[
                    f"累计浮亏≈{abs(cumulative_u):.2f}U",
                    f"阈值={self.engine.loss_threshold:.2f}U（未平仓，继续监控）",
                ],
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
            self._schedule_feishu(
                strategy,
                title="恐慌止损触发（单层亏损>50%）",
                body_lines=[
                    f"单层: 第{layer}层",
                    f"当前价≈{current_price:.8f}",
                    "后续：市价类平仓并按配置重开",
                ],
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
            self._schedule_feishu(
                strategy,
                title="硬止损触发（累计浮亏达阈值）",
                body_lines=[
                    f"累计浮亏≈{abs(cumulative_u):.2f}U",
                    f"阈值={self.engine.loss_threshold:.2f}U",
                    f"当前价≈{current_price:.8f}",
                ],
            )

        await self._close_all(session, strategy, symbol, exchange, positions, current_price, close_reason)

        await session.refresh(strategy)

        if strategy.reopen_after_close and strategy.status == "running":
            self._schedule_feishu(
                strategy,
                title="浮亏止损完成 · 自动重开",
                body_lines=[f"原因={close_reason}", "已按配置重新开首单并挂新单"],
            )
            await self._open_initial(session, strategy, symbol, exchange, current_price)
        else:
            strategy.status = "stopped"
            await session.commit()
            self._schedule_feishu(
                strategy,
                title="浮亏止损完成 · 策略已停止",
                body_lines=[f"原因={close_reason}", "reopen_after_close=否"],
            )

        return True

    async def _close_all(self, session, strategy, symbol, exchange, positions, current_price, reason: str):
        """Close all positions for this strategy+symbol via market order. Record trades."""
        # Cancel all pending orders first
        pending = order_tracker.get_active_for_strategy(strategy.id)
        for o in pending:
            if GridExecutor._order_symbol_matches(o.symbol, symbol):
                try:
                    await exchange.cancel_order(o.order_id, o.symbol)
                except Exception:
                    pass

        # Market close all — skip if TP already closed the position on exchange
        close_success = True
        if reason in ("take_profit", "stop_loss"):
            logger.info("Skipping close_position for strategy=%d: position already closed by %s order", strategy.id, reason)
        else:
            close_success = False
            try:
                result = await exchange.close_position(symbol, strategy.direction)
                if result:
                    close_success = True
            except Exception as e:
                logger.error("Failed to close position for strategy=%d %s: %s", strategy.id, symbol, e)
                strategy_log_service.error(strategy.id, f"交易所平仓失败: {e}")

        if not close_success and reason not in ("take_profit", "stop_loss"):
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
