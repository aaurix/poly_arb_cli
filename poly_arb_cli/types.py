from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Platform(str, Enum):
    POLYMARKET = "polymarket"
    OPINION = "opinion"


@dataclass
class Market:
    platform: Platform
    market_id: str
    title: str
    end_date: Optional[str] = None
    yes_token_id: Optional[str] = None
    no_token_id: Optional[str] = None
    category: Optional[str] = None
    volume: Optional[float] = None
    liquidity: Optional[float] = None


@dataclass
class PriceQuote:
    yes_price: float
    no_price: float
    spread_bps: Optional[float] = None
    yes_liquidity: Optional[float] = None
    no_liquidity: Optional[float] = None


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]

    def best_bid(self) -> Optional[OrderBookLevel]:
        return self.bids[0] if self.bids else None

    def best_ask(self) -> Optional[OrderBookLevel]:
        return self.asks[0] if self.asks else None


@dataclass
class MatchedMarket:
    polymarket: Market
    opinion: Market
    similarity: Optional[float] = None


@dataclass
class ArbOpportunity:
    pair: MatchedMarket
    route: str  # "PM_NO + OP_YES" or "PM_YES + OP_NO"
    cost: float
    profit_percent: float
    size: Optional[float] = None
    max_size: Optional[float] = None
    price_breakdown: Optional[str] = None


@dataclass
class Position:
    platform: Platform
    token_id: str
    symbol: str
    balance: float
    available: Optional[float] = None


@dataclass
class TradeLegResult:
    platform: Platform
    market_id: str
    side: str
    price: float
    size: float
    order_id: Optional[str] = None
    status: str = "unknown"
    error: Optional[str] = None


@dataclass
class TradeResult:
    opportunity: ArbOpportunity
    pm_leg: TradeLegResult
    op_leg: TradeLegResult
    success: bool
    notes: Optional[str] = None
