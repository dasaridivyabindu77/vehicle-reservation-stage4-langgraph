import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


class ReservationFileStore:
    """Thread-safe, append-only storage for confirmed reservations."""

    def __init__(self, file_path: str | Path):
        self.path = Path(file_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._state_path = self.path.with_suffix(self.path.suffix + ".approved.json")

    @staticmethod
    def _clean(value, max_length=120):
        text = str(value or "").replace("|", "/").replace("\r", " ").replace("\n", " ").strip()
        return text[:max_length]

    def _written_references(self):
        if not self._state_path.exists():
            return set()
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return set(data if isinstance(data, list) else [])
        except (json.JSONDecodeError, OSError):
            return set()

    def write_confirmed(self, reservation: dict, approval_time=None) -> bool:
        reference = str(reservation.get("reference", "")).strip()
        if not reference:
            raise ValueError("Reservation reference is required.")

        with self._lock:
            written = self._written_references()
            if reference in written:
                return False

            name = self._clean(reservation.get("customer_name"), 80)
            car_number = self._clean(reservation.get("space_code") or reservation.get("car_number"), 40)
            date = self._clean(reservation.get("reservation_date"), 20)
            arrival = self._clean(reservation.get("arrival_time"), 10)
            duration = reservation.get("duration_hours")
            try:
                duration_text = f"{int(duration)} hour(s)"
            except (TypeError, ValueError):
                duration_text = str(duration or "").strip()
            reservation_period = self._clean(
                f"{date} {arrival}" + (f" for {duration_text}" if duration_text else ""),
                100,
            )
            approval_time = approval_time or datetime.now(timezone.utc).isoformat(timespec="seconds")
            line = f"{name} | {car_number} | {reservation_period} | {self._clean(approval_time, 40)}\n"

            with self.path.open("a", encoding="utf-8", newline="\n") as output:
                output.write(line)
                output.flush()
                os.fsync(output.fileno())

            written.add(reference)
            temporary = self._state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(sorted(written)), encoding="utf-8")
            os.replace(temporary, self._state_path)
            return True
