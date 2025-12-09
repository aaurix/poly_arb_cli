"""基于 ccxt 的永续/期货只读客户端。

仅用于行情、资金费率查询，不包含任何交易功能。
"""

from __future__ import annotations

import asyncio
import math
from typing import Optional

from ..config import Settings


class PerpClient:
    """封装 ccxt 交易所实例，支持在扫描阶段获取标的价格与资金费率。

    初始化不强制要求 API Key；若缺失 ccxt 依赖，会在首次调用时抛出
    友好的错误提示，避免 CLI 无响应。
    """

    def __init__(self, settings: Settings, exchange_id: Optional[str] = None):
        self.settings = settings
        self.exchange_id = (exchange_id or settings.perp_exchange).lower()
        self._import_error: Optional[Exception] = None
        self._exchange = None

        try:
            import ccxt  # type: ignore
        except Exception as exc:  # noqa: BLE001
            self._import_error = exc
            return

        if not hasattr(ccxt, self.exchange_id):
            self._import_error = RuntimeError(f"ccxt exchange '{self.exchange_id}' not found")
            return

        exchange_cls = getattr(ccxt, self.exchange_id)
        self._exchange = exchange_cls(
            {
                "apiKey": settings.perp_api_key or "",
                "secret": settings.perp_api_secret or "",
                "enableRateLimit": True,
            }
        )

        # Binance 系列可切换沙箱；其他交易所直接忽略。
        if settings.perp_testnet and hasattr(self._exchange, "set_sandbox_mode"):
            try:
                self._exchange.set_sandbox_mode(True)
            except Exception:
                pass

    async def fetch_mark_price(self, symbol: str) -> float:
        """读取标的的现价或标记价格。

        Args:
            symbol: ccxt 符号（如 ``BTC/USDT:USDT``）。

        Returns:
            最新价格，若无法获取则抛出异常。
        """
        exchange = self._require_exchange()
        ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)
        price = ticker.get("markPrice") or ticker.get("last") or ticker.get("close")
        if price is None:
            raise RuntimeError(f"mark price unavailable for {symbol}")
        return float(price)

    async def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """获取当前资金费率（若交易所支持）。

        Args:
            symbol: ccxt 符号。

        Returns:
            资金费率（周期化，通常为 8h），若接口不可用则返回 None。
        """
        exchange = self._require_exchange()
        try:
            rate = await asyncio.to_thread(exchange.fetch_funding_rate, symbol)
        except Exception:
            return None
        value = rate.get("fundingRate") if isinstance(rate, dict) else None
        return float(value) if value is not None else None

    async def close(self) -> None:
        """关闭 ccxt 连接（如适用）。"""
        if self._exchange and hasattr(self._exchange, "close"):
            try:
                await asyncio.to_thread(self._exchange.close)
            except Exception:
                pass

    async def fetch_realized_vol(
        self,
        symbol: str,
        *,
        timeframe: str = "1h",
        lookback_days: int = 7,
        max_candles: int = 500,
    ) -> Optional[float]:
        """基于 OHLCV 计算历史年化波动率。

        Args:
            symbol: ccxt 符号。
            timeframe: K 线周期（如 1h/1d）。
            lookback_days: 向前回溯天数。
            max_candles: 最多抓取的 K 线数量。

        Returns:
            年化波动率，缺少数据或解析失败返回 None。
        """
        exchange = self._require_exchange()
        seconds_per_bar = _timeframe_seconds(timeframe)
        if seconds_per_bar <= 0:
            return None
        est_limit = int((lookback_days * 24 * 3600) / seconds_per_bar) + 1
        limit = max(2, min(max_candles, est_limit))
        try:
            ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)
        except Exception:
            return None
        closes = [row[4] for row in ohlcv if len(row) >= 5 and row[4]]
        if len(closes) < 2:
            return None
        returns = []
        for prev, curr in zip(closes[:-1], closes[1:]):
            if prev <= 0 or curr <= 0:
                continue
            returns.append(math.log(curr / prev))
        if len(returns) < 2:
            return None
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        daily_factor = 365 * 24 * 3600 / seconds_per_bar
        return math.sqrt(var * daily_factor)

    def _require_exchange(self):
        """检查 ccxt 初始化状态，未就绪时抛出带上下文的错误。

        Returns:
            已初始化的 ccxt 交易所实例。

        Raises:
            RuntimeError: 当未安装 ccxt 或 exchange id 无效时抛出。
        """
        if self._exchange:
            return self._exchange
        if self._import_error:
            raise RuntimeError(
                "ccxt not available; install it or adjust pyproject optional deps."
            ) from self._import_error
        raise RuntimeError(f"ccxt exchange {self.exchange_id} not initialized")


def _timeframe_seconds(tf: str) -> int:
    """将 ccxt 风格 timeframe 转换为秒。"""
    if not tf:
        return 0
    tf = tf.strip().lower()
    unit = tf[-1]
    value_str = tf[:-1] if unit.isalpha() else tf
    try:
        value = int(value_str)
    except Exception:
        return 0
    if unit == "s":
        return value
    if unit == "m":
        return value * 60
    if unit == "h":
        return value * 3600
    if unit == "d":
        return value * 86400
    if unit == "w":
        return value * 604800
    return 0
