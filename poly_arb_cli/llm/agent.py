from __future__ import annotations

import os
from typing import Optional

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from langchain_core.vectorstores import VectorStore

from ..config import Settings
from .vectorstore import build_docs_vectorstore, build_markets_vectorstore


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


def run_question(question: str, model: Optional[str] = None, mode: str = "auto") -> str:
    """CLI `agent` 命令入口。

    长期设计：统一通过 Agentic RAG Graph（Graph + retriever + LLM）执行，
    `mode` 仅作为初始路由提示：

    - auto: 让图中的 classify 节点自动判定 docs/markets。
    - docs: 强制偏向文档问答（route=docs）。
    - markets: 强制偏向市场研究（route=markets）。
    - graph: 与 auto 类似，仅显式指定使用图。
    - tools: 暂时视为 auto，与 Graph 配合使用（保留兼容性）。
    """

    from .agentic_rag_graph import build_agentic_rag_graph

    mode = (mode or "auto").lower()
    lower = question.lower()
    prefer_docs = any(key in lower for key in ["readme", "architecture", "架构", "命令", "cli", "文档"])

    graph = build_agentic_rag_graph()
    user_msg = {"role": "user", "content": question}

    route_hint: str | None = None
    if mode == "docs" or (mode == "auto" and prefer_docs):
        route_hint = "docs"
    elif mode == "markets":
        route_hint = "markets"
    else:
        route_hint = None

    initial_state = {
        "messages": [user_msg],
        "question": question,
        "rewritten_question": None,
        "route": route_hint,
        "platform_filter": None,
        "docs": [],
        "context": "",
    }
    resp = graph.invoke(initial_state)
    messages = resp.get("messages") or []
    if messages:
        return str(messages[-1].get("content") or messages[-1])
    return str(resp)


__all__ = [
    "build_docs_rag_chain",
    "run_question",
]
