"""LLM Agent 入口。

当前仅暴露 CLI 使用的 `run_question`，其内部统一通过
LangGraph Agentic RAG 图（Graph + retriever + LLM）执行。
"""

from .agent import run_question

__all__ = ["run_question"]
