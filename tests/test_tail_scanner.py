from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from poly_arb_cli.config import Settings
from poly_arb_cli.services.tail_scanner import TailSweepOpportunity, _hours_to_resolve
from poly_arb_cli.types import Market, OrderBook, OrderBookLevel, Platform


def test_hours_to_resolve_with_future_end_date() -> None:
    now = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = (now + timedelta(hours=10)).isoformat()
    m = Market(
        platform=Platform.POLYMARKET,
        market_id="1",
        title="Test",
        end_date=end,
    )
    hours = _hours_to_resolve(m, now=now)
    assert hours is not None
    assert pytest.approx(hours, rel=1e-3) == 10.0


def test_hours_to_resolve_returns_none_for_past() -> None:
    now = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = (now - timedelta(hours=1)).isoformat()
    m = Market(
        platform=Platform.POLYMARKET,
        market_id="1",
        title="Test",
        end_date=end,
    )
    assert _hours_to_resolve(m, now=now) is None


def test_tail_sweep_opportunity_dataclass() -> None:
    """简单检查 TailSweepOpportunity 字段与类型不抛异常。"""

    m = Market(platform=Platform.POLYMARKET, market_id="1", title="Test")
    opp = TailSweepOpportunity(
        market=m,
        yes_price=0.99,
        max_sweep_size=100.0,
        notional=99.0,
        expected_yield_percent=0.5,
        annualized_yield_percent=10.0,
        hours_to_resolve=12.0,
        risk_flags=["thin_book"],
    )
    assert opp.market is m
    assert opp.yes_price == 0.99
    assert "thin_book" in opp.risk_flags


def test_orderbook_helpers_do_not_fail() -> None:
    """验证与 OrderBook 相关的类型在构造时不抛异常。"""

    book = OrderBook(
        bids=[OrderBookLevel(price=0.5, size=10)],
        asks=[OrderBookLevel(price=0.99, size=20)],
    )
    assert book.best_ask() is not None
    assert book.best_bid() is not None
