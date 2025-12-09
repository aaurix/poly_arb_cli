"""CLI 通用工具与共享对象。

本模块提供：

- 统一的 Rich `console` 实例；
- 客户端构建辅助函数；
- 平台参数规范化与标题匹配工具；
- 用于各子命令复用的表格渲染函数。
"""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from ..clients.opinion import OpinionClient
from ..clients.polymarket import PolymarketClient
from ..config import Settings
from ..types import ArbOpportunity, HedgeOpportunity, Market, OrderBook

console = Console()


def build_clients(settings: Settings) -> tuple[PolymarketClient, OpinionClient]:
    """根据配置构建 Polymarket 与 Opinion 客户端实例。

    Args:
        settings: 全局配置对象。

    Returns:
        一个包含 PolymarketClient 与 OpinionClient 的二元组。
    """
    return PolymarketClient(settings), OpinionClient(settings)


def normalize_platform(value: str, allow_all: bool = True) -> str:
    """规范化并校验平台入参。

    Args:
        value: 用户输入的平台名称。
        allow_all: 是否允许 ``\"all\"`` 作为合法选项。

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


def matches_query(title: str, query: str) -> bool:
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


async def find_market_by_id(client, market_id: str, search_limit: int = 500) -> Market | None:
    """在单个平台内按 ID 查找市场。

    Args:
        client: 具有 ``list_active_markets`` 方法的市场客户端。
        market_id: 目标市场的人类可读 ID。
        search_limit: 为查找预拉取的最大市场数量。

    Returns:
        若找到则返回对应 ``Market``，否则返回 ``None``。
    """
    markets = await client.list_active_markets(limit=search_limit)
    for mk in markets:
        if mk.market_id == market_id:
            return mk
    return None


def print_opportunities(opportunities: list[ArbOpportunity]) -> None:
    """以 Rich 表格形式渲染套利机会列表。

    Args:
        opportunities: 套利机会列表。
    """
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


def print_hedge_opportunities(opportunities: list[HedgeOpportunity]) -> None:
    """渲染基于衍生品概率的对冲机会列表。

    Args:
        opportunities: 对冲机会列表。
    """
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


def print_orderbook(label: str, book: OrderBook, depth: int) -> None:
    """打印单侧订单簿（YES/NO）。

    Args:
        label: 标签名称（YES/NO）。
        book: 订单簿数据。
        depth: 展示的最大档位。
    """
    table = Table(title=f"{label} Orderbook", header_style="bold cyan")
    table.add_column("Side", style="yellow")
    table.add_column("Price", justify="right")
    table.add_column("Size", justify="right")
    for level in book.asks[:depth]:
        table.add_row("ASK", f"{level.price:.4f}", f"{level.size:.2f}")
    for level in book.bids[:depth]:
        table.add_row("BID", f"{level.price:.4f}", f"{level.size:.2f}")
    console.print(table)


__all__ = [
    "console",
    "build_clients",
    "normalize_platform",
    "matches_query",
    "find_market_by_id",
    "print_opportunities",
    "print_hedge_opportunities",
    "print_orderbook",
]

