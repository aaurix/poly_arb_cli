"""CLI 入口与 Rich 可视化。

提供市场列表、盘口查询、套利扫描等命令行接口。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import click
from rich.console import Console
from rich.table import Table

from .clients.opinion import OpinionClient
from .clients.polymarket import PolymarketClient
from .clients.perp import PerpClient
from .config import Settings
from .llm.agent import run_question
from .services.matcher import match_markets
from .services.hedge_scanner import load_hedge_markets, scan_hedged_opportunities
from .services.scanner import scan_once
from .storage import log_opportunities, timestamp
from .types import ArbOpportunity, HedgeOpportunity, Market, OrderBook, Position
from .ui.dashboard import run_dashboard

console = Console()


def _build_clients(settings: Settings) -> tuple[PolymarketClient, OpinionClient]:
    """根据配置构建 Polymarket 与 Opinion 客户端实例。"""
    return PolymarketClient(settings), OpinionClient(settings)


async def _list_markets(platform: str, limit: int, sort: str | None = None) -> None:
    """列出指定平台的活跃市场，可按成交量/流动性排序。"""
    settings = Settings.load()
    platform = _normalize_platform(platform)
    pm_client, op_client = _build_clients(settings)

    try:
        rows: list[Market] = []
        if platform in ("polymarket", "all"):
            # 为了有意义的排序，这里多取一些市场再在本地排序截断。
            fetch_limit = max(limit, 100) if sort else limit
            rows.extend(await pm_client.list_active_markets(limit=fetch_limit))
        if platform in ("opinion", "all"):
            rows.extend(await op_client.list_active_markets(limit=limit))

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


async def _scan_hedge(
    map_path: Path,
    pm_limit: int,
    min_edge: float | None,
    default_vol: float | None,
    exchange: str | None,
) -> None:
    """对标的型市场执行一次中性对冲机会扫描。"""
    settings_overrides = {"perp_exchange": exchange} if exchange else None
    settings = Settings.load(overrides=settings_overrides)
    pm_client = PolymarketClient(settings)
    perp_client = PerpClient(settings, exchange_id=exchange)
    try:
        mappings = load_hedge_markets(map_path)
        if not mappings:
            console.print(f"[yellow]No hedge mappings found in {map_path}[/yellow]")
            return

        opportunities = await scan_hedged_opportunities(
            pm_client,
            perp_client,
            mappings,
            pm_limit=pm_limit,
            min_edge_percent=min_edge if min_edge is not None else settings.hedge_min_edge_percent,
            default_vol=default_vol if default_vol is not None else settings.hedge_default_vol,
            min_gap_sigma=settings.hedge_min_gap_sigma,
        )
        _print_hedge_opportunities(opportunities)
        log_opportunities(
            [
                {
                    "ts": timestamp(),
                    "market_id": opp.market.market_id,
                    "title": opp.market.title,
                    "underlying": opp.underlying_symbol,
                    "pm_yes": opp.pm_yes,
                    "implied_yes": opp.implied_yes,
                    "edge_pct": opp.edge_percent,
                    "strike": opp.strike,
                    "expiry": opp.expiry,
                    "funding": opp.funding_rate,
                    "note": opp.note,
                }
                for opp in opportunities
            ],
            file_name="hedged_opportunities.jsonl",
        )
    finally:
        await asyncio.gather(pm_client.close(), perp_client.close())


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
    table = Table(
        title="Arbitrage Opportunities",
        header_style="bold cyan",
        show_lines=False,
        row_styles=["dim", ""],
    )
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


def _print_hedge_opportunities(opportunities: list[HedgeOpportunity]) -> None:
    """渲染基于衍生品概率的对冲机会。"""
    table = Table(
        title="Hedged Opportunities",
        header_style="bold cyan",
        show_lines=False,
        row_styles=["dim", ""],
    )
    table.add_column("Market ID", overflow="fold")
    table.add_column("Title", overflow="fold")
    table.add_column("Underlying", style="magenta")
    table.add_column("Type", justify="left")
    table.add_column("PM YES", justify="right")
    table.add_column("Implied YES", justify="right")
    table.add_column("Edge %", justify="right")
    table.add_column("Px/Strike", justify="right")
    table.add_column("Expiry", overflow="fold")
    table.add_column("Funding", justify="right")
    table.add_column("Note", overflow="fold")

    for opp in opportunities:
        edge_style = "green" if opp.edge_percent > 0 else "red"
        table.add_row(
            opp.market.market_id,
            opp.market.title,
            opp.underlying_symbol,
            f"{opp.prob_source}/{opp.barrier or '-'}",
            f"{opp.pm_yes:.4f}",
            f"{opp.implied_yes:.4f}",
            f"[{edge_style}]{opp.edge_percent:.2f}[/{edge_style}]",
            f"{opp.underlying_price:.1f}/{opp.strike:.0f}",
            opp.expiry,
            f"{opp.funding_rate:.6f}" if opp.funding_rate is not None else "-",
            opp.note or "",
        )
    if not opportunities:
        console.print("[yellow]No hedged opportunities found[/yellow]")
    else:
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
            for m in pm_markets:
                # 同时在标题、数值 ID、condition_id 上做模糊匹配
                if (
                    _matches_query(m.title, query)
                    or query.lower() in (m.market_id or "").lower()
                    or (getattr(m, "condition_id", "") and query.lower() in m.condition_id.lower())
                ):
                    rows.append(m)
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
@click.option(
    "--sort",
    type=click.Choice(["none", "volume", "liquidity"], case_sensitive=False),
    default="volume",
    show_default=True,
    help="按 24h 成交量或当前流动性排序（仅对 Polymarket 有效）。",
)
def list_markets(platform: str, limit: int, sort: str) -> None:
    """显示活跃市场列表，默认按 24h 成交量排序。"""
    sort_key = None if sort == "none" else sort
    asyncio.run(_list_markets(platform=platform, limit=limit, sort=sort_key))


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


@main.command("scan-hedge")
@click.option(
    "--map-path",
    default="data/underlying_map.json",
    show_default=True,
    type=click.Path(path_type=Path),
    help="JSON mapping of Polymarket markets to underlyings.",
)
@click.option(
    "--pm-limit",
    default=200,
    show_default=True,
    type=int,
    help="Max Polymarket markets to pull.",
)
@click.option(
    "--min-edge",
    default=None,
    type=float,
    help="Minimum absolute edge percent to display.",
)
@click.option(
    "--vol",
    default=None,
    type=float,
    help="Override default annualized vol for probability calc.",
)
@click.option(
    "--exchange",
    default=None,
    type=str,
    help="Override perp exchange id (ccxt id).",
)
def scan_hedge(
    map_path: Path, pm_limit: int, min_edge: float | None, vol: float | None, exchange: str | None
) -> None:
    """Compare Polymarket prices with perp-implied probabilities for mapped markets."""
    asyncio.run(
        _scan_hedge(
            map_path=map_path,
            pm_limit=pm_limit,
            min_edge=min_edge,
            default_vol=vol,
            exchange=exchange,
        )
    )


@main.command("match-preview")
@click.option("--limit", default=10, show_default=True, type=int)
@click.option("--threshold", default=0.6, show_default=True, type=float)
def match_preview(limit: int, threshold: float) -> None:
    """Preview how markets are paired."""
    asyncio.run(_preview_matches(limit=limit, threshold=threshold))


@main.command("run-bot")
@click.option("--interval", default=30, show_default=True, type=int, help="Seconds between scans.")
@click.option("--threshold", default=0.6, show_default=True, type=float)
@click.option(
    "--use-ws",
    is_flag=True,
    default=False,
    help="优先使用 Polymarket WebSocket 行情（若可用），否则退回 REST 盘口。",
)
def run_bot(interval: int, threshold: float, use_ws: bool) -> None:
    """Continuously scan for arbitrage with live-updating table."""

    async def _loop() -> None:
        from rich.live import Live

        settings = Settings.load(overrides={"scan_interval_seconds": interval})
        pm_client, op_client = _build_clients(settings)

        pm_state = None
        feed_task = None

        # 如选择 use_ws，则启动 MARKET WebSocket feed 并维护本地 state。
        if use_ws:
            from .connectors.polymarket_ws import MarketWsFeed, PolymarketStreamState

            pm_state = PolymarketStreamState()
            # 订阅当前活跃市场的所有 YES/NO token
            pm_markets = await pm_client.list_active_markets(limit=50)
            asset_ids: set[str] = set()
            for m in pm_markets:
                if m.yes_token_id:
                    asset_ids.add(m.yes_token_id)
                if m.no_token_id:
                    asset_ids.add(m.no_token_id)
            feed = MarketWsFeed(settings, pm_state, asset_ids)
            feed_task = asyncio.create_task(feed.run())

        try:
            with Live(refresh_per_second=4, console=console) as live:
                while True:
                    opportunities = await scan_once(
                        pm_client,
                        op_client,
                        limit=20,
                        threshold=threshold,
                        pm_state=pm_state,
                    )
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
            if feed_task:
                feed_task.cancel()
                # 忽略取消异常
                try:
                    await feed_task
                except Exception:
                    pass
            await asyncio.gather(pm_client.close(), op_client.close())

    asyncio.run(_loop())


@main.command("trades-tape")
@click.option(
    "--min-notional",
    default=1000.0,
    show_default=True,
    type=float,
    help="过滤最小名义金额（size*price），单位约为 USDC。",
)
@click.option(
    "--interval",
    default=5,
    show_default=True,
    type=int,
    help="刷新间隔秒数。",
)
@click.option(
    "--window",
    default=50,
    show_default=True,
    type=int,
    help="界面中最多展示的最近成交条数。",
)
def trades_tape(min_notional: float, interval: int, window: int) -> None:
    """实时展示 Polymarket 大额成交流水（默认使用 WS，必要时回退 Data-API）。"""

    async def _run() -> None:
        from rich.live import Live
        from rich.layout import Layout
        from rich.panel import Panel

        settings = Settings.load()
        pm_client, op_client = _build_clients(settings)

        # 统计状态
        total_count = 0
        total_notional = 0.0

        # 为 WS 模式准备的本地 state
        pm_state = None
        feed_task = None
        condition_to_title: dict[str, str] = {}
        token_to_outcome: dict[str, str] = {}

        # 默认启动 WS feed；若失败则下方自动回退到 Data-API
        from .connectors.polymarket_ws import MarketWsFeed, PolymarketStreamState

        pm_state = PolymarketStreamState()
        pm_markets = await pm_client.list_active_markets(limit=200)
        asset_ids: set[str] = set()
        for m in pm_markets:
            # 使用 “数值 ID | 标题” 的展示形式，并同时支持按 market_id 与 condition_id 查询。
            display_title = f"{m.market_id} | {m.title}"
            condition_to_title[m.market_id] = display_title
            if getattr(m, "condition_id", None):
                condition_to_title[m.condition_id] = display_title
            if m.yes_token_id:
                asset_ids.add(m.yes_token_id)
                token_to_outcome[m.yes_token_id] = "YES"
            if m.no_token_id:
                asset_ids.add(m.no_token_id)
                token_to_outcome[m.no_token_id] = "NO"
        if asset_ids:
            feed = MarketWsFeed(settings, pm_state, asset_ids)
            feed_task = asyncio.create_task(feed.run())

        try:
            with Live(console=console, refresh_per_second=4) as live:
                while True:
                    # 获取最新成交：优先 WS，本地无数据则退回 Data-API
                    recent_trades: list[TradeEvent] = []
                    if pm_state is not None and pm_state.trades_by_condition:
                        from itertools import chain

                        buf_iter = pm_state.trades_by_condition.values()
                        all_trades = list(chain.from_iterable(buf_iter))
                        all_trades = [t for t in all_trades if t.notional >= min_notional]
                        # 时间倒序
                        recent_trades = sorted(all_trades, key=lambda x: x.timestamp, reverse=True)[:window]
                    else:
                        trades = await pm_client.get_recent_trades(limit=200)
                        trades = [t for t in trades if t.notional >= min_notional]
                        recent_trades = trades[:window]

                    total_notional = sum(t.notional for t in recent_trades)
                    total_count = len(recent_trades)
                    avg_notional = total_notional / total_count if total_count else 0.0

                    # 统计面板
                    stats_table = Table(show_header=False, box=None)
                    stats_table.add_column("Metric", style="cyan", justify="right")
                    stats_table.add_column("Value", justify="left")
                    stats_table.add_row("Trades", f"{total_count}")
                    stats_table.add_row("Notional", f"{total_notional:,.2f}")
                    stats_table.add_row("Avg trade", f"{avg_notional:,.2f}")
                    stats_panel = Panel(stats_table, title="Polymarket Trade Stats", border_style="green")

                    # 成交流水表
                    tape_table = Table(
                        title=f"Polymarket Trades ≥ {min_notional:.0f}",
                        header_style="bold cyan",
                        show_lines=False,
                        row_styles=["dim", ""],
                    )
                    tape_table.add_column("Time (UTC)", justify="right")
                    tape_table.add_column("Market", overflow="fold")
                    tape_table.add_column("Outcome", justify="left")
                    tape_table.add_column("Side", justify="center")
                    tape_table.add_column("Size", justify="right")
                    tape_table.add_column("Price", justify="right")
                    tape_table.add_column("Notional", justify="right")
                    tape_table.add_column("Trader", overflow="fold")

                    for t in recent_trades:
                        side = (t.side or "").upper()
                        side_style = "green" if side == "BUY" else "red"
                        time_str = t.dt.strftime("%H:%M:%S")
                        trader = t.pseudonym or (t.wallet[:10] + "..." if t.wallet else "")
                        title = condition_to_title.get(t.condition_id, t.title or t.condition_id)
                        outcome = token_to_outcome.get(t.token_id, "") or (t.outcome or "")
                        tape_table.add_row(
                            time_str,
                            title,
                            outcome,
                            f"[{side_style}]{side}[/{side_style}]",
                            f"{t.size:.2f}",
                            f"{t.price:.3f}",
                            f"{t.notional:.2f}",
                            trader,
                        )

                    layout = Layout()
                    layout.split_column(
                        Layout(stats_panel, name="stats", size=5),
                        Layout(tape_table, name="tape", ratio=1),
                    )
                    live.update(layout)

                    await asyncio.sleep(interval)
        finally:
            if feed_task:
                feed_task.cancel()
                try:
                    await feed_task
                except Exception:
                    pass
            await asyncio.gather(pm_client.close(), op_client.close())

    asyncio.run(_run())


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
