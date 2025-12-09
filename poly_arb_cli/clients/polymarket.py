"""Polymarket 相关 API 客户端封装。"""

from __future__ import annotations

import asyncio
import json
from typing import Iterable, List, Optional

import httpx

from ..config import Settings
from ..types import Market, OrderBook, OrderBookLevel, Platform, Position, PriceQuote, Tag, TradeEvent


class PolymarketClient:
    """Polymarket 数据客户端。

    使用 Gamma API 获取市场元数据，使用 CLOB 客户端查询盘口与价格。
    目前仅实现读取能力，交易相关接口会抛出异常。
    """

    def __init__(self, settings: Settings, base_url: Optional[str] = None):
        self.settings = settings
        self.base_url = base_url or settings.polymarket_base_url
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)
        self._data_http = httpx.AsyncClient(base_url=settings.polymarket_data_url, timeout=10.0)

        self._clob_client = None
        self._clob_import_error: Optional[Exception] = None
        try:
            # Level 0 CLOB client: public endpoints (e.g. orderbook) require only host.
            from py_clob_client.client import ClobClient  # type: ignore
        except Exception as exc:  # noqa: BLE001
            self._clob_import_error = exc
        else:
            # 初始化 CLOB 客户端；如已配置 API 凭证则一并注入，便于后续使用 L2 接口。
            self._clob_client = ClobClient(host=settings.polymarket_clob_url)
            try:
                # 优先使用显式配置的 API 凭证
                if (
                    settings.polymkt_clob_api_key
                    and settings.polymkt_clob_api_secret
                    and settings.polygon_clob_api_passphrase
                ):
                    self._clob_client.set_api_creds(
                        {
                            "apiKey": settings.polymkt_clob_api_key,
                            "secret": settings.polymkt_clob_api_secret,
                            "passphrase": settings.polygon_clob_api_passphrase,
                        }
                    )
                # 否则尝试通过私钥自动推导（官方 SDK 推荐方式）
                elif settings.polymarket_private_key:
                    self._clob_client.set_api_creds(self._clob_client.create_or_derive_api_creds())
            except Exception:
                # API 凭证注入失败不影响只读接口使用，具体 L2 调用会在运行时报错。
                pass

    async def list_active_markets(self, limit: int = 50, *, tag_id: Optional[str] = None) -> List[Market]:
        """获取当前可交易的 Polymarket 市场列表。

        Args:
            limit: 返回的最大市场数量。
            tag_id: 可选 tag ID，仅返回该标签下的市场。

        Returns:
            按 Gamma API 返回顺序排好的 `Market` 列表。
        """
        params = {
            "active": True,
            "closed": False,
            "archived": False,
            "limit": limit,
            "enableOrderBook": True,
        }
        if tag_id:
            params["tag_id"] = tag_id
        resp = await self._http.get("/markets", params=params)
        resp.raise_for_status()
        payload = resp.json()
        markets_raw = payload if isinstance(payload, list) else []

        results: List[Market] = []
        for mk in markets_raw[:limit]:
            condition_id = mk.get("conditionId")
            market_id = mk.get("id") or condition_id or mk.get("marketHash") or mk.get("_id")
            title = mk.get("question") or mk.get("title") or mk.get("name") or str(market_id)
            # 分类与标签字段：Gamma 通常提供 `category` 与 `tags`。
            raw_category = mk.get("category")
            raw_tags = mk.get("tags") or []
            tags: list[str] = []
            if isinstance(raw_tags, list):
                tags = [str(t) for t in raw_tags if t is not None]
            # 若未显式提供 category，则使用首个 tag 作为粗粒度分类。
            category = str(raw_category) if raw_category else (tags[0] if tags else None)
            # 成交量与流动性字段（采用 24 小时 CLOB 成交量与当前 CLOB 流动性）
            volume_24h = (
                mk.get("volume24hrClob")
                or mk.get("volume24hr")
                or mk.get("volume24hrclob")
                or mk.get("volume24HrClob")
            )
            liquidity = mk.get("liquidityClob") or mk.get("liquidityNum") or mk.get("liquidity")
            try:
                vol_val = float(volume_24h) if volume_24h is not None else None
            except Exception:
                vol_val = None
            try:
                liq_val = float(liquidity) if liquidity is not None else None
            except Exception:
                liq_val = None
            # 事件结束/结算时间：不同版本 Gamma 可能使用 endDate / end_time / closeDate 等字段。
            end_date = _parse_market_end_date(mk)
            # clobTokenIds is a stringified list in Gamma; parse if present.
            yes_token = None
            no_token = None
            clob_token_ids = mk.get("clobTokenIds")
            token_ids: Optional[List[str]] = None
            if isinstance(clob_token_ids, str):
                try:
                    token_ids = json.loads(clob_token_ids)
                except Exception:
                    token_ids = None
            elif isinstance(clob_token_ids, list):
                token_ids = clob_token_ids
            if token_ids and len(token_ids) >= 2:
                yes_token = str(token_ids[0])
                no_token = str(token_ids[1])

            results.append(
                Market(
                    platform=Platform.POLYMARKET,
                    market_id=str(market_id),
                    title=str(title),
                    condition_id=str(condition_id) if condition_id else None,
                    end_date=end_date,
                    category=category,
                    volume=vol_val,
                    liquidity=liq_val,
                    yes_token_id=str(yes_token) if yes_token else None,
                    no_token_id=str(no_token) if no_token else None,
                    tags=tags or None,
                )
            )
        return results

    async def get_best_prices(self, market: Market) -> PriceQuote:
        """基于 CLOB 盘口计算给定市场 YES/NO 最优价格。

        Args:
            market: 目标市场对象，需包含 clob token id。

        Returns:
            汇总 YES/NO 最优买价与近端流动性的 `PriceQuote`。
        """
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
        """获取指定市场一侧合约（YES/NO）的盘口信息。

        Args:
            market: 目标市场。
            side: 选择 YES 或 NO，对应不同 clob token。

        Returns:
            规范化后的 `OrderBook`，包含 bids 与 asks。
        """
        token_id = market.yes_token_id if side.lower() == "yes" else market.no_token_id
        if not token_id or not self._clob_client:
            # 若缺少 token 或 CLOB 客户端未初始化，直接返回空盘口。
            return OrderBook(bids=[], asks=[])

        # py-clob-client get_order_book 为同步调用，这里用线程封装。
        # 若网络或 CLOB 出现异常，让异常抛出到 CLI 层，避免静默返回空盘口。
        ob_summary = await asyncio.to_thread(self._clob_client.get_order_book, token_id)

        bids_raw = getattr(ob_summary, "bids", None) or []
        asks_raw = getattr(ob_summary, "asks", None) or []
        bids = [_to_level(entry) for entry in bids_raw if _to_level(entry) is not None]
        asks = [_to_level(entry) for entry in asks_raw if _to_level(entry) is not None]
        return OrderBook(bids=bids, asks=asks)

    async def _fallback_orders(self, market: Market) -> tuple[list, list]:
        """备用方案：直接从 Gamma 订单接口读取盘口（部分老接口兼容）。

        Args:
            market: 目标市场。

        Returns:
            二元组 (bids, asks)，为未解析的原始列表。
        """
        try:
            resp = await self._http.get("/orders", params={"market": market.market_id, "limit": 50}, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            return data.get("bids") or [], data.get("asks") or []
        except Exception:
            return [], []

    async def place_order(self, market: Market, side: str, price: float, size: float) -> str:
        """提交订单（暂未实现）。

        当前仅占位，后续会接入带签名的 py-clob-client 交易流程。

        Args:
            market: 目标市场。
            side: 买卖方向或 YES/NO。
            price: 报价。
            size: 数量。

        Returns:
            订单 ID（当前总是抛出异常）。
        """
        raise RuntimeError("Trading not yet wired for Polymarket; only read-only orderbooks are supported.")

    async def cancel_order(self, order_id: str) -> bool:
        raise RuntimeError("Cancel not yet implemented for Polymarket client.")

    async def get_balances(self) -> list[Position]:
        """查询账户余额（未实现，占位）。

        Returns:
            当前总是返回空列表，后续会接入 CLOB 或链上余额。
        """
        return []

    async def close(self) -> None:
        """关闭底层 HTTP 客户端。"""
        await self._http.aclose()
        await self._data_http.aclose()

    async def get_recent_trades(self, *, limit: int = 200) -> List[TradeEvent]:
        """从 Data-API 获取最近成交列表。

        使用 `https://data-api.polymarket.com/trades`，按时间倒序返回最近的
        全市场成交记录。该接口为只读，无需 CLOB 凭证。

        Args:
            limit: 最大返回条数。

        Returns:
            解析后的 `TradeEvent` 列表，按时间倒序排序。
        """
        try:
            resp = await self._data_http.get("/trades", params={"limit": limit})
            resp.raise_for_status()
            raw_list = resp.json()
        except Exception:
            return []

        trades: List[TradeEvent] = []
        if not isinstance(raw_list, list):
            return trades

        for item in raw_list:
            try:
                condition_id = str(item.get("conditionId") or "")
                token_id = str(item.get("asset") or "")
                side = str(item.get("side") or "")
                size = float(item.get("size") or 0.0)
                price = float(item.get("price") or 0.0)
                ts = int(item.get("timestamp") or 0)
                title = str(item.get("title") or "")
                outcome = item.get("outcome") or None
                tx_hash = item.get("transactionHash") or None
                wallet = item.get("proxyWallet") or None
                pseudonym = item.get("pseudonym") or None
                notional = size * price
                trades.append(
                    TradeEvent(
                        condition_id=condition_id,
                        token_id=token_id,
                        side=side,
                        size=size,
                        price=price,
                        notional=notional,
                        timestamp=ts,
                        title=title,
                        outcome=outcome,
                        tx_hash=tx_hash,
                        wallet=wallet,
                        pseudonym=pseudonym,
                    )
                )
            except Exception:
                continue

        return trades

    async def list_tags(self, limit: int = 100, offset: int = 0) -> List[Tag]:
        """列出 Polymarket Gamma 上的标签列表。

        Args:
            limit: 返回的最大标签数量。
            offset: 起始偏移量，用于分页。

        Returns:
            标签数据列表；发生错误时返回空列表。
        """
        try:
            resp = await self._http.get(
                "/tags",
                params={"limit": limit, "offset": offset},
            )
            resp.raise_for_status()
            payload = resp.json()
            tags_raw = payload if isinstance(payload, list) else []
        except Exception:
            return []

        results: List[Tag] = []
        for item in tags_raw:
            try:
                tag_id = str(item.get("id") or "")
                label = str(item.get("label") or item.get("slug") or tag_id)
                slug = str(item.get("slug") or label).lower()
                if not tag_id:
                    continue
                results.append(Tag(id=tag_id, label=label, slug=slug))
            except Exception:
                continue
        return results

    async def get_tag_by_slug(self, slug: str) -> Optional[Tag]:
        """根据 slug 查询单个标签。

        Args:
            slug: 标签 slug，例如 \"politics\"。

        Returns:
            匹配的标签对象；未找到或错误时返回 None。
        """
        if not slug:
            return None
        try:
            resp = await self._http.get(f"/tags/slug/{slug}")
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None

        if not isinstance(data, dict):
            return None
        try:
            tag_id = str(data.get("id") or "")
            label = str(data.get("label") or data.get("slug") or tag_id)
            slug_val = str(data.get("slug") or label).lower()
            if not tag_id:
                return None
            return Tag(id=tag_id, label=label, slug=slug_val)
        except Exception:
            return None


def _lookup(entries: Iterable[dict[str, object]], market_id: str) -> dict[str, object]:
    """在原始列表中按 market_id 查找元素。

    Args:
        entries: 市场字典列表。
        market_id: 目标市场 ID。

    Returns:
        匹配到的字典对象。

    Raises:
        KeyError: 未找到目标市场时抛出。
    """
    for entry in entries:
        if entry.get("market_id") == market_id:
            return entry
    raise KeyError(f"Unknown market_id: {market_id}")


def _nested(obj: dict, keys: Iterable[str]) -> Optional[object]:
    """尝试按多个候选 key 查找字段。

    Args:
        obj: 原始字典对象。
        keys: 依次尝试的 key 列表。

    Returns:
        找到的字段值；若全部缺失则返回 None。
    """
    for key in keys:
        if key in obj:
            return obj[key]
    return None


def _to_level(entry: object) -> Optional[OrderBookLevel]:
    """将 CLOB 返回的任意盘口条目统一转换为 OrderBookLevel。

    兼容 py-clob-client 的 `OrderSummary` 对象、dict 以及
    形如 ``[price, size]`` 的列表/元组。

    Args:
        entry: 单条原始盘口记录。

    Returns:
        规范化后的 `OrderBookLevel`，若解析失败则返回 None。
    """
    # py-clob-client: OrderSummary(price='0.001', size='34962.94')
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
    """根据方向返回盘口最优价（买取最优卖价，卖取最优买价）。"""
    level = orderbook.best_ask() if side == "buy" else orderbook.best_bid()
    return float(level.price) if level else 1.0


def _parse_market_end_date(data: dict) -> Optional[str]:
    """从 Gamma 市场字典中提取结束时间并标准化为 ISO8601 字符串。

    Gamma 在不同版本中可能使用 ``endDate``、``end_time``、``closeDate``、
    ``resolveTime`` 等字段来表示市场结束或结算时间。本函数会按常见字段
    顺序尝试读取，并将 Unix 时间戳或可解析的字符串统一转换为 ISO8601。

    Args:
        data: 单个 Gamma 市场的原始字典。

    Returns:
        ISO8601 格式的 UTC 时间字符串；若无法解析则返回 ``None``。
    """
    from datetime import datetime, timezone

    candidates = [
        "endDate",
        "end_date",
        "endTime",
        "end_time",
        "closeDate",
        "close_date",
        "resolveTime",
        "resolve_time",
    ]
    raw = None
    for key in candidates:
        if key in data and data[key]:
            raw = data[key]
            break
    if raw is None:
        return None

    # Unix 时间戳（秒或毫秒）
    try:
        if isinstance(raw, (int, float)):
            ts = float(raw)
            # 认为大于 10^11 的为毫秒级时间戳
            if ts > 1e11:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.isoformat()
    except Exception:
        pass

    # 字符串：尝试直接解析或补充时区信息
    if isinstance(raw, str):
        txt = raw.strip()
        # 若已经是 ISO8601，优先直接返回
        try:
            dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
        # 作为备选方案，再尝试解析为整数时间戳
        try:
            ts = float(txt)
            if ts > 1e11:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.isoformat()
        except Exception:
            return None

    return None
