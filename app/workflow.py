from typing import NotRequired, TypedDict

from langgraph.graph import END, StateGraph


class WorkflowState(TypedDict, total=False):
    session_id: str
    message: str
    response: dict
    reference: str
    decision: str
    reservation: dict
    recorded: bool


class ReservationWorkflow:
    """LangGraph orchestration for chatbot, administrator, and MCP storage stages."""

    def __init__(self, chatbot, administrator_agent, reservation_store):
        self.chatbot = chatbot
        self.administrator_agent = administrator_agent
        self.reservation_store = reservation_store
        self.process_graph = self._build_process_graph()
        self.approval_graph = self._build_approval_graph()

    def _build_process_graph(self):
        graph = StateGraph(WorkflowState)

        def user_interaction(state):
            response = self.chatbot.process(state["session_id"], state["message"])
            return {"response": response, "reservation": response.get("reservation")}

        def administrator_wait(state):
            reservation = state.get("reservation")
            return {
                "reference": reservation["reference"] if reservation else "",
                "response": state["response"],
            }

        def route_after_chat(state):
            reservation = state.get("reservation")
            if reservation and reservation.get("status") == "pending":
                return "administrator_wait"
            return END

        graph.add_node("user_interaction", user_interaction)
        graph.add_node("administrator_wait", administrator_wait)
        graph.set_entry_point("user_interaction")
        graph.add_conditional_edges(
            "user_interaction",
            route_after_chat,
            {"administrator_wait": "administrator_wait", END: END},
        )
        graph.add_edge("administrator_wait", END)
        return graph.compile()

    def _build_approval_graph(self):
        graph = StateGraph(WorkflowState)

        def administrator_approval(state):
            reservation = self.administrator_agent.respond(
                state["reference"], state["decision"]
            )
            return {"reservation": reservation}

        def mcp_recording(state):
            reservation = state.get("reservation")
            recorded = False
            if reservation and reservation["status"] == "confirmed":
                recorded = self.reservation_store.write_confirmed(reservation)
            return {"recorded": recorded}

        def route_after_decision(state):
            reservation = state.get("reservation")
            if reservation and reservation["status"] == "confirmed":
                return "mcp_recording"
            return END

        graph.add_node("administrator_approval", administrator_approval)
        graph.add_node("mcp_recording", mcp_recording)
        graph.set_entry_point("administrator_approval")
        graph.add_conditional_edges(
            "administrator_approval",
            route_after_decision,
            {"mcp_recording": "mcp_recording", END: END},
        )
        graph.add_edge("mcp_recording", END)
        return graph.compile()

    def process_message(self, session_id, message):
        result = self.process_graph.invoke({
            "session_id": session_id,
            "message": message,
        })
        return result["response"]

    def process_admin_decision(self, reference, decision):
        result = self.approval_graph.invoke({
            "reference": reference,
            "decision": decision,
        })
        return result.get("reservation")
