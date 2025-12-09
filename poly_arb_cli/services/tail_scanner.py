"""Polymarket 尾盘扫货策略扫描服务。

本模块基于 Gamma 市场元数据与 CLOB 盘口/WS 行情，识别
「Yes 价格接近 1 且即将结算」的尾盘扫货机会，并输出
结构化的机会列表，供 CLI 或上层机器人消费。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from ..clients.polymarket import PolymarketClient
from ..config import Settings
from ..connectors.polymarket_ws import PolymarketStreamState
from ..types import Market, OrderBook, OrderBookLevel


@dataclass
class TailSweepOpportunity:
    """描述单个尾盘扫货机会的数据类。

    Attributes:
        market: 目标 Polymarket 市场。
        yes_price: 当前 YES 最优买入价（盘口 best ask）。
        max_sweep_size: 在可接受深度内可扫的 YES 数量。
        notional: 名义金额，约等于 ``yes_price * max_sweep_size``。
        expected_yield_percent: 预期收益率（未考虑时间价值），单位百分比。
        annualized_yield_percent: 按剩余时间简单折算的年化收益率（%）。
        hours_to_resolve: 距离市场结束/结算的剩余小时数。
        risk_flags: 风险提示标签列表，例如 ``thin_book``、``long_horizon`` 等。
    """

    market: Market
    yes_price: float
    max_sweep_size: float
    notional: float
    expected_yield_percent: float
    annualized_yield_percent: float
    hours_to_resolve: float
    risk_flags: list[str]


def _parse_end_dt(market: Market) -> Optional[datetime]:
    """将 Market 中的 `end_date` 字段解析为 UTC datetime。

    Args:
        market: 包含 end_date 的市场对象。

    Returns:
        UTC 时区的 datetime；若缺失或解析失败则返回 ``None``。
    """
    if not market.end_date:
        return None
    raw = market.end_date.strip()
    try:
        # 支持 "2024-01-01T00:00:00Z" 或带偏移的 ISO8601。
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        # 兼容数值时间戳字符串
        try:
            ts = float(raw)
            if ts > 1e11:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None


def _hours_to_resolve(market: Market, now: Optional[datetime] = None) -> Optional[float]:
    """计算距离市场结束/结算的剩余小时数。

    Args:
        market: 目标市场。
        now: 当前时间，主要用于测试注入；缺省时取 ``datetime.utcnow``。

    Returns:
        剩余小时数；若 end_date 缺失或已过期则返回 ``None``。
    """
    end_dt = _parse_end_dt(market)
    if end_dt is None:
        return None
    if now is None:
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
    if end_dt <= now:
        return None
    delta = end_dt - now
    return delta.total_seconds() / 3600.0


def _best_ask(book: OrderBook) -> Optional[OrderBookLevel]:
    """返回盘口最优卖一价位。"""

    return book.best_ask()


def _estimate_fill_size(book: OrderBook, target_size: float) -> float:
    """在不考虑滑点约束下估算可扫数量。

    Args:
        book: YES 一侧订单簿。
        target_size: 希望扫单的最大数量。

    Returns:
        在盘口前几档中累计可成交的数量（不超过 target_size）。
    """
    remaining = target_size
    filled = 0.0
    for level in book.asks:
        if remaining <= 0:
            break
        take = min(level.size, remaining)
        filled += take
        remaining -= take
    return filled


async def scan_tail_once(
    pm_client: PolymarketClient,
    *,
    pm_state: Optional[PolymarketStreamState],
    settings: Settings,
    limit: int = 500,
) -> List[TailSweepOpportunity]:
    """执行一次 Polymarket 尾盘扫货机会扫描。

    该函数遵循「先从 WS，本地 state 取盘口，失败时退回 REST」
    的策略，筛选出 Yes 价格接近 1 且即将结算的市场，并按预期
    收益率与名义金额排序输出机会列表。

    Args:
        pm_client: Polymarket Gamma/CLOB 客户端实例。
        pm_state: 可选的 WebSocket 行情本地状态；若为 ``None`` 则只用 REST。
        settings: 全局配置对象，从中读取尾盘相关阈值。
        limit: Gamma 市场列表的最大拉取数量。

    Returns:
        尾盘扫货机会列表，按预期收益率降序排列。
    """
    markets = await pm_client.list_active_markets(limit=limit)
    now = datetime.utcnow().replace(tzinfo=timezone.utc)

    results: list[TailSweepOpportunity] = []
    for m in markets:
        if m.platform.value != "polymarket":
            continue
        if not m.yes_token_id:
            continue

        hours = _hours_to_resolve(m, now=now)
        if hours is None:
            continue
        if hours > settings.tail_max_hours_to_resolve:
            continue

        # 获取 YES 盘口：优先 WS state，退回 REST。
        book: OrderBook
        if pm_state is not None:
            ob = pm_state.get_orderbook_for_market(m, side="yes")
            book = ob or OrderBook(bids=[], asks=[])
        else:
            book = OrderBook(bids=[], asks=[])
        if not book.asks:
            book = await pm_client.get_orderbook(m, side="yes")
        if not book.asks:
            continue

        best = _best_ask(book)
        if best is None:
            continue
        price = float(best.price)
        if price < settings.tail_min_yes_price:
            continue

        # 估算可扫规模与名义金额。
        sweep_size = _estimate_fill_size(book, min(settings.max_trade_size, settings.tail_max_sweep_size))
        if sweep_size <= 0:
            continue
        notional = price * sweep_size
        if notional < settings.tail_min_notional:
            continue

        # 预期收益率（忽略时间价值）：(1 - price) * (1 - fee) 相对 price。
        gross_profit = (1.0 - price) * (1.0 - settings.tail_fee_rate)
        if gross_profit <= 0:
            continue
        expected_yield = (gross_profit / price) * 100.0
        if expected_yield < settings.tail_min_yield_percent:
            continue

        # 基于剩余时间计算简单年化收益率，便于不同到期时间的机会比较。
        if hours <= 0:
            continue
        days = hours / 24.0
        annualized_yield = expected_yield * (365.0 / days)
        if annualized_yield < settings.tail_min_annualized_yield_percent:
            continue

        # 风险标记：简单版本，仅根据时间窗口与盘口深度做提示。
        flags: list[str] = []
        if hours > 24:
            flags.append("long_horizon")
        # 盘口前五档总流动性过低则标记为 thin_book。
        total_size = sum(level.size for level in book.asks[:5])
        if total_size < sweep_size * 1.2:
            flags.append("thin_book")

        opp = TailSweepOpportunity(
            market=m,
            yes_price=price,
            max_sweep_size=sweep_size,
            notional=notional,
            expected_yield_percent=expected_yield,
            annualized_yield_percent=annualized_yield,
            hours_to_resolve=hours,
            risk_flags=flags,
        )
        results.append(opp)

    # 按预期收益率和名义金额排序，优先高收益、高规模机会。
    results.sort(key=lambda o: (o.expected_yield_percent, o.notional), reverse=True)
    return results


__all__ = ["TailSweepOpportunity", "scan_tail_once"]
