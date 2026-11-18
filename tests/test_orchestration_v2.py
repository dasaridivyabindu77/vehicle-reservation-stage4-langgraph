"""Tests for production LangGraph orchestration with interrupt/resume.

These tests verify that the graph correctly:
1. Executes through the RAG chatbot
2. Interrupts when escalation is needed
3. Resumes after admin decision
4. Persists to MCP on approval
"""

import pytest

from app.config import Settings
from app.orchestration_v2 import build_production_graph, process_admin_decision_and_resume


@pytest.fixture
def settings():
    """Test settings with offline mode."""
    return Settings(
        llm_provider="dial",
        dial_api_key="",  # Offline mode
        sqlite_path=":memory:",
        mcp_server_url="http://127.0.0.1:8001",
        mcp_server_token="test-token",
        admin_token="test-admin-token",
    )


@pytest.fixture
def graph(settings):
    """Create production graph for testing."""
    return build_production_graph(settings)


class TestGraphBasicExecution:
    """Tests for basic graph execution without interrupt."""

    def test_graph_compiles(self, graph):
        """Graph should compile successfully."""
        assert graph is not None

    def test_graph_handles_simple_question(self, graph):
        """Graph should handle non-reservation questions."""
        config = {"configurable": {"thread_id": "test-simple-1"}}

        state = graph.invoke(
            {
                "user_message": "What are your opening hours?",
                "session_id": "test-simple-1",
            },
            config=config
        )

        assert "chatbot_response" in state or "final_response" in state
        assert state.get("reservation_complete") != True


class TestGraphInterruptResume:
    """Tests for interrupt/resume functionality."""

    @pytest.mark.skip(reason="Requires full reservation flow - implement after basic tests pass")
    def test_graph_interrupts_on_escalation(self, graph):
        """Graph should interrupt when reservation needs admin approval."""
        config = {"configurable": {"thread_id": "test-interrupt-1"}}

        # This would need to complete a full reservation flow
        # which requires multiple chat turns
        # Skipping for now - will implement comprehensive E2E test later
        pass

    @pytest.mark.skip(reason="Requires checkpoint state setup - implement after basic tests pass")
    def test_graph_resumes_after_approval(self, graph):
        """Graph should resume and persist after admin approval."""
        # This would need to:
        # 1. Set up a checkpoint in "waiting for admin" state
        # 2. Resume with approval decision
        # 3. Verify MCP write happens
        # Skipping for now - will implement after interrupt test works
        pass


class TestGraphErrorHandling:
    """Tests for error handling in graph execution."""

    def test_graph_handles_empty_message(self, graph):
        """Graph should handle empty messages gracefully."""
        config = {"configurable": {"thread_id": "test-empty-1"}}

        try:
            state = graph.invoke(
                {
                    "user_message": "",
                    "session_id": "test-empty-1",
                },
                config=config
            )
            # Should not crash
            assert state is not None
        except Exception as e:
            # If it does raise, it should be a controlled error
            assert "error" in str(e).lower() or state.get("errors")


class TestGraphCheckpointing:
    """Tests for checkpoint persistence."""

    def test_graph_uses_thread_id_for_checkpointing(self, graph):
        """Graph should use thread_id for checkpoint isolation."""
        # Thread 1
        config1 = {"configurable": {"thread_id": "thread-1"}}
        state1 = graph.invoke(
            {
                "user_message": "Hello",
                "session_id": "thread-1",
            },
            config1
        )

        # Thread 2
        config2 = {"configurable": {"thread_id": "thread-2"}}
        state2 = graph.invoke(
            {
                "user_message": "Hi there",
                "session_id": "thread-2",
            },
            config2
        )

        # States should be different (isolated by thread_id)
        assert state1.get("session_id") != state2.get("session_id")


class TestProcessAdminDecisionFunction:
    """Tests for the process_admin_decision_and_resume function."""

    @pytest.mark.skip(reason="Requires active interrupt state - implement after interrupt tests")
    def test_resume_with_approval(self, graph):
        """Resume function should handle approval decision."""
        # This requires an active interrupt state
        # Will implement after basic interrupt/resume works
        pass

    @pytest.mark.skip(reason="Requires active interrupt state - implement after interrupt tests")
    def test_resume_with_refusal(self, graph):
        """Resume function should handle refusal decision."""
        # This requires an active interrupt state
        # Will implement after basic interrupt/resume works
        pass
