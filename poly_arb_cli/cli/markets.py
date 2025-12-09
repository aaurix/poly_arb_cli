"""市场相关 CLI 子命令。

包含：

- 活跃市场列表；
- 市场搜索；
- 单市场价格与盘口查询。
"""

from __future__ import annotations

import asyncio

import click
from rich.table import Table

from ..config import Settings
from ..types import Market
from . import main
from .common import (
    build_clients,
    console,
    find_market_by_id,
    matches_query,
    normalize_platform,
    print_orderbook,
)


async def _list_markets(
    platform: str,
    limit: int,
    sort: str | None = None,
    category: str | None = None,
    tag_slug: str | None = None,
    tag_id: str | None = None,
) -> None:
    """列出指定平台的活跃市场，可按成交量/流动性排序。

    Args:
        platform: 目标平台（polymarket/opinion/all）。
        limit: 每个平台最多展示的市场数量。
        sort: 排序字段，支持 ``\"volume\"``、``\"liquidity\"`` 或 ``None``。
    """
    settings = Settings.load()
    platform_norm = normalize_platform(platform)
    pm_client, op_client = build_clients(settings)

    try:
        rows: list[Market] = []
        if platform_norm in ("polymarket", "all"):
            # 为了有意义的排序，这里多取一些市场再在本地排序截断。
            fetch_limit = max(limit, 100) if sort else limit
            pm_tag_id = None
            if tag_slug and not tag_id:
                # 尝试通过 slug 查询 tag_id
                tag = await pm_client.get_tag_by_slug(tag_slug)
                if not tag:
                    console.print(f"[yellow]No Polymarket tag found for slug: {tag_slug}[/yellow]")
                else:
                    pm_tag_id = tag.id
            elif tag_id:
                pm_tag_id = tag_id

            rows.extend(await pm_client.list_active_markets(limit=fetch_limit, tag_id=pm_tag_id))
        if platform_norm in ("opinion", "all"):
            rows.extend(await op_client.list_active_markets(limit=limit))

        # 按分类/标签过滤（仅对 Polymarket 有意义）
        if category:
            cat_lower = category.lower()
            filtered: list[Market] = []
            for m in rows:
                if m.category and cat_lower in m.category.lower():
                    filtered.append(m)
                    continue
                if getattr(m, "tags", None):
                    if any(cat_lower in t.lower() for t in m.tags or []):
                        filtered.append(m)
                        continue
            rows = filtered

        # 按需排序（仅对有数值的字段生效）
        if sort == "volume":
            rows.sort(key=lambda m: (m.volume or 0.0), reverse=True)
        elif sort == "liquidity":
            rows.sort(key=lambda m: (m.liquidity or 0.0), reverse=True)

        rows = rows[:limit]

        table = Table(title="Markets", show_lines=False)
        table.add_column("Platform")
        table.add_column("ID")
        table.add_column("Title")
        if sort in ("volume", "liquidity"):
            table.add_column("24h Volume", justify="right")
            table.add_column("Liquidity", justify="right")

        for market in rows:
            if sort in ("volume", "liquidity"):
                table.add_row(
                    market.platform.value,
                    market.market_id,
                    market.title,
                    f"{market.volume or 0:.2f}",
                    f"{market.liquidity or 0:.2f}",
                )
            else:
                table.add_row(market.platform.value, market.market_id, market.title)
        console.print(table)
    finally:
        await asyncio.gather(pm_client.close(), op_client.close())


async def _search_markets(platform: str, query: str, limit: int, search_limit: int = 500) -> None:
    """根据标题关键字或 slug 片段搜索市场。

    Args:
        platform: 目标平台（polymarket/opinion/all）。
        query: 标题关键字或 slug 片段。
        limit: 最多展示的匹配条数。
        search_limit: 每个平台最多扫描的市场数量。
    """
    settings = Settings.load()
    p = normalize_platform(platform)
    pm_client, op_client = build_clients(settings)
    try:
        rows: list[Market] = []
        if p in ("polymarket", "all"):
            pm_markets = await pm_client.list_active_markets(limit=search_limit)
            for m in pm_markets:
                # 同时在标题、数值 ID、condition_id 上做模糊匹配
                if (
                    matches_query(m.title, query)
                    or query.lower() in (m.market_id or "").lower()
                    or (getattr(m, "condition_id", "") and query.lower() in m.condition_id.lower())
                ):
                    rows.append(m)
        if p in ("opinion", "all"):
            op_markets = await op_client.list_active_markets(limit=search_limit)
            rows.extend(m for m in op_markets if matches_query(m.title, query))

        rows = rows[:limit]
        table = Table(title=f"Markets matching '{query}'", show_lines=False)
        table.add_column("Platform")
        table.add_column("ID")
        table.add_column("Title")
        for market in rows:
            table.add_row(market.platform.value, market.market_id, market.title)
        if not rows:
            console.print(f"[yellow]No markets found for query: {query}[/yellow]")
        else:
            console.print(table)
    finally:
        await asyncio.gather(pm_client.close(), op_client.close())


@main.command("list-markets")
@click.option("--platform", default="all", show_default=True, help="polymarket|opinion|all")
@click.option("--limit", default=10, show_default=True, type=int, help="Max markets per venue.")
@click.option(
    "--category",
    default=None,
    show_default=False,
    help="按 Polymarket 分类/标签过滤（如 politics、crypto 等）。",
)
@click.option(
    "--tag-slug",
    default=None,
    show_default=False,
    help="Polymarket 标签 slug（如 politics）。会使用 tag 过滤 Gamma /markets。",
)
@click.option(
    "--tag-id",
    default=None,
    show_default=False,
    help="Polymarket 标签 ID（整数 ID）。会使用 tag_id 过滤 Gamma /markets。",
)
@click.option(
    "--sort",
    type=click.Choice(["none", "volume", "liquidity"], case_sensitive=False),
    default="volume",
    show_default=True,
    help="按 24h 成交量或当前流动性排序（仅对 Polymarket 有效）。",
)
def list_markets(
    platform: str,
    limit: int,
    sort: str,
    category: str | None,
    tag_slug: str | None,
    tag_id: str | None,
) -> None:
    """显示活跃市场列表，支持按分类或标签过滤。"""
    sort_key = None if sort == "none" else sort
    asyncio.run(
        _list_markets(
            platform=platform,
            limit=limit,
            sort=sort_key,
            category=category,
            tag_slug=tag_slug,
            tag_id=tag_id,
        )
    )


async def _list_categories(platform: str) -> None:
    """列出指定平台当前活跃市场的分类/标签聚合信息。

    目前仅对 Polymarket 有实际意义。
    """
    settings = Settings.load()
    p = normalize_platform(platform)
    pm_client, op_client = build_clients(settings)
    try:
        categories: dict[str, dict[str, float]] = {}
        if p in ("polymarket", "all"):
            pm_markets = await pm_client.list_active_markets(limit=500)
            for m in pm_markets:
                cat = (m.category or "uncategorized").strip()
                key = cat or "uncategorized"
                bucket = categories.setdefault(key, {"count": 0.0, "volume": 0.0})
                bucket["count"] += 1
                if m.volume is not None:
                    bucket["volume"] += m.volume

        if p in ("opinion", "all"):
            # Opinion 目前暂未显式使用分类，保留接口结构方便未来扩展。
            pass

        if not categories:
            console.print("[yellow]No category information available.[/yellow]")
            return

        table = Table(title="Polymarket Categories", show_lines=False)
        table.add_column("Category")
        table.add_column("Markets", justify="right")
        table.add_column("24h Volume", justify="right")
        for name, stats in sorted(categories.items(), key=lambda kv: kv[1]["volume"], reverse=True):
            table.add_row(name, f"{int(stats['count'])}", f"{stats['volume']:.2f}")
        console.print(table)
    finally:
        await asyncio.gather(pm_client.close(), op_client.close())


@main.command("list-categories")
@click.option("--platform", default="polymarket", show_default=True, help="polymarket|opinion|all")
def list_categories(platform: str) -> None:
    """列出当前活跃市场的分类/标签聚合情况。

    注意：Polymarket 目前主要通过 tags 进行组织，Gamma 的
    `category` 字段在很多市场上可能为空，因此结果中出现大量
    \"uncategorized\" 属于上游数据的预期表现。
    """
    asyncio.run(_list_categories(platform=platform))


@main.command("search-markets")
@click.argument("query", type=str)
@click.option("--platform", default="polymarket", show_default=True, help="polymarket|opinion|all")
@click.option("--limit", default=20, show_default=True, type=int, help="Max results to display.")
def search_markets(query: str, platform: str, limit: int) -> None:
    """根据标题关键字或 slug 查询市场。"""
    asyncio.run(_search_markets(platform=platform, query=query, limit=limit))


@main.command("orderbook")
@click.argument("market_id", type=str)
@click.option("--platform", default="polymarket", show_default=True, help="polymarket|opinion")
@click.option("--depth", default=10, show_default=True, type=int)
def orderbook(market_id: str, platform: str, depth: int) -> None:
    """展示指定市场的 YES/NO 订单簿深度。"""

    async def _show() -> None:
        settings = Settings.load()
        from ..clients.opinion import OpinionClient
        from ..clients.polymarket import PolymarketClient

        pm_client, op_client = build_clients(settings)
        try:
            p = normalize_platform(platform, allow_all=False)
            client = pm_client if p == "polymarket" else op_client
            target = await find_market_by_id(client, market_id)
            if not target:
                console.print(f"[red]Market {market_id} not found on {p}[/red]")
                return
            yes_book = await client.get_orderbook(target, side="yes")
            no_book = await client.get_orderbook(target, side="no")
            console.rule(f"{p.upper()} | {target.title}")
            print_orderbook("YES", yes_book, depth)
            print_orderbook("NO", no_book, depth)
        finally:
            await asyncio.gather(pm_client.close(), op_client.close())

    asyncio.run(_show())


@main.command("price")
@click.argument("market_id", type=str)
@click.option("--platform", default="polymarket", show_default=True, help="polymarket|opinion")
def price(market_id: str, platform: str) -> None:
    """展示指定市场的 YES/NO 最优价格与近端流动性。"""

    async def _show() -> None:
        settings = Settings.load()
        pm_client, op_client = build_clients(settings)
        try:
            p = normalize_platform(platform, allow_all=False)
            client = pm_client if p == "polymarket" else op_client
            target = await find_market_by_id(client, market_id)
            if not target:
                console.print(f"[red]Market {market_id} not found on {p}[/red]")
                return
            quote = await client.get_best_prices(target)
            table = Table(title=f"{p.upper()} prices for {market_id}", header_style="bold cyan")
            table.add_column("Side")
            table.add_column("Best Price", justify="right")
            table.add_column("Liquidity", justify="right")
            table.add_row("YES", f"{quote.yes_price:.4f}", f"{(quote.yes_liquidity or 0):.2f}")
            table.add_row("NO", f"{quote.no_price:.4f}", f"{(quote.no_liquidity or 0):.2f}")
            console.print(table)
        finally:
            await asyncio.gather(pm_client.close(), op_client.close())

    asyncio.run(_show())


__all__ = ["list_markets", "search_markets", "orderbook", "price", "list_categories"]
