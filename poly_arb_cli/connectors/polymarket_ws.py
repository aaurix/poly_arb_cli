"""Polymarket WebSocket feed 与本地状态管理。

本模块提供三部分：

1. `PolymarketStreamState`：在内存中维护订单簿与最近成交；
2. `MarketWsFeed`：订阅 CLOB MARKET channel，实时更新状态；
3. 简单的工具方法，方便套利扫描器优先从本地 state 读取盘口。
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, List, Optional

import websockets

from ..config import Settings
from ..types import Market, OrderBook, OrderBookLevel, TradeEvent


@dataclass
class PolymarketStreamState:
    """Polymarket 行情本地状态。

    Attributes:
        orderbooks: 以 token_id 为键的最新订单簿快照。
        trades_by_condition: 每个 condition_id 最近的成交事件环形缓冲。
        max_trades_per_market: 单市场最多保留的成交数。
    """

    orderbooks: Dict[str, OrderBook] = field(default_factory=dict)
    trades_by_condition: Dict[str, Deque[TradeEvent]] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=200))
    )
    max_trades_per_market: int = 200

    def apply_book_snapshot(self, asset_id: str, bids: Iterable[dict], asks: Iterable[dict]) -> None:
        """根据 MARKET channel 的 book 消息更新指定资产的订单簿。"""
        bid_levels: List[OrderBookLevel] = []
        ask_levels: List[OrderBookLevel] = []
        for entry in bids:
            level = _to_level(entry)
            if level is not None:
                bid_levels.append(level)
        for entry in asks:
            level = _to_level(entry)
            if level is not None:
                ask_levels.append(level)

        # WS 消息已经按照从优到劣的顺序给出，不再排序。
        self.orderbooks[asset_id] = OrderBook(bids=bid_levels, asks=ask_levels)

    def append_last_trade(self, data: dict) -> None:
        """根据 last_trade_price 消息追加一条成交记录。"""
        try:
            asset_id = str(data.get("asset_id") or "")
            condition_id = str(data.get("market") or "")
            side = str(data.get("side") or "")
            size = float(data.get("size") or 0.0)
            price = float(data.get("price") or 0.0)
            ts_raw = data.get("timestamp")
            # 文档中 timestamp 为毫秒字符串，这里统一转换为秒。
            ts = int(int(ts_raw) / 1000) if ts_raw is not None else 0
        except Exception:
            return

        notional = size * price
        trade = TradeEvent(
            condition_id=condition_id,
            token_id=asset_id,
            side=side,
            size=size,
            price=price,
            notional=notional,
            timestamp=ts,
            title=condition_id,
            outcome=None,
        )
        buf = self.trades_by_condition[condition_id]
        if buf.maxlen != self.max_trades_per_market:
            buf = deque(buf, maxlen=self.max_trades_per_market)
            self.trades_by_condition[condition_id] = buf
        buf.append(trade)

    def get_orderbook_for_market(self, market: Market, side: str = "yes") -> Optional[OrderBook]:
        """根据 Market 对象与 YES/NO 返回对应 token 的订单簿。"""
        token_id = market.yes_token_id if side.lower() == "yes" else market.no_token_id
        if not token_id:
            return None
        return self.orderbooks.get(token_id)

    def get_last_trades(self, condition_id: str, limit: int = 50) -> List[TradeEvent]:
        """获取某个 condition 最近的成交列表。"""
        buf = self.trades_by_condition.get(condition_id)
        if not buf:
            return []
        return list(buf)[-limit:]


class MarketWsFeed:
    """Polymarket MARKET WebSocket feed。

    订阅指定 token_ids 的 MARKET channel，持续维护本地订单簿与成交。
    """

    def __init__(self, settings: Settings, state: PolymarketStreamState, asset_ids: Iterable[str]):
        self.settings = settings
        self.state = state
        self.asset_ids = [a for a in asset_ids if a]
        # 官方文档推荐的 MARKET channel 地址
        self.ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        self._stop = False

    async def run(self) -> None:
        """启动 WS 连接并持续监听，内部包含简单重连策略。"""
        backoff = 1
        while not self._stop:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as ws:
                    # 订阅指定资产
                    sub_msg = {
                        "type": "MARKET",
                        # 文档使用 `assets_ids` 字段，示例中为 asset ids 列表
                        "assets_ids": self.asset_ids,
                    }
                    await ws.send(json.dumps(sub_msg))
                    backoff = 1  # 连上后重置退避

                    async for raw in ws:
                        # 服务端有时返回单个对象，有时返回数组；统一归一化为列表处理
                        try:
                            parsed = json.loads(raw)
                        except Exception:
                            continue

                        items = parsed if isinstance(parsed, list) else [parsed]

                        for data in items:
                            if not isinstance(data, dict):
                                continue

                            event_type = data.get("event_type")

                            # book 快照：有时带 event_type=book，有时仅包含 bids/asks 字段
                            if event_type == "book" or any(k in data for k in ("bids", "asks", "buys", "sells")):
                                asset_id = str(data.get("asset_id") or "")
                                if not asset_id:
                                    continue
                                bids = data.get("bids") or data.get("buys") or []
                                asks = data.get("asks") or data.get("sells") or []
                                self.state.apply_book_snapshot(asset_id, bids, asks)
                                continue

                            # 成交事件：文档中为 event_type=last_trade_price
                            if event_type == "last_trade_price":
                                self.state.append_last_trade(data)
                                continue

                            # 其他 event_type（price_change / tick_size_change）当前忽略
            except Exception:
                # 简单指数退避重连
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def stop(self) -> None:
        """请求结束 WS 循环。"""

        self._stop = True


def _to_level(entry: object) -> Optional[OrderBookLevel]:
    """将 WS 返回的订单簿条目转换为 OrderBookLevel。"""
    if isinstance(entry, dict):
        price = entry.get("price")
        size = entry.get("size")
        if price is None or size is None:
            return None
        try:
            return OrderBookLevel(price=float(price), size=float(size))
        except Exception:
            return None
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        try:
            return OrderBookLevel(price=float(entry[0]), size=float(entry[1]))
        except Exception:
            return None
    return None
