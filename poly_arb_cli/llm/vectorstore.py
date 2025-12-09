"""向量索引与检索器构建工具。

本模块提供基于 Chroma 的简单文档与市场向量索引构建函数，
用于 RAG 与 Agentic RAG 场景。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

# 关闭 Chroma 的匿名遥测，避免终端中出现噪音日志。
os.environ.setdefault("CHROMA_TELEMETRY_ENABLED", "false")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

from langchain_community.document_loaders import TextLoader
from langchain_community.vectorstores import Chroma
from langchain_core.vectorstores import VectorStore
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from ..clients.opinion import OpinionClient
from ..clients.polymarket import PolymarketClient
from ..config import Settings
from ..types import Market, Platform


def _default_embeddings(settings: Optional[Settings] = None) -> OpenAIEmbeddings:
    """返回默认的 Embeddings 实例。

    当前使用 OpenAI 兼容接口，模型由环境变量控制。

    Returns:
        OpenAIEmbeddings 实例。
    """

    settings = settings or Settings.load()
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )


def build_docs_vectorstore(
    *,
    paths: Optional[Iterable[Path]] = None,
    persist_dir: Path | None = None,
    settings: Optional[Settings] = None,
) -> VectorStore:
    """构建项目文档的向量索引。

    Args:
        paths: 需要索引的文档路径列表，若为空则默认包含 README 与 docs 目录。
        persist_dir: Chroma 持久化目录，若为空则仅驻留内存。

    Returns:
        构建完成的 VectorStore 对象。
    """
    settings = settings or Settings.load()
    base = Path(".").resolve()
    docs_paths: list[Path] = []

    if paths:
        for p in paths:
            docs_paths.append(p)
    else:
        docs_paths.append(base / "README.md")
        docs_dir = base / "docs"
        if docs_dir.is_dir():
            for child in docs_dir.glob("*.md"):
                docs_paths.append(child)

    documents = []
    for p in docs_paths:
        if not p.is_file():
            continue
        loader = TextLoader(str(p), encoding="utf-8")
        documents.extend(loader.load())

    if not documents:
        raise RuntimeError("No documentation files found for RAG index.")

    splitter = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=200)
    splits = splitter.split_documents(documents)

    embeddings = _default_embeddings(settings)
    if persist_dir:
        persist_dir = Path(persist_dir).expanduser().resolve()
        persist_dir.mkdir(parents=True, exist_ok=True)
    vectorstore = Chroma.from_documents(
        documents=splits,
        embedding=embeddings,
        persist_directory=str(persist_dir) if persist_dir else None,
        collection_name="poly_arb_docs",
    )
    return vectorstore


def _market_to_text(m: Market) -> str:
    """将 Market 对象序列化为适合向量检索的文本。

    Args:
        m: 市场元数据。

    Returns:
        组合后的文本描述。
    """
    parts: list[str] = [m.platform.value, m.market_id, m.title]
    if m.category:
        parts.append(f"category={m.category}")
    if getattr(m, "tags", None):
        tag_str = ", ".join(m.tags or [])
        parts.append(f"tags={tag_str}")
    return " | ".join(parts)


async def build_markets_vectorstore(
    settings: Optional[Settings] = None,
    *,
    limit: int = 1000,
    persist_dir: Path | None = None,
    sort_by: str | None = "volume",
    min_volume: Optional[float] = None,
    min_liquidity: Optional[float] = None,
) -> VectorStore:
    """构建 Polymarket/Opinion 市场的语义向量索引。

    Args:
        settings: 可选配置对象，缺省时自动从环境加载。
        limit: 每个平台最大索引的市场数量。
        persist_dir: Chroma 持久化目录，若为空则仅驻留内存。
        sort_by: 允许按字段排序（支持 "volume" 或 "liquidity"），用于优先索引活跃度高的市场。
        min_volume: 24h 成交量下限，低于该值的市场不进入索引。
        min_liquidity: 流动性下限，低于该值的市场不进入索引。

    Returns:
        构建完成的 VectorStore 对象。
    """
    settings = settings or Settings.load()
    pm_client = PolymarketClient(settings)
    op_client = OpinionClient(settings)

    try:
        pm_markets = await pm_client.list_active_markets(limit=limit)
        op_markets = await op_client.list_active_markets(limit=limit)
    finally:
        # 这里向量构建属于离线操作，构建完成后即关闭客户端。
        import asyncio

        await asyncio.gather(pm_client.close(), op_client.close())

    all_markets: list[Market] = []
    all_markets.extend(pm_markets)
    all_markets.extend(op_markets)

    # 先按阈值过滤，再排序
    if min_volume is not None:
        all_markets = [m for m in all_markets if (m.volume or 0.0) >= min_volume]
    if min_liquidity is not None:
        all_markets = [m for m in all_markets if (m.liquidity or 0.0) >= min_liquidity]

    if sort_by == "volume":
        all_markets.sort(key=lambda m: (m.volume or 0.0), reverse=True)
    elif sort_by == "liquidity":
        all_markets.sort(key=lambda m: (m.liquidity or 0.0), reverse=True)

    from langchain_core.documents import Document

    docs: list[Document] = []
    for m in all_markets:
        text = _market_to_text(m)
        # Chroma 只接受 str/int/float/bool，且不允许 None 或复杂类型。
        meta: dict[str, object] = {
            "platform": m.platform.value,
            "market_id": str(m.market_id),
        }
        if m.condition_id:
            meta["condition_id"] = str(m.condition_id)
        if m.category:
            meta["category"] = str(m.category)
        if getattr(m, "tags", None):
            # 将 tags 列表序列化为逗号分隔字符串，避免复杂类型。
            meta["tags"] = ",".join(str(t) for t in (m.tags or []))
        docs.append(Document(page_content=text, metadata=meta))

    if not docs:
        raise RuntimeError("No markets available for building vector index.")

    embeddings = _default_embeddings(settings)
    if persist_dir:
        persist_dir = Path(persist_dir).expanduser().resolve()
        persist_dir.mkdir(parents=True, exist_ok=True)
    vectorstore = Chroma.from_documents(
        documents=docs,
        embedding=embeddings,
        persist_directory=str(persist_dir) if persist_dir else None,
        collection_name="poly_arb_markets",
    )
    return vectorstore


__all__ = ["build_docs_vectorstore", "build_markets_vectorstore"]
