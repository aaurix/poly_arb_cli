"""Polymarket 标签相关 CLI 子命令。

主要用于列出 Gamma 上的 tags，便于结合 `--tag-id` 或 `--tag-slug`
过滤市场列表。
"""

from __future__ import annotations

import asyncio

import click
from rich.table import Table

from ..config import Settings
from . import main
from .common import console


@main.command("list-tags")
@click.option(
    "--limit",
    default=50,
    show_default=True,
    type=int,
    help="最多列出的标签数量。",
)
def list_tags(limit: int) -> None:
    """列出 Polymarket Gamma 上的标签（tags）。"""

    async def _run() -> None:
        settings = Settings.load()
        from ..clients.polymarket import PolymarketClient

        pm_client = PolymarketClient(settings)
        try:
            tags = await pm_client.list_tags(limit=limit)
            if not tags:
                console.print("[yellow]No tags returned from Gamma /tags.[/yellow]")
                return
            table = Table(title="Polymarket Tags", show_lines=False)
            table.add_column("ID", justify="right")
            table.add_column("Slug")
            table.add_column("Label")
            for tag in tags:
                table.add_row(tag.id, tag.slug, tag.label)
            console.print(table)
        finally:
            await pm_client.close()

    asyncio.run(_run())


__all__ = ["list_tags"]

