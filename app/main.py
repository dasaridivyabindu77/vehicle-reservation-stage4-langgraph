import os
import hmac
from flask import Flask, jsonify, render_template, request, session

from app.admin_agent import AdministratorAgent
from app.chatbot import Chatbot
from app.database import Database
from app.reservation_store import ReservationFileStore
from app.vector_store import StaticVectorStore
from app.workflow import ReservationWorkflow

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")


def create_app(test_config=None):
    config = test_config or {}
    app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"), static_folder=os.path.join(BASE_DIR, "static"))
    app.secret_key = config.get("SECRET_KEY", os.environ.get("CHATBOT_SECRET_KEY", "dev-only-change-me"))
    db_path = config.get("DATABASE", os.path.join(DATA_DIR, "chatbot.db"))
    chroma_path = config.get("CHROMA_PATH", os.path.join(DATA_DIR, "chroma"))
    database = Database(db_path)
    database.initialize()
    vector_store = StaticVectorStore(chroma_path, os.path.join(DATA_DIR, "static_knowledge.json"))
    reservation_file = config.get(
        "RESERVATION_FILE",
        os.environ.get("RESERVATION_FILE", os.path.join(DATA_DIR, "confirmed_reservations.txt")),
    )
    reservation_store = ReservationFileStore(reservation_file)
    administrator_agent = AdministratorAgent(database, reservation_store)
    admin_api_key = config.get("ADMIN_API_KEY", os.environ.get("ADMIN_API_KEY"))
    chatbot = Chatbot(vector_store, database, administrator_agent)
    workflow = ReservationWorkflow(chatbot, administrator_agent, reservation_store)
    app.extensions["chatbot"] = chatbot
    app.extensions["administrator_agent"] = administrator_agent
    app.extensions["reservation_store"] = reservation_store
    app.extensions["reservation_workflow"] = workflow

    def admin_authorized():
        if not admin_api_key:
            return True
        supplied = request.headers.get("X-Admin-Key", "")
        return hmac.compare_digest(supplied, str(admin_api_key))

    @app.get("/")
    def index():
        if "chat_session_id" not in session:
            session["chat_session_id"] = os.urandom(16).hex()
        return render_template("index.html")

    @app.post("/api/chat")
    def chat():
        payload = request.get_json(silent=True) or {}
        message = payload.get("message", "")
        if not isinstance(message, str) or not message.strip():
            return jsonify({"error": "Message is required."}), 400
        if "chat_session_id" not in session:
            session["chat_session_id"] = os.urandom(16).hex()
        response = workflow.process_message(session["chat_session_id"], message.strip())
        return jsonify(response)

    @app.post("/api/reset")
    def reset():
        session_id = session.get("chat_session_id")
        if session_id:
            chatbot.sessions.pop(session_id, None)
            chatbot.pending_reservations.pop(session_id, None)
        return jsonify({"reply": "Conversation reset.", "session": {}})

    @app.get("/api/admin/requests")
    def admin_requests():
        if not admin_authorized():
            return jsonify({"error": "Unauthorized administrator access."}), 401
        status = request.args.get("status", "pending")
        return jsonify({"requests": administrator_agent.list_requests(status if status != "all" else None)})

    def admin_decision(reference, decision):
        if not admin_authorized():
            return jsonify({"error": "Unauthorized administrator access."}), 401
        try:
            reservation = workflow.process_admin_decision(reference, decision)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        except OSError as error:
            return jsonify({"error": "Unable to persist the confirmed reservation."}), 500
        if not reservation:
            return jsonify({"error": "Reservation request not found."}), 404
        return jsonify({"reservation": reservation})

    @app.post("/api/admin/respond")
    def admin_respond():
        if not admin_authorized():
            return jsonify({"error": "Unauthorized administrator access."}), 401
        payload = request.get_json(silent=True) or {}
        reference = payload.get("reference")
        decision = payload.get("decision")
        if not reference or not decision:
            return jsonify({"error": "Reference and decision are required."}), 400
        return admin_decision(reference, decision)

    @app.post("/api/admin/requests/<reference>/approve")
    def admin_approve(reference):
        return admin_decision(reference, "approve")

    @app.post("/api/admin/requests/<reference>/refuse")
    def admin_refuse(reference):
        return admin_decision(reference, "refuse")

    @app.post("/api/admin/requests/<reference>/reject")
    def admin_reject(reference):
        return admin_decision(reference, "refuse")

    @app.post("/api/mcp/tools/write_confirmed_reservation")
    def mcp_write_confirmed_reservation():
        mcp_key = config.get("MCP_API_KEY", os.environ.get("MCP_API_KEY", admin_api_key))
        supplied = request.headers.get("Authorization", "")
        if mcp_key and not (
            supplied.startswith("Bearer ")
            and hmac.compare_digest(supplied[7:], str(mcp_key))
        ):
            return jsonify({"error": "Unauthorized MCP access."}), 401
        payload = request.get_json(silent=True) or {}
        if payload.get("approval_status", "confirmed").lower() != "confirmed":
            return jsonify({"error": "Only confirmed reservations may be stored."}), 400
        required = ("name", "car_number", "reservation_period")
        if any(not str(payload.get(field, "")).strip() for field in required):
            return jsonify({"error": "name, car_number, and reservation_period are required."}), 400
        reservation = {
            "reference": payload.get("reference", "MCP-" + os.urandom(6).hex().upper()),
            "customer_name": payload["name"],
            "space_code": payload["car_number"],
            "reservation_date": payload["reservation_period"],
            "arrival_time": "",
        }
        try:
            written = reservation_store.write_confirmed(reservation, payload.get("approval_time"))
        except (OSError, ValueError) as error:
            return jsonify({"error": "Unable to store reservation.", "detail": str(error)}), 500
        return jsonify({"written": written, "file": str(reservation_store.path)})

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "vector_documents": vector_store.collection.count(), "dynamic_database": "connected"})

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
