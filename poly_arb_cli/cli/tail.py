"""Polymarket 尾盘扫货策略相关 CLI 子命令。

当前提供：

- `tail-watch`：持续监控 Polymarket 即将结算且 YES 价格接近 1 的市场，
  输出尾盘扫货机会列表，优先使用 WebSocket 行情，失败时回退 REST。
"""

from __future__ import annotations

import asyncio

import click
from rich.table import Table

from ..clients.polymarket import PolymarketClient
from ..config import Settings
from ..connectors.polymarket_ws import MarketWsFeed, PolymarketStreamState
from ..services.tail_scanner import TailSweepOpportunity, scan_tail_once
from . import main
from .common import console


async def _tail_watch_loop(
    interval: int,
    use_ws: bool,
    limit: int,
    min_price: float | None,
    min_yield: float | None,
    max_hours: float | None,
    min_notional: float | None,
    max_sweep: float | None,
) -> None:
    """尾盘机会监控主循环。

    Args:
        interval: 扫描间隔秒数。
        use_ws: 是否启用 Polymarket WebSocket 行情。
        limit: Gamma 市场最大拉取数量。
        min_price: 覆盖配置中的最小 YES 价格阈值。
        min_yield: 覆盖配置中的最小收益率阈值。
        max_hours: 覆盖配置中的最大结算剩余小时数。
        min_notional: 覆盖配置中的最小名义金额。
        max_sweep: 覆盖配置中的最大扫单数量。
    """
    overrides: dict[str, float] = {"scan_interval_seconds": float(interval)}
    if min_price is not None:
        overrides["tail_min_yes_price"] = float(min_price)
    if min_yield is not None:
        overrides["tail_min_yield_percent"] = float(min_yield)
    if max_hours is not None:
        overrides["tail_max_hours_to_resolve"] = float(max_hours)
    if min_notional is not None:
        overrides["tail_min_notional"] = float(min_notional)
    if max_sweep is not None:
        overrides["tail_max_sweep_size"] = float(max_sweep)

    settings = Settings.load(overrides=overrides)
    pm_client = PolymarketClient(settings)

    pm_state: PolymarketStreamState | None = None
    feed_task: asyncio.Task | None = None

    try:
        if use_ws:
            # 使用当前活跃市场的 YES/NO token 启动 MARKET WS 行情订阅。
            pm_state = PolymarketStreamState()
            pm_markets = await pm_client.list_active_markets(limit=min(limit, 200))
            asset_ids: set[str] = set()
            for m in pm_markets:
                if m.yes_token_id:
                    asset_ids.add(m.yes_token_id)
                if m.no_token_id:
                    asset_ids.add(m.no_token_id)
            if asset_ids:
                feed = MarketWsFeed(settings, pm_state, asset_ids)
                feed_task = asyncio.create_task(feed.run())

        from rich.live import Live

        with Live(refresh_per_second=4, console=console) as live:
            while True:
                opportunities = await scan_tail_once(
                    pm_client,
                    pm_state=pm_state,
                    settings=settings,
                    limit=limit,
                )
                table = _build_table(opportunities)
                live.update(table)
                await asyncio.sleep(interval)
    finally:
        if feed_task:
            feed_task.cancel()
            try:
                await feed_task
            except Exception:
                pass
        await pm_client.close()


def _build_table(opportunities: list[TailSweepOpportunity]) -> Table:
    """将尾盘机会列表渲染为 Rich 表格。"""

    table = Table(
        title="Polymarket Tail Sweep Opportunities",
        header_style="bold cyan",
        show_lines=False,
        row_styles=["dim", ""],
    )
    table.add_column("Market ID")
    table.add_column("Title")
    table.add_column("YES Price", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Notional", justify="right")
    table.add_column("Yield %", justify="right")
    table.add_column("Ann. %", justify="right")
    table.add_column("Hours", justify="right")
    table.add_column("Flags")

    for opp in opportunities:
        yield_style = (
            "green"
            if opp.expected_yield_percent >= 0.3
            else ("yellow" if opp.expected_yield_percent >= 0.1 else "red")
        )
        table.add_row(
            opp.market.market_id,
            opp.market.title,
            f"{opp.yes_price:.4f}",
            f"{opp.max_sweep_size:.2f}",
            f"{opp.notional:.2f}",
            f"[{yield_style}]{opp.expected_yield_percent:.3f}[/{yield_style}]",
            f"{opp.annualized_yield_percent:.1f}",
            f"{opp.hours_to_resolve:.1f}",
            ",".join(opp.risk_flags),
        )
    if not opportunities:
        table.caption = "No tail opportunities found for current filters."
    return table


@main.command("tail-watch")
@click.option("--interval", default=30, show_default=True, type=int, help="Seconds between scans.")
@click.option("--limit", default=500, show_default=True, type=int, help="Max Polymarket markets to pull.")
@click.option(
    "--use-ws/--no-ws",
    default=True,
    show_default=True,
    help="是否启用 Polymarket WebSocket 行情，本地缓存盘口。",
)
@click.option(
    "--min-price",
    default=None,
    type=float,
    help="最小 YES 价格阈值（例如 0.95）。默认为配置中的 tail_min_yes_price。",
)
@click.option(
    "--min-yield",
    default=None,
    type=float,
    help="最小预期收益率（百分比），默认为配置中的 tail_min_yield_percent。",
)
@click.option(
    "--max-hours",
    default=None,
    type=float,
    help="最大结算剩余小时数，默认为配置中的 tail_max_hours_to_resolve。",
)
@click.option(
    "--min-notional",
    default=None,
    type=float,
    help="最小名义金额（美元），默认为配置中的 tail_min_notional。",
)
@click.option(
    "--max-sweep",
    default=None,
    type=float,
    help="最大扫单数量，默认为配置中的 tail_max_sweep_size，与 max_trade_size 取较小值。",
)
def tail_watch(
    interval: int,
    use_ws: bool,
    limit: int,
    min_price: float | None,
    min_yield: float | None,
    max_hours: float | None,
    min_notional: float | None,
    max_sweep: float | None,
) -> None:
    """持续监控 Polymarket 尾盘扫货机会并以表格形式展示。"""

    asyncio.run(
        _tail_watch_loop(
            interval=interval,
            use_ws=use_ws,
            limit=limit,
            min_price=min_price,
            min_yield=min_yield,
            max_hours=max_hours,
            min_notional=min_notional,
            max_sweep=max_sweep,
        )
    )


__all__ = ["tail_watch"]
