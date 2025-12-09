from __future__ import annotations

from typing import List

from ..clients.opinion import OpinionClient
from ..clients.polymarket import PolymarketClient
from ..types import ArbOpportunity, MatchedMarket
from .matcher import match_markets
from .pricing import best_price, clamp_slippage, compute_fill


async def scan_once(polymarket_client: PolymarketClient, opinion_client: OpinionClient, *, limit: int = 50, threshold: float = 0.6) -> List[ArbOpportunity]:
    """
    Fetch markets, match them, and evaluate arbitrage conditions.
    Uses depth-based pricing with slippage checks.
    """
    pm_markets = await polymarket_client.list_active_markets(limit=limit)
    op_markets = await opinion_client.list_active_markets(limit=limit)
    matched = match_markets(pm_markets, op_markets, threshold=threshold)

    results: List[ArbOpportunity] = []
    for pair in matched:
        settings = polymarket_client.settings  # shared config
        target_size = settings.default_quote_size

        # Fetch orderbooks
        pm_yes_book = await polymarket_client.get_orderbook(pair.polymarket, side="yes")
        pm_no_book = await polymarket_client.get_orderbook(pair.polymarket, side="no")
        op_yes_book = await opinion_client.get_orderbook(pair.opinion, side="yes")
        op_no_book = await opinion_client.get_orderbook(pair.opinion, side="no")

        # Route: PM_NO + OP_YES
        pm_no_best = best_price(pm_no_book, side="buy")
        op_yes_best = best_price(op_yes_book, side="buy")
        pm_no_fill = compute_fill(pm_no_book, side="buy", size=target_size)
        op_yes_fill = compute_fill(op_yes_book, side="buy", size=target_size)
        size_no_yes = min(pm_no_fill.filled_size, op_yes_fill.filled_size)
        cost_no_yes = pm_no_fill.average_price + op_yes_fill.average_price
        profit = (1 - cost_no_yes) * 100
        if (
            size_no_yes >= settings.min_trade_size
            and cost_no_yes < 1
            and profit >= settings.min_profit_percent
            and clamp_slippage(pm_no_best, pm_no_fill.average_price, settings.max_slippage_bps)
            and clamp_slippage(op_yes_best, op_yes_fill.average_price, settings.max_slippage_bps)
        ):
            results.append(
                ArbOpportunity(
                    pair=pair,
                    route="PM_NO + OP_YES",
                    cost=cost_no_yes,
                    profit_percent=profit,
                    size=size_no_yes,
                    max_size=min(pm_no_fill.filled_size, op_yes_fill.filled_size),
                    price_breakdown=f"PM_NO {pm_no_fill.average_price:.4f} | OP_YES {op_yes_fill.average_price:.4f}",
                )
            )

        # Route: PM_YES + OP_NO
        pm_yes_best = best_price(pm_yes_book, side="buy")
        op_no_best = best_price(op_no_book, side="buy")
        pm_yes_fill = compute_fill(pm_yes_book, side="buy", size=target_size)
        op_no_fill = compute_fill(op_no_book, side="buy", size=target_size)
        size_yes_no = min(pm_yes_fill.filled_size, op_no_fill.filled_size)
        cost_yes_no = pm_yes_fill.average_price + op_no_fill.average_price
        profit = (1 - cost_yes_no) * 100
        if (
            size_yes_no >= settings.min_trade_size
            and cost_yes_no < 1
            and profit >= settings.min_profit_percent
            and clamp_slippage(pm_yes_best, pm_yes_fill.average_price, settings.max_slippage_bps)
            and clamp_slippage(op_no_best, op_no_fill.average_price, settings.max_slippage_bps)
        ):
            results.append(
                ArbOpportunity(
                    pair=pair,
                    route="PM_YES + OP_NO",
                    cost=cost_yes_no,
                    profit_percent=profit,
                    size=size_yes_no,
                    max_size=min(pm_yes_fill.filled_size, op_no_fill.filled_size),
                    price_breakdown=f"PM_YES {pm_yes_fill.average_price:.4f} | OP_NO {op_no_fill.average_price:.4f}",
                )
            )

    return sorted(results, key=lambda opp: opp.profit_percent, reverse=True)
