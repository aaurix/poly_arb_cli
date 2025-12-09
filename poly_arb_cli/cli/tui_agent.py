"""TUI 与 LLM Agent 相关 CLI 子命令。"""

from __future__ import annotations

import click

from ..config import Settings
from ..llm.agent import run_question
from ..ui.dashboard import run_dashboard
from . import main
from .common import console


@main.command("tui")
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--threshold", default=0.6, show_default=True, type=float)
def tui(limit: int, threshold: float) -> None:
    """启动基于 Textual 的套利机会仪表盘。"""
    settings = Settings.load()
    run_dashboard(settings=settings, demo=False, limit=limit, threshold=threshold)


@main.command("agent")
@click.argument("question", type=str)
@click.option("--model", default=None, help="LLM model name (OpenAI-compatible).")
@click.option(
    "--mode",
    type=click.Choice(["auto", "docs", "markets", "graph"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="选择 Agent 模式：文档 RAG、市场 RAG 或 Agentic RAG Graph。",
)
def agent(question: str, model: str | None, mode: str) -> None:
    """通过 LangChain Agent / RAG 回答问题。"""
    answer = run_question(question, model=model, mode=mode)
    console.print(answer)


__all__ = ["tui", "agent"]
