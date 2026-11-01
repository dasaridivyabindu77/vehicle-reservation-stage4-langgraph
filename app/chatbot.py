import re
from datetime import datetime, timedelta

from app.guardrails import sanitize_input

VEHICLE_ALIASES = {
    "compact": "compact",
    "small car": "compact",
    "standard": "standard",
    "car": "standard",
    "vehicle": "standard",
    "motorcycle": "motorcycle",
    "bike": "motorcycle",
}


class Chatbot:
    def __init__(self, vector_store, database, administrator_agent=None):
        self.vector_store = vector_store
        self.database = database
        self.administrator_agent = administrator_agent
        self.sessions = {}
        self.pending_reservations = {}

    def _vehicle_type(self, text):
        lower = text.lower()
        for alias in ("small car", "motorcycle", "standard", "compact", "bike", "car", "vehicle"):
            if re.search(rf"\b{re.escape(alias)}\b", lower):
                return VEHICLE_ALIASES[alias]
        return None

    def process(self, session_id, user_message):
        guardrail = sanitize_input(user_message)
        if not guardrail.allowed:
            return {
                "reply": "I cannot process that request because it appears to ask me to bypass safety controls. Please rephrase.",
                "blocked": True,
                "session": self.sessions.get(session_id, {}),
            }

        text = guardrail.redacted
        state = self.sessions.setdefault(session_id, {})
        lower = text.lower()
        if any(word in lower for word in ("start over", "reset conversation")):
            state = {}
            self.sessions[session_id] = state
            self.pending_reservations.pop(session_id, None)

        reference = self.pending_reservations.get(session_id)
        if reference and not state.get("step") and any(word in lower for word in ("status", "approved", "confirmed", "refused", "request")):
            reservation = self.database.get_reservation(reference)
            if reservation:
                status = reservation["status"]
                if status == "pending":
                    reply = f"Your reservation request {reference} is still waiting for administrator approval."
                elif status == "confirmed":
                    reply = f"Approved. Reservation {reference} is confirmed for {reservation['reservation_date']} at {reservation['arrival_time']}."
                else:
                    reply = f"Reservation {reference} was refused by the administrator. No booking was created."
                return {"reply": reply, "session": {}, "reservation": reservation}

        if state.get("step"):
            return self._reservation_step(session_id, state, text)

        if any(word in lower for word in ("reserve", "reservation", "book", "booking")):
            state["step"] = "vehicle_type"
            return {
                "reply": "I can help with a reservation. Which vehicle type: compact car, standard car, or motorcycle?",
                "session": state,
                "action": "collect_vehicle_type",
            }

        hits = self.vector_store.search(text, top_k=3)
        relevant = [hit for hit in hits if hit["score"] >= 0.05]

        if any(word in lower for word in ("price", "cost", "fee", "rate", "how much")):
            vehicle_type = self._vehicle_type(text) or "standard"
            availability = self.database.get_availability(
                vehicle_type, datetime.now().strftime("%Y-%m-%d"), "09:00", 1
            )
            return {
                "reply": (
                    f"The {vehicle_type} rate is ${availability['hourly_price']:0.2f} per hour. "
                    f"Today at 09:00, {availability['available_count']} of {availability['total']} spaces are available."
                ),
                "session": state,
                "sources": ["Dynamic availability database"] + [hit["title"] for hit in relevant],
                "guardrails": {"redacted": guardrail.redacted != user_message},
            }

        if not relevant:
            reply = (
                "I do not have enough verified facility information to answer that. "
                "You can ask about location, parking zones, booking, payment, or cancellation."
            )
        else:
            reply = " ".join(hit["text"] for hit in relevant[:2])
        return {
            "reply": reply,
            "session": state,
            "sources": [hit["title"] for hit in relevant],
            "guardrails": {"redacted": guardrail.redacted != user_message},
        }

    def _reservation_step(self, session_id, state, text):
        step = state["step"]
        if step == "vehicle_type":
            vehicle_type = self._vehicle_type(text)
            if not vehicle_type:
                return {"reply": "Please choose compact, standard, or motorcycle.", "session": state}
            state["vehicle_type"] = vehicle_type
            state["step"] = "date"
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            return {"reply": f"What date would you like? Use YYYY-MM-DD, for example {tomorrow}.", "session": state}
        if step == "date":
            try:
                reservation_date = datetime.strptime(text.strip(), "%Y-%m-%d").date()
            except ValueError:
                return {"reply": "Please enter a valid date in YYYY-MM-DD format.", "session": state}
            if reservation_date < datetime.now().date():
                return {"reply": "The reservation date cannot be in the past.", "session": state}
            state["reservation_date"] = reservation_date.isoformat()
            state["step"] = "arrival"
            return {"reply": "What arrival time? Use 24-hour HH:MM, for example 09:30.", "session": state}
        if step == "arrival":
            try:
                arrival = datetime.strptime(text.strip(), "%H:%M").strftime("%H:%M")
            except ValueError:
                return {"reply": "Please enter a valid time in HH:MM format.", "session": state}
            state["arrival_time"] = arrival
            state["step"] = "duration"
            return {"reply": "How many hours will you park? Enter a whole number from 1 to 24.", "session": state}
        if step == "duration":
            try:
                duration = int(text.strip())
            except ValueError:
                duration = 0
            if not 1 <= duration <= 24:
                return {"reply": "Duration must be a whole number from 1 to 24.", "session": state}
            state["duration_hours"] = duration
            availability = self.database.get_availability(
                state["vehicle_type"], state["reservation_date"], state["arrival_time"], duration
            )
            if not availability or not availability["spaces"]:
                self.sessions[session_id] = {}
                return {"reply": "No suitable space is available for that period. Please start a new reservation.", "session": {}}
            state["step"] = "confirmation"
            return {
                "reply": (
                    f"Confirm: {state['vehicle_type']} space on {state['reservation_date']} "
                    f"at {state['arrival_time']} for {duration} hour(s). "
                    f"Estimated total: ${availability['estimated_total']:0.2f}. Reply CONFIRM or CANCEL."
                ),
                "session": state,
            }
        if step == "confirmation":
            if "confirm" in text.lower():
                state["step"] = "name"
                return {"reply": "What name should the reservation be held under?", "session": state}
            if "cancel" in text.lower():
                self.sessions[session_id] = {}
                return {"reply": "Reservation cancelled. No booking was created.", "session": {}}
            return {"reply": "Reply CONFIRM to continue or CANCEL to stop.", "session": state}
        if step == "name":
            name = re.sub(r"\s+", " ", text).strip(" .")
            if len(name) < 2 or len(name) > 80 or not re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", name):
                return {"reply": "Please enter a valid name using letters, spaces, hyphens, or apostrophes.", "session": state}
            try:
                result = self.database.create_reservation({
                    "customer_name": name,
                    "vehicle_type": state["vehicle_type"],
                    "reservation_date": state["reservation_date"],
                    "arrival_time": state["arrival_time"],
                    "duration_hours": state["duration_hours"],
                })
            except ValueError as error:
                self.sessions[session_id] = {}
                return {"reply": str(error), "session": {}}
            reservation = self.database.get_reservation(result["reference"])
            if self.administrator_agent:
                self.administrator_agent.submit_request(reservation)
            self.sessions[session_id] = {}
            self.pending_reservations[session_id] = result["reference"]
            return {
                "reply": (
                    f"Reservation request sent to the administrator for approval. Reference {result['reference']}; "
                    f"space {result['space_code']}; total ${result['total_price']:0.2f}. "
                    "Your booking is not confirmed yet. Send 'status' to check the administrator's response."
                ),
                "session": {},
                "reservation": reservation,
            }
        return {"reply": "How can I help?", "session": state}
