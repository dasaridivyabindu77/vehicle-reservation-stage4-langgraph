"""Tests for greeting and intent handling in the chatbot.

These tests verify that:
1. Greetings are recognized and responded to appropriately
2. Greetings during booking flow don't trigger validation errors
3. Licence plate validation only runs during the car_number slot
4. General questions work both outside and during booking
5. Existing booking and validation workflows are preserved
"""

import pytest
from datetime import datetime, timedelta

from app.config import Settings
from app.chatbot import Chatbot


@pytest.fixture
def test_bot(tmp_path):
    """Create a test chatbot instance."""
    settings = Settings(
        dial_api_key="",
        sqlite_path=str(tmp_path / "test.db")
    )
    bot = Chatbot(settings=settings, seed=True)
    yield bot
    bot.close()


class TestGreetings:
    """Test greeting recognition and responses."""

    def test_greeting_hi_outside_booking(self, test_bot):
        """Greeting 'hi' outside booking should give helpful response."""
        response = test_bot.chat("hi", session_id="greet-1")

        assert response.intent == "info"
        assert not response.in_reservation
        assert "CityPark" in response.reply
        assert "assist" in response.reply.lower() or "help" in response.reply.lower()

    def test_greeting_hello_outside_booking(self, test_bot):
        """Greeting 'hello' outside booking should give helpful response."""
        response = test_bot.chat("hello", session_id="greet-2")

        assert response.intent == "info"
        assert not response.in_reservation
        assert "CityPark" in response.reply

    def test_greeting_good_morning_outside_booking(self, test_bot):
        """Greeting 'good morning' outside booking should work."""
        response = test_bot.chat("good morning", session_id="greet-3")

        assert response.intent == "info"
        assert not response.in_reservation
        assert "CityPark" in response.reply

    def test_greeting_hey_outside_booking(self, test_bot):
        """Greeting 'hey' outside booking should work."""
        response = test_bot.chat("hey", session_id="greet-4")

        assert response.intent == "info"
        assert not response.in_reservation


class TestGreetingsDuringBooking:
    """Test that greetings during booking flow are handled gracefully."""

    def test_greeting_during_name_input(self, test_bot):
        """Greeting during name input should not trigger validation error."""
        session_id = "greet-booking-1"

        # Start booking
        r1 = test_bot.chat("book parking", session_id=session_id)
        assert r1.in_reservation
        assert "first name" in r1.reply.lower()

        # Send greeting instead of name
        r2 = test_bot.chat("hi", session_id=session_id)

        # Should not say "invalid name" - should give helpful response
        assert "invalid" not in r2.reply.lower()
        assert "letter" not in r2.reply.lower()  # Not name validation error
        assert r2.in_reservation  # Still in booking
        # Should get info response or reminder
        assert len(r2.reply) > 0

    def test_greeting_during_plate_input(self, test_bot):
        """Greeting during plate input should NOT trigger plate validation error."""
        session_id = "greet-booking-2"

        # Complete to plate input
        test_bot.chat("book parking", session_id=session_id)
        test_bot.chat("John", session_id=session_id)
        r1 = test_bot.chat("Smith", session_id=session_id)

        assert "licence plate" in r1.reply.lower() or "car" in r1.reply.lower()

        # Send greeting instead of plate
        r2 = test_bot.chat("hello", session_id=session_id)

        # Should NOT say "valid licence plate with 4-10 letters"
        assert "4-10" not in r2.reply
        assert "licence plate" not in r2.reply.lower() or "valid" not in r2.reply.lower()
        assert r2.in_reservation

    def test_greeting_during_datetime_input(self, test_bot):
        """Greeting during datetime input should not trigger datetime validation error."""
        session_id = "greet-booking-3"

        # Complete to datetime input
        test_bot.chat("reserve spot", session_id=session_id)
        test_bot.chat("Test", session_id=session_id)
        test_bot.chat("User", session_id=session_id)
        r1 = test_bot.chat("ABC1234", session_id=session_id)

        assert "start" in r1.reply.lower() or "when" in r1.reply.lower()

        # Send greeting instead of datetime
        r2 = test_bot.chat("hey", session_id=session_id)

        # Should NOT say "YYYY-MM-DD HH:MM format"
        assert "YYYY-MM-DD" not in r2.reply
        assert r2.in_reservation


class TestLicencePlateValidation:
    """Test that licence plate validation only runs during the car_number slot."""

    def test_invalid_plate_rejected_during_plate_slot(self, test_bot):
        """Invalid plate should be rejected ONLY during car_number slot."""
        session_id = "plate-valid-1"

        # Complete to plate slot
        test_bot.chat("book", session_id=session_id)
        test_bot.chat("Alice", session_id=session_id)
        r1 = test_bot.chat("Test", session_id=session_id)

        assert "licence plate" in r1.reply.lower() or "plate" in r1.reply.lower()

        # Invalid plate (too short)
        r2 = test_bot.chat("AB1", session_id=session_id)

        # Should reject with plate validation message
        assert "valid licence plate" in r2.reply.lower() or "4-10" in r2.reply

    def test_valid_plate_accepted(self, test_bot):
        """Valid plate should be accepted during car_number slot."""
        session_id = "plate-valid-2"

        # Complete to plate slot
        test_bot.chat("book", session_id=session_id)
        test_bot.chat("Bob", session_id=session_id)
        test_bot.chat("Test", session_id=session_id)

        # Valid plate
        r = test_bot.chat("ABC1234", session_id=session_id)

        # Should move to next slot (start time)
        assert "start" in r.reply.lower() or "when" in r.reply.lower()
        assert "valid licence plate" not in r.reply.lower()

    def test_plate_validation_not_run_outside_booking(self, test_bot):
        """Plate-like strings outside booking should not trigger validation."""
        # Send something that looks like a short plate outside booking
        r = test_bot.chat("AB1", session_id="no-booking-1")

        # Should not get plate validation error
        assert "valid licence plate" not in r.reply.lower()
        assert "4-10" not in r.reply


class TestGeneralQuestions:
    """Test that general questions work properly."""

    def test_question_outside_booking(self, test_bot):
        """Questions outside booking should get info responses."""
        response = test_bot.chat("what are your hours?", session_id="q-1")

        assert response.intent == "info"
        assert not response.in_reservation

    def test_question_during_booking(self, test_bot):
        """Questions during booking should get info responses, not validation errors."""
        session_id = "q-booking-1"

        # Start booking
        test_bot.chat("book parking", session_id=session_id)

        # Ask question
        r = test_bot.chat("what are your prices?", session_id=session_id)

        # Should get info response, not name validation error
        assert "valid name" not in r.reply.lower()
        assert r.in_reservation


class TestBookingWorkflow:
    """Test that existing booking workflow is preserved."""

    def test_greeting_and_complete_sequential_booking_flow(self, test_bot):
        """A greeting and questions must not skip or validate the current slot."""
        session_id = "booking-greeting-e2e"

        greeting = test_bot.chat("hello", session_id=session_id)
        assert greeting.intent == "info"
        assert "citypark" in greeting.reply.lower()
        assert "book a space" in greeting.reply.lower()

        start_booking = test_bot.chat(
            "I'd like to book a space", session_id=session_id
        )
        assert start_booking.in_reservation
        assert "first name" in start_booking.reply.lower()

        greeting_during_booking = test_bot.chat("hi", session_id=session_id)
        assert greeting_during_booking.intent == "info"
        assert "valid name" not in greeting_during_booking.reply.lower()
        assert test_bot._sessions[session_id].next_slot() == "first_name"

        last_name_prompt = test_bot.chat("Ada", session_id=session_id)
        assert "last name" in last_name_prompt.reply.lower()

        question_during_booking = test_bot.chat(
            "What are your parking prices?", session_id=session_id
        )
        assert question_during_booking.intent == "info"
        assert "valid name" not in question_during_booking.reply.lower()
        assert "valid licence plate" not in question_during_booking.reply.lower()
        assert test_bot._sessions[session_id].next_slot() == "last_name"

        plate_prompt = test_bot.chat("Lovelace", session_id=session_id)
        assert "licence plate" in plate_prompt.reply.lower()

        question_at_plate = test_bot.chat(
            "What are your opening hours?", session_id=session_id
        )
        assert question_at_plate.intent == "info"
        assert "valid licence plate" not in question_at_plate.reply.lower()
        assert test_bot._sessions[session_id].next_slot() == "car_number"

        start_prompt = test_bot.chat("AB1234", session_id=session_id)
        assert "start" in start_prompt.reply.lower()

        future = datetime.now() + timedelta(days=7)
        start_time = future.strftime("%Y-%m-%d 09:00")
        end_time = future.strftime("%Y-%m-%d 17:00")
        end_prompt = test_bot.chat(start_time, session_id=session_id)
        assert "end" in end_prompt.reply.lower()

        completed = test_bot.chat(end_time, session_id=session_id)
        assert not completed.in_reservation
        assert "pending administrator confirmation" in completed.reply.lower()
        assert "Ada Lovelace" in completed.reply
        assert "AB1234" in completed.reply

        reservation_row = test_bot.sql.get_latest_reservation_for_session(session_id)
        assert reservation_row is not None
        assert reservation_row.status == "pending_admin"
        assert reservation_row.first_name == "Ada"
        assert reservation_row.last_name == "Lovelace"
        assert reservation_row.car_number == "AB1234"
        assert test_bot.sql.reservation_count() == 1

    def test_complete_booking_flow(self, test_bot):
        """Full booking flow should still work end-to-end."""
        session_id = "booking-complete"

        # Start
        r1 = test_bot.chat("I want to book parking", session_id=session_id)
        assert r1.in_reservation
        assert "first name" in r1.reply.lower()

        # First name
        r2 = test_bot.chat("John", session_id=session_id)
        assert "last name" in r2.reply.lower()

        # Last name
        r3 = test_bot.chat("Doe", session_id=session_id)
        assert "licence plate" in r3.reply.lower() or "car" in r3.reply.lower()

        # Car number
        r4 = test_bot.chat("ABC1234", session_id=session_id)
        assert "start" in r4.reply.lower() or "when" in r4.reply.lower()

        # Start time
        future = datetime.now() + timedelta(days=7)
        start_time = future.strftime("%Y-%m-%d 10:00")
        r5 = test_bot.chat(start_time, session_id=session_id)
        assert "end" in r5.reply.lower()

        # End time
        end_time = future.strftime("%Y-%m-%d 18:00")
        r6 = test_bot.chat(end_time, session_id=session_id)

        # Should complete and show confirmation
        assert "reservation" in r6.reply.lower()
        assert "ABC1234" in r6.reply

    def test_cancel_booking(self, test_bot):
        """Cancel command should work."""
        session_id = "booking-cancel"

        test_bot.chat("book parking", session_id=session_id)
        test_bot.chat("Test", session_id=session_id)

        r = test_bot.chat("cancel", session_id=session_id)

        assert "cancel" in r.reply.lower()
        assert not test_bot._active_session(session_id)

    def test_reset_booking(self, test_bot):
        """Reset command should work."""
        session_id = "booking-reset"

        test_bot.chat("book", session_id=session_id)
        test_bot.chat("Wrong", session_id=session_id)

        r = test_bot.chat("reset", session_id=session_id)

        assert "reset" in r.reply.lower()
        assert test_bot._active_session(session_id)  # Still active, just reset

    def test_restart_booking_clears_and_reprompts(self, test_bot):
        """Restart should discard collected slots and ask for the first name."""
        session_id = "booking-restart"

        test_bot.chat("book parking", session_id=session_id)
        test_bot.chat("Ada", session_id=session_id)

        response = test_bot.chat("restart", session_id=session_id)

        assert "reset" in response.reply.lower()
        assert "first name" in response.reply.lower()
        assert test_bot._active_session(session_id)
        assert test_bot._sessions[session_id].data == {}


class TestValidationErrors:
    """Test that validation errors still work correctly."""

    def test_invalid_name_rejected(self, test_bot):
        """Invalid names should still be rejected."""
        session_id = "invalid-name"

        test_bot.chat("book", session_id=session_id)

        # Name with numbers (invalid)
        r = test_bot.chat("John123", session_id=session_id)

        assert "valid name" in r.reply.lower() or "letter" in r.reply.lower()

    def test_invalid_datetime_rejected(self, test_bot):
        """Invalid datetime should still be rejected."""
        session_id = "invalid-time"

        # Complete to start time slot
        test_bot.chat("book", session_id=session_id)
        test_bot.chat("Test", session_id=session_id)
        test_bot.chat("User", session_id=session_id)
        test_bot.chat("XYZ999", session_id=session_id)

        # Invalid datetime format
        r = test_bot.chat("tomorrow", session_id=session_id)

        assert "YYYY-MM-DD" in r.reply or "format" in r.reply.lower()

    def test_past_datetime_rejected(self, test_bot):
        """Past datetime should still be rejected."""
        session_id = "past-time"

        # Complete to start time slot
        test_bot.chat("book", session_id=session_id)
        test_bot.chat("Test", session_id=session_id)
        test_bot.chat("User", session_id=session_id)
        test_bot.chat("ABC123", session_id=session_id)

        # Past datetime
        r = test_bot.chat("2020-01-01 10:00", session_id=session_id)

        assert "past" in r.reply.lower() or "future" in r.reply.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
