from app.sql_store import DynamicDataStore


def _store(tmp_path):
    store = DynamicDataStore(str(tmp_path / "dyn.db"))
    store.seed()
    return store


def test_seed_populates_zones_and_hours(tmp_path):
    store = _store(tmp_path)
    zones = store.zones()
    assert len(zones) == 3
    assert store.total_available() == sum(z.available_spaces for z in zones)
    assert "Opening hours" in store.hours_summary()
    store.close()


def test_seed_is_idempotent(tmp_path):
    store = _store(tmp_path)
    store.seed()  # second call must not duplicate
    assert len(store.zones()) == 3
    store.close()


def test_create_reservation_decrements_availability(tmp_path):
    store = _store(tmp_path)
    zone = store.zones()[0]
    before = zone.available_spaces
    rid = store.create_reservation(
        "Ada", "Lovelace", "AL1234", "2026-12-01T09:00", "2026-12-01T17:00",
        zone_id=zone.zone_id,
    )
    assert rid >= 1
    assert store.reservation_count() == 1
    after = next(z for z in store.zones() if z.zone_id == zone.zone_id).available_spaces
    assert after == before - 1
    store.close()
