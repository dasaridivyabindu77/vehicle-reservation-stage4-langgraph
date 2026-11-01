import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_PRICES = {"compact": 4.0, "standard": 6.0, "motorcycle": 3.0}
DEFAULT_CAPACITY = {"compact": 12, "standard": 20, "motorcycle": 8}


class Database:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self):
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS spaces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vehicle_type TEXT NOT NULL,
                    zone TEXT NOT NULL,
                    space_code TEXT NOT NULL UNIQUE,
                    hourly_price REAL NOT NULL CHECK(hourly_price >= 0)
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    customer_name TEXT NOT NULL,
                    vehicle_type TEXT NOT NULL,
                    reservation_date TEXT NOT NULL,
                    arrival_time TEXT NOT NULL,
                    duration_hours INTEGER NOT NULL CHECK(duration_hours BETWEEN 1 AND 24),
                    space_code TEXT NOT NULL,
                    total_price REAL NOT NULL CHECK(total_price >= 0),
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reservation_lookup
                ON reservations(reservation_date, arrival_time, space_code, status);
                """
            )
            existing = db.execute("SELECT COUNT(*) FROM spaces").fetchone()[0]
            if existing == 0:
                rows = []
                for vehicle_type, capacity in DEFAULT_CAPACITY.items():
                    zone = {"compact": "A", "standard": "B", "motorcycle": "C"}[vehicle_type]
                    for index in range(1, capacity + 1):
                        rows.append((vehicle_type, zone, f"{zone}{index:02d}", DEFAULT_PRICES[vehicle_type]))
                db.executemany(
                    "INSERT INTO spaces(vehicle_type, zone, space_code, hourly_price) VALUES (?, ?, ?, ?)",
                    rows,
                )

    def get_availability(self, vehicle_type, reservation_date, arrival_time, duration=1):
        if vehicle_type not in DEFAULT_PRICES:
            return None
        start = datetime.strptime(f"{reservation_date} {arrival_time}", "%Y-%m-%d %H:%M")
        end = start + timedelta(hours=duration)
        with self.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM spaces WHERE vehicle_type = ?", (vehicle_type,)
            ).fetchone()[0]
            occupied = db.execute(
                """
                SELECT space_code, reservation_date, arrival_time, duration_hours
                FROM reservations WHERE vehicle_type = ? AND status = 'confirmed'
                """,
                (vehicle_type,),
            ).fetchall()
            unavailable = set()
            for item in occupied:
                existing_start = datetime.strptime(
                    f"{item['reservation_date']} {item['arrival_time']}", "%Y-%m-%d %H:%M"
                )
                existing_end = existing_start + timedelta(hours=item["duration_hours"])
                if start < existing_end and end > existing_start:
                    unavailable.add(item["space_code"])
            open_spaces = db.execute(
                "SELECT space_code FROM spaces WHERE vehicle_type = ? ORDER BY space_code",
                (vehicle_type,),
            ).fetchall()
            available = [row["space_code"] for row in open_spaces if row["space_code"] not in unavailable]
            price = db.execute(
                "SELECT hourly_price FROM spaces WHERE vehicle_type = ? LIMIT 1", (vehicle_type,)
            ).fetchone()["hourly_price"]
        return {
            "total": total,
            "available_count": len(available),
            "spaces": available[:5],
            "hourly_price": price,
            "estimated_total": round(price * duration, 2),
        }

    def create_reservation(self, payload):
        availability = self.get_availability(
            payload["vehicle_type"], payload["reservation_date"],
            payload["arrival_time"], payload["duration_hours"],
        )
        if not availability or not availability["spaces"]:
            raise ValueError("No parking spaces are available for that period.")
        reference = "VR-" + secrets.token_hex(3).upper()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO reservations(
                    reference, customer_name, vehicle_type, reservation_date,
                    arrival_time, duration_hours, space_code, total_price, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reference, payload["customer_name"], payload["vehicle_type"],
                    payload["reservation_date"], payload["arrival_time"],
                    payload["duration_hours"], availability["spaces"][0],
                    availability["estimated_total"], datetime.utcnow().isoformat(timespec="seconds"),
                    "pending",
                ),
            )
        return {"reference": reference, "space_code": availability["spaces"][0], "total_price": availability["estimated_total"]}

    def get_reservation(self, reference):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM reservations WHERE reference = ?", (reference,)
            ).fetchone()
        return dict(row) if row else None

    def list_reservation_requests(self, status=None):
        with self.connect() as db:
            if status:
                rows = db.execute(
                    "SELECT * FROM reservations WHERE status = ? ORDER BY id DESC", (status,)
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM reservations ORDER BY id DESC"
                ).fetchall()
        return [dict(row) for row in rows]

    def update_reservation_status(self, reference, status):
        if status not in {"pending", "confirmed", "refused", "cancelled"}:
            raise ValueError("Invalid reservation status.")
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE reservations SET status = ? WHERE reference = ?", (status, reference)
            )
        if cursor.rowcount == 0:
            return None
        return self.get_reservation(reference)
