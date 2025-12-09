"""基于 LangGraph 的 Agentic RAG 图（正式流程）。

当前图包含若干节点：

- classify: 判定问题类型（docs / markets / tools），可附带平台过滤；
- query_rewrite: 将用户问题改写为更利于检索的短句；
- retrieve: 按类型调用对应 retriever 或实时 API 聚合上下文；
- grade: 让 LLM 选择最相关的文档片段，过滤噪声；
- answer: 基于上下文生成回答（包括 tools 节返回的动态数据）；
- answer_check: 检查回答是否被上下文支持，不足则提示。

其中 tools 分支用于“动态数据”问题（如成交量最大市场），
会直接调用 Polymarket/Opinion API 获取最新信息，再交由 LLM
进行归纳总结。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, TypedDict

from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from ..clients.opinion import OpinionClient
from ..clients.polymarket import PolymarketClient
from ..config import Settings
from ..types import Market
from .vectorstore import build_docs_vectorstore, build_markets_vectorstore


class RagState(TypedDict):
    """Agentic RAG 图的状态定义。"""

    messages: list[dict]
    question: str
    rewritten_question: str | None
    route: str | None
    platform_filter: str | None
    docs: list[Document]
    context: str


def _load_docs_store(settings: Settings) -> VectorStore:
    target = settings.ensure_data_dir() / "chroma_docs"
    return build_docs_vectorstore(persist_dir=target, settings=settings)


def _load_markets_store(settings: Settings, *, limit: int = 1500) -> VectorStore:
    target = settings.ensure_data_dir() / "chroma_markets"
    return asyncio.get_event_loop().run_until_complete(
        build_markets_vectorstore(settings=settings, limit=limit, persist_dir=target)
    )


def build_agentic_rag_graph() -> Any:
    """构建 Agentic RAG LangGraph。

    Returns:
        编译后的 LangGraph workflow，可通过 `.invoke` / `.stream` 调用。
    """

    settings = Settings.load()
    llm = ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )

    docs_store = _load_docs_store(settings)
    markets_store = _load_markets_store(settings)

    docs_retriever = docs_store.as_retriever(search_kwargs={"k": 6})

    def classify_node(state: RagState) -> RagState:
        """分类问题类型与平台过滤（避免强制 JSON 模式）。"""

        question = state.get("question") or state["messages"][-1].get("content", "")

        # 先做一个简单 heuristics，作为 JSON 解析失败时的兜底。
        q_lower = question.lower()
        prefer_docs = any(
            key in q_lower for key in ["readme", "architecture", "架构", "命令", "cli", "文档"]
        )
        prefer_tools = any(
            key in q_lower for key in ["成交量", "24h", "24小时", "流动性", "liquidity", "按成交量", "按流动性"]
        )
        if prefer_tools:
            default_route = "tools"
        elif prefer_docs:
            default_route = "docs"
        else:
            default_route = "markets"

        route = default_route
        platform: str | None = None

        prompt = (
            "你是一个路由器，负责判断用户问题属于哪一类：\n"
            "- 若问题主要是关于项目 README、架构、配置、命令用法，route=docs；\n"
            "- 若问题主要是关于市场、价格、盘口、成交量、套利，route=markets。\n"
            "同时，如果问题中明显提到了 polymarket 或 opinion，可以在 platform 中标记对应平台，"
            "否则 platform 设为 null。\n\n"
            "请严格输出一个 JSON，对象格式如下（不要添加任何说明文字）：\n"
            '{"route": "docs|markets", "platform": "polymarket|opinion|null"}\n\n'
            f"用户问题：{question}\n"
        )

        try:
            resp = llm.invoke(prompt)
            text = (getattr(resp, "content", "") or "").strip()
            data = json.loads(text)
            route = str(data.get("route") or default_route).lower()
            platform_raw = data.get("platform")
            if isinstance(platform_raw, str):
                platform_raw = platform_raw.strip().lower()
                platform = platform_raw if platform_raw in {"polymarket", "opinion"} else None
        except Exception:
            # 解析失败时沿用 heuristics
            route = default_route
            platform = None

        if route not in {"docs", "markets", "tools"}:
            route = default_route

        return {
            **state,
            "route": route,
            "platform_filter": platform,
            "question": question,
            "rewritten_question": None,
            "docs": [],
        }

    def rewrite_node(state: RagState) -> RagState:
        """将问题改写为更利于检索的短句。"""

        question = state.get("question") or state["messages"][-1].get("content", "")
        prompt = (
            "请将下面的问题改写成简洁、利于检索的表达，保持原语言，不要添加无关信息。"
            "如果已经足够简洁，则原样返回。\n\n"
            f"问题: {question}"
        )
        resp = llm.invoke(prompt)
        rewritten = (getattr(resp, "content", None) or "").strip() or question
        return {**state, "rewritten_question": rewritten}

    def retrieve_node(state: RagState) -> RagState:
        """根据路由从对应向量索引检索上下文。"""

        question = (
            state.get("rewritten_question")
            or state.get("question")
            or state["messages"][-1].get("content", "")
        )
        route = (state.get("route") or "docs").lower()
        platform_filter = (state.get("platform_filter") or "").lower()

        docs: list[Document] = []
        if route == "tools":
            # tools 分支：直接通过 API 实时获取成交量最高市场等动态信息，
            # 然后将结果串联为上下文文本，交由后续 answer 节点生成自然语言回答。
            try:
                platforms: list[str]
                if platform_filter in {"polymarket", "opinion"}:
                    platforms = [platform_filter]
                else:
                    platforms = ["polymarket"]

                async def _fetch() -> list[Market]:
                    pm_client = PolymarketClient(settings)
                    op_client = OpinionClient(settings)
                    try:
                        results: list[Market] = []
                        for p in platforms:
                            if p == "polymarket":
                                pm_markets = await pm_client.list_active_markets(limit=200)
                                pm_markets.sort(key=lambda m: (m.volume or 0.0), reverse=True)
                                results.extend(pm_markets[:10])
                            elif p == "opinion":
                                op_markets = await op_client.list_active_markets(limit=200)
                                op_markets.sort(key=lambda m: (m.volume or 0.0), reverse=True)
                                results.extend(op_markets[:10])
                        return results[:10]
                    finally:
                        await asyncio.gather(pm_client.close(), op_client.close())

                top_markets = asyncio.run(_fetch())
                if not top_markets:
                    serialized = "未能从实时 API 获取到任何活跃市场，可能是上游接口暂不可用。"
                else:
                    lines: list[str] = [
                        "以下为根据实时 24h 成交量排序的活跃市场（最多前 10 条）："
                    ]
                    for m in top_markets:
                        lines.append(
                            f"- [{m.platform.value}] {m.market_id} | {m.title} | "
                            f"24hVolume={m.volume or 0:.2f} | Liquidity={m.liquidity or 0:.2f}"
                        )
                    serialized = "\n".join(lines)
            except Exception as exc:  # noqa: BLE001
                serialized = f"实时查询市场信息失败：{exc}"

            return {**state, "docs": [], "context": serialized}

        if route == "markets":
            search_kwargs = {"k": 8}
            if platform_filter in {"polymarket", "opinion"}:
                search_kwargs["filter"] = {"platform": platform_filter}
            retriever = markets_store.as_retriever(search_kwargs=search_kwargs)
            docs = retriever.invoke(question)
        else:
            docs = docs_retriever.invoke(question)

        serialized = "\n\n".join(
            f"Source: {d.metadata}\nContent: {d.page_content}" for d in docs
        )
        return {**state, "docs": docs, "context": serialized}

    def grade_node(state: RagState) -> RagState:
        """让 LLM 选择最相关的文档，降低无关噪声。"""

        docs = state.get("docs") or []
        if not docs:
            return state

        question = state.get("rewritten_question") or state.get("question") or ""
        # 构造一个简化的打分提示，把每个文档编号后呈现
        snippets = []
        for idx, doc in enumerate(docs):
            content = doc.page_content
            meta = doc.metadata
            snippets.append(f"[{idx}] {meta} :: {content[:500]}")
        prompt = (
            "根据用户问题，选择最相关的文档编号列表，最多保留 4 个。\n"
            "仅输出用逗号分隔的编号，不要其他内容。\n\n"
            f"问题: {question}\n\n"
            "文档片段:\n" + "\n\n".join(snippets)
        )
        try:
            resp = llm.invoke(prompt)
            text = (getattr(resp, "content", "") or "").strip()
            keep_indices = []
            for part in text.replace("，", ",").split(","):
                part = part.strip()
                if part.isdigit():
                    keep_indices.append(int(part))
            keep_indices = [i for i in keep_indices if 0 <= i < len(docs)][:4]
        except Exception:
            keep_indices = []

        if keep_indices:
            filtered_docs = [docs[i] for i in keep_indices]
        else:
            filtered_docs = docs[:4]  # 保守策略：缺省取前 4 条

        serialized = "\n\n".join(
            f"Source: {d.metadata}\nContent: {d.page_content}" for d in filtered_docs
        )
        return {**state, "docs": filtered_docs, "context": serialized}

    def answer_node(state: RagState) -> RagState:
        """基于检索上下文生成回答，避免胡编。"""

        question = state.get("question") or state["messages"][-1].get("content", "")
        context = state.get("context") or ""
        answer_llm = llm
        prompt = (
            "你是跨平台预测市场助手。使用下面的上下文回答用户问题，"
            "若信息不足请直说不知道，不要编造。\n\n"
            f"上下文:\n{context}\n\n"
            f"问题:\n{question}\n"
        )
        resp = answer_llm.invoke(prompt)
        messages = state.get("messages", [])
        messages.append({"role": "assistant", "content": resp.content})
        return {**state, "messages": messages}

    def answer_check_node(state: RagState) -> RagState:
        """检查回答是否被上下文支持，若不支持则提示信息不足。"""

        messages = state.get("messages", [])
        if not messages:
            return state
        last = messages[-1].get("content", "")
        context = state.get("context") or ""

        # 为了兼容任意 OpenAI 兼容后端，这里不使用 structured_output，
        # 而是让模型返回简单的 YES/NO + 原因，并手动解析。
        prompt = (
            "请判断下面的回答是否被给定的上下文充分支持。\n"
            "如果支持，请以 'YES: 原因' 格式回答；如果不支持，请以 'NO: 原因' 格式回答。\n\n"
            f"上下文:\n{context}\n\n"
            f"回答:\n{last}\n"
        )
        resp = llm.invoke(prompt)
        text = (getattr(resp, "content", "") or "").strip()
        verdict = text.split(":", 1)[0].strip().upper()
        reason = text.split(":", 1)[1].strip() if ":" in text else ""

        if verdict.startswith("NO"):
            fallback = (
                "根据提供的上下文信息不足，无法给出可靠结论。"
                f"原因: {reason or '回答与上下文不一致或缺乏支撑。'}"
            )
            messages[-1]["content"] = fallback

        return {**state, "messages": messages}

    graph_builder = StateGraph(RagState)
    graph_builder.add_node("classify", classify_node)
    graph_builder.add_node("query_rewrite", rewrite_node)
    graph_builder.add_node("retrieve", retrieve_node)
    graph_builder.add_node("grade", grade_node)
    graph_builder.add_node("answer", answer_node)
    graph_builder.add_node("answer_check", answer_check_node)

    graph_builder.add_edge(START, "classify")
    graph_builder.add_edge("classify", "query_rewrite")
    graph_builder.add_edge("query_rewrite", "retrieve")
    graph_builder.add_edge("retrieve", "grade")
    graph_builder.add_edge("grade", "answer")
    graph_builder.add_edge("answer", "answer_check")
    graph_builder.add_edge("answer_check", END)

    workflow = graph_builder.compile()
    return workflow


__all__ = ["build_agentic_rag_graph", "RagState"]
