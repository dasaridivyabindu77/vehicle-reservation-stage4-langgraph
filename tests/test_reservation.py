from app.reservation import SLOTS, ingest, start_session


def test_full_slot_fill_completes():
    s = start_session()
    assert s.next_slot() == "first_name"
    assert not ingest(s, "Ada").done
    assert not ingest(s, "Lovelace").done
    assert not ingest(s, "AL-1234").done
    assert not ingest(s, "2026-12-01 09:00").done
    result = ingest(s, "2026-12-01 17:00")
    assert result.done is True
    assert result.reservation_ready is True
    assert s.is_complete()
    assert s.data["car_number"] == "AL1234"  # normalised
    assert len(SLOTS) == 5


def test_invalid_plate_reprompts():
    s = start_session()
    ingest(s, "Ada")
    ingest(s, "Lovelace")
    result = ingest(s, "!!")  # too short / invalid
    assert result.done is False
    assert "licence plate" in result.reply.lower()
    assert "car_number" not in s.data


def test_end_before_start_reprompts():
    s = start_session()
    ingest(s, "Ada")
    ingest(s, "Lovelace")
    ingest(s, "AL1234")
    ingest(s, "2026-12-01 17:00")
    result = ingest(s, "2026-12-01 09:00")  # before start
    assert result.done is False
    assert "after the start" in result.reply.lower()
    assert "end" not in s.data


def test_past_date_rejected():
    s = start_session()
    ingest(s, "Ada")
    ingest(s, "Lovelace")
    ingest(s, "AL1234")
    result = ingest(s, "2000-01-01 09:00")
    assert result.done is False
    assert "past" in result.reply.lower()
