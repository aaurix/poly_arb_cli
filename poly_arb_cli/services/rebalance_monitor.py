"""基于 Polymarket 行情的市场再平衡监控服务。

本模块利用 `PolymarketStreamState` 中维护的订单簿与最近成交，
为每个市场构建简单的 YES 价格平滑基线，并在价格短期大幅偏离
该基线且伴随大额成交时输出结构化的再平衡监控信号。

该监控仅负责识别「可能的超调」与「潜在反向套利方向」，不直接
触发下单，适合作为 CLI 仪表盘或上层策略的输入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from ..connectors.polymarket_ws import PolymarketStreamState
from ..types import Market, OrderBook, OrderBookLevel, RebalanceSignal


@dataclass
class RebalanceMonitor:
    """Polymarket 市场再平衡监控器。

    该监控器为每个 condition 维护一个 YES 价格的指数平滑基线，
    周期性地对当前盘口价格与基线做比较，结合最近成交规模与时间
    窗口，识别「鲸鱼冲击后价格短期偏离」的情形，并生成
    :class:`RebalanceSignal` 列表。

    Attributes:
        baseline_yes: 每个 condition_id 对应的 YES 价格平滑基线。
        ema_alpha: 指数平滑系数，取值 (0, 1]；越大代表基线反应越快。
    """

    baseline_yes: Dict[str, float] = field(default_factory=dict)
    ema_alpha: float = 0.2

    def _estimate_yes_price(self, book: OrderBook) -> Optional[float]:
        """根据订单簿估算当前 YES 价格。

        优先返回 bid/ask 中值；若缺失一侧，则退回另一侧最优价。

        Args:
            book: YES 一侧订单簿。

        Returns:
            估算的 YES 价格；若订单簿为空则返回 ``None``。
        """
        best_bid: Optional[OrderBookLevel] = book.best_bid()
        best_ask: Optional[OrderBookLevel] = book.best_ask()
        if best_bid and best_ask:
            return float((best_bid.price + best_ask.price) / 2.0)
        if best_bid:
            return float(best_bid.price)
        if best_ask:
            return float(best_ask.price)
        return None

    def _update_baseline(self, condition_id: str, price: float) -> float:
        """更新并返回指定 condition 的 YES 基线价格。

        Args:
            condition_id: 市场条件 ID。
            price: 当前估算的 YES 价格。

        Returns:
            更新后的平滑基线价格。
        """
        previous = self.baseline_yes.get(condition_id)
        if previous is None:
            baseline = price
        else:
            alpha = self.ema_alpha
            baseline = alpha * price + (1.0 - alpha) * previous
        self.baseline_yes[condition_id] = baseline
        return baseline

    def detect_signals(
        self,
        state: PolymarketStreamState,
        markets: Iterable[Market],
        *,
        min_abs_move: float = 0.15,
        min_notional: float = 500.0,
        max_age_seconds: int = 300,
        min_trades: int = 1,
        now: Optional[datetime] = None,
    ) -> List[RebalanceSignal]:
        """基于当前订单簿与最近成交识别再平衡监控信号。

        该方法不会访问网络，仅依赖本地 `PolymarketStreamState`
        中维护的盘口与成交缓存。推荐由上层定期调用，例如在
        CLI 的事件循环中每隔数秒执行一次。

        Args:
            state: Polymarket 行情本地状态。
            markets: 需要监控的市场列表。
            min_abs_move: 触发信号的最小绝对价格偏离（如 0.15 即 15 个点）。
            min_notional: 最近一笔成交的最小名义金额阈值。
            max_age_seconds: 最近成交允许的最大时间间隔（秒）。
            min_trades: 触发信号前要求的最小成交条数。
            now: 当前时间，主要用于测试注入；缺省为当前 UTC 时间。

        Returns:
            按价格偏离绝对值降序排列的 :class:`RebalanceSignal` 列表。
        """
        if now is None:
            now = datetime.utcnow().replace(tz=timezone.utc)
        now_ts = int(now.timestamp())
        window_seconds = max_age_seconds

        results: List[RebalanceSignal] = []

        for market in markets:
            if market.platform.value != "polymarket":
                continue
            if not market.condition_id or not market.yes_token_id:
                continue

            book = state.get_orderbook_for_market(market, side="yes")
            if book is None:
                continue

            current_yes = self._estimate_yes_price(book)
            if current_yes is None:
                continue

            baseline = self._update_baseline(market.condition_id, current_yes)
            delta = current_yes - baseline
            if abs(delta) < min_abs_move:
                # 价格偏离尚不足以构成超调信号。
                continue

            trades = state.get_last_trades(market.condition_id, limit=50)
            if len(trades) < min_trades:
                continue

            last_trade = trades[-1]
            age = now_ts - int(last_trade.timestamp or 0)
            if age < 0 or age > max_age_seconds:
                # 最近成交过旧，可能不是短期冲击。
                continue
            if last_trade.notional < min_notional:
                # 成交规模不足以视为鲸鱼或明显情绪波动。
                continue

            direction = "short_yes" if delta > 0 else "short_no"
            reason_parts: list[str] = []
            move_pct = abs(delta) * 100.0
            reason_parts.append(f"价格相对基线偏离约 {move_pct:.1f}%")
            reason_parts.append(f"最近成交名义金额约 {last_trade.notional:.2f} USDC")
            if age > window_seconds / 2:
                reason_parts.append(f"成交距今 {age} 秒，接近监控窗口上限")

            reason = "；".join(reason_parts)

            signal = RebalanceSignal(
                market=market,
                direction=direction,
                current_yes=current_yes,
                baseline_yes=baseline,
                delta=delta,
                last_trade_notional=last_trade.notional,
                window_seconds=window_seconds,
                reason=reason,
            )
            results.append(signal)

        # 按价格偏离绝对值与最近成交规模排序，优先级高的信号排前面。
        results.sort(key=lambda s: (abs(s.delta), s.last_trade_notional), reverse=True)
        return results


__all__ = ["RebalanceMonitor"]

