"""Opinion 相关 API 与 SDK 客户端封装。"""

from __future__ import annotations

import asyncio
from typing import Iterable, List, Optional

import httpx

from ..config import Settings
from ..types import Market, OrderBook, OrderBookLevel, Platform, Position, PriceQuote


class OpinionClient:
    """Opinion 数据客户端。

    读取部分优先通过 HTTP Open API 完成（只需 API Key），
    交易与账户查询依赖官方 CLOB SDK（需要私钥）。
    在未配置任何凭证时，读取接口会优雅降级为返回空结果。
    """

    def __init__(self, settings: Settings, base_url: Optional[str] = None):
        self.settings = settings
        self.base_url = base_url or settings.opinion_host
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)
        self._sdk_client = None
        self._sdk_import_error: Optional[Exception] = None
        self._topic_status_filter = None
        self._order_side_enum = None
        self._place_order_input = None

        # 仅在同时存在 API Key 与私钥时初始化 SDK，用于交易与账户操作。
        if settings.opinion_api_key and settings.opinion_private_key:
            try:
                from opinion_clob_sdk import Client, TopicStatusFilter
                from opinion_clob_sdk.models import OrderSide, PlaceOrderDataInput
            except Exception as exc:  # noqa: BLE001
                self._sdk_import_error = exc
            else:
                self._topic_status_filter = TopicStatusFilter
                self._order_side_enum = OrderSide
                self._place_order_input = PlaceOrderDataInput
                self._sdk_client = Client(
                    host=self.base_url,
                    apikey=settings.opinion_api_key,
                    private_key=settings.opinion_private_key,
                )

    async def list_active_markets(self, limit: int = 50) -> List[Market]:
        """返回 Opinion 当前激活的市场列表。

        优先使用 Open API `/openapi/market`，仅依赖 API Key。
        当 Open API 不可用时，回退到 CLOB SDK。

        Args:
            limit: 返回的最大市场数量。

        Returns:
            `Market` 数据类列表；当既没有 API Key 也没有 SDK 时返回空列表。
        """
        # --- 首选：Open API ---
        if self.settings.opinion_api_key:
            try:
                # Open API 文档：GET /openapi/market
                # page: 页码，size: 每页数量（最大 20），status: activated，marketType: 0（二元）
                page = 1
                size = min(max(limit, 1), 20)
                collected: List[Market] = []
                headers = {"apikey": self.settings.opinion_api_key}

                while len(collected) < limit:
                    params = {
                        "page": page,
                        "size": size,
                        "status": "activated",
                        "marketType": 0,
                    }
                    resp = await self._http.get("/openapi/market", params=params, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    result = data.get("result") if isinstance(data, dict) else None
                    markets_raw = (result or {}).get("list") or []
                    if not markets_raw:
                        break

                    for mk in markets_raw:
                        market_id = mk.get("marketId")
                        title = mk.get("marketTitle") or str(market_id)
                        yes_token = mk.get("yesTokenId")
                        no_token = mk.get("noTokenId")
                        collected.append(
                            Market(
                                platform=Platform.OPINION,
                                market_id=str(market_id),
                                title=str(title),
                                yes_token_id=str(yes_token) if yes_token else None,
                                no_token_id=str(no_token) if no_token else None,
                            )
                        )
                        if len(collected) >= limit:
                            break

                    if len(markets_raw) < size:
                        break
                    page += 1

                if collected:
                    return collected[:limit]
            except Exception:
                # Open API 调用失败时静默回退到 SDK
                pass

        # --- 回退：CLOB SDK ---
        if self._sdk_client:
            status_filter = self._topic_status_filter.ACTIVATED if self._topic_status_filter else None
            markets = await asyncio.to_thread(self._sdk_client.get_markets, status=status_filter, limit=limit)

            results: List[Market] = []
            for mk in markets:
                market_id = _get(mk, "topic_id") or _get(mk, "market_id") or _get(mk, "id") or ""
                title = _get(mk, "title") or _get(mk, "name") or market_id
                yes_token = _get(mk, "yes_token_id") or _get(mk, "yesTokenId") or _get(mk, "token_yes")
                no_token = _get(mk, "no_token_id") or _get(mk, "noTokenId") or _get(mk, "token_no")
                results.append(
                    Market(
                        platform=Platform.OPINION,
                        market_id=str(market_id),
                        title=str(title),
                        yes_token_id=str(yes_token) if yes_token else None,
                        no_token_id=str(no_token) if no_token else None,
                    )
                )
            return results

        # 无 API Key 且无 SDK 时，返回空列表以保持上层逻辑健壮。
        return []

    async def get_best_prices(self, market: Market) -> PriceQuote:
        """基于盘口返回市场 YES/NO 的最优价格。

        Args:
            market: 目标市场。

        Returns:
            若既未配置 Open API Key 也未配置 SDK，则返回价格为 1、
            流动性为 0 的占位值，用于让扫描器自动跳过。
        """
        if not self.settings.opinion_api_key and not self._sdk_client:
            # 完全未配置 Opinion，返回中性价格避免干扰套利逻辑。
            return PriceQuote(yes_price=1.0, no_price=1.0, yes_liquidity=0.0, no_liquidity=0.0)

        yes_book = await self.get_orderbook(market, side="yes")
        no_book = await self.get_orderbook(market, side="no")
        yes_price = _best_price(yes_book, side="buy")
        no_price = _best_price(no_book, side="buy")
        return PriceQuote(
            yes_price=yes_price,
            no_price=no_price,
            yes_liquidity=_liquidity(yes_book),
            no_liquidity=_liquidity(no_book),
        )

    async def get_orderbook(self, market: Market, side: str = "yes") -> OrderBook:
        """查询 Opinion 某个市场的盘口。

        Args:
            market: 目标市场。
            side: YES 或 NO。

        Returns:
            规范化后的 `OrderBook`；缺少 token id 或凭证时返回空盘口。
        """
        token_id = market.yes_token_id if side.lower() == "yes" else market.no_token_id
        if not token_id:
            return OrderBook(bids=[], asks=[])

        # --- 首选：Open API `/openapi/token/orderbook` ---
        if self.settings.opinion_api_key:
            try:
                headers = {"apikey": self.settings.opinion_api_key}
                resp = await self._http.get(
                    "/openapi/token/orderbook",
                    params={"token_id": token_id},
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                result = data.get("result") if isinstance(data, dict) else None
                bids_raw = (result or {}).get("bids") or []
                asks_raw = (result or {}).get("asks") or []
            except Exception:
                bids_raw, asks_raw = [], []
        else:
            bids_raw, asks_raw = [], []

        # 如果 Open API 没有返回有效数据，则尝试回退到 SDK。
        if (not bids_raw and not asks_raw) and self._sdk_client:
            raw = await asyncio.to_thread(self._sdk_client.get_orderbook, token_id=token_id)
            bids_raw = _get(raw, "bids") or []
            asks_raw = _get(raw, "asks") or []

        bids = [_to_level(entry) for entry in bids_raw if _to_level(entry) is not None]
        asks = [_to_level(entry) for entry in asks_raw if _to_level(entry) is not None]
        return OrderBook(bids=bids, asks=asks)

    async def place_order(self, market: Market, side: str, price: float, size: float) -> str:
        """通过 Opinion CLOB SDK 提交订单。

        Args:
            market: 目标市场。
            side: YES/NO 或 BUY/SELL。
            price: 报价。
            size: 数量。

        Returns:
            创建成功的订单 ID。
        """
        client = self._require_sdk()
        if not self._place_order_input or not self._order_side_enum:
            raise RuntimeError("Opinion SDK enums unavailable; check installed version.")

        token_id = market.yes_token_id if side.lower() == "yes" else market.no_token_id
        if not token_id:
            raise ValueError("Token id missing for market; cannot place order.")

        order_input = self._place_order_input(
            token_id=token_id,
            side=self._order_side_enum.BUY if side.lower() in {"yes", "buy"} else self._order_side_enum.SELL,
            price=price,
            size=size,
        )
        order_response = await asyncio.to_thread(client.place_order, order_input)
        return str(order_response.get("order_id") if isinstance(order_response, dict) else order_response)

    async def cancel_order(self, order_id: str) -> bool:
        client = self._require_sdk()
        try:
            await asyncio.to_thread(client.cancel_order, order_id)
            return True
        except Exception:
            return False

    async def get_balances(self) -> list[Position]:
        client = self._require_sdk()
        try:
            raw = await asyncio.to_thread(client.get_my_balances)
        except Exception:
            return []
        positions: list[Position] = []
        if isinstance(raw, dict):
            for token, bal in raw.items():
                try:
                    balance = float(bal.get("balance") or bal.get("amount") or bal)
                except Exception:
                    continue
                positions.append(
                    Position(
                        platform=Platform.OPINION,
                        token_id=str(token),
                        symbol=str(token),
                        balance=balance,
                    )
                )
        return positions

    def _require_sdk(self):
        if self._sdk_client:
            return self._sdk_client
        if self._sdk_import_error:
            raise RuntimeError(
                "opinion-clob-sdk not available; run `poetry install` with network access."
            ) from self._sdk_import_error
        raise RuntimeError("Opinion SDK client not initialized.")

    async def close(self) -> None:
        """关闭 Opinion HTTP 客户端。"""
        await self._http.aclose()


def _lookup(entries: Iterable[dict[str, object]], market_id: str) -> dict[str, object]:
    """在市场列表中查找给定 ID 的元素。"""
    for entry in entries:
        if entry.get("market_id") == market_id:
            return entry
    raise KeyError(f"Unknown market_id: {market_id}")


def _get(obj: object, key: str) -> Optional[object]:
    """从任意 SDK 对象中尝试提取字段或属性。"""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _to_level(entry: object) -> Optional[OrderBookLevel]:
    """将 Opinion 盘口返回的任意条目统一转换为 OrderBookLevel。"""
    # SDK OrderSummary 对象：有 price/size 属性。
    if not isinstance(entry, (dict, list, tuple)):
        price = getattr(entry, "price", None)
        size = getattr(entry, "size", None) or getattr(entry, "quantity", None)
        if price is not None and size is not None:
            return OrderBookLevel(price=float(price), size=float(size))

    if isinstance(entry, dict):
        price = entry.get("price")
        size = entry.get("size") or entry.get("quantity")
        if price is None or size is None:
            return None
        return OrderBookLevel(price=float(price), size=float(size))

    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        return OrderBookLevel(price=float(entry[0]), size=float(entry[1]))

    return None


def _liquidity(book: OrderBook) -> float:
    """估算盘口前五档的总流动性。"""
    return sum(level.size for level in book.asks[:5]) + sum(level.size for level in book.bids[:5])


def _best_price(orderbook: OrderBook, side: str) -> float:
    """根据方向返回盘口最优价。"""
    level = orderbook.best_ask() if side == "buy" else orderbook.best_bid()
    return float(level.price) if level else 1.0
