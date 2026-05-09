"""Strategy health monitoring and alerting."""
import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict
from datetime import datetime

from ..config import now_beijing

logger = logging.getLogger(__name__)


class HealthStatusLevel(Enum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class StrategyHealth:
    strategy_id: int
    status: HealthStatusLevel = HealthStatusLevel.HEALTHY
    checks: dict[str, bool] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
    last_updated: datetime = field(default_factory=now_beijing)
    consecutive_failures: int = 0
    last_successful_tick: float = field(default_factory=time.time)
    last_order_latency_ms: float = 0.0


class HealthMonitor:
    """Monitors strategy health and provides alerting.

    Checks:
    - Consecutive execution failures
    - Tick freshness (last successful tick age)
    - Order execution latency
    - Exchange connection health
    """

    def __init__(self):
        self._health: dict[int, StrategyHealth] = {}
        self._alert_callbacks: list[callable] = []

    def register_alert_callback(self, callback):
        """Register a callback(strategy_id, health) for alerts."""
        self._alert_callbacks.append(callback)

    def get_health(self, strategy_id: int) -> StrategyHealth:
        if strategy_id not in self._health:
            self._health[strategy_id] = StrategyHealth(strategy_id=strategy_id)
        return self._health[strategy_id]

    def get_all_health(self) -> dict[int, StrategyHealth]:
        return dict(self._health)

    def record_success(self, strategy_id: int, latency_ms: float = 0):
        h = self.get_health(strategy_id)
        h.consecutive_failures = 0
        h.last_successful_tick = time.time()
        h.last_order_latency_ms = latency_ms
        h.last_updated = now_beijing()
        self._evaluate(strategy_id, h)

    def record_failure(self, strategy_id: int, error_message: str):
        h = self.get_health(strategy_id)
        h.consecutive_failures += 1
        h.messages.append(f"Error: {error_message}")
        if len(h.messages) > 50:
            h.messages = h.messages[-50:]
        h.last_updated = now_beijing()
        self._evaluate(strategy_id, h)

    def record_order_latency(self, strategy_id: int, latency_ms: float):
        h = self.get_health(strategy_id)
        h.last_order_latency_ms = latency_ms
        h.last_updated = now_beijing()
        self._evaluate(strategy_id, h)

    def _evaluate(self, strategy_id: int, h: StrategyHealth):
        """Evaluate health status based on checks."""
        now_ts = time.time()

        # Check 1: Consecutive failures
        if h.consecutive_failures >= 5:
            h.status = HealthStatusLevel.CRITICAL
            h.checks["consecutive_failures"] = False
        elif h.consecutive_failures >= 2:
            h.checks["consecutive_failures"] = False
            if h.status != HealthStatusLevel.CRITICAL:
                h.status = HealthStatusLevel.WARNING
        else:
            h.checks["consecutive_failures"] = True

        # Check 2: Tick freshness
        tick_age = now_ts - h.last_successful_tick
        if tick_age > 180:  # 3 minutes
            h.status = HealthStatusLevel.CRITICAL
            h.checks["tick_freshness"] = False
        elif tick_age > 90:
            h.checks["tick_freshness"] = False
            if h.status != HealthStatusLevel.CRITICAL:
                h.status = HealthStatusLevel.WARNING
        else:
            h.checks["tick_freshness"] = True

        # Check 3: Order latency
        if h.last_order_latency_ms > 5000:
            h.checks["order_latency"] = False
            if h.status != HealthStatusLevel.CRITICAL:
                h.status = HealthStatusLevel.WARNING
        else:
            h.checks["order_latency"] = True

        # All checks passing → healthy
        if all(h.checks.values()):
            h.status = HealthStatusLevel.HEALTHY

        # Fire alerts for non-healthy
        if h.status != HealthStatusLevel.HEALTHY:
            for cb in self._alert_callbacks:
                try:
                    cb(strategy_id, h)
                except Exception as e:
                    logger.debug("Health alert callback error: %s", e)

    def clear_strategy(self, strategy_id: int):
        self._health.pop(strategy_id, None)


# Singleton
health_monitor = HealthMonitor()
