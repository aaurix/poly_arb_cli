from __future__ import annotations

import os
from typing import Optional

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from langchain_core.vectorstores import VectorStore

from ..clients.opinion import OpinionClient
from ..clients.polymarket import PolymarketClient
from ..config import Settings
from .tools import default_tools
from .vectorstore import build_docs_vectorstore, build_markets_vectorstore


def build_agent(model: Optional[str] = None, *, settings: Optional[Settings] = None):
    """构建基础工具型 Agent（MVP 版本）。

    该 Agent 仅绑定简单的 list/get_orderbook 工具，主要用于
    快速实验，不包含 RAG 能力。

    Args:
        model: 模型名称，缺省时从环境变量 OPENAI_MODEL 读取。
        settings: 可选配置对象，未提供时自动加载。

    Returns:
        三元组 (agent, pm_client, op_client)。
    """
    settings = settings or Settings.load()
    llm = _load_model(model, settings=settings)
    pm_client = PolymarketClient(settings)
    op_client = OpinionClient(settings)
    tools = default_tools(pm_client, op_client)
    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt="You are a Polymarket-Opinion assistant. Use tools to fetch markets and prices.",
    )
    return agent, pm_client, op_client


def _load_model(model: Optional[str], settings: Optional[Settings] = None) -> BaseChatModel:
    """根据名称加载 Chat 模型，使用 OpenAI 兼容接口。"""

    settings = settings or Settings.load()
    name = model or settings.openai_model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    return ChatOpenAI(
        model=name,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )


def build_docs_rag_chain(
    docs_store: Optional[VectorStore] = None,
    *,
    model: Optional[str] = None,
) -> tuple[object, VectorStore]:
    """构建面向项目文档的 2-Step RAG Chain。

    该 Chain 适合回答「README / docs / 架构设计」相关问题，
    实现为经典的检索-生成两步 RAG。

    Args:
        docs_store: 已构建的 VectorStore，若为空则自动构建。
        model: 使用的聊天模型名称。

    Returns:
        (rag_chain, vectorstore) 二元组，便于调用方复用索引。
    """
    from langchain.chains import create_retrieval_chain
    from langchain.chains.combine_documents import create_stuff_documents_chain
    from langchain_core.prompts import ChatPromptTemplate

    if docs_store is None:
        settings = Settings.load()
        docs_store = build_docs_vectorstore(persist_dir=settings.ensure_data_dir() / "chroma_docs")  # type: ignore[arg-type]

    retriever = docs_store.as_retriever(search_kwargs={"k": 6})
    llm = _load_model(model)

    prompt = ChatPromptTemplate.from_template(
        "你是本项目的技术助手，请严格依据给出的文档片段回答问题。\n"
        "若文档中信息不足，请直接说明不知道，不要胡编。\n\n"
        "问题: {input}\n\n"
        "相关文档片段:\n{context}\n"
    )
    combine_chain = create_stuff_documents_chain(llm, prompt)
    rag_chain = create_retrieval_chain(retriever, combine_chain)
    return rag_chain, docs_store


def build_market_rag_agent(
    markets_store: Optional[VectorStore] = None,
    *,
    model: Optional[str] = None,
    settings: Optional[Settings] = None,
):
    """构建面向市场研究的 RAG Agent（语义检索 + 工具调用）。

    该 Agent 代表 MVP 的「市场 RAG」形态：

    - 使用向量索引对 Polymarket/Opinion 市场做语义检索；
    - 使用已有 list/get_orderbook 工具查询行情；
    - 由系统 Prompt 引导先检索再比较。

    Args:
        markets_store: 已构建的市场 VectorStore，若为空则自动构建。
        model: 聊天模型名称。
        settings: 可选配置对象。

    Returns:
        (agent, vectorstore, pm_client, op_client) 四元组。
    """
    from langchain.tools import tool

    settings = settings or Settings.load()
    llm = _load_model(model)
    pm_client = PolymarketClient(settings)
    op_client = OpinionClient(settings)

    if markets_store is None:
        import asyncio

        markets_store = asyncio.get_event_loop().run_until_complete(
            build_markets_vectorstore(settings=settings, persist_dir=settings.ensure_data_dir() / "chroma_markets")  # type: ignore[arg-type]
        )

    retriever = markets_store.as_retriever(search_kwargs={"k": 8})

    @tool("semantic_search_markets", return_direct=False)
    def semantic_search_markets(query: str, k: int = 8) -> str:
        """通过语义检索查找与查询最相关的预测市场。"""
        docs = retriever.invoke(query)
        docs = docs[:k]
        lines = []
        for d in docs:
            meta = d.metadata
            lines.append(
                f"{meta.get('platform')} | {meta.get('market_id')} | "
                f"{meta.get('category') or ''} | {d.page_content}"
            )
        return "\n".join(lines)

    base_tools = default_tools(pm_client, op_client)
    tools = [semantic_search_markets, *base_tools]

    system_prompt = (
        "You are a cross-venue prediction market research assistant.\n"
        "Always start by using `semantic_search_markets` to find relevant markets, "
        "then call list_markets/get_orderbook to compare prices when needed.\n"
    )
    agent = create_agent(model=llm, tools=tools, system_prompt=system_prompt)
    return agent, markets_store, pm_client, op_client


def run_question(question: str, model: Optional[str] = None, mode: str = "auto") -> str:
    """CLI `agent` 命令入口，支持多种模式：

    - auto: 依据关键词自动选择 docs RAG 或基础工具 Agent。
    - docs: 强制使用文档 RAG（2-step）。
    - tools: 使用简单工具型 Agent（list/orderbook）。
    - markets: 使用语义检索 + 工具型 Agent（市场研究）。
    - graph: 使用基础 LangGraph Agentic RAG（目前基于文档检索）。
    """

    mode = (mode or "auto").lower()
    lower = question.lower()
    prefer_docs = any(key in lower for key in ["readme", "architecture", "架构", "命令", "cli", "文档"])

    if mode == "docs" or (mode == "auto" and prefer_docs):
        rag_chain, _ = build_docs_rag_chain(model=model)
        resp = rag_chain.invoke({"input": question})
        answer = resp.get("answer") or resp.get("output_text") or resp
        return str(answer)

    if mode == "markets":
        agent, _, pm_client, op_client = build_market_rag_agent(model=model)
        try:
            resp = agent.invoke({"messages": [{"role": "user", "content": question}]})
            return str(resp)
        finally:
            try:
                import asyncio

                asyncio.get_event_loop().run_until_complete(pm_client.close())
                asyncio.get_event_loop().run_until_complete(op_client.close())
            except Exception:
                pass

    if mode == "graph":
        from .agentic_rag_graph import build_agentic_rag_graph

        graph = build_agentic_rag_graph()
        user_msg = {"role": "user", "content": question}
        resp = graph.invoke(
            {
                "messages": [user_msg],
                "question": question,
                "rewritten_question": None,
                "route": None,
                "platform_filter": None,
                "docs": [],
                "context": "",
            }
        )
        messages = resp.get("messages") or []
        if messages:
            return str(messages[-1].get("content") or messages[-1])
        return str(resp)

    # 默认或 mode == "tools"
    agent, pm_client, op_client = build_agent(model=model)
    try:
        resp = agent.invoke({"messages": [{"role": "user", "content": question}]})
        return str(resp)
    finally:
        try:
            import asyncio

            asyncio.get_event_loop().run_until_complete(pm_client.close())
            asyncio.get_event_loop().run_until_complete(op_client.close())
        except Exception:
            pass


__all__ = [
    "build_agent",
    "build_docs_rag_chain",
    "build_market_rag_agent",
    "run_question",
]
