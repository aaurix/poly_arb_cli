"""RebalanceMonitor 再平衡监控逻辑的基础单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from poly_arb_cli.connectors.polymarket_ws import PolymarketStreamState
from poly_arb_cli.services.rebalance_monitor import RebalanceMonitor
from poly_arb_cli.types import Market, OrderBook, OrderBookLevel, Platform, TradeEvent


def _make_market() -> Market:
    """构造一个用于测试的简单 Polymarket 市场。"""

    return Market(
        platform=Platform.POLYMARKET,
        market_id="m1",
        title="Test market",
        condition_id="c1",
        yes_token_id="y1",
        no_token_id="n1",
    )


def _append_trade(state: PolymarketStreamState, *, notional: float, timestamp: int) -> None:
    """在测试用 state 中追加一条成交记录。"""

    trade = TradeEvent(
        condition_id="c1",
        token_id="y1",
        side="BUY",
        size=notional,
        price=0.5,
        notional=notional,
        timestamp=timestamp,
        title="Test market",
    )
    state.trades_by_condition["c1"].append(trade)


def test_rebalance_monitor_emits_signal_on_large_move() -> None:
    """价格相对基线出现大幅偏离时应产生再平衡信号。"""

    state = PolymarketStreamState()
    market = _make_market()

    # 初始订单簿：价格在 0.5 左右。
    state.orderbooks["y1"] = OrderBook(
        bids=[OrderBookLevel(price=0.49, size=100.0)],
        asks=[OrderBookLevel(price=0.51, size=100.0)],
    )
    now = datetime.now(timezone.utc)
    ts_now = int(now.timestamp())
    _append_trade(state, notional=1000.0, timestamp=ts_now)

    monitor = RebalanceMonitor()

    # 首次检测：用于建立基线，不应触发信号。
    signals_first = monitor.detect_signals(
        state,
        [market],
        min_abs_move=0.1,
        min_notional=500.0,
        max_age_seconds=300,
        now=now,
    )
    assert signals_first == []

    # 价格大幅上升到 0.8 附近，并伴随一笔新的大额成交。
    state.orderbooks["y1"] = OrderBook(
        bids=[OrderBookLevel(price=0.79, size=100.0)],
        asks=[OrderBookLevel(price=0.81, size=100.0)],
    )
    later = now.replace(second=now.second + 1)
    ts_later = int(later.timestamp())
    _append_trade(state, notional=1200.0, timestamp=ts_later)

    signals_second = monitor.detect_signals(
        state,
        [market],
        min_abs_move=0.1,
        min_notional=500.0,
        max_age_seconds=300,
        now=later,
    )

    assert len(signals_second) == 1
    signal = signals_second[0]
    assert signal.direction == "short_yes"
    assert signal.current_yes > signal.baseline_yes
