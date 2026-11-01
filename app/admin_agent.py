from langchain_core.prompts import PromptTemplate


class AdministratorAgent:
    """LangChain-based agent that prepares and processes administrator approval requests."""

    def __init__(self, database, reservation_store=None):
        self.database = database
        self.reservation_store = reservation_store
        self.request_template = PromptTemplate.from_template(
            """Reservation approval request
Reference: {reference}
Customer: {customer_name}
Vehicle: {vehicle_type}
Date: {reservation_date}
Arrival: {arrival_time}
Duration: {duration_hours} hour(s)
Space: {space_code}
Total: ${total_price}
Status: {status}"""
        )

    def submit_request(self, reservation):
        message = self.request_template.format(
            reference=reservation["reference"],
            customer_name=reservation["customer_name"],
            vehicle_type=reservation["vehicle_type"],
            reservation_date=reservation["reservation_date"],
            arrival_time=reservation["arrival_time"],
            duration_hours=reservation["duration_hours"],
            space_code=reservation["space_code"],
            total_price=f"{reservation['total_price']:0.2f}",
            status=reservation["status"],
        )
        return {"reservation": reservation, "message": message}

    def list_requests(self, status="pending"):
        return self.database.list_reservation_requests(status)

    def respond(self, reference, decision):
        normalized = decision.strip().lower()
        if normalized in {"approve", "approved", "confirm", "confirmed"}:
            status = "confirmed"
        elif normalized in {"refuse", "refused", "reject", "rejected", "deny", "denied"}:
            status = "refused"
        else:
            raise ValueError("Decision must be approve or refuse.")
        current = self.database.get_reservation(reference)
        if not current:
            return None
        return self.database.update_reservation_status(reference, status)


HumanAdministratorAgent = AdministratorAgent
