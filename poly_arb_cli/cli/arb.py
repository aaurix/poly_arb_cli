"""套利与对冲相关 CLI 子命令。

包含：

- 单次跨盘套利扫描；
- 对冲机会扫描；
- 标题匹配预览；
- 持续运行的套利机器人。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import click
from rich.table import Table

from ..clients.perp import PerpClient
from ..config import Settings
from ..services.hedge_scanner import load_hedge_markets, scan_hedged_opportunities
from ..services.matcher import match_markets
from ..services.rebalance_monitor import RebalanceMonitor
from ..services.scanner import scan_once
from ..storage import log_opportunities, timestamp
from ..types import ArbOpportunity, HedgeOpportunity
from . import main
from .common import build_clients, console, print_hedge_opportunities, print_opportunities


async def _scan(limit: int, threshold: float) -> None:
    """执行一次套利扫描并打印结果。

    Args:
        limit: 每个平台最多拉取的市场数量。
        threshold: 标题匹配的相似度阈值。
    """
    settings = Settings.load()
    pm_client, op_client = build_clients(settings)
    try:
        opportunities = await scan_once(pm_client, op_client, limit=limit, threshold=threshold)
        print_opportunities(opportunities)
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
    no_realized_vol: bool,
) -> None:
    """对标的型市场执行一次中性对冲机会扫描。

    Args:
        map_path: 对冲市场映射配置文件路径。
        pm_limit: 最大拉取的 Polymarket 市场数量。
        min_edge: 最小显示的 edge 百分比。
        default_vol: 默认年化波动率（未使用历史波动率时）。
        exchange: perp 交易所 ID（ccxt 名称）。
        no_realized_vol: 是否禁用历史波动率计算。
    """
    settings_overrides = {"perp_exchange": exchange} if exchange else None
    settings = Settings.load(overrides=settings_overrides)
    from ..clients.polymarket import PolymarketClient

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
            use_realized_vol=settings.hedge_use_realized_vol and not no_realized_vol,
            vol_timeframe=settings.hedge_vol_timeframe,
            vol_lookback_days=settings.hedge_vol_lookback_days,
            vol_max_candles=settings.hedge_vol_max_candles,
        )
        print_hedge_opportunities(opportunities)
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
    """预览标题匹配后的市场对，用于调试匹配质量。

    Args:
        limit: 每个平台最多拉取的市场数量。
        threshold: 标题相似度阈值。
    """
    settings = Settings.load()
    pm_client, op_client = build_clients(settings)
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


async def _rebalance_watch_loop(
    interval: int,
    limit: int,
    min_abs_move: float,
    min_notional: float,
    max_age_seconds: int,
) -> None:
    """持续监控 Polymarket 市场再平衡机会并在终端展示。

    Args:
        interval: 两次扫描之间的时间间隔（秒）。
        limit: 监控的 Polymarket 活跃市场数量上限。
        min_abs_move: 触发再平衡信号的最小绝对价格偏离（例如 0.15）。
        min_notional: 最近一笔成交的最小名义金额（美元）。
        max_age_seconds: 最近成交允许的最大时间间隔（秒）。
    """
    from rich.live import Live
    from rich.table import Table

    settings = Settings.load()
    from ..clients.polymarket import PolymarketClient
    from ..connectors.polymarket_ws import MarketWsFeed, PolymarketStreamState

    pm_client = PolymarketClient(settings)
    state = PolymarketStreamState()
    monitor = RebalanceMonitor()

    try:
        pm_markets = await pm_client.list_active_markets(limit=limit)
        pm_markets = [m for m in pm_markets if m.platform.value == "polymarket"]

        asset_ids: set[str] = set()
        for m in pm_markets:
            if m.yes_token_id:
                asset_ids.add(m.yes_token_id)
            if m.no_token_id:
                asset_ids.add(m.no_token_id)

        feed = MarketWsFeed(settings, state, asset_ids)
        feed_task = asyncio.create_task(feed.run())

        try:
            with Live(refresh_per_second=4, console=console) as live:
                while True:
                    signals = monitor.detect_signals(
                        state,
                        pm_markets,
                        min_abs_move=min_abs_move,
                        min_notional=min_notional,
                        max_age_seconds=max_age_seconds,
                    )

                    table = Table(
                        title="Rebalance Signals (Polymarket)",
                        header_style="bold cyan",
                        show_lines=False,
                        row_styles=["dim", ""],
                    )
                    table.add_column("Market ID")
                    table.add_column("Title")
                    table.add_column("Direction")
                    table.add_column("Current YES", justify="right")
                    table.add_column("Baseline YES", justify="right")
                    table.add_column("Delta", justify="right")
                    table.add_column("Last Notional", justify="right")
                    table.add_column("Reason")

                    for sig in signals:
                        table.add_row(
                            sig.market.market_id,
                            sig.market.title,
                            sig.direction,
                            f"{sig.current_yes:.4f}",
                            f"{sig.baseline_yes:.4f}",
                            f"{sig.delta:.4f}",
                            f"{sig.last_trade_notional:.2f}",
                            sig.reason or "",
                        )

                    live.update(table)
                    await asyncio.sleep(interval)
        finally:
            feed_task.cancel()
            try:
                await feed_task
            except Exception:
                pass
    finally:
        await pm_client.close()


@main.command("scan-arb")
@click.option("--limit", default=10, show_default=True, type=int, help="Max markets per venue.")
@click.option("--threshold", default=0.6, show_default=True, type=float, help="Title similarity threshold.")
def scan_arb(limit: int, threshold: float) -> None:
    """执行一次跨盘套利扫描并打印结果。"""
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
@click.option(
    "--no-realized-vol",
    is_flag=True,
    default=False,
    help="Disable realized vol fetch; rely on static vol.",
)
def scan_hedge(
    map_path: Path,
    pm_limit: int,
    min_edge: float | None,
    vol: float | None,
    exchange: str | None,
    no_realized_vol: bool,
) -> None:
    """比较 Polymarket 概率与 perp 隐含概率，寻找对冲机会。"""
    asyncio.run(
        _scan_hedge(
            map_path=map_path,
            pm_limit=pm_limit,
            min_edge=min_edge,
            default_vol=vol,
            exchange=exchange,
            no_realized_vol=no_realized_vol,
        )
    )


@main.command("match-preview")
@click.option("--limit", default=10, show_default=True, type=int)
@click.option("--threshold", default=0.6, show_default=True, type=float)
def match_preview(limit: int, threshold: float) -> None:
    """预览 Polymarket 与 Opinion 市场的标题匹配结果。"""
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
    """持续扫描套利机会并以 Rich 表格实时展示。"""

    async def _loop() -> None:
        from rich.live import Live

        settings = Settings.load(overrides={"scan_interval_seconds": interval})
        pm_client, op_client = build_clients(settings)

        pm_state = None
        feed_task = None

        # 如选择 use_ws，则启动 MARKET WebSocket feed 并维护本地 state。
        if use_ws:
            from ..connectors.polymarket_ws import MarketWsFeed, PolymarketStreamState

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
                    table = Table(
                        title="Arbitrage Opportunities (live)",
                        header_style="bold cyan",
                        show_lines=False,
                        row_styles=["dim", ""],
                    )
                    table.add_column("Route", style="magenta")
                    table.add_column("PM ID")
                    table.add_column("OP ID")
                    table.add_column("Size", justify="right")
                    table.add_column("Cost", justify="right")
                    table.add_column("Profit %", justify="right")
                    table.add_column("Breakdown")
                    for opp in opportunities:
                        profit_style = (
                            "green" if opp.profit_percent >= 2.0 else ("yellow" if opp.profit_percent >= 1.0 else "red")
                        )
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


@main.command("monitor-rebalance")
@click.option("--interval", default=5, show_default=True, type=int, help="Seconds between scans.")
@click.option(
    "--limit",
    default=50,
    show_default=True,
    type=int,
    help="Max active Polymarket markets to monitor.",
)
@click.option(
    "--min-abs-move",
    default=0.15,
    show_default=True,
    type=float,
    help="Minimum absolute YES price deviation to trigger signal (e.g. 0.15).",
)
@click.option(
    "--min-notional",
    default=500.0,
    show_default=True,
    type=float,
    help="Minimum notional of the last trade (USDC) to consider as whale/activity.",
)
@click.option(
    "--max-age",
    default=300,
    show_default=True,
    type=int,
    help="Maximum age in seconds of the last trade to treat as a short-term shock.",
)
def monitor_rebalance(
    interval: int,
    limit: int,
    min_abs_move: float,
    min_notional: float,
    max_age: int,
) -> None:
    """实时监控 Polymarket 市场再平衡机会，仅输出监控信号不自动下单。"""
    asyncio.run(
        _rebalance_watch_loop(
            interval=interval,
            limit=limit,
            min_abs_move=min_abs_move,
            min_notional=min_notional,
            max_age_seconds=max_age,
        )
    )


__all__ = ["scan_arb", "scan_hedge", "match_preview", "run_bot", "monitor_rebalance"]
