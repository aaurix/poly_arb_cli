"""基于 ccxt 的永续/期货只读客户端。

仅用于行情、资金费率查询，不包含任何交易功能。
"""

from __future__ import annotations

import asyncio
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
