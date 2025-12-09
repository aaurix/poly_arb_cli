from __future__ import annotations

import asyncio
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Static

from ..clients.opinion import OpinionClient
from ..clients.polymarket import PolymarketClient
from ..config import Settings
from ..services.scanner import scan_once
from ..types import ArbOpportunity


class DashboardApp(App):
    """Textual TUI showing live arbitrage opportunities."""

    CSS_PATH = None

    def __init__(self, settings: Settings, demo: bool = False, limit: int = 20, threshold: float = 0.6):
        super().__init__()
        self.settings = settings
        self.demo = demo
        self.limit = limit
        self.threshold = threshold
        self.pm_client: Optional[PolymarketClient] = None
        self.op_client: Optional[OpinionClient] = None
        self.table: Optional[DataTable] = None
        self.status_text: Optional[Static] = None
        self._refresh_task: Optional[asyncio.Task] = None

    def compose(self) -> ComposeResult:
        self.table = DataTable(zebra_stripes=True)
        self.table.add_columns("Route", "PM ID", "OP ID", "Size", "Cost", "Profit %", "Breakdown")
        self.status_text = Static("Loading...", classes="status")
        yield Header()
        yield Horizontal(self.table, self.status_text)
        yield Footer()

    async def on_mount(self) -> None:
        self.pm_client, self.op_client = self._build_clients()
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def on_unmount(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
        if self.pm_client and self.op_client:
            await asyncio.gather(self.pm_client.close(), self.op_client.close())

    async def _refresh_loop(self) -> None:
        while True:
            await self._refresh_data()
            await asyncio.sleep(self.settings.scan_interval_seconds)

    async def _refresh_data(self) -> None:
        if not self.pm_client or not self.op_client or not self.table:
            return
        opportunities = await scan_once(self.pm_client, self.op_client, limit=self.limit, threshold=self.threshold)
        self._render_opportunities(opportunities)
        if self.status_text:
            self.status_text.update(f"Found {len(opportunities)} opps | interval {self.settings.scan_interval_seconds}s")
        # persist snapshot
        from ..storage import log_opportunities, timestamp  # local import to avoid cycles

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

    def _render_opportunities(self, opportunities: list[ArbOpportunity]) -> None:
        if not self.table:
            return
        self.table.clear()
        for opp in opportunities:
            self.table.add_row(
                opp.route,
                opp.pair.polymarket.market_id,
                opp.pair.opinion.market_id,
                f"{opp.size or 0:.2f}",
                f"{opp.cost:.4f}",
                f"{opp.profit_percent:.2f}",
                opp.price_breakdown or "",
            )

    def _build_clients(self) -> tuple[PolymarketClient, OpinionClient]:
        return PolymarketClient(self.settings), OpinionClient(self.settings)


def run_dashboard(settings: Settings, demo: bool = False, limit: int = 20, threshold: float = 0.6) -> None:
    app = DashboardApp(settings=settings, demo=demo, limit=limit, threshold=threshold)
    app.run()
