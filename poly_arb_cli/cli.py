"""CLI 入口与 Rich 可视化。

提供市场列表、盘口查询、套利扫描等命令行接口。
"""

from __future__ import annotations

import asyncio
import click
from rich.console import Console
from rich.table import Table

from .clients.opinion import OpinionClient
from .clients.polymarket import PolymarketClient
from .config import Settings
from .llm.agent import run_question
from .services.matcher import match_markets
from .services.scanner import scan_once
from .storage import log_opportunities, timestamp
from .types import ArbOpportunity, Market, OrderBook, Position
from .ui.dashboard import run_dashboard

console = Console()


def _build_clients(settings: Settings) -> tuple[PolymarketClient, OpinionClient]:
    """根据配置构建 Polymarket 与 Opinion 客户端实例。"""
    return PolymarketClient(settings), OpinionClient(settings)


async def _list_markets(platform: str, limit: int) -> None:
    """列出指定平台的活跃市场。"""
    settings = Settings.load()
    platform = _normalize_platform(platform)
    pm_client, op_client = _build_clients(settings)

    try:
        rows: list[Market] = []
        if platform in ("polymarket", "all"):
            rows.extend(await pm_client.list_active_markets(limit=limit))
        if platform in ("opinion", "all"):
            rows.extend(await op_client.list_active_markets(limit=limit))

        table = Table(title="Markets", show_lines=False)
        table.add_column("Platform")
        table.add_column("ID")
        table.add_column("Title")
        for market in rows:
            table.add_row(market.platform.value, market.market_id, market.title)
        console.print(table)
    finally:
        await asyncio.gather(pm_client.close(), op_client.close())


async def _scan(limit: int, threshold: float) -> None:
    """执行一次套利扫描并打印结果。"""
    settings = Settings.load()
    pm_client, op_client = _build_clients(settings)
    try:
        opportunities = await scan_once(pm_client, op_client, limit=limit, threshold=threshold)
        _print_opportunities(opportunities)
        log_opportunities(
            [
                {
                    "ts": timestamp(),
                    "route": opp.route,
                    "pm_id": opp.pair.polymarket.market_id,
                    "op_id": opp.pair.opinion.market_id,
                    "size": opp.size,
                    "cost": opp.cost,
                    "profit_pct": opp.profit_percent,
                    "breakdown": opp.price_breakdown,
                }
                for opp in opportunities
            ]
        )
    finally:
        await asyncio.gather(pm_client.close(), op_client.close())


async def _preview_matches(limit: int, threshold: float) -> None:
    """预览标题匹配后的市场对，用于调试匹配质量。"""
    settings = Settings.load()
    pm_client, op_client = _build_clients(settings)
    try:
        pm_markets = await pm_client.list_active_markets(limit=limit)
        op_markets = await op_client.list_active_markets(limit=limit)
        matches = match_markets(pm_markets, op_markets, threshold=threshold)

        table = Table(title="Matched Markets")
        table.add_column("Similarity", justify="right")
        table.add_column("Polymarket")
        table.add_column("Opinion")
        for match in matches:
            score = f"{match.similarity:.2f}" if match.similarity is not None else "?"
            table.add_row(score, match.polymarket.title, match.opinion.title)
        console.print(table)
    finally:
        await asyncio.gather(pm_client.close(), op_client.close())


def _print_opportunities(opportunities: list[ArbOpportunity]) -> None:
    """以 Rich 表格形式渲染套利机会列表。"""
    table = Table(title="Arbitrage Opportunities", header_style="bold cyan", show_lines=False, row_styles=["dim", ""])
    table.add_column("Route", style="magenta", justify="left")
    table.add_column("PM ID", overflow="fold")
    table.add_column("OP ID", overflow="fold")
    table.add_column("Size", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Profit %", justify="right")
    table.add_column("Breakdown", overflow="fold")

    for opp in opportunities:
        profit_style = "green" if opp.profit_percent >= 2.0 else ("yellow" if opp.profit_percent >= 1.0 else "red")
        table.add_row(
            opp.route,
            opp.pair.polymarket.market_id,
            opp.pair.opinion.market_id,
            f"{opp.size or 0:.2f}",
            f"{opp.cost:.4f}",
            f"[{profit_style}]{opp.profit_percent:.2f}[/{profit_style}]",
            opp.price_breakdown or "",
        )
    console.print(table)


def _print_orderbook(label: str, book: OrderBook, depth: int) -> None:
    """打印单侧订单簿（YES/NO）。"""
    table = Table(title=f"{label} Orderbook", header_style="bold cyan")
    table.add_column("Side", style="yellow")
    table.add_column("Price", justify="right")
    table.add_column("Size", justify="right")
    for level in book.asks[:depth]:
        table.add_row("ASK", f"{level.price:.4f}", f"{level.size:.2f}")
    for level in book.bids[:depth]:
        table.add_row("BID", f"{level.price:.4f}", f"{level.size:.2f}")
    console.print(table)


def _normalize_platform(value: str, allow_all: bool = True) -> str:
    """规范化并校验平台入参。

    Args:
        value: 用户输入的平台名称。
        allow_all: 是否允许 \"all\" 作为合法选项。

    Returns:
        规范化后的平台字符串。

    Raises:
        click.BadParameter: 当取值非法时抛出。
    """
    val = (value or "").lower()
    valid = {"polymarket", "opinion"}
    if allow_all:
        valid.add("all")
    if val not in valid:
        raise click.BadParameter(f"Platform must be one of: {', '.join(sorted(valid))}")
    return val


def _matches_query(title: str, query: str) -> bool:
    """判断标题是否与用户查询关键字匹配。

    支持普通子串匹配与 slug 风格（如 ``will-israel-strike-lebanon-on``）
    的模糊匹配。

    Args:
        title: 市场标题。
        query: 用户输入关键字或 slug 片段。

    Returns:
        True 表示匹配成功。
    """
    t = (title or "").lower()
    q = (query or "").lower()
    if not q:
        return False
    if q in t:
        return True
    # 将空格替换为连字符，支持 slug 风格匹配。
    hyphen_title = t.replace(" ", "-")
    if q in hyphen_title:
        return True
    # 去除空格和连字符，做更宽松的模糊匹配。
    normalized_title = t.replace(" ", "").replace("-", "")
    normalized_query = q.replace(" ", "").replace("-", "")
    return normalized_query in normalized_title


async def _find_market_by_id(client, market_id: str, search_limit: int = 500) -> Market | None:
    """在单个平台内按 ID 查找市场。"""
    markets = await client.list_active_markets(limit=search_limit)
    for mk in markets:
        if mk.market_id == market_id:
            return mk
    return None


async def _search_markets(platform: str, query: str, limit: int, search_limit: int = 500) -> None:
    """根据标题关键字或 slug 片段搜索市场。

    Args:
        platform: 目标平台（polymarket/opinion/all）。
        query: 标题关键字或 slug 片段。
        limit: 最多展示的匹配条数。
        search_limit: 每个平台最多扫描的市场数量。
    """
    settings = Settings.load()
    p = _normalize_platform(platform)
    pm_client, op_client = _build_clients(settings)
    try:
        rows: list[Market] = []
        if p in ("polymarket", "all"):
            pm_markets = await pm_client.list_active_markets(limit=search_limit)
            rows.extend(m for m in pm_markets if _matches_query(m.title, query))
        if p in ("opinion", "all"):
            op_markets = await op_client.list_active_markets(limit=search_limit)
            rows.extend(m for m in op_markets if _matches_query(m.title, query))

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


@click.group()
def main() -> None:
    """Polymarket-Opinion arbitrage CLI."""


@main.command("list-markets")
@click.option("--platform", default="all", show_default=True, help="polymarket|opinion|all")
@click.option("--limit", default=10, show_default=True, type=int, help="Max markets per venue.")
def list_markets(platform: str, limit: int) -> None:
    """显示活跃市场列表。"""
    asyncio.run(_list_markets(platform=platform, limit=limit))


@main.command("search-markets")
@click.argument("query", type=str)
@click.option("--platform", default="polymarket", show_default=True, help="polymarket|opinion|all")
@click.option("--limit", default=20, show_default=True, type=int, help="Max results to display.")
def search_markets(query: str, platform: str, limit: int) -> None:
    """根据标题关键字或 slug 查询市场。"""
    asyncio.run(_search_markets(platform=platform, query=query, limit=limit))


@main.command("scan-arb")
@click.option("--limit", default=10, show_default=True, type=int, help="Max markets per venue.")
@click.option("--threshold", default=0.6, show_default=True, type=float, help="Title similarity threshold.")
def scan_arb(limit: int, threshold: float) -> None:
    """Run a single arbitrage scan and print opportunities."""
    asyncio.run(_scan(limit=limit, threshold=threshold))


@main.command("match-preview")
@click.option("--limit", default=10, show_default=True, type=int)
@click.option("--threshold", default=0.6, show_default=True, type=float)
def match_preview(limit: int, threshold: float) -> None:
    """Preview how markets are paired."""
    asyncio.run(_preview_matches(limit=limit, threshold=threshold))


@main.command("run-bot")
@click.option("--interval", default=30, show_default=True, type=int, help="Seconds between scans.")
@click.option("--threshold", default=0.6, show_default=True, type=float)
def run_bot(interval: int, threshold: float) -> None:
    """Continuously scan for arbitrage with live-updating table."""

    async def _loop() -> None:
        from rich.live import Live

        settings = Settings.load(overrides={"scan_interval_seconds": interval})
        pm_client, op_client = _build_clients(settings)
        try:
            with Live(refresh_per_second=4, console=console) as live:
                while True:
                    opportunities = await scan_once(pm_client, op_client, limit=20, threshold=threshold)
                    table = Table(title="Arbitrage Opportunities (live)", header_style="bold cyan", show_lines=False, row_styles=["dim", ""])
                    table.add_column("Route", style="magenta")
                    table.add_column("PM ID")
                    table.add_column("OP ID")
                    table.add_column("Size", justify="right")
                    table.add_column("Cost", justify="right")
                    table.add_column("Profit %", justify="right")
                    table.add_column("Breakdown")
                    for opp in opportunities:
                        profit_style = "green" if opp.profit_percent >= 2.0 else ("yellow" if opp.profit_percent >= 1.0 else "red")
                        table.add_row(
                            opp.route,
                            opp.pair.polymarket.market_id,
                            opp.pair.opinion.market_id,
                            f"{opp.size or 0:.2f}",
                            f"{opp.cost:.4f}",
                            f"[{profit_style}]{opp.profit_percent:.2f}[/{profit_style}]",
                            opp.price_breakdown or "",
                        )
                    live.update(table)
                    await asyncio.sleep(interval)
        finally:
            await asyncio.gather(pm_client.close(), op_client.close())

    asyncio.run(_loop())


@main.command("tui")
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--threshold", default=0.6, show_default=True, type=float)
def tui(limit: int, threshold: float) -> None:
    """Launch Textual-based dashboard for opportunities."""
    settings = Settings.load()
    run_dashboard(settings=settings, demo=False, limit=limit, threshold=threshold)


@main.command("agent")
@click.argument("question", type=str)
@click.option("--model", default=None, help="LLM model name (OpenAI-compatible).")
def agent(question: str, model: str | None) -> None:
    """Ask a question via LangChain agent with market/orderbook tools."""
    answer = run_question(question, model=model)
    console.print(answer)


@main.command("positions")
@click.option("--platform", default="all", show_default=True, help="polymarket|opinion|all")
def positions(platform: str) -> None:
    """Show balances/positions for configured accounts."""

    async def _show() -> None:
        settings = Settings.load()
        p = _normalize_platform(platform)
        pm_client, op_client = _build_clients(settings)
        try:
            rows: list[Position] = []
            if p in ("polymarket", "all"):
                rows.extend(await pm_client.get_balances())
            if p in ("opinion", "all"):
                rows.extend(await op_client.get_balances())
            table = Table(title="Balances", header_style="bold cyan")
            table.add_column("Platform")
            table.add_column("Token")
            table.add_column("Balance", justify="right")
            for pos in rows:
                table.add_row(pos.platform.value, pos.symbol, f"{pos.balance:.4f}")
            console.print(table)
        finally:
            await asyncio.gather(pm_client.close(), op_client.close())

    asyncio.run(_show())


@main.command("orderbook")
@click.argument("market_id", type=str)
@click.option("--platform", default="polymarket", show_default=True, help="polymarket|opinion")
@click.option("--depth", default=10, show_default=True, type=int)
def orderbook(market_id: str, platform: str, depth: int) -> None:
    """Show orderbook depth for YES and NO tokens."""

    async def _show() -> None:
        settings = Settings.load()
        pm_client, op_client = _build_clients(settings)
        try:
            p = _normalize_platform(platform, allow_all=False)
            client = pm_client if p == "polymarket" else op_client
            target = await _find_market_by_id(client, market_id)
            if not target:
                console.print(f"[red]Market {market_id} not found on {p}[/red]")
                return
            yes_book = await client.get_orderbook(target, side="yes")
            no_book = await client.get_orderbook(target, side="no")
            console.rule(f"{p.upper()} | {target.title}")
            _print_orderbook("YES", yes_book, depth)
            _print_orderbook("NO", no_book, depth)
        finally:
            await asyncio.gather(pm_client.close(), op_client.close())

    asyncio.run(_show())


@main.command("price")
@click.argument("market_id", type=str)
@click.option("--platform", default="polymarket", show_default=True, help="polymarket|opinion")
def price(market_id: str, platform: str) -> None:
    """Display best YES/NO prices for a specific market."""

    async def _show() -> None:
        settings = Settings.load()
        pm_client, op_client = _build_clients(settings)
        try:
            p = _normalize_platform(platform, allow_all=False)
            client = pm_client if p == "polymarket" else op_client
            target = await _find_market_by_id(client, market_id)
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
