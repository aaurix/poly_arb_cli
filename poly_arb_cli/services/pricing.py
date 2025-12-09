from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from ..types import OrderBook, OrderBookLevel


@dataclass
class FillComputation:
    average_price: float
    filled_size: float
    notional: float


def compute_fill(orderbook: OrderBook, side: Literal["buy", "sell"], size: float) -> FillComputation:
    """
    Walk the book to compute average fill price and notional for a given size.
    - buy: consume asks from best to worst
    - sell: consume bids from best to worst
    """
    levels = orderbook.asks if side == "buy" else orderbook.bids
    remaining = size
    notional = 0.0
    filled = 0.0

    for level in levels:
        take_size = min(level.size, remaining)
        notional += take_size * level.price
        filled += take_size
        remaining -= take_size
        if remaining <= 0:
            break

    if filled == 0:
        return FillComputation(average_price=1.0, filled_size=0.0, notional=0.0)

    return FillComputation(average_price=notional / filled, filled_size=filled, notional=notional)


def best_price(orderbook: OrderBook, side: Literal["buy", "sell"]) -> float:
    level: Optional[OrderBookLevel] = orderbook.best_ask() if side == "buy" else orderbook.best_bid()
    return float(level.price) if level else 1.0


def clamp_slippage(entry_price: float, avg_price: float, max_slippage_bps: int) -> bool:
    """Return True if average price is within slippage tolerance from entry price."""
    if entry_price == 0:
        return False
    diff_bps = abs(avg_price - entry_price) / entry_price * 10_000
    return diff_bps <= max_slippage_bps
