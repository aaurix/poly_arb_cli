"""基于 LangGraph 的 Agentic RAG 图（正式流程）。

当前图包含六个节点：

- classify: 判定问题类型（docs / markets），可附带平台过滤；
- query_rewrite: 将用户问题改写为更利于检索的短句；
- retrieve: 按类型调用对应 retriever 聚合上下文；
- grade: 让 LLM 选择最相关的文档片段，过滤噪声；
- answer: 基于上下文生成回答；
- answer_check: 检查回答是否被上下文支持，不足则提示。
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal, TypedDict

from langchain_openai import ChatOpenAI
from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from ..config import Settings
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


class RouteDecision(BaseModel):
    """用于分类节点的结构化输出。"""

    route: Literal["docs", "markets"] = Field(description="问题更像文档问答还是市场研究")
    platform: str | None = Field(
        default=None, description="可选的平台过滤，如 polymarket/opinion/all"
    )


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
    markets_retriever = markets_store.as_retriever(search_kwargs={"k": 8})

    router = llm.with_structured_output(RouteDecision)
    grader = llm.with_structured_output(
        BaseModel.model_construct,  # placeholder, see grade_node for custom logic
    )

    def classify_node(state: RagState) -> RagState:
        """使用结构化输出分类问题类型与平台过滤。"""

        question = state.get("question") or state["messages"][-1].get("content", "")
        decision = router.invoke(
            [
                {
                    "role": "system",
                    "content": (
                        "判断用户的问题更像文档问答还是市场研究："
                        "若涉及 CLI 用法、架构、配置 -> docs；"
                        "若涉及市场、价格、盘口、套利 -> markets。"
                        "若问题显式提到 polymarket/opinion，也写入 platform；否则 None。"
                    ),
                },
                {"role": "user", "content": question},
            ]
        )
        return {
            **state,
            "route": decision.route,
            "platform_filter": (decision.platform or None),
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
        if route == "markets":
            search_kwargs = {"k": 8}
            if platform_filter in {"polymarket", "opinion"}:
                search_kwargs["filter"] = {"platform": platform_filter}
            docs = markets_retriever.invoke(question, search_kwargs=search_kwargs)
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

    class AnswerCheck(BaseModel):
        supported: bool = Field(description="回答是否被上下文充分支持")
        reason: str = Field(description="简要说明")

    def answer_check_node(state: RagState) -> RagState:
        """检查回答是否被上下文支持，若不支持则提示信息不足。"""

        messages = state.get("messages", [])
        if not messages:
            return state
        last = messages[-1].get("content", "")
        context = state.get("context") or ""

        checker = llm.with_structured_output(AnswerCheck)
        decision = checker.invoke(
            [
                {
                    "role": "system",
                    "content": "判断回答是否完全基于给定上下文，不足则标记为不支持。",
                },
                {
                    "role": "user",
                    "content": f"上下文:\n{context}\n\n回答:\n{last}",
                },
            ]
        )
        if not decision.supported:
            fallback = (
                "根据提供的上下文信息不足，无法给出可靠结论。"
                f"原因: {decision.reason}"
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
