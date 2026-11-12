"""Dynamic data store (SQLite).

Per the optional spec enhancement, volatile facts live in SQL rather than the
vector store: space **availability**, **prices**, and **opening hours** — plus
the **reservations** table that the interactive booking flow writes to. Static
knowledge (location, rules, booking process) lives in the vector store instead.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS parking_zones (
    zone_id         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    total_spaces    INTEGER NOT NULL,
    available_spaces INTEGER NOT NULL,
    price_per_hour  REAL NOT NULL,
    price_per_day   REAL NOT NULL,
    ev_charging     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS opening_hours (
    day        TEXT PRIMARY KEY,
    open_time  TEXT NOT NULL,
    close_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reservations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name  TEXT NOT NULL,
    last_name   TEXT NOT NULL,
    car_number  TEXT NOT NULL,
    zone_id     TEXT,
    start_ts    TEXT NOT NULL,
    end_ts      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending_admin',
    created_at  TEXT NOT NULL
);
"""

_ZONES = [
    # zone_id, name, total, available, per_hour, per_day, ev
    ("A", "Zone A - Covered Garage", 120, 18, 3.5, 24.0, 1),
    ("B", "Zone B - Open Lot", 200, 76, 2.0, 14.0, 0),
    ("C", "Zone C - Premium / EV", 40, 5, 5.0, 36.0, 1),
]

_HOURS = [
    ("Mon-Fri", "06:00", "23:00"),
    ("Saturday", "07:00", "23:00"),
    ("Sunday", "08:00", "22:00"),
]


@dataclass
class Zone:
    zone_id: str
    name: str
    total_spaces: int
    available_spaces: int
    price_per_hour: float
    price_per_day: float
    ev_charging: bool


class DynamicDataStore:
    """Thin wrapper over a SQLite connection with seed + query helpers."""

    def __init__(self, path: str = "citypark.db") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def seed(self, force: bool = False) -> None:
        """Populate reference data. Idempotent unless ``force`` is set."""
        cur = self.conn.cursor()
        if force:
            cur.execute("DELETE FROM parking_zones")
            cur.execute("DELETE FROM opening_hours")
        if cur.execute("SELECT COUNT(*) FROM parking_zones").fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO parking_zones "
                "(zone_id, name, total_spaces, available_spaces, price_per_hour, "
                "price_per_day, ev_charging) VALUES (?,?,?,?,?,?,?)",
                _ZONES,
            )
        if cur.execute("SELECT COUNT(*) FROM opening_hours").fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO opening_hours (day, open_time, close_time) VALUES (?,?,?)",
                _HOURS,
            )
        self.conn.commit()

    # --- queries ---
    def zones(self) -> List[Zone]:
        rows = self.conn.execute("SELECT * FROM parking_zones ORDER BY zone_id").fetchall()
        return [
            Zone(
                zone_id=r["zone_id"],
                name=r["name"],
                total_spaces=r["total_spaces"],
                available_spaces=r["available_spaces"],
                price_per_hour=r["price_per_hour"],
                price_per_day=r["price_per_day"],
                ev_charging=bool(r["ev_charging"]),
            )
            for r in rows
        ]

    def total_available(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(available_spaces), 0) AS n FROM parking_zones"
        ).fetchone()
        return int(row["n"])

    # --- human-readable summaries used as dynamic retrieval context ---
    def availability_summary(self) -> str:
        zones = self.zones()
        lines = [f"Total spaces available right now: {self.total_available()}."]
        for z in zones:
            lines.append(
                f"{z.name}: {z.available_spaces} of {z.total_spaces} free"
                + (" (EV charging)" if z.ev_charging else "")
            )
        return "\n".join(lines)

    def price_summary(self) -> str:
        lines = ["Current parking prices:"]
        for z in self.zones():
            lines.append(
                f"{z.name}: ${z.price_per_hour:.2f}/hour, ${z.price_per_day:.2f}/day"
            )
        return "\n".join(lines)

    def hours_summary(self) -> str:
        rows = self.conn.execute("SELECT * FROM opening_hours").fetchall()
        lines = ["Opening hours:"]
        for r in rows:
            lines.append(f"{r['day']}: {r['open_time']} - {r['close_time']}")
        return "\n".join(lines)

    def dynamic_context(self) -> str:
        """All dynamic facts as one block, for grounding generative answers."""
        return "\n\n".join(
            [self.availability_summary(), self.price_summary(), self.hours_summary()]
        )

    # --- reservations ---
    def create_reservation(
        self,
        first_name: str,
        last_name: str,
        car_number: str,
        start_ts: str,
        end_ts: str,
        zone_id: Optional[str] = None,
        status: str = "confirmed",
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO reservations "
            "(first_name, last_name, car_number, zone_id, start_ts, end_ts, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                first_name,
                last_name,
                car_number,
                zone_id,
                start_ts,
                end_ts,
                status,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        if zone_id:
            cur.execute(
                "UPDATE parking_zones SET available_spaces = MAX(available_spaces - 1, 0) "
                "WHERE zone_id = ?",
                (zone_id,),
            )
        self.conn.commit()
        return int(cur.lastrowid)

    def reservation_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0])

    def close(self) -> None:
        self.conn.close()
