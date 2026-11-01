import hmac
import os
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.reservation_store import ReservationFileStore


app = FastAPI(title="Confirmed Reservation MCP Server")
store = ReservationFileStore(
    os.environ.get(
        "RESERVATION_FILE",
        os.path.join(os.path.dirname(__file__), "data", "confirmed_reservations.txt"),
    )
)
MCP_API_KEY = os.environ.get("MCP_API_KEY")


class ConfirmedReservation(BaseModel):
    reference: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    car_number: str = Field(min_length=1, max_length=40)
    reservation_period: str = Field(min_length=1, max_length=100)
    approval_status: Literal["confirmed"] = "confirmed"
    approval_time: str | None = Field(default=None, max_length=40)


@app.get("/health")
def health():
    return {"status": "ok", "storage": str(store.path)}


@app.post("/mcp/tools/write_confirmed_reservation")
def write_confirmed_reservation(
    payload: ConfirmedReservation,
    authorization: str | None = Header(default=None),
):
    if MCP_API_KEY:
        supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied, MCP_API_KEY):
            raise HTTPException(status_code=401, detail="Unauthorized MCP access.")
    reservation = {
        "reference": payload.reference,
        "customer_name": payload.name,
        "space_code": payload.car_number,
        "reservation_date": payload.reservation_period,
        "arrival_time": "",
    }
    try:
        written = store.write_confirmed(reservation, payload.approval_time)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=500, detail="Unable to store reservation.") from error
    return {"written": written, "file": str(store.path)}
