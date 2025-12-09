from __future__ import annotations

from typing import List

from langchain.tools import tool

from ..clients.opinion import OpinionClient
from ..clients.polymarket import PolymarketClient
from ..types import Platform


def default_tools(pm_client: PolymarketClient, op_client: OpinionClient):
    """构建基础工具集合。

    这些工具仅依赖 HTTP 只读接口，不涉及下单与账户操作，
    便于在 Agent 与 RAG 场景中安全复用。
    """

    @tool("list_markets", return_direct=False)
    async def list_markets(platform: str = "all", limit: int = 10) -> str:
        """List active markets from polymarket/opinion/all."""
        targets: List[str] = [platform] if platform in ("polymarket", "opinion") else ["polymarket", "opinion"]
        rows = []
        if "polymarket" in targets:
            rows.extend(await pm_client.list_active_markets(limit=limit))
        if "opinion" in targets:
            rows.extend(await op_client.list_active_markets(limit=limit))
        return "\n".join(f"{m.platform.value}: {m.market_id} | {m.title}" for m in rows)

    @tool("get_orderbook", return_direct=False)
    async def get_orderbook(market_id: str, platform: str = "polymarket") -> str:
        """Get top-of-book for YES/NO tokens on a platform."""
        client = pm_client if platform == "polymarket" else op_client
        markets = await client.list_active_markets(limit=200)
        target = next((m for m in markets if m.market_id == market_id), None)
        if not target:
            return f"market {market_id} not found on {platform}"
        yes_book = await client.get_orderbook(target, side="yes")
        no_book = await client.get_orderbook(target, side="no")
        yes_best = yes_book.best_ask()
        no_best = no_book.best_ask()
        return (
            f"{platform} {market_id}\n"
            f"YES best: {yes_best.price if yes_best else 'n/a'} size {yes_best.size if yes_best else 'n/a'}\n"
            f"NO best: {no_best.price if no_best else 'n/a'} size {no_best.size if no_best else 'n/a'}"
        )

    return [list_markets, get_orderbook]


__all__ = ["default_tools"]
