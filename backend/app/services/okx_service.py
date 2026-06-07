"""OKX exchange implementation via ccxt."""
import logging
from typing import Optional
import ccxt.async_support as ccxt_async

from .exchange_base import BaseExchangeService, retry_with_backoff

logger = logging.getLogger(__name__)


class OkxService(BaseExchangeService):
    """OKX USD-M Futures exchange wrapper."""

    exchange_id = "okx"

    def __init__(self, api_key: str = "", secret: str = "", testnet: bool = True, hedge_mode: bool = True):
        super().__init__(api_key, secret, testnet, hedge_mode)
        self._exchange: Optional[ccxt_async.okx] = None
        self._passphrase = ""  # OKX requires passphrase

    def set_passphrase(self, passphrase: str):
        self._passphrase = passphrase

    @property
    def exchange(self) -> ccxt_async.okx:
        if self._exchange is None:
            self._exchange = self._create_exchange()
        return self._exchange

    def _create_exchange(self):
        from ..config import settings

        config = {
            "apiKey": self.api_key,
            "secret": self.secret,
            "password": self._passphrase,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
        if settings.http_proxy:
            config["proxies"] = {"http": settings.http_proxy, "https": settings.http_proxy}

        ex = ccxt_async.okx(config)
        if self.testnet:
            ex.set_sandbox_mode(True)
        return ex

    # ---- Symbol formatting ----

    def _format_symbol(self, symbol: str) -> str:
        """BTCUSDT -> BTC/USDT:USDT（与 Binance 统一 ccxt 永续写法一致）。"""
        if "/" in symbol:
            if ":USDT" not in symbol and symbol.endswith("/USDT"):
                return f"{symbol}:USDT"
            return symbol
        s = self._norm_sym(symbol)
        if s.endswith("USDT"):
            base = s[:-4]
            return f"{base}/USDT:USDT"
        return symbol

    def _to_okx_inst_id(self, symbol: str) -> str:
        """BTCUSDT -> BTC-USDT-SWAP"""
        s = self._norm_sym(symbol)
        if s.endswith("USDT"):
            base = s[:-4]
            return f"{base}-USDT-SWAP"
        return s

    # ---- Market Data ----

    async def fetch_balance(self) -> dict:
        return await retry_with_backoff(
            "okx.fetch_balance",
            lambda: self.exchange.fetch_balance(),
        )

    async def fetch_ticker(self, symbol: str) -> dict:
        return await retry_with_backoff(
            "okx.fetch_ticker",
            lambda: self.exchange.fetch_ticker(self._format_symbol(symbol)),
        )

    async def fetch_tickers(self, symbols: Optional[list[str]] = None) -> dict:
        formatted = [self._format_symbol(s) for s in symbols] if symbols else None
        return await retry_with_backoff(
            "okx.fetch_tickers",
            lambda: self.exchange.fetch_tickers(formatted),
        )

    async def fetch_positions(self, symbols: Optional[list[str]] = None) -> list[dict]:
        """与 BinanceService 相同策略：指定 symbols 失败时 load_markets 后重试，再退回全量并按 norm 过滤。"""
        if not symbols:
            raw_all = await retry_with_backoff(
                "okx.fetch_positions(all)",
                lambda: self.exchange.fetch_positions(None),
            )
            return list(raw_all or [])
        formatted = [self._format_symbol(s) for s in symbols]
        try:
            raw_pos = await retry_with_backoff(
                "okx.fetch_positions",
                lambda: self.exchange.fetch_positions(formatted),
            )
            return list(raw_pos or [])
        except Exception as e:
            msg = str(e).lower()
            if "does not have market symbol" in msg or "marketsymbol" in msg or "invalid symbol" in msg:
                try:
                    await self.exchange.load_markets(True)
                    raw2 = await self.exchange.fetch_positions(formatted)
                    return list(raw2 or [])
                except Exception as e2:
                    logger.debug("okx.fetch_positions(%s) after load_markets: %s", symbols, e2)
                logger.warning("okx.fetch_positions symbol missing, fallback all: %s", symbols)
                raw = await self.exchange.fetch_positions(None)
                want = {self._norm_sym(s) for s in symbols}
                return [p for p in (raw or []) if self._norm_sym(p.get("symbol") or "") in want]
            raise

    async def fetch_leverage(self, symbol: str) -> float:
        try:
            positions = await self.fetch_positions([symbol])
            formatted = self._format_symbol(symbol)
            want = self._norm_sym(symbol)
            for p in positions:
                if self._norm_sym(str(p.get("symbol") or "")) != want and p.get("symbol") != formatted:
                    continue
                return float(p.get("leverage", 20))
        except Exception:
            pass
        return 20.0

    async def fetch_order(self, order_id: str, symbol: str) -> dict:
        return await retry_with_backoff(
            "okx.fetch_order",
            lambda: self.exchange.fetch_order(order_id, self._format_symbol(symbol)),
        )

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> list[dict]:
        fmt = self._format_symbol(symbol) if symbol else None
        return await retry_with_backoff(
            "okx.fetch_open_orders",
            lambda: self.exchange.fetch_open_orders(fmt),
        )

    async def fetch_open_algo_orders(self, symbol: str) -> list:
        """OKX 条件单（含止损）在 open orders 的 stop 通道。"""
        formatted = self._format_symbol(symbol)
        try:
            rows = await retry_with_backoff(
                "okx.fetch_open_orders(stop)",
                lambda: self.exchange.fetch_open_orders(formatted, params={"stop": True}),
            )
            return rows or []
        except Exception as e:
            logger.debug("OKX fetch_open_algo_orders %s: %s", symbol, e)
            return []

    async def fetch_algo_order(self, algo_id: str, symbol: str) -> dict:
        formatted = self._format_symbol(symbol)
        try:
            return await retry_with_backoff(
                "okx.fetch_algo_order",
                lambda: self.exchange.fetch_order(
                    algo_id, formatted, params={"algoId": algo_id},
                ),
            )
        except Exception:
            return await retry_with_backoff(
                "okx.fetch_algo_order(stop)",
                lambda: self.exchange.fetch_order(
                    algo_id, formatted, params={"stop": True, "algoId": algo_id},
                ),
            )

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        return await retry_with_backoff(
            "okx.fetch_order_book",
            lambda: self.exchange.fetch_order_book(self._format_symbol(symbol), limit),
        )

    async def fetch_markets(self) -> list:
        raw = await retry_with_backoff(
            "okx.fetch_markets",
            lambda: self.exchange.load_markets(True),
        )
        if isinstance(raw, dict):
            return list(raw.values())
        return raw or []

    async def _market_min_amount(self, symbol: str) -> float:
        formatted = self._format_symbol(symbol)
        try:
            await self.exchange.load_markets()
            m = self.exchange.market(formatted)
            lim = (m.get("limits") or {}).get("amount") or {}
            mn = lim.get("min")
            if mn is not None:
                mnf = float(mn)
                if mnf > 0:
                    return float(self.exchange.amount_to_precision(formatted, mnf))
        except Exception as e:
            logger.warning("OKX _market_min_amount(%s): %s", symbol, e)
        return 0.0

    async def min_order_amount(self, symbol: str) -> float:
        return await self._market_min_amount(symbol)

    async def normalize_order_amount(self, symbol: str, amount: float) -> float:
        if amount <= 0:
            return 0.0
        formatted = self._format_symbol(symbol)
        try:
            await self.exchange.load_markets()
            m = self.exchange.market(formatted)
            step = float(self.exchange.amount_to_precision(formatted, amount))
            mnf = await self._market_min_amount(symbol)
            if mnf > 0:
                step = float(self.exchange.amount_to_precision(formatted, max(step, mnf)))
            return max(0.0, step)
        except Exception as e:
            logger.warning("OKX normalize_order_amount(%s): %s", symbol, e)
            return float(amount)

    async def quote_usdt_to_order_amount(self, symbol: str, quote_usdt: float, ref_price: float) -> float:
        """OKX 永续：amount 为合约张数 sz；名义 U ≈ 张数 × 价 × 每张基础数量(contractSize/ctVal)。"""
        if quote_usdt <= 0 or ref_price <= 0:
            return 0.0
        formatted = self._format_symbol(symbol)
        try:
            await self.exchange.load_markets()
            m = self.exchange.market(formatted)
            if not m.get("contract"):
                return float(quote_usdt) / float(ref_price)
            cs = float(m.get("contractSize") or 0)
            info = m.get("info") or {}
            if isinstance(info, dict) and cs <= 0:
                try:
                    cs = float(info.get("ctVal") or 0)
                except (TypeError, ValueError):
                    cs = 0.0
            if cs <= 0:
                return float(quote_usdt) / float(ref_price)
            per_contract = float(ref_price) * float(cs)
            if per_contract <= 0:
                return 0.0
            return float(quote_usdt) / per_contract
        except Exception as e:
            logger.warning("OKX quote_usdt_to_order_amount(%s): %s", symbol, e)
            return float(quote_usdt) / float(ref_price)

    async def fetch_price_limits(self, symbol: str) -> tuple[float, float]:
        """OKX 公开接口：返回 (最高买价 buyLmt, 最低卖价 sellLmt)。未知时为 (0, 0)。"""
        inst = self._to_okx_inst_id(symbol)
        try:
            raw = await retry_with_backoff(
                "okx.public_price_limit",
                lambda: self.exchange.publicGetPublicPriceLimit({"instId": inst}),
            )
            rows = (raw or {}).get("data") or []
            if not rows:
                return 0.0, 0.0
            row = rows[0] if isinstance(rows[0], dict) else {}
            buy_lmt = float(row.get("buyLmt") or 0)
            sell_lmt = float(row.get("sellLmt") or 0)
            return buy_lmt, sell_lmt
        except Exception as e:
            logger.debug("OKX fetch_price_limits(%s): %s", symbol, e)
            return 0.0, 0.0

    async def normalize_limit_price(
        self, symbol: str, side: str, price: float,
    ) -> tuple[float, Optional[str]]:
        if price <= 0:
            return 0.0, None
        formatted = self._format_symbol(symbol)
        try:
            await self.exchange.load_markets()
            px = float(self.exchange.price_to_precision(formatted, price))
        except Exception as e:
            logger.debug("OKX price_to_precision(%s): %s", symbol, e)
            px = float(price)

        max_buy, min_sell = await self.fetch_price_limits(symbol)
        side_l = (side or "").lower()
        if side_l == "buy" and max_buy > 0 and px > max_buy:
            try:
                px = float(self.exchange.price_to_precision(formatted, max_buy))
            except Exception:
                px = max_buy
            return px, f"OKX限价带:最高买价={max_buy}"
        if side_l == "sell" and min_sell > 0 and px < min_sell:
            try:
                px = float(self.exchange.price_to_precision(formatted, min_sell))
            except Exception:
                px = min_sell
            return px, f"OKX限价带:最低卖价={min_sell}"
        return px, None

    async def linear_contract_ct_val(self, symbol: str) -> float:
        formatted = self._format_symbol(symbol)
        try:
            await self.exchange.load_markets()
            m = self.exchange.market(formatted)
            cs = float(m.get("contractSize") or 0)
            info = m.get("info") or {}
            if isinstance(info, dict) and cs <= 0:
                try:
                    cs = float(info.get("ctVal") or 0)
                except (TypeError, ValueError):
                    cs = 0.0
            if cs > 0:
                return float(cs)
            if not m.get("contract"):
                return 1.0
        except Exception as e:
            logger.warning("OKX linear_contract_ct_val(%s): %s", symbol, e)
        return 1.0

    # ---- Orders ----

    def _order_param_combos(self, position_side: str, reduce_only: bool) -> list[dict]:
        """与 BinanceService._create_order_with_fallback 相同的组合顺序（hedge：posSide+reduceOnly → posSide → 空）。"""
        pos_side = "long" if position_side.upper() == "LONG" else "short"
        combos: list[dict] = []
        if self.hedge_mode:
            p1: dict = {"posSide": pos_side}
            if reduce_only:
                p1["reduceOnly"] = True
            combos.append(p1)
            if reduce_only:
                combos.append({"posSide": pos_side})
            combos.append({})
        else:
            p0: dict = {}
            if reduce_only:
                p0["reduceOnly"] = True
            combos.append(p0)
            combos.append({})
        return combos

    @staticmethod
    def _okx_combo_error_retryable(err: Exception) -> bool:
        """OKX / ccxt 常见可重试：reduceOnly 冗余、posSide 与持仓模式不匹配等。"""
        err_str = str(err).lower()
        needles = (
            "reduceonly", "reduce only",
            "posside", "pos side", "position side",
            "51169", "51119", "51120", "51121", "51020", "50276",
            "-1106", "-4061",
        )
        return any(n in err_str for n in needles)

    async def _create_order_with_fallback(
        self,
        formatted_symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None,
        reduce_only: bool,
        position_side: str,
        label: str,
        extra_params: dict | None = None,
    ) -> dict:
        combos = self._order_param_combos(position_side, reduce_only)
        last_exc: Exception | None = None
        for idx, combo in enumerate(combos):
            params = dict(extra_params or {})
            params.update(combo)
            try:
                # 空 dict 在 Python 中为假值，不能写成 params or None，否则 ccxt 收到 None 会报 'NoneType' is not iterable
                extra: dict = {
                    "type": order_type,
                    "side": side,
                    "amount": amount,
                    "params": params or {},
                }
                if price is not None:
                    extra["price"] = price
                tag = f"okx.create_{label}_order(combo{idx})" if idx > 0 else f"okx.create_{label}_order"
                return await retry_with_backoff(
                    tag,
                    lambda s=formatted_symbol, e=extra: self.exchange.create_order(symbol=s, **e),
                )
            except Exception as e:
                last_exc = e
                if self._okx_combo_error_retryable(e):
                    logger.debug("OKX order %s combo%d: %s, trying next", label, idx, e)
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    async def create_market_order(
        self, symbol: str, side: str, amount: float,
        reduce_only: bool = False, position_side: str = "LONG",
    ) -> dict:
        formatted = self._format_symbol(symbol)
        return await self._create_order_with_fallback(
            formatted, "market", side, amount, None,
            reduce_only, position_side, "market", None,
        )

    async def create_limit_order(
        self, symbol: str, side: str, amount: float, price: float,
        reduce_only: bool = False, position_side: str = "LONG",
    ) -> dict:
        formatted = self._format_symbol(symbol)
        return await self._create_order_with_fallback(
            formatted, "limit", side, amount, price,
            reduce_only, position_side, "limit", None,
        )

    async def create_stop_loss_order(
        self, symbol: str, side: str, amount: float, stop_price: float,
        reduce_only: bool = True, position_side: str = "LONG",
    ) -> dict:
        """止损市价单：参数组合回退与市价单一致。"""
        formatted = self._format_symbol(symbol)
        extra = {"stopPrice": stop_price, "triggerPrice": stop_price}
        return await self._create_order_with_fallback(
            formatted, "market", side, amount, None,
            reduce_only, position_side, "stop_loss", extra,
        )

    async def cancel_all_pending_orders_for_symbol(
        self, symbol: str, position_side_lower: str | None = None,
    ) -> int:
        """撤销该合约下挂单；可选仅撤销某一持仓腿（双向模式下同币种对向策略并行）。

        position_side_lower 为 ``long`` / ``short`` 时不再调用交易所一键全撤，以免误杀另一腿。
        """
        formatted = self._format_symbol(symbol)
        n = 0
        filt = (position_side_lower or "").strip().lower()
        leg_filter = filt if filt in ("long", "short") else None

        def row_matches(row: dict) -> bool:
            if not BaseExchangeService.open_order_matches_strategy_symbol(row, symbol):
                return False
            if leg_filter is None:
                return True
            leg = BaseExchangeService.open_order_leg_side_lower(row)
            if not leg:
                return False
            return leg == leg_filter

        if leg_filter is None:
            try:
                r = await retry_with_backoff(
                    "okx.cancel_all_orders",
                    lambda: self.exchange.cancel_all_orders(formatted),
                )
                if isinstance(r, list):
                    n += len(r)
                elif r is not None:
                    n += 1
            except Exception as e:
                logger.warning("OKX cancel_all_orders(%s): %s", symbol, e)

        for use_stop in (False, True):
            p: dict = {"stop": True} if use_stop else {}
            try:
                rows = await retry_with_backoff(
                    f"okx.fetch_open_orders(sweep stop={use_stop})",
                    lambda par=dict(p): self.exchange.fetch_open_orders(formatted, params=par),
                )
            except Exception as e:
                logger.debug("OKX fetch_open_orders sweep stop=%s: %s", use_stop, e)
                continue
            for row in rows or []:
                if not row_matches(row):
                    continue
                oid = str(row.get("id") or row.get("orderId") or row.get("algoId") or "")
                if not oid:
                    continue
                try:
                    cparams = {"stop": True} if use_stop else {}
                    await retry_with_backoff(
                        "okx.cancel_order(sweep)",
                        lambda i=oid, cp=dict(cparams): self.exchange.cancel_order(
                            i, formatted, params=cp or {},
                        ),
                    )
                    n += 1
                except Exception as e:
                    logger.debug("OKX cancel_order sweep %s: %s", oid, e)

        return n

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        return await retry_with_backoff(
            "okx.cancel_order",
            lambda: self.exchange.cancel_order(order_id, self._format_symbol(symbol)),
        )

    async def cancel_algo_order(self, algo_id: str, symbol: str) -> dict:
        """撤销 OKX 条件/计划单（止损等）。须走 cancel-algos，即 ccxt 的 params.stop/trigger。"""
        formatted = self._format_symbol(symbol)
        return await retry_with_backoff(
            "okx.cancel_algo_order",
            lambda: self.exchange.cancel_order(algo_id, formatted, params={"stop": True}),
        )

    async def close_position(self, symbol: str, side: str) -> dict:
        formatted = self._format_symbol(symbol)
        positions = await self.fetch_positions([symbol])
        total = 0.0
        for pos in positions:
            if BaseExchangeService.position_row_matches_leg(pos, symbol, side.lower(), formatted):
                total += BaseExchangeService.position_row_contracts_abs(pos)

        if total <= 0:
            logger.warning("OKX close_position: no contracts for %s %s", symbol, side)
            return {}

        total = await self.normalize_order_amount(symbol, total)
        if total <= 0:
            logger.warning("OKX close_position: normalized qty is 0 for %s %s", symbol, side)
            return {}

        close_side = "sell" if side == "long" else "buy"
        position_side = "LONG" if side == "long" else "SHORT"
        return await self.create_market_order(
            symbol, close_side, total,
            reduce_only=True, position_side=position_side,
        )

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage for a symbol on OKX."""
        formatted = self._format_symbol(symbol)
        try:
            await retry_with_backoff(
                "okx.set_leverage",
                lambda: self.exchange.set_leverage(leverage, formatted, params={"mgnMode": "isolated"}),
            )
        except Exception as e:
            try:
                await retry_with_backoff(
                    "okx.set_leverage_cross",
                    lambda: self.exchange.set_leverage(leverage, formatted, params={"mgnMode": "cross"}),
                )
            except Exception as e2:
                logger.warning("OKX set_leverage(%s, %d) failed: %s / %s", symbol, leverage, e, e2)

    # ---- WebSocket ----

    @property
    def ws_exchange(self):
        """与 BinanceService 一致，供调度器 watch_orders 使用；与 watch_tickers 共用同一 WS 实例。"""
        if not hasattr(self, "_ws_exchange") or self._ws_exchange is None:
            self._ws_exchange = self._create_ws_exchange()
        return self._ws_exchange

    async def watch_tickers(self, symbols: Optional[list[str]] = None):
        formatted = [self._format_symbol(s) for s in symbols] if symbols else None
        return await self.ws_exchange.watch_tickers(formatted)

    def _create_ws_exchange(self):
        from ..config import settings
        import ccxt.pro as ccxtpro

        config = {
            "apiKey": self.api_key,
            "secret": self.secret,
            "password": self._passphrase,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
        if settings.http_proxy:
            config["proxies"] = {"http": settings.http_proxy, "https": settings.http_proxy}

        ex = ccxtpro.okx(config)
        if self.testnet:
            ex.set_sandbox_mode(True)
        return ex

    async def close(self):
        if self._exchange:
            try:
                await self._exchange.close()
            except Exception:
                pass
            self._exchange = None
        if hasattr(self, '_ws_exchange') and self._ws_exchange:
            try:
                await self._ws_exchange.close()
            except Exception:
                pass
            self._ws_exchange = None
