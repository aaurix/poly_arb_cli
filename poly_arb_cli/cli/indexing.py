"""向量索引构建相关命令。

提供构建/刷新文档索引与市场索引的 CLI 入口，便于在生产环境
通过定时任务或手动触发，避免在查询时动态重建索引。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from ..config import Settings
from ..llm.vectorstore import build_docs_vectorstore, build_markets_vectorstore
from . import main
from .common import console


@main.command("build-docs-index")
@click.option(
    "--persist-dir",
    type=click.Path(path_type=Path),
    default=None,
    show_default=False,
    help="Chroma 持久化目录，缺省使用 data/chroma_docs。",
)
def build_docs_index(persist_dir: Path | None) -> None:
    """构建文档向量索引（2-step RAG 使用）。"""
    settings = Settings.load()
    target = persist_dir or settings.ensure_data_dir() / "chroma_docs"
    target.mkdir(parents=True, exist_ok=True)
    build_docs_vectorstore(persist_dir=target)
    console.print(f"[green]Docs index built at {target}[/green]")


@main.command("build-markets-index")
@click.option("--limit", default=1000, show_default=True, type=int, help="每个平台最大拉取市场数。")
@click.option(
    "--persist-dir",
    type=click.Path(path_type=Path),
    default=None,
    show_default=False,
    help="Chroma 持久化目录，缺省使用 data/chroma_markets。",
)
@click.option(
    "--sort",
    type=click.Choice(["none", "volume", "liquidity"], case_sensitive=False),
    default="volume",
    show_default=True,
    help="索引前是否按成交量/流动性排序（优先索引活跃市场）。",
)
@click.option(
    "--min-volume",
    default=None,
    type=float,
    show_default=False,
    help="24h 成交量下限，低于该值的市场不进入索引。",
)
@click.option(
    "--min-liquidity",
    default=None,
    type=float,
    show_default=False,
    help="流动性下限，低于该值的市场不进入索引。",
)
def build_markets_index(
    limit: int,
    persist_dir: Path | None,
    sort: str,
    min_volume: float | None,
    min_liquidity: float | None,
) -> None:
    """构建市场语义索引（跨平台市场检索用）。"""

    async def _run() -> None:
        settings = Settings.load()
        target = persist_dir or settings.ensure_data_dir() / "chroma_markets"
        target.mkdir(parents=True, exist_ok=True)
        await build_markets_vectorstore(
            settings=settings,
            limit=limit,
            persist_dir=target,
            sort_by=None if sort == "none" else sort,
            min_volume=min_volume,
            min_liquidity=min_liquidity,
        )
        console.print(f"[green]Markets index built at {target}[/green]")

    asyncio.run(_run())


__all__ = ["build_docs_index", "build_markets_index"]
