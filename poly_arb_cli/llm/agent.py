from __future__ import annotations

import os
from typing import Optional

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from ..clients.opinion import OpinionClient
from ..clients.polymarket import PolymarketClient
from ..config import Settings
from .tools import default_tools


def build_agent(model: Optional[str] = None, *, settings: Optional[Settings] = None):
    """
    Build a LangChain v1 agent with Polymarket/Opinion tools.
    Requires an OpenAI-compatible API key in environment (e.g., OPENAI_API_KEY).
    """
    settings = settings or Settings.load()
    llm = _load_model(model)
    pm_client = PolymarketClient(settings)
    op_client = OpinionClient(settings)
    tools = default_tools(pm_client, op_client)
    agent = create_agent(model=llm, tools=tools, system_prompt="You are a Polymarket-Opinion assistant. Use tools to fetch markets and prices.")
    return agent, pm_client, op_client


def run_question(question: str, model: Optional[str] = None) -> str:
    """Convenience helper to run a one-off question through the agent."""
    agent, pm_client, op_client = build_agent(model=model)
    try:
        resp = agent.invoke({"messages": [{"role": "user", "content": question}]})
        return str(resp)
    finally:
        # best-effort cleanup
        try:
            import asyncio

            asyncio.get_event_loop().run_until_complete(pm_client.close())
            asyncio.get_event_loop().run_until_complete(op_client.close())
        except Exception:
            pass


def _load_model(model: Optional[str]) -> BaseChatModel:
    name = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    return init_chat_model(name)
