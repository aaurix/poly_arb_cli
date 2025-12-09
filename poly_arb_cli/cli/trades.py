"""成交流水相关 CLI 子命令。

目前主要提供 `trades-tape` 命令，用于实时展示 Polymarket 大额成交。
"""

from __future__ import annotations

import asyncio

import click
from rich.table import Table

from ..config import Settings
from ..types import TradeEvent
from . import main
from .common import build_clients, console


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
    """实时展示 Polymarket 大额成交流水。

    默认优先使用 WebSocket 行情，若 WS 尚无成交则回退到 Data-API。
    """

    async def _run() -> None:
        from rich.layout import Layout
        from rich.live import Live
        from rich.panel import Panel

        settings = Settings.load()
        pm_client, op_client = build_clients(settings)

        # 统计状态
        total_count = 0
        total_notional = 0.0

        # 为 WS 模式准备的本地 state
        pm_state = None
        feed_task = None
        condition_to_title: dict[str, str] = {}
        token_to_outcome: dict[str, str] = {}

        # 默认启动 WS feed；若失败则下方自动回退到 Data-API
        from ..connectors.polymarket_ws import MarketWsFeed, PolymarketStreamState

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


__all__ = ["trades_tape"]

