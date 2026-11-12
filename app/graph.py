"""LangGraph turn orchestration.

Each user turn flows through a compiled state graph:

    input_guard ─▶ route ─┬▶ reserve ───────────────▶ output_guard ─▶ END
                          └▶ retrieve ─▶ generate ──▶ output_guard ─▶ END
    (blocked input short-circuits straight to END)

The nodes delegate the real work to the :class:`~app.chatbot.Chatbot` passed in
at build time (retrieval, grounding, generation, reservation slot-filling,
redaction). Keeping the logic on the bot makes each node a thin, testable step
while still giving us an explicit, inspectable graph.
"""

from __future__ import annotations

from typing import List, Optional, Tuple, TypedDict

from langchain_core.documents import Document
from langgraph.graph import END, START, StateGraph


class TurnState(TypedDict, total=False):
    message: str
    session_id: str
    intent: str  # "reserve" | "info" | "smalltalk"
    documents: List[Tuple[Document, float]]
    dynamic_context: str
    answer: str
    refused: bool
    blocked: bool


def build_turn_graph(bot):
    """Compile the per-turn StateGraph bound to a chatbot instance."""

    def input_guard(state: TurnState) -> TurnState:
        verdict = bot.guard_input(state["message"])
        if not verdict.allowed:
            return {"blocked": True, "answer": verdict.reason, "intent": "blocked"}
        return {"blocked": False}

    def route(state: TurnState) -> TurnState:
        return {"intent": bot.classify(state["message"], state["session_id"])}

    def reserve(state: TurnState) -> TurnState:
        answer = bot.run_reservation(state["session_id"], state["message"])
        return {"answer": answer, "refused": False}

    def retrieve(state: TurnState) -> TurnState:
        docs, dynamic_ctx = bot.retrieve(state["message"])
        return {"documents": docs, "dynamic_context": dynamic_ctx}

    def generate(state: TurnState) -> TurnState:
        answer, refused = bot.generate(
            state["message"],
            state.get("documents", []),
            state.get("dynamic_context", ""),
        )
        return {"answer": answer, "refused": refused}

    def output_guard(state: TurnState) -> TurnState:
        return {"answer": bot.guard_output(state.get("answer", ""), state["session_id"])}

    def after_input(state: TurnState) -> str:
        return "blocked" if state.get("blocked") else "route"

    def after_route(state: TurnState) -> str:
        return "reserve" if state.get("intent") == "reserve" else "retrieve"

    graph = StateGraph(TurnState)
    graph.add_node("input_guard", input_guard)
    graph.add_node("route", route)
    graph.add_node("reserve", reserve)
    graph.add_node("retrieve", retrieve)
    graph.add_node("generate", generate)
    graph.add_node("output_guard", output_guard)

    graph.add_edge(START, "input_guard")
    graph.add_conditional_edges(
        "input_guard", after_input, {"blocked": END, "route": "route"}
    )
    graph.add_conditional_edges(
        "route", after_route, {"reserve": "reserve", "retrieve": "retrieve"}
    )
    graph.add_edge("retrieve", "generate")
    graph.add_edge("reserve", "output_guard")
    graph.add_edge("generate", "output_guard")
    graph.add_edge("output_guard", END)

    return graph.compile()
