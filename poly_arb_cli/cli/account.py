"""账户与余额相关 CLI 子命令。"""

from __future__ import annotations

import asyncio

import click
from rich.table import Table

from ..config import Settings
from ..types import Position
from . import main
from .common import build_clients, console, normalize_platform


@main.command("positions")
@click.option("--platform", default="all", show_default=True, help="polymarket|opinion|all")
def positions(platform: str) -> None:
    """展示当前账户在各平台的余额与持仓。"""

    async def _show() -> None:
        settings = Settings.load()
        p = normalize_platform(platform)
        pm_client, op_client = build_clients(settings)
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


__all__ = ["positions"]

