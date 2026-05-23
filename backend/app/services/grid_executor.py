"""Grid strategy executor — state machine that drives the martingale grid lifecycle."""
import asyncio
import logging
from decimal import Decimal, ROUND_HALF_UP
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
    1. No position → 市价首单 + 止盈限价 + （至多）一单下一层加仓限价 + 可选止损条件单
    2. 有持仓 → 止损 / 止盈 / 加仓 成交检测
    3. TP 成交 → 全平记账 +（可选）重开首单
    4. 加仓限价成交 → 合并均价并重挂止盈/止损 → 挂「下一层」加仓限价（单层链式）
    5. SL 成交 → 全平记账 +（可选）重开首单
    """

    MAX_CONSECUTIVE_FAILURES = 3
    MAX_ORDER_QTY = 1000000.0

    def __init__(self, engine: GridStrategyEngine, strategy_id: int = 0):
        self.engine = engine
        self._strategy_id = strategy_id

    @staticmethod
    def _order_symbol_matches(tracker_symbol: str, strategy_symbol: str) -> bool:
        return BaseExchangeService._norm_sym(tracker_symbol) == BaseExchangeService._norm_sym(
            strategy_symbol
        )

    @staticmethod
    def _short_order_id(order_id: str, head: int = 18) -> str:
        o = (order_id or "").strip()
        if not o:
            return "-"
        if len(o) <= head:
            return o
        return f"{o[:head]}…"

    @staticmethod
    def _order_id_from_create_response(order: dict | None) -> str:
        if not order:
            return ""
        oid = str(order.get("id") or order.get("orderId") or "").strip()
        if oid:
            return oid
        info = order.get("info")
        if isinstance(info, dict):
            for k in ("ordId", "algoId"):
                v = info.get(k)
                if v is not None and str(v).strip():
                    return str(v).strip()
        return ""

    @staticmethod
    def _normalize_public_order_status(raw: dict | None) -> str:
        if not raw:
            return ""
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        st = (raw.get("status") or "").lower()
        if isinstance(info, dict):
            ost = str(info.get("state") or info.get("ordStatus") or "").lower()
            if ost and ost not in ("none", "null"):
                st = ost if not st else st
        return (st or "").strip()

    @staticmethod
    def _limit_row_acceptable_after_create(raw: dict | None) -> tuple[bool, str]:
        if not raw:
            return False, "nil"
        st = GridExecutor._normalize_public_order_status(raw)
        filled = float(raw.get("filled", 0) or 0)
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        if isinstance(info, dict):
            try:
                acc = float(info.get("accFillSz") or 0)
                if acc > filled:
                    filled = acc
            except (TypeError, ValueError):
                pass
        dead = {"canceled", "cancelled", "expired"}
        if st in dead:
            return False, st
        live = {"open", "live", "new", "effective", "pending", "partially_filled", "partiallyfilled"}
        if st in live:
            return True, st
        if st in ("closed", "filled") and filled > 1e-12:
            return True, f"{st}(filled)"
        if st in ("closed", "filled"):
            return False, f"{st}(no_fill)"
        return False, f"ambiguous:{st or '?'}"

    async def _verify_post_limit_order(
        self, exchange, symbol: str, order_id: str,
    ) -> tuple[bool, str]:
        oid = (order_id or "").strip()
        if not oid:
            return False, "empty_id"
        delays = (0.0, 0.45, 0.95)
        last_hint = ""
        for d in delays:
            if d > 0:
                await asyncio.sleep(d)
            try:
                raw = await exchange.fetch_order(oid, symbol)
            except Exception as e:
                logger.debug("post-limit verify fetch_order %s: %s", oid, e)
                raw = None
            ok, hint = GridExecutor._limit_row_acceptable_after_create(raw)
            last_hint = hint
            if ok:
                return True, hint
            if raw and GridExecutor._normalize_public_order_status(raw) in (
                "canceled", "cancelled", "expired",
            ):
                return False, hint
        try:
            oo = await exchange.fetch_open_orders(symbol)
        except Exception as e:
            return False, f"verify_fail(open_orders:{e}); last_fetch={last_hint}"
        for row in oo or []:
            rid = str(row.get("id") or row.get("orderId") or "").strip()
            info = row.get("info") if isinstance(row.get("info"), dict) else {}
            if isinstance(info, dict) and not rid:
                rid = str(info.get("ordId") or "").strip()
            if rid == oid:
                return True, "in_open_orders"
        return False, f"not_found; last_fetch={last_hint}"

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
        if qty <= 0:
            return 0.0
        if qty >= 1:
            return round(qty, 4)
        if qty >= 0.01:
            return round(qty, 6)
        return round(qty, 8)

    @staticmethod
    def _clear_positions_add_oid(positions: list) -> None:
        for p in positions:
            p.add_limit_order_id = None

    @staticmethod
    def _anchor_grid_entry_price(positions: list) -> float:
        for p in positions:
            if int(getattr(p, "grid_level", 0) or 0) == 0:
                return float(getattr(p, "entry_price", 0.0) or 0.0)
        return 0.0

    @staticmethod
    def _set_all_positions_add_oid(positions: list, oid: str | None) -> None:
        o = (oid or "").strip() or None
        for p in positions:
            p.add_limit_order_id = o

    @staticmethod
    def _set_all_positions_sl_oid(positions: list, oid: str | None) -> None:
        o = (oid or "").strip() or None
        for p in positions:
            p.sl_algo_order_id = o

    async def _rehydrate_grid_add_tracker_if_needed(
        self, exchange, strategy, symbol: str, positions: list,
    ) -> None:
        oid = ""
        for p in positions:
            o = (getattr(p, "add_limit_order_id", None) or "").strip()
            if o:
                oid = o
                break
        if not oid:
            return
        if order_tracker.get(oid):
            await order_tracker.check_order(exchange, oid, symbol)
            return

        side_raw = "buy" if strategy.direction == "long" else "sell"
        try:
            raw = await exchange.fetch_order(oid, symbol)
        except Exception as e:
            logger.warning("grid_add rehydrate fetch_order %s: %s", oid, e)
            return

        amt = float(raw.get("amount", 0) or 0)
        if amt <= 1e-16:
            amt = float(raw.get("filled", 0) or 0)
        px_raw = raw.get("price")
        px_lim = float(px_raw) if px_raw not in (None, "") else 0.0
        avg_px = BaseExchangeService.avg_fill_price_from_order(raw)
        use_px = px_lim if px_lim > 0 else avg_px

        order_tracker.add(
            oid, symbol, side_raw, "limit",
            amt if amt > 0 else float(raw.get("filled", 0) or 0),
            use_px if use_px > 0 else 0.0,
            strategy.id, "grid_add",
        )
        updated = await order_tracker.check_order(exchange, oid, symbol)
        co = updated or order_tracker.get(oid)
        if co and co.is_done and co.status != OrderState.FILLED:
            self._clear_positions_add_oid(positions)
            strategy_log_service.info(
                strategy.id,
                f"加仓挂单 {oid[:16]}… 已不是待成交状态，已清空本地挂单号并将于本周期尝试挂下一层",
            )

    async def _resolve_grid_add_fill_qty_price(
        self, exchange, order_id: str, symbol: str, co, current_price: float,
    ) -> tuple[float, float]:
        filled_qty = float(getattr(co, "filled", 0) or 0)
        if filled_qty <= 0:
            filled_qty = float(getattr(co, "amount", 0) or 0)
        fill_price = float(getattr(co, "price", 0) or 0)
        if fill_price <= 0:
            fill_price = await self._filled_exit_price(exchange, order_id, co.symbol or symbol)
        if fill_price <= 0:
            fill_price = float(current_price or 0)
        return filled_qty, fill_price

    async def _grid_add_order_still_open(
        self, exchange, symbol: str, order_id: str,
    ) -> bool | None:
        oid = (order_id or "").strip()
        if not oid:
            return None
        try:
            oo = await exchange.fetch_open_orders(symbol)
        except Exception as e:
            logger.debug("fetch_open_orders grid_add check %s: %s", symbol, e)
            return None
        for row in oo or []:
            info = row.get("info") if isinstance(row.get("info"), dict) else {}
            row_oid = str(row.get("id") or row.get("orderId") or "").strip()
            if isinstance(info, dict) and not row_oid:
                row_oid = str(info.get("ordId") or "").strip()
            if row_oid == oid:
                return True
        return False

    async def _cancel_tp_orders_with_verify(
        self,
        session,
        strategy,
        symbol,
        exchange,
        positions,
        *,
        context: str = "",
    ) -> bool:
        """撤掉止盈单并验证是否真正撤销。返回 True 表示所有止盈单已确认撤销或不存在。"""
        ctx = (context or "").strip()
        prefix = f"「{ctx}」 " if ctx else ""
        canceled: list[str] = []
        failed_detail: list[str] = []
        seen: set[str] = set()

        old_tp_oids: list[str] = []
        for p in positions or []:
            oid = (getattr(p, "tp_limit_order_id", None) or "").strip()
            if oid and oid not in seen:
                old_tp_oids.append(oid)
                seen.add(oid)

        tp_orders = order_tracker.get_pending_by_purpose(strategy.id, "tp")
        for o in tp_orders:
            if not GridExecutor._order_symbol_matches(o.symbol, symbol):
                continue
            if o.order_id not in seen:
                old_tp_oids.append(o.order_id)
                seen.add(o.order_id)

        for oid in old_tp_oids:
            sym = symbol
            try:
                await exchange.cancel_order(oid, sym)
                canceled.append(oid)
            except Exception as e:
                failed_detail.append(f"{GridExecutor._short_order_id(oid)}:{e}")
                logger.debug("Cancel TP id %s: %s", oid, e)
                strategy_log_service.warning(
                    strategy.id,
                    f"{symbol} {prefix}撤限价止盈失败 Id={GridExecutor._short_order_id(oid)}: {e}",
                )
            finally:
                order_tracker.discard_order(oid)

        for p in positions or []:
            p.tp_limit_order_id = None

        if canceled:
            prev = ",".join(GridExecutor._short_order_id(x) for x in canceled[:12])
            if len(canceled) > 12:
                prev += f" 等{len(canceled)}笔"
            strategy_log_service.info(
                strategy.id,
                f"{symbol} {prefix}撤限价止盈完成: 成功 {len(canceled)} 笔 [{prev}]",
            )

        if failed_detail:
            for oid_str in old_tp_oids:
                still_open = await self._tp_order_still_open_on_exchange(exchange, symbol, oid_str)
                if still_open is True:
                    strategy_log_service.warning(
                        strategy.id,
                        f"{symbol} {prefix}止盈单 {GridExecutor._short_order_id(oid_str)} "
                        f"撤销后仍在交易所挂单中，可能影响新止盈挂单",
                    )
                    return False
        return True

    async def _apply_grid_add_fill(
        self,
        session,
        strategy,
        symbol: str,
        exchange,
        positions: list,
        current_price: float,
        order_id: str,
        filled_qty: float,
        fill_price: float,
        *,
        skip_tp_sl_refresh: bool = False,
    ) -> bool:
        """加仓成交后的统一处理：记持仓、刷新止盈/止损、挂下一层。

        skip_tp_sl_refresh=True 时仅记录持仓不刷新止盈止损（用于批量加仓合并处理）。
        """
        oid = (order_id or "").strip()
        if not oid or filled_qty <= 0 or fill_price <= 0:
            return False
        if any((getattr(p, "exchange_order_id", None) or "").strip() == oid for p in positions):
            return False

        side = strategy.direction
        position_side = "LONG" if side == "long" else "SHORT"
        max_level = max((int(getattr(p, "grid_level", 0) or 0) for p in positions), default=0)
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
            exchange_order_id=oid,
            grid_trigger_price=fill_price,
        )
        session.add(pos)
        await session.commit()
        await session.refresh(pos)
        positions.append(pos)
        self._clear_positions_add_oid(positions)

        strategy_log_service.success(
            strategy.id,
            f"加仓成交记录成功: Lv{next_level} 数量={filled_qty:.4f} 价格={fill_price:.4f} "
            f"订单ID={oid}（已写入本地持仓）",
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

        if skip_tp_sl_refresh:
            return True

        await self._refresh_tp_and_sl_after_change(
            session, strategy, symbol, exchange, positions, current_price,
            context="加仓后刷新止盈",
        )

        await session.commit()
        await self._maybe_place_next_grid_limit_order(strategy, symbol, exchange, positions)
        await session.commit()
        return True

    async def _refresh_tp_and_sl_after_change(
        self,
        session,
        strategy,
        symbol: str,
        exchange,
        positions: list,
        current_price: float,
        *,
        context: str = "",
    ) -> None:
        """仓位变化后统一刷新止盈和止损（撤旧→验证→挂新）。"""
        side = strategy.direction
        position_side = "LONG" if side == "long" else "SHORT"

        strategy_log_service.info(
            strategy.id,
            f"{symbol}: {context}，撤销旧止盈/止损后以合并仓位重挂",
        )

        cancel_ok = await self._cancel_tp_orders_with_verify(
            session, strategy, symbol, exchange, positions, context=context,
        )
        if not cancel_ok:
            strategy_log_service.warning(
                strategy.id,
                f"{symbol}: {context}旧止盈单撤销未确认，仍尝试挂新止盈（可能存在双止盈单风险）",
            )

        ex_qty = await self._exchange_leg_contracts(exchange, symbol, strategy.direction)
        if ex_qty >= 0 and ex_qty <= 1e-12:
            strategy_log_service.warning(
                strategy.id,
                f"{symbol}: {context}撤销旧止盈后发现交易所持仓已平，跳过挂新止盈（可能止盈已成交）",
            )
            return

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
                tp_order_id = GridExecutor._order_id_from_create_response(tp_order)
                ok_px, px_hint = await self._verify_post_limit_order(exchange, symbol, tp_order_id)
                if not ok_px:
                    strategy_log_service.error(
                        strategy.id,
                        f"重新挂单止盈: API 返回后交易所核验失败 {symbol} ordId≈"
                        f"{GridExecutor._short_order_id(tp_order_id)} [{px_hint}]；"
                        f"下周期将通过补挂机制重试。",
                    )
                    for p in positions:
                        p.tp_limit_order_id = tp_order_id
                        p.take_profit_price = tp_price
                    order_tracker.add(
                        tp_order_id, symbol, tp_side, "limit",
                        total_qty, tp_price, strategy.id, "tp",
                    )
                else:
                    for p in positions:
                        p.tp_limit_order_id = tp_order_id
                        p.take_profit_price = tp_price
                    order_tracker.add(
                        tp_order_id, symbol, tp_side, "limit",
                        total_qty, tp_price, strategy.id, "tp",
                    )
                    strategy_log_service.success(
                        strategy.id,
                        f"重新挂单止盈成功: {symbol} 合并数量={total_qty:.4f} 均价={avg_entry:.4f} "
                        f"止盈价={tp_price:.4f} ({strategy.tp_pct}%) 订单ID={tp_order_id} （所核验={px_hint}）",
                    )
                    self._schedule_feishu(
                        strategy,
                        title=f"{context}止盈（合并仓位）",
                        body_lines=[
                            f"合并数量≈{total_qty:.8f}",
                            f"加权均价≈{avg_entry:.8f}",
                            f"止盈价={tp_price:.8f}",
                            f"订单ID: {tp_order_id}",
                            f"核验≈{px_hint}",
                        ],
                    )
            except Exception as e:
                logger.error("Failed to place new TP after change: %s", e)
                strategy_log_service.error(
                    strategy.id,
                    f"重新挂单止盈失败: {e}（下周期将通过补挂机制重试）",
                )
        else:
            strategy_log_service.warning(strategy.id, f"止盈数量超限({total_qty:.2f}),跳过挂单,请手动止盈")

        if float(strategy.cumulative_loss_threshold_u or 0) > 0 and float(strategy.stop_loss_close_pct or 0) > 0:
            await self._cancel_sl_orders(session, strategy, symbol, exchange, context=context)
            await self._compute_and_place_stop_loss(
                strategy=strategy,
                symbol=symbol,
                exchange=exchange,
                db_positions=positions,
                position_side=position_side,
                log_label=f"{context}止损",
            )

    async def _infer_grid_add_from_position_delta(
        self,
        session,
        strategy,
        symbol: str,
        exchange,
        positions: list,
        current_price: float,
    ) -> bool:
        add_oid = ""
        for p in positions:
            o = (getattr(p, "add_limit_order_id", None) or "").strip()
            if o:
                add_oid = o
                break

        db_qty = sum(float(getattr(p, "quantity", 0) or 0) for p in positions)
        ex_qty = await self._exchange_leg_contracts(exchange, symbol, strategy.direction)
        if ex_qty < 0 or ex_qty <= db_qty + 1e-9:
            return False

        if not add_oid:
            pending_add = [
                o for o in order_tracker.get_pending_by_purpose(strategy.id, "grid_add")
                if GridExecutor._order_symbol_matches(o.symbol, symbol)
            ]
            if pending_add:
                add_oid = pending_add[0].order_id
            else:
                filled_add = [
                    o for o in order_tracker.get_filled(strategy.id, "grid_add")
                    if GridExecutor._order_symbol_matches(o.symbol, symbol)
                ]
                if filled_add:
                    add_oid = filled_add[0].order_id

        delta = ex_qty - db_qty
        if not add_oid:
            anchor = self._anchor_grid_entry_price(positions)
            max_lvl = max(int(getattr(p, "grid_level", 0) or 0) for p in positions)
            gl = self.engine.get_next_grid_add(anchor, max_lvl, strategy.direction)
            fill_price = float(gl.trigger_price if gl else 0) or float(current_price or 0)
            strategy_log_service.warning(
                strategy.id,
                f"加仓推断(无挂单号): {symbol} 所侧 {ex_qty:.4f} 张 > DB {db_qty:.4f} 张，"
                f"按增量 {delta:.4f} 张处理",
            )
            return await self._apply_grid_add_fill(
                session, strategy, symbol, exchange, positions, current_price,
                f"inferred-{strategy.id}-{max_lvl + 1}", delta, fill_price,
            )

        still_open = await self._grid_add_order_still_open(exchange, symbol, add_oid)
        if still_open is True:
            return False

        co = order_tracker.get(add_oid)
        if not co:
            side_raw = "buy" if strategy.direction == "long" else "sell"
            order_tracker.add(
                add_oid, symbol, side_raw, "limit",
                delta, current_price, strategy.id, "grid_add",
            )
            co = order_tracker.get(add_oid)

        filled_qty, fill_price = delta, float(current_price or 0)
        if co:
            try:
                raw = await exchange.fetch_order(add_oid, co.symbol or symbol)
                co = order_tracker._apply_raw_to_co(co, raw)
                if co.status == OrderState.FILLED:
                    fq, fp = await self._resolve_grid_add_fill_qty_price(
                        exchange, add_oid, symbol, co, current_price,
                    )
                    if fq > 0:
                        filled_qty = fq
                    if fp > 0:
                        fill_price = fp
            except Exception as e:
                logger.info(
                    "Grid add infer: fetch_order %s failed, using position delta %.8f: %s",
                    add_oid, delta, e,
                )
            if fill_price <= 0 and float(getattr(co, "price", 0) or 0) > 0:
                fill_price = float(co.price)
        if fill_price <= 0:
            anchor = self._anchor_grid_entry_price(positions)
            max_lvl = max(int(getattr(p, "grid_level", 0) or 0) for p in positions)
            gl = self.engine.get_next_grid_add(anchor, max_lvl, strategy.direction)
            if gl and float(gl.trigger_price or 0) > 0:
                fill_price = float(gl.trigger_price)

        strategy_log_service.warning(
            strategy.id,
            f"加仓推断: {symbol} 所侧张数 {ex_qty:.4f} > DB {db_qty:.4f}，"
            f"按 Lv{max(int(getattr(p, 'grid_level', 0) or 0) for p in positions) + 1} 成交处理 "
            f"数量≈{filled_qty:.4f} 价格≈{fill_price:.4f} (订单ID={add_oid[:20]}…)",
        )
        return await self._apply_grid_add_fill(
            session, strategy, symbol, exchange, positions, current_price,
            add_oid, filled_qty, fill_price,
        )

    async def _maybe_place_next_grid_limit_order(
        self, strategy, symbol: str, exchange, positions: list,
    ) -> None:
        pending = [
            o for o in order_tracker.get_pending_by_purpose(strategy.id, "grid_add")
            if GridExecutor._order_symbol_matches(o.symbol, symbol)
        ]
        if pending:
            return

        anchor = self._anchor_grid_entry_price(positions)
        if anchor <= 0:
            return

        max_lvl = max(int(getattr(p, "grid_level", 0) or 0) for p in positions)
        if max_lvl >= self.engine.max_layers:
            self._clear_positions_add_oid(positions)
            return

        gl = self.engine.get_next_grid_add(anchor, max_lvl, strategy.direction)
        if not gl:
            self._clear_positions_add_oid(positions)
            return

        oid = await self._place_grid_add(strategy, symbol, exchange, gl)
        if oid:
            self._set_all_positions_add_oid(positions, oid)

    async def _purge_exchange_open_orders(
        self, exchange, symbol: str, strategy_id: int, direction: str,
    ) -> int:
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
        return BaseExchangeService.avg_fill_price_from_order(raw)

    async def _exchange_leg_contracts(self, exchange, symbol: str, direction: str) -> float:
        side = (direction or "").strip().lower()
        formatted = symbol
        fmt = getattr(exchange, "_format_symbol", None)
        if callable(fmt):
            try:
                formatted = fmt(symbol)
            except Exception:
                formatted = symbol
        try:
            raw = await exchange.fetch_positions([symbol])
        except Exception as e:
            logger.debug("fetch_positions for leg flat check %s: %s", symbol, e)
            return -1.0
        total = 0.0
        for rp in raw or []:
            if not BaseExchangeService.position_row_matches_leg(rp, symbol, side, formatted):
                continue
            total += BaseExchangeService.position_row_contracts_abs(rp)
        return total

    async def _tp_order_still_open_on_exchange(
        self, exchange, symbol: str, tp_order_id: str,
    ) -> bool | None:
        oid = (tp_order_id or "").strip()
        if not oid:
            return None
        try:
            oo = await exchange.fetch_open_orders(symbol)
        except Exception as e:
            logger.debug("fetch_open_orders for tp check %s: %s", symbol, e)
            return None
        for row in oo or []:
            info = row.get("info") if isinstance(row.get("info"), dict) else {}
            row_oid = str(row.get("id") or row.get("orderId") or "").strip()
            if isinstance(info, dict) and not row_oid:
                row_oid = str(info.get("ordId") or "").strip()
            if row_oid == oid:
                return True
        return False

    async def _check_sl_filled_before_tp_infer(
        self, exchange, strategy, symbol: str, session=None,
    ) -> bool:
        """止盈推断前先检查止损是否已成交，避免止损误判为止盈。"""
        sl_orders = order_tracker.get_pending_by_purpose(strategy.id, "stop_loss")
        for o in sl_orders:
            if not GridExecutor._order_symbol_matches(o.symbol, symbol):
                continue
            updated = await order_tracker.check_order(exchange, o.order_id, o.symbol)
            if updated and updated.status == OrderState.FILLED:
                return True

        for p_sl_oid in await self._get_sl_oids_from_positions(strategy, symbol, session=session):
            if order_tracker.get(p_sl_oid):
                continue
            try:
                raw = await exchange.fetch_order(p_sl_oid, symbol)
                st = GridExecutor._normalize_public_order_status(raw)
                if st in ("closed", "filled", "effective"):
                    return True
            except Exception:
                pass
        return False

    async def _get_sl_oids_from_positions(self, strategy, symbol: str, session=None) -> list[str]:
        try:
            from sqlalchemy import select
            if session:
                r = await session.execute(
                    select(Position.sl_algo_order_id).where(
                        Position.strategy_id == strategy.id,
                        Position.closed_at.is_(None),
                        Position.sl_algo_order_id.isnot(None),
                    )
                )
                return [row[0] for row in r.all() if row[0]]
            from ..database import async_session
            async with async_session() as s:
                r = await s.execute(
                    select(Position.sl_algo_order_id).where(
                        Position.strategy_id == strategy.id,
                        Position.closed_at.is_(None),
                        Position.sl_algo_order_id.isnot(None),
                    )
                )
                return [row[0] for row in r.all() if row[0]]
        except Exception:
            return []

    async def _infer_tp_filled_from_exchange_flat(
        self,
        exchange,
        strategy,
        symbol: str,
        positions: list,
        by_id: dict,
        session=None,
    ):
        tp_oid = ""
        tp_price_hint = 0.0
        for p in positions:
            o = (p.tp_limit_order_id or "").strip()
            if o:
                tp_oid = o
                tp_price_hint = float(p.take_profit_price or 0) or 0.0
                break
        if not tp_oid:
            return None

        ex_qty = await self._exchange_leg_contracts(exchange, symbol, strategy.direction)
        if ex_qty < 0 or ex_qty > 1e-12:
            return None

        sl_was_filled = await self._check_sl_filled_before_tp_infer(exchange, strategy, symbol, session=session)
        if sl_was_filled:
            strategy_log_service.warning(
                strategy.id,
                f"止盈推断跳过: {symbol} 检测到止损单已成交，交由止损流程处理（避免误判）",
            )
            return None

        still_open = await self._tp_order_still_open_on_exchange(exchange, symbol, tp_oid)
        if still_open is True:
            return None

        co = by_id.get(tp_oid) or order_tracker.get(tp_oid)
        if not co:
            tp_side_hint = "sell" if strategy.direction == "long" else "buy"
            order_tracker.add(
                tp_oid,
                symbol,
                tp_side_hint,
                "limit",
                float(positions[0].quantity or 0),
                tp_price_hint,
                strategy.id,
                "tp",
            )
            co = order_tracker.get(tp_oid)

        if not co:
            return None

        try:
            raw = await exchange.fetch_order(tp_oid, co.symbol or symbol)
            co = order_tracker._apply_raw_to_co(co, raw)
            if co.status == OrderState.FILLED:
                return co
        except Exception as e:
            logger.info(
                "TP infer: fetch_order %s failed while leg flat (%s), treating as filled",
                tp_oid,
                e,
            )

        if still_open is not True:
            co.status = OrderState.FILLED
            if co.price <= 0 and tp_price_hint > 0:
                co.price = tp_price_hint
            strategy_log_service.warning(
                strategy.id,
                f"止盈推断: {symbol} 交易所持仓已平且止盈限价单不在挂单中，按止盈成交处理 "
                f"(订单ID={tp_oid[:20]}{'…' if len(tp_oid) > 20 else ''})",
            )
            return co
        return None

    async def _filled_exit_price(self, exchange, order_id: str, sym: str) -> float:
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
        loss_u = float(getattr(strategy, "cumulative_loss_threshold_u", 0) or 0)
        pct = float(getattr(strategy, "stop_loss_close_pct", 100) or 0)
        if loss_u <= 0 or pct <= 0:
            return

        avg_raw, qty_raw, basis, ct_from_pos = await self._avg_qty_for_stop_loss_basis(
            exchange, symbol, strategy.direction, db_positions
        )
        qty_basis = await exchange.normalize_order_amount(symbol, qty_raw)
        qty_close_raw = qty_raw * (pct / 100.0)
        qty_sl = await exchange.normalize_order_amount(symbol, qty_close_raw)
        ct_market = await exchange.linear_contract_ct_val(symbol)
        ct_val = ct_market
        if ct_from_pos > 0:
            ct_val = ct_from_pos
        sl_price = self.engine.stop_loss_price_for_fixed_usdt_loss(
            avg_raw, qty_basis, loss_u, strategy.direction, ct_val=ct_val,
        )
        dir_low = (strategy.direction or "").lower()

        def _sl_price_ok(px: float) -> bool:
            if px <= 0:
                return False
            if dir_low == "long":
                return px < avg_raw
            if dir_low == "short":
                return px > avg_raw
            return False

        if not _sl_price_ok(sl_price) and ct_from_pos > 0 and abs(ct_from_pos - ct_market) > 1e-9:
            sl_price = self.engine.stop_loss_price_for_fixed_usdt_loss(
                avg_raw, qty_basis, loss_u, strategy.direction, ct_val=ct_market,
            )
            ct_val = ct_market
        sl_side = "sell" if strategy.direction == "long" else "buy"

        if not _sl_price_ok(sl_price):
            strategy_log_service.warning(
                strategy.id,
                f"{log_label}跳过: 止损触发亏损({loss_u:.2f}U)相对当前仓位过大或 ctVal 不准，"
                f"无法得到有效触发价(sl_price={sl_price:.8f})。"
                f"请降低「止损触发亏损」或增大首单/加仓仓位；"
                f"当前约 {qty_basis:.4f} 张 @ {avg_raw:.6f}",
            )
            return

        if qty_basis <= 0 or qty_sl <= 0 or not self._check_order_qty(qty_sl, symbol):
            strategy_log_service.warning(
                strategy.id,
                f"{log_label}跳过: 无效价或量 basis={basis} avg={avg_raw:.6f} raw_qty={qty_raw:.8f} "
                f"basis_qty(step)={qty_basis:.8f} close_qty(step)={qty_sl:.8f} pct={pct:.2f}% "
                f"ct_val={ct_val:.8f} loss_u={loss_u:.6f} sl_price={sl_price:.8f}",
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
            self._set_all_positions_sl_oid(db_positions, sl_order_id)
            strategy_log_service.success(
                strategy.id,
                f"{log_label}操作成功: basis={basis} avg={avg_raw:.6f} "
                f"平仓比例={pct:.2f}% 条件单数量(step)={qty_sl:.8f}（估算触发亏损≈{loss_u:.2f}U） "
                f"止损价={sl_price:.6f} 订单ID={sl_order_id or '-'}",
            )
            self._schedule_feishu(
                strategy,
                title=f"{log_label}（市价条件单 · 按比例减仓）",
                body_lines=[
                    f"basis={basis} 加权均价≈{avg_raw:.6f}",
                    f"平仓比例≈{pct:.2f}% 条件单数量(step)≈{qty_sl:.8f}",
                    f"触发价≈{sl_price:.6f}",
                    f"名义亏损阈值≈ {loss_u:.2f} U（推算触发价）",
                    f"algoId={sl_order_id}" if sl_order_id else "",
                ],
            )
            logger.info(
                "%s SL placed sym=%s basis=%s avg=%s qty_close=%s pct=%s sl_price=%s loss_u=%s",
                log_label,
                symbol,
                basis,
                avg_raw,
                qty_sl,
                pct,
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
        from sqlalchemy import select
        stmt = select(Position).where(
            Position.strategy_id == strategy.id,
            Position.symbol == symbol,
            Position.closed_at.is_(None),
        ).order_by(Position.grid_level)
        result = await session.execute(stmt)
        open_positions = list(result.scalars().all())

        if getattr(exchange, "exchange_id", "") == "okx":
            await order_tracker.check_all_pending(exchange, strategy.id)

        if not open_positions:
            try:
                purged = await self._purge_exchange_open_orders(
                    exchange, symbol, strategy.id, strategy.direction,
                )
                if purged > 0:
                    strategy_log_service.info(
                        strategy.id,
                        f"无本地持仓，重开首单前已撤销 {symbol} 残留挂单约 {purged} 笔",
                    )
            except Exception as e:
                logger.warning("purge before reopen strategy=%d: %s", strategy.id, e)
            await self._open_initial(session, strategy, symbol, exchange, current_price)
            return

        for pos in open_positions:
            pos.mark_price = current_price
            if pos.side == "long":
                pos.unrealized_pnl = (current_price - float(pos.entry_price)) * float(pos.quantity)
            else:
                pos.unrealized_pnl = (float(pos.entry_price) - current_price) * float(pos.quantity)

        tp_precheck = await self._quick_tp_fill_precheck(
            session, strategy, symbol, exchange, open_positions, current_price,
        )
        if tp_precheck:
            return

        sl_filled = await self._check_stop_loss_fills(session, strategy, symbol, exchange, open_positions, current_price)
        if sl_filled:
            return

        add_filled = await self._check_grid_add_fills(session, strategy, symbol, exchange, open_positions, current_price)

        tp_filled = await self._check_tp_fills(session, strategy, symbol, exchange, open_positions, current_price)
        if tp_filled:
            return

        if not add_filled:
            await self._maybe_place_next_grid_limit_order(strategy, symbol, exchange, open_positions)

        await self._maybe_retry_tp_if_missing(
            strategy, symbol, exchange, open_positions, current_price,
        )

        await self._maybe_retry_stop_loss_if_missing(
            strategy, symbol, exchange, open_positions, current_price,
        )

        await session.commit()

    async def _quick_tp_fill_precheck(
        self,
        session,
        strategy,
        symbol: str,
        exchange,
        positions: list,
        current_price: float,
    ) -> bool:
        """快速预检：止盈单是否已成交（交易所持仓已平）。

        解决问题9（竞态条件）：在处理加仓成交之前先检查止盈是否已触发，
        避免加仓处理后撤销旧止盈、挂新止盈，导致止盈成交被遗漏。
        返回 True 表示止盈已成交且已处理完毕，调用方应直接 return。
        """
        tp_oid = ""
        for p in positions:
            o = (getattr(p, "tp_limit_order_id", None) or "").strip()
            if o:
                tp_oid = o
                break
        if not tp_oid:
            return False

        ex_qty = await self._exchange_leg_contracts(exchange, symbol, strategy.direction)
        if ex_qty < 0 or ex_qty > 1e-12:
            return False

        sl_was_filled = await self._check_sl_filled_before_tp_infer(exchange, strategy, symbol, session=session)
        if sl_was_filled:
            return False

        tp_filled = await self._check_tp_fills(session, strategy, symbol, exchange, positions, current_price)
        return tp_filled

    async def _maybe_retry_tp_if_missing(
        self, strategy, symbol: str, exchange, positions: list, current_price: float,
    ) -> None:
        """持仓存在但无有效止盈挂单时补挂（解决问题1/11：止盈挂单失败无重试）。"""
        if not positions:
            return

        has_tp_oid = False
        for p in positions:
            oid = (getattr(p, "tp_limit_order_id", None) or "").strip()
            if oid:
                has_tp_oid = True
                break

        total_qty = sum(float(p.quantity) for p in positions)

        if has_tp_oid and order_tracker.has_active_tp_for_symbol(strategy.id, symbol):
            tp_orders = order_tracker.get_pending_by_purpose(strategy.id, "tp")
            for tp_o in tp_orders:
                if GridExecutor._order_symbol_matches(tp_o.symbol, symbol):
                    if total_qty > 0 and abs(tp_o.amount - total_qty) > total_qty * 0.01:
                        strategy_log_service.warning(
                            strategy.id,
                            f"止盈数量不匹配: {symbol} 止盈单数量={tp_o.amount:.4f} "
                            f"当前持仓={total_qty:.4f}，需要刷新止盈",
                        )
                        break
            else:
                return

        if has_tp_oid:
            for p in positions:
                oid = (getattr(p, "tp_limit_order_id", None) or "").strip()
                if oid:
                    still_open = await self._tp_order_still_open_on_exchange(exchange, symbol, oid)
                    if still_open is True:
                        if not order_tracker.get(oid):
                            tp_side_hint = "sell" if strategy.direction == "long" else "buy"
                            order_tracker.add(
                                oid, symbol, tp_side_hint, "limit",
                                sum(float(pp.quantity) for pp in positions),
                                float(p.take_profit_price or 0) or 0.0,
                                strategy.id, "tp",
                            )
                        return
                    break

        side = strategy.direction
        position_side = "LONG" if side == "long" else "SHORT"
        avg_entry = self.engine.calculate_avg_entry(positions)
        tp_price = self.engine.calculate_tp_price(avg_entry, side)
        tp_side = "sell" if side == "long" else "buy"

        if not self._check_order_qty(total_qty, symbol):
            strategy_log_service.warning(strategy.id, f"补挂止盈跳过: 数量超限({total_qty:.2f})")
            return

        old_tp_oids: list[str] = []
        for p in positions:
            o = (getattr(p, "tp_limit_order_id", None) or "").strip()
            if o and o not in old_tp_oids:
                old_tp_oids.append(o)
        for old_oid in old_tp_oids:
            try:
                await exchange.cancel_order(old_oid, symbol)
            except Exception:
                pass
            order_tracker.discard_order(old_oid)
        for p in positions:
            p.tp_limit_order_id = None

        strategy_log_service.info(
            strategy.id,
            f"补挂止盈: {symbol} 检测到无有效止盈挂单，正在补挂 合并数量={total_qty:.4f} "
            f"均价={avg_entry:.4f} 止盈价={tp_price:.4f}",
        )
        try:
            tp_order = await exchange.create_limit_order(
                symbol, tp_side, total_qty, tp_price,
                reduce_only=True, position_side=position_side,
            )
            tp_order_id = GridExecutor._order_id_from_create_response(tp_order)
            ok_tp, tp_hint = await self._verify_post_limit_order(exchange, symbol, tp_order_id)
            if ok_tp:
                for p in positions:
                    p.tp_limit_order_id = tp_order_id
                    p.take_profit_price = tp_price
                order_tracker.add(
                    tp_order_id, symbol, tp_side, "limit",
                    total_qty, tp_price, strategy.id, "tp",
                )
                strategy_log_service.success(
                    strategy.id,
                    f"补挂止盈成功: {symbol} 合并数量={total_qty:.4f} 止盈价={tp_price:.4f} "
                    f"订单ID={tp_order_id} （所核验={tp_hint}）",
                )
            else:
                for p in positions:
                    p.tp_limit_order_id = tp_order_id
                    p.take_profit_price = tp_price
                order_tracker.add(
                    tp_order_id, symbol, tp_side, "limit",
                    total_qty, tp_price, strategy.id, "tp",
                )
                strategy_log_service.error(
                    strategy.id,
                    f"补挂止盈: 核验失败 {symbol} ordId≈{GridExecutor._short_order_id(tp_order_id)} [{tp_hint}]；"
                    f"已记录订单ID，下周期将再次核验或补挂",
                )
        except Exception as e:
            logger.error("Retry TP failed strategy=%d: %s", strategy.id, e)
            strategy_log_service.error(strategy.id, f"补挂止盈失败: {e}（下周期将重试）")

    async def _maybe_retry_stop_loss_if_missing(
        self, strategy, symbol: str, exchange, positions: list, current_price: float,
    ) -> None:
        if float(getattr(strategy, "cumulative_loss_threshold_u", 0) or 0) <= 0:
            return
        if float(getattr(strategy, "stop_loss_close_pct", 0) or 0) <= 0:
            return
        pending = [
            o for o in order_tracker.get_pending_by_purpose(strategy.id, "stop_loss")
            if GridExecutor._order_symbol_matches(o.symbol, symbol)
        ]
        if pending:
            return
        for p in positions:
            oid = (getattr(p, "sl_algo_order_id", None) or "").strip()
            if oid:
                if not order_tracker.get(oid):
                    order_tracker.add(
                        oid, symbol,
                        "sell" if strategy.direction == "long" else "buy",
                        "stop", 0.0, 0.0, strategy.id, "stop_loss",
                    )
                    updated = await order_tracker.check_order(exchange, oid, symbol)
                    if updated and updated.is_active:
                        return
                else:
                    co = order_tracker.get(oid)
                    if co and co.is_active:
                        return
        position_side = "LONG" if strategy.direction == "long" else "SHORT"
        await self._compute_and_place_stop_loss(
            strategy=strategy,
            symbol=symbol,
            exchange=exchange,
            db_positions=positions,
            position_side=position_side,
            log_label="补挂止损",
        )

    async def _open_initial(self, session, strategy, symbol, exchange, current_price) -> bool:
        if int(getattr(strategy, "consecutive_failures", 0) or 0) >= self.MAX_CONSECUTIVE_FAILURES:
            logger.error("Strategy %d: consecutive failures (%d) exceeded limit, stopping", strategy.id, int(getattr(strategy, "consecutive_failures", 0) or 0))
            strategy_log_service.error(strategy.id, f"连续开仓失败{int(getattr(strategy, 'consecutive_failures', 0) or 0)}次,策略自动停止")
            strategy.status = "stopped"
            await session.commit()
            return False

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
                strategy.consecutive_failures = int(getattr(strategy, "consecutive_failures", 0) or 0) + 1
                await session.commit()
                return False
        else:
            if current_price > 0:
                qty = await exchange.quote_usdt_to_order_amount(
                    symbol, float(strategy.base_qty_value), float(current_price),
                )
            else:
                logger.error("Cannot calculate qty: current_price is 0")
                strategy_log_service.error(strategy.id, "开仓失败: 无法获取当前价格")
                strategy.consecutive_failures = int(getattr(strategy, "consecutive_failures", 0) or 0) + 1
                await session.commit()
                return False

        if qty <= 0:
            logger.error("Calculated qty is 0 for strategy %d", strategy.id)
            strategy_log_service.error(strategy.id, "开仓失败: 计算数量为0")
            strategy.consecutive_failures = int(getattr(strategy, "consecutive_failures", 0) or 0) + 1
            await session.commit()
            return False

        qty = self._round_qty(qty)
        qty = await exchange.normalize_order_amount(symbol, qty)

        try:
            order = await exchange.create_market_order(
                symbol, side_raw, qty, reduce_only=False, position_side=position_side,
            )
        except Exception as e:
            logger.error("Failed to open initial position for %s %s: %s", strategy.id, symbol, e)
            strategy_log_service.error(strategy.id, f"开仓失败: {symbol} {strategy.direction} - {e}")
            strategy.consecutive_failures = int(getattr(strategy, "consecutive_failures", 0) or 0) + 1
            await session.commit()
            return False

        strategy.consecutive_failures = 0

        entry_price, filled_qty = await self._resolve_entry_fill(
            exchange, symbol, order, current_price, qty_fallback=qty,
        )

        oid_mkt = str(order.get("id", "") or "")
        strategy_log_service.success(
            strategy.id,
            f"开仓成功: {symbol} {strategy.direction} 市价首单 数量={filled_qty:.4f} 价格={entry_price:.4f} "
            f"订单ID={oid_mkt}",
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

        order_tracker.add(
            str(order.get("id", "")), symbol, side_raw, "market",
            filled_qty, entry_price, strategy.id, "initial_entry",
        )

        tp_price = self.engine.calculate_tp_price(entry_price, strategy.direction)
        tp_side = "sell" if strategy.direction == "long" else "buy"
        if self._check_order_qty(filled_qty, symbol):
            try:
                tp_order = await exchange.create_limit_order(
                    symbol, tp_side, filled_qty, tp_price,
                    reduce_only=True, position_side=position_side,
                )
                oid_tp = GridExecutor._order_id_from_create_response(tp_order)
                ok_tp, tp_hint = await self._verify_post_limit_order(exchange, symbol, oid_tp)
                pos.tp_limit_order_id = oid_tp
                pos.take_profit_price = tp_price
                order_tracker.add(
                    oid_tp, symbol, tp_side, "limit",
                    filled_qty, tp_price, strategy.id, "tp",
                )
                if not ok_tp:
                    strategy_log_service.error(
                        strategy.id,
                        f"挂单止盈: 核验失败 {symbol} ordId≈{GridExecutor._short_order_id(oid_tp)} [{tp_hint}]；"
                        f"已记录订单ID，下周期将通过补挂机制核验或重挂。",
                    )
                else:
                    strategy_log_service.success(
                        strategy.id,
                        f"挂单止盈操作成功（首仓）: {symbol} 数量={filled_qty:.4f} 止盈价={tp_price:.4f} ({strategy.tp_pct}%) "
                        f"订单ID={oid_tp} （所核验={tp_hint}）",
                    )
                    self._schedule_feishu(
                        strategy,
                        title="挂单止盈（限价 reduce-only）",
                        body_lines=[
                            f"数量≈{filled_qty:.8f}",
                            f"止盈价={tp_price:.8f}（{strategy.tp_pct}%）",
                            f"订单ID: {oid_tp}",
                            f"核验状态≈{tp_hint}",
                        ],
                    )
            except Exception as e:
                logger.error("Failed to place TP order for %s %s: %s", strategy.id, symbol, e)
                strategy_log_service.error(strategy.id, f"挂单止盈失败: {e}（下周期将通过补挂机制重试）")
        else:
            strategy_log_service.warning(strategy.id, f"止盈数量超限({filled_qty:.2f}),跳过挂单,请手动止盈")

        await session.commit()

        if float(strategy.cumulative_loss_threshold_u or 0) > 0 and float(strategy.stop_loss_close_pct or 0) > 0:
            await self._compute_and_place_stop_loss(
                strategy=strategy,
                symbol=symbol,
                exchange=exchange,
                db_positions=[pos],
                position_side=position_side,
                log_label="挂单止损",
            )
        else:
            strategy_log_service.info(
                strategy.id,
                f"{symbol}: 首仓挂单完成，未挂交易所止损（止损触发亏损=0 或 止损平仓比例=0）",
            )

        gl_init = self.engine.get_next_grid_add(entry_price, 0, strategy.direction)
        if gl_init:
            oid_add = await self._place_grid_add(strategy, symbol, exchange, gl_init)
            if oid_add:
                pos.add_limit_order_id = oid_add
                strategy_log_service.success(
                    strategy.id,
                    "挂单加仓(链式): 已挂第 1 层限价单（同时仅一单，成交后再挂下一层）",
                )
                self._schedule_feishu(
                    strategy,
                    title="挂单加仓 · 单层链式（第 1 层）",
                    body_lines=[
                        f"Lv1 触发价≈{gl_init.trigger_price:.8f}",
                        f"锚定首单价≈{entry_price:.8f}",
                        f"加仓层上限={self.engine.max_layers}",
                    ],
                )
            else:
                strategy_log_service.warning(
                    strategy.id,
                    "挂单加仓: 首层限价单挂单失败（下一调度周期会自动重试）",
                )

        await session.commit()

        logger.info(
            "Grid initial open: %s %s qty=%.4f entry=%.4f tp=%.4f grid_next_lv=%s",
            strategy.direction, symbol, filled_qty, entry_price, tp_price,
            1 if gl_init else "-",
        )
        return True

    async def _place_grid_add(self, strategy, symbol, exchange, grid_level: GridLevel):
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
            order_id = GridExecutor._order_id_from_create_response(order)
            ok_ga, ga_hint = await self._verify_post_limit_order(exchange, symbol, order_id)
            if not ok_ga:
                strategy_log_service.error(
                    strategy.id,
                    f"挂单加仓 Lv{grid_level.level}: API 返回后核验失败 {symbol} ordId≈"
                    f"{GridExecutor._short_order_id(order_id)} [{ga_hint}]；"
                    f"所里可能无成单，请以交易所当前委托为准。",
                )
                return None
            order_tracker.add(
                order_id, symbol, add_side, "limit",
                qty, trigger_price, strategy.id, "grid_add",
            )
            strategy_log_service.success(
                strategy.id,
                f"挂单加仓 Lv{grid_level.level}: 数量={qty:.4f} 触发价={trigger_price:.4f} "
                f"(累计跌幅{grid_level.drop_pct}%) 订单ID={order_id} （所核验={ga_hint}）",
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
        tp_orders = order_tracker.get_pending_by_purpose(strategy.id, "tp")
        filled_orders = order_tracker.get_filled(strategy.id, "tp")
        by_id: dict = {o.order_id: o for o in tp_orders + filled_orders}

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

        partially_filled = order_tracker.get_partially_filled_by_purpose(strategy.id, "tp")
        for pf in partially_filled:
            if GridExecutor._order_symbol_matches(pf.symbol, symbol) and pf.order_id not in by_id:
                by_id[pf.order_id] = pf

        for o in by_id.values():
            if not GridExecutor._order_symbol_matches(o.symbol, symbol):
                continue
            if o.status == OrderState.FILLED:
                break
        else:
            tp_oids_to_check = [
                o for o in by_id.values()
                if GridExecutor._order_symbol_matches(o.symbol, symbol)
                and o.status in (OrderState.PENDING, OrderState.PARTIALLY_FILLED)
            ]
            for o in tp_oids_to_check:
                try:
                    updated = await order_tracker.check_order(exchange, o.order_id, o.symbol or symbol)
                    if updated and updated.status == OrderState.FILLED:
                        break
                except Exception:
                    pass
            else:
                for p in positions:
                    oid = (p.tp_limit_order_id or "").strip()
                    if not oid:
                        continue
                    try:
                        raw = await exchange.fetch_order(oid, symbol)
                        if raw:
                            filled_qty = float(raw.get("filled", 0) or 0)
                            if filled_qty > 0:
                                co = order_tracker.get(oid)
                                if co:
                                    order_tracker._apply_raw_to_co(co, raw)
                                    if co.status == OrderState.FILLED:
                                        break
                    except Exception:
                        pass

        ref_tp = None
        for o in by_id.values():
            if not GridExecutor._order_symbol_matches(o.symbol, symbol):
                continue
            if o.status == OrderState.FILLED:
                ref_tp = o
                break

        if ref_tp is None:
            ref_tp = await self._infer_tp_filled_from_exchange_flat(
                exchange, strategy, symbol, positions, by_id, session=session,
            )

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

        await self._close_all(
            session, strategy, symbol, exchange, positions, current_price, "take_profit",
            exit_price_override=tp_exit,
            positions_already_closed=True,
        )

        await session.refresh(strategy)

        if strategy.reopen_after_close and strategy.status == "running":
            strategy_log_service.info(
                strategy.id,
                f"{symbol}: 止盈记账完成；按配置自动重开 — 正在清理残留挂单并尝试市价首单…",
            )
            try:
                again = await self._purge_exchange_open_orders(exchange, symbol, strategy.id, strategy.direction)
                if again > 0:
                    strategy_log_service.info(
                        strategy.id,
                        f"重开首单前已撤销 {symbol} 残留挂单约 {again} 笔（避免与上一轮订单重叠）",
                    )
            except Exception as e:
                logger.warning("purge before reopen after TP strategy=%d: %s", strategy.id, e)
            reopened = await self._open_initial(session, strategy, symbol, exchange, current_price)
            if reopened:
                strategy_log_service.success(
                    strategy.id,
                    f"止盈后续: {symbol} 已自动完成重开首单及挂单（止盈/加仓/止损按策略配置）",
                )
                self._schedule_feishu(
                    strategy,
                    title="止盈完成 · 已重开首单",
                    body_lines=[
                        "市价首单已成功，并已挂限价止盈 / 单层加仓链 / 可选止损（见策略日志明细）",
                    ],
                )
            else:
                strategy_log_service.warning(
                    strategy.id,
                    f"{symbol}: 止盈后自动重开首单未完成；"
                    "请查看上方的开仓失败日志，下一调度周期将再次尝试。",
                )
                self._schedule_feishu(
                    strategy,
                    title="止盈完成 · 自动重开未成功",
                    body_lines=[
                        "请核对余额/保证金及上方「开仓失败」日志",
                        "策略若未标记停止，下周期仍会重试开仓",
                    ],
                )
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
        await self._rehydrate_grid_add_tracker_if_needed(exchange, strategy, symbol, positions)

        add_oid_db = ""
        for p in positions:
            o = (getattr(p, "add_limit_order_id", None) or "").strip()
            if o:
                add_oid_db = o
                break
        if add_oid_db:
            co_exist = order_tracker.get(add_oid_db)
            if co_exist:
                await order_tracker.check_order(exchange, add_oid_db, co_exist.symbol or symbol)
            else:
                side_raw = "buy" if strategy.direction == "long" else "sell"
                anchor = self._anchor_grid_entry_price(positions)
                max_lvl = max(int(getattr(p, "grid_level", 0) or 0) for p in positions)
                gl = self.engine.get_next_grid_add(anchor, max_lvl, strategy.direction)
                px_hint = float(gl.trigger_price if gl else 0) or current_price
                qty_hint = 0.0
                try:
                    raw = await exchange.fetch_order(add_oid_db, symbol)
                    qty_hint = float(raw.get("amount", 0) or 0)
                    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
                    if isinstance(info, dict):
                        sz = float(info.get("sz") or 0)
                        if sz > qty_hint:
                            qty_hint = sz
                    px_o = float(raw.get("price", 0) or 0)
                    if px_o > 0:
                        px_hint = px_o
                except Exception as e:
                    logger.debug("grid_add sync fetch_order %s: %s", add_oid_db, e)
                if qty_hint <= 0 and gl:
                    qty_hint = await exchange.quote_usdt_to_order_amount(
                        symbol, float(gl.quantity), float(px_hint or current_price or 1),
                    )
                order_tracker.add(
                    add_oid_db, symbol, side_raw, "limit",
                    qty_hint, px_hint, strategy.id, "grid_add",
                )
                await order_tracker.check_order(exchange, add_oid_db, symbol)

        add_orders = order_tracker.get_pending_by_purpose(strategy.id, "grid_add")
        filled_orders = order_tracker.get_filled(strategy.id, "grid_add")
        all_orders = {o.order_id: o for o in add_orders + filled_orders}
        if add_oid_db and add_oid_db not in all_orders:
            co0 = order_tracker.get(add_oid_db)
            if co0:
                all_orders[add_oid_db] = co0

        filled_list: list[tuple[str, float, float]] = []
        processed_oids: set[str] = set()
        for p in positions:
            eo = (getattr(p, "exchange_order_id", None) or "").strip()
            if eo:
                processed_oids.add(eo)

        for o in list(all_orders.values()):
            if not GridExecutor._order_symbol_matches(o.symbol, symbol):
                continue
            if o.order_id in processed_oids:
                continue
            if o.status != OrderState.FILLED:
                updated = await order_tracker.check_order(exchange, o.order_id, o.symbol or symbol)
                if not updated or updated.status != OrderState.FILLED:
                    continue
                o = updated

            filled_qty, fill_price = await self._resolve_grid_add_fill_qty_price(
                exchange, o.order_id, symbol, o, current_price,
            )
            if filled_qty <= 0 or fill_price <= 0:
                still_open = await self._grid_add_order_still_open(exchange, symbol, o.order_id)
                if still_open is not True:
                    if filled_qty <= 0:
                        filled_qty = float(o.amount or 0)
                    if fill_price <= 0:
                        fill_price = float(o.price or 0) or current_price
                if filled_qty <= 0 or fill_price <= 0:
                    logger.warning(
                        "Grid add filled but invalid qty/price: %s qty=%.4f price=%.4f",
                        o.order_id, filled_qty, fill_price,
                    )
                    continue

            filled_list.append((o.order_id, filled_qty, fill_price))

        if filled_list:
            for idx, (fid, fq, fp) in enumerate(filled_list):
                is_last = idx == len(filled_list) - 1
                await self._apply_grid_add_fill(
                    session, strategy, symbol, exchange, positions, current_price,
                    fid, fq, fp,
                    skip_tp_sl_refresh=not is_last,
                )
            return True

        return await self._infer_grid_add_from_position_delta(
            session, strategy, symbol, exchange, positions, current_price,
        )

    async def _cancel_tp_orders(
        self,
        session,
        strategy,
        symbol,
        exchange,
        positions,
        *,
        context: str = "",
    ):
        ctx = (context or "").strip()
        prefix = f"「{ctx}」 " if ctx else ""
        canceled: list[str] = []
        failed_detail: list[str] = []
        seen: set[str] = set()
        for p in positions or []:
            oid = (getattr(p, "tp_limit_order_id", None) or "").strip()
            if not oid or oid in seen:
                continue
            if not GridExecutor._order_symbol_matches(p.symbol or symbol, symbol):
                continue
            seen.add(oid)
            sym = p.symbol or symbol
            try:
                await exchange.cancel_order(oid, sym)
                canceled.append(oid)
            except Exception as e:
                failed_detail.append(f"{GridExecutor._short_order_id(oid)}:{e}")
                logger.debug("Cancel TP from DB id %s: %s", oid, e)
                strategy_log_service.warning(
                    strategy.id,
                    f"{symbol} {prefix}撤限价止盈失败 Id={GridExecutor._short_order_id(oid)}: {e}",
                )
            finally:
                order_tracker.discard_order(oid)

        tp_orders = order_tracker.get_pending_by_purpose(strategy.id, "tp")
        for o in tp_orders:
            if not GridExecutor._order_symbol_matches(o.symbol, symbol):
                continue
            oid = o.order_id
            if oid in seen:
                continue
            seen.add(oid)
            sym_o = o.symbol or symbol
            try:
                await exchange.cancel_order(oid, sym_o)
                canceled.append(oid)
            except Exception as e:
                failed_detail.append(f"{GridExecutor._short_order_id(oid)}:{e}")
                logger.debug("Cancel TP order %s: %s", oid, e)
                strategy_log_service.warning(
                    strategy.id,
                    f"{symbol} {prefix}撤限价止盈(track)失败 Id={GridExecutor._short_order_id(oid)}: {e}",
                )
            finally:
                order_tracker.discard_order(oid)

        for p in positions or []:
            p.tp_limit_order_id = None

        attempted = len(canceled) + len(failed_detail)
        if canceled:
            prev = ",".join(GridExecutor._short_order_id(x) for x in canceled[:12])
            if len(canceled) > 12:
                prev += f" 等{len(canceled)}笔"
            strategy_log_service.info(
                strategy.id,
                f"{symbol} {prefix}撤限价止盈完成: 成功 {len(canceled)} 笔 [{prev}]",
            )
        elif attempted > 0:
            strategy_log_service.warning(
                strategy.id,
                f"{symbol} {prefix}撤限价止盈未完成: API 均无成功撤销，请核对交易所挂单与权限",
            )
        elif ctx:
            strategy_log_service.info(strategy.id, f"{symbol} {prefix}无待撤限价止盈单")

    async def _cancel_sl_orders(
        self, session, strategy, symbol, exchange, *, context: str = "",
    ):
        ctx = (context or "").strip()
        prefix = f"「{ctx}」 " if ctx else ""
        sl_orders = order_tracker.get_pending_by_purpose(strategy.id, "stop_loss")
        matched_sl = [
            o for o in sl_orders
            if GridExecutor._order_symbol_matches(o.symbol, symbol)
        ]
        canceled_ids: list[str] = []
        if not matched_sl:
            logger.debug(
                "cancel_sl_orders: no pending stop_loss for sym=%s strategy=%s ctx=%s",
                symbol,
                strategy.id,
                ctx,
            )
            return

        failed = 0
        for o in matched_sl:
            oid = o.order_id
            try:
                await exchange.cancel_algo_order(oid, symbol)
                order_tracker.discard_order(oid)
                canceled_ids.append(oid)
                logger.info("Cancelled SL order %s for strategy %d", oid, strategy.id)
            except Exception as e:
                failed += 1
                logger.warning("Cancel SL order %s failed strategy=%d: %s", oid, strategy.id, e)
                strategy_log_service.warning(
                    strategy.id,
                    f"{symbol} {prefix}撤止损条件单失败 Id={GridExecutor._short_order_id(oid)}: {e}",
                )
        if canceled_ids:
            prev = ",".join(GridExecutor._short_order_id(x) for x in canceled_ids[:8])
            strategy_log_service.info(
                strategy.id,
                f"{symbol} {prefix}撤止损条件单完成: 成功 {len(canceled_ids)} 笔 [{prev}]",
            )
        elif matched_sl and failed > 0:
            strategy_log_service.warning(
                strategy.id,
                f"{symbol} {prefix}撤止损条件单: 本轮无成功撤销，请核对交易所 algo 挂单",
                )

    async def _cancel_pending_grid_add_orders(self, strategy, symbol: str, exchange, *, context: str = "") -> None:
        ctx = (context or "").strip()
        prefix = f"「{ctx}」 " if ctx else ""
        pending = [
            o for o in order_tracker.get_pending_by_purpose(strategy.id, "grid_add")
            if GridExecutor._order_symbol_matches(o.symbol, symbol)
        ]
        canceled_ids: list[str] = []
        failed = 0
        if not pending:
            logger.debug(
                "cancel_pending_grid_add: none for sym=%s strategy=%s ctx=%s",
                symbol,
                strategy.id,
                ctx,
            )
            return

        for o in pending:
            oid = o.order_id
            try:
                await exchange.cancel_order(oid, o.symbol)
                order_tracker.discard_order(oid)
                canceled_ids.append(oid)
            except Exception as e:
                failed += 1
                logger.debug("cancel pending grid_add %s: %s", oid, e)
                strategy_log_service.warning(
                    strategy.id,
                    f"{symbol} {prefix}撤限价加仓失败 Id={GridExecutor._short_order_id(oid)}: {e}",
                )
        if canceled_ids:
            prev = ",".join(GridExecutor._short_order_id(x) for x in canceled_ids[:8])
            strategy_log_service.info(
                strategy.id,
                f"{symbol} {prefix}撤限价加仓完成: 成功 {len(canceled_ids)} 笔 [{prev}]",
            )
        elif pending and failed > 0:
            strategy_log_service.warning(
                strategy.id,
                f"{symbol} {prefix}撤限价加仓: 本轮无成功撤销，可能影响后续链式加仓",
            )

    @staticmethod
    def _allocate_stop_close_quantities(active_positions: list, total_close: float) -> list[float]:
        total_before = sum(float(p.quantity) for p in active_positions)
        if total_before <= 0 or total_close <= 0:
            return [0.0] * len(active_positions)
        tc = min(total_close, total_before)
        n = len(active_positions)
        out: list[float] = []
        acc = 0.0
        for i, p in enumerate(active_positions):
            qi = float(p.quantity)
            if i == n - 1:
                ai = max(0.0, tc - acc)
            else:
                ai = GridExecutor._round_qty(tc * qi / total_before)
                acc += ai
            out.append(ai)
        return out

    async def _check_stop_loss_fills(self, session, strategy, symbol, exchange, positions, current_price) -> bool:
        loss_u = float(getattr(strategy, "cumulative_loss_threshold_u", 0) or 0)
        pct_sl = float(getattr(strategy, "stop_loss_close_pct", 100) or 0)
        if loss_u <= 0 or pct_sl <= 0:
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

        filled_qty_sl = float(ref_sl.filled) if ref_sl.filled > 0 else float(ref_sl.amount)

        strategy_log_service.success(
            strategy.id,
            f"止损触发: {symbol} 止损成交数量≈{filled_qty_sl:.8f} 成交价≈{sl_exit:.6f}，按比例减仓记账",
        )
        self._schedule_feishu(
            strategy,
            title="止损触发（条件单 · 按比例减仓）",
            body_lines=[
                f"减仓数量≈{filled_qty_sl:.8f}",
                f"成交价≈{sl_exit:.8f}",
                "止损后不自动重开；有剩余持仓时将刷新止盈与加仓链",
            ],
        )

        strategy_log_service.info(
            strategy.id,
            f"{symbol}: 止损条件单已成交 — 正在撤销旧止盈/止损/待成交加仓限价，随后按剩余仓位重挂",
        )
        await self._cancel_tp_orders(
            session, strategy, symbol, exchange, positions, context="止损减仓前",
        )
        await self._cancel_sl_orders(
            session, strategy, symbol, exchange, context="止损减仓前",
        )
        await self._cancel_pending_grid_add_orders(
            strategy, symbol, exchange, context="止损减仓前",
        )

        order_tracker.discard_order(ref_sl.order_id)

        active = [
            p for p in positions
            if getattr(p, "closed_at", None) is None and float(getattr(p, "quantity", 0) or 0) > 1e-18
        ]
        total_before = sum(float(p.quantity) for p in active)
        if total_before <= 0:
            strategy.status = "stopped"
            await session.commit()
            order_tracker.clear_strategy(strategy.id)
            try:
                await self._purge_exchange_open_orders(exchange, symbol, strategy.id, strategy.direction)
            except Exception as e:
                logger.warning("purge after SL empty strategy=%d: %s", strategy.id, e)
            return True

        tc = min(max(filled_qty_sl, 0.0), total_before)
        allocations = GridExecutor._allocate_stop_close_quantities(active, tc)

        now = now_beijing()
        dust_gate = max(total_before * 1e-9, 1e-12)
        for pos, ai in zip(active, allocations):
            if ai <= 1e-18:
                continue
            exit_price = sl_exit
            if pos.side == "long":
                pnl = float(Decimal(str(exit_price)) - Decimal(str(pos.entry_price))) * ai
                pct = float((Decimal(str(exit_price)) - Decimal(str(pos.entry_price))) / Decimal(str(pos.entry_price)) * 100) if float(pos.entry_price) > 0 else 0
            else:
                pnl = float(Decimal(str(pos.entry_price)) - Decimal(str(exit_price))) * ai
                pct = float((Decimal(str(pos.entry_price)) - Decimal(str(exit_price))) / Decimal(str(pos.entry_price)) * 100) if float(pos.entry_price) > 0 else 0

            trade = Trade(
                strategy_id=strategy.id,
                account_id=strategy.account_id,
                symbol=pos.symbol,
                side=pos.side,
                quantity=ai,
                entry_price=float(pos.entry_price),
                exit_price=exit_price,
                realized_pnl=round(pnl, 8),
                pnl_pct=round(pct, 8),
                entry_time=pos.opened_at or now,
                exit_time=now,
                layer=pos.layer,
                grid_level=pos.grid_level,
                close_reason="stop_loss",
            )
            session.add(trade)
            pos.quantity = float(pos.quantity) - ai
            if float(pos.quantity) <= dust_gate:
                pos.quantity = 0.0
                pos.closed_at = now

        self._clear_positions_add_oid(positions)
        self._set_all_positions_sl_oid(positions, None)

        await session.commit()

        survivors = [
            p for p in positions
            if getattr(p, "closed_at", None) is None and float(getattr(p, "quantity", 0) or 0) > 1e-18
        ]
        remaining_total = sum(float(p.quantity) for p in survivors)

        try:
            rem_norm = await exchange.normalize_order_amount(symbol, remaining_total)
        except Exception:
            rem_norm = remaining_total

        if remaining_total <= 1e-18 or rem_norm <= 0:
            strategy.status = "stopped"
            await session.commit()
            strategy_log_service.success(strategy.id, f"止损后已无剩余持仓，策略已停止: {symbol}")
            order_tracker.clear_strategy(strategy.id)
            try:
                await self._purge_exchange_open_orders(exchange, symbol, strategy.id, strategy.direction)
            except Exception as e:
                logger.warning("purge after SL flat strategy=%d: %s", strategy.id, e)
            return True

        await self._refresh_tp_and_sl_after_change(
            session, strategy, symbol, exchange, survivors, current_price,
            context="止损减仓后",
        )

        await self._maybe_place_next_grid_limit_order(strategy, symbol, exchange, survivors)

        await session.commit()
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
        try:
            pre_purge = await self._purge_exchange_open_orders(exchange, symbol, strategy.id, strategy.direction)
            if pre_purge > 0:
                strategy_log_service.info(
                    strategy.id,
                    f"平仓前已撤销 {symbol} 交易所挂单约 {pre_purge} 笔（止盈/止损/加仓等）",
                )
        except Exception as e:
            logger.warning("purge before close_all strategy=%d: %s", strategy.id, e)

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

        now = now_beijing()
        for pos in positions:
            exit_price = resolved_exit
            ep = float(pos.entry_price)
            pq = float(pos.quantity)
            if pos.side == "long":
                pnl = float(Decimal(str(exit_price)) - Decimal(str(ep))) * pq
                pct = float((Decimal(str(exit_price)) - Decimal(str(ep))) / Decimal(str(ep)) * 100) if ep > 0 else 0
            else:
                pnl = float(Decimal(str(ep)) - Decimal(str(exit_price))) * pq
                pct = float((Decimal(str(ep)) - Decimal(str(exit_price))) / Decimal(str(ep)) * 100) if ep > 0 else 0

            trade = Trade(
                strategy_id=strategy.id,
                account_id=strategy.account_id,
                symbol=pos.symbol,
                side=pos.side,
                quantity=pq,
                entry_price=ep,
                exit_price=exit_price,
                realized_pnl=round(pnl, 8),
                pnl_pct=round(pct, 8),
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
