from __future__ import annotations

import asyncio
from typing import Optional

from tenacity import AsyncRetrying, RetryError, retry_if_exception_type, stop_after_attempt, wait_fixed

from ..config import Settings
from ..clients.opinion import OpinionClient
from ..clients.polymarket import PolymarketClient
from ..types import ArbOpportunity, Platform, TradeLegResult, TradeResult


class Trader:
    """Coordinates simultaneous orders on both venues with basic rollback hooks."""

    def __init__(self, polymarket_client: PolymarketClient, opinion_client: OpinionClient, settings: Settings):
        self.polymarket_client = polymarket_client
        self.opinion_client = opinion_client
        self.settings = settings

    async def execute(self, opportunity: ArbOpportunity, size: float) -> TradeResult:
        """Submit paired orders with simple retry and rollback strategy."""
        pm_side, op_side = _route_to_sides(opportunity.route)
        pm_leg = TradeLegResult(platform=Platform.POLYMARKET, market_id=opportunity.pair.polymarket.market_id, side=pm_side, price=opportunity.cost / 2, size=size, status="pending")
        op_leg = TradeLegResult(platform=Platform.OPINION, market_id=opportunity.pair.opinion.market_id, side=op_side, price=opportunity.cost / 2, size=size, status="pending")

        # Place both orders concurrently with retry.
        try:
            pm_order_id, op_order_id = await asyncio.gather(
                self._place_with_retry(self.polymarket_client, pm_leg, opportunity),
                self._place_with_retry(self.opinion_client, op_leg, opportunity),
            )
            pm_leg.order_id, op_leg.order_id = pm_order_id, op_order_id
            pm_leg.status = "submitted"
            op_leg.status = "submitted"
            success = True
            notes = None
        except Exception as exc:  # noqa: BLE001
            # Attempt rollback: best-effort cancel/hedge.
            pm_leg.status = pm_leg.status if pm_leg.order_id else "failed"
            op_leg.status = op_leg.status if op_leg.order_id else "failed"
            pm_leg.error = pm_leg.error or str(exc)
            op_leg.error = op_leg.error or str(exc)
            await self._rollback(pm_leg, op_leg)
            success = False
            notes = f"rollback attempted: {exc}"

        return TradeResult(opportunity=opportunity, pm_leg=pm_leg, op_leg=op_leg, success=success, notes=notes)

    async def _place_with_retry(self, client, leg: TradeLegResult, opportunity: ArbOpportunity) -> Optional[str]:
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_fixed(1),
                retry=retry_if_exception_type(Exception),
                reraise=True,
            ):
                with attempt:
                    order_id = await self._place_leg(client, leg, opportunity)
                    return order_id
        except RetryError as exc:  # noqa: BLE001
            leg.status = "failed"
            leg.error = f"retry-exhausted: {exc}"
            raise

    async def _place_leg(self, client, leg: TradeLegResult, opportunity: ArbOpportunity) -> str:
        market = (
            opportunity.pair.polymarket
            if leg.platform == Platform.POLYMARKET
            else opportunity.pair.opinion
        )
        order_id = await client.place_order(market=market, side=leg.side, price=leg.price, size=leg.size)
        leg.status = "submitted"
        leg.order_id = order_id
        return order_id

    async def _rollback(self, pm_leg: TradeLegResult, op_leg: TradeLegResult) -> None:
        """
        Best-effort rollback placeholder.
        - If only one leg succeeded, attempt to cancel that leg (if supported) or place a hedge on the same venue.
        """
        # Attempt cancels if order ids exist
        if pm_leg.order_id:
            try:
                await self.polymarket_client.cancel_order(pm_leg.order_id)
            except Exception:
                pm_leg.error = pm_leg.error or "cancel_failed"
        if op_leg.order_id:
            try:
                await self.opinion_client.cancel_order(op_leg.order_id)
            except Exception:
                op_leg.error = op_leg.error or "cancel_failed"
        # Hedging not implemented yet; left as future work.

    async def close(self) -> None:
        await asyncio.gather(self.polymarket_client.close(), self.opinion_client.close())

def _route_to_sides(route: str) -> tuple[str, str]:
    if route == "PM_NO + OP_YES":
        return ("no", "yes")
    if route == "PM_YES + OP_NO":
        return ("yes", "no")
    raise ValueError(f"Unknown route: {route}")
