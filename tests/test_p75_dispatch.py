"""P7.5 focused tests for the default coding dispatcher and reviewer chain.

Deterministic tests using injected registry state and mocked outcomes.
No live endpoints are called.
"""

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from second_shift import (
    DefaultCodingDispatcher,
    ReviewOutcome,
    GoalClass,
)
from second_shift.state import StateFile
from second_shift.pool_recovery import get_usable_member_ids


class TestP75BasicRouting:
    """Basic routing tests for P7.5."""

    def test_init_creates_state_directory(self):
        """Test that initialization creates the state directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)
            assert Path(tmpdir).exists()

    def test_route_coding_to_p40_first(self):
        """Test that initial routing goes to P40."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)

            registry = {
                "models": [
                    {"id": "p40", "status": "active", "credit_status": "available"},
                    {"id": "copilot", "status": "active", "credit_status": "available"},
                ],
            }

            registry = dispatcher.route_coding_request(
                goal_id="test-goal-1",
                goal_class="coding",
                registry=registry,
            )

            dispatcher_state = dispatcher.get_dispatcher_state(registry)
            assert dispatcher_state["selected_model"] == "p40"
            assert dispatcher_state["last_decision"] == "route_to_p40"

    def test_p40_failure_counter_increments_on_substantive_failure(self):
        """Test that P40 failure counter increments on substantive failures."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)

            registry = {
                "models": [
                    {"id": "p40", "status": "active", "credit_status": "available"},
                ],
            }

            # Route first time
            registry = dispatcher.route_coding_request(
                goal_id="test-goal-2",
                goal_class="coding",
                registry=registry,
            )

            # Record substantive failure - this increments the counter
            registry = dispatcher.record_attempt_result(
                goal_id="test-goal-2",
                goal_class="coding",
                model_id="p40",
                provider="llama.cpp",
                classification="incorrect_code",
                credit_status="available",
                reviewer="codex",
                review_outcome="rejected",
                reviewer_evidence="buggy",
                evidence=["test-fail"],
                registry=registry,
            )

            assert dispatcher.get_p40_failure_count("test-goal-2", "coding") == 1

            # Second substantive failure
            registry = dispatcher.record_attempt_result(
                goal_id="test-goal-2",
                goal_class="coding",
                model_id="p40",
                provider="llama.cpp",
                classification="tests_failed",
                credit_status="available",
                reviewer="codex",
                review_outcome="rejected",
                reviewer_evidence="still failing",
                evidence=["test-fail-2"],
                registry=registry,
            )

            assert dispatcher.get_p40_failure_count("test-goal-2", "coding") == 2

    def test_different_goals_independent_counters(self):
        """Test that different goals have independent failure counters."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)

            registry = {
                "models": [
                    {"id": "p40", "status": "active", "credit_status": "available"},
                ],
            }

            # Record substantive failure for first goal
            registry = dispatcher.route_coding_request(
                goal_id="test-goal-3a",
                goal_class="coding",
                registry=registry,
            )
            registry = dispatcher.record_attempt_result(
                goal_id="test-goal-3a",
                goal_class="coding",
                model_id="p40",
                provider="llama.cpp",
                classification="incorrect_code",
                credit_status="available",
                reviewer="codex",
                review_outcome="rejected",
                reviewer_evidence="buggy",
                evidence=["fail"],
                registry=registry,
            )
            assert dispatcher.get_p40_failure_count("test-goal-3a", "coding") == 1

            # Second goal should be independent
            registry = dispatcher.route_coding_request(
                goal_id="test-goal-3b",
                goal_class="coding",
                registry=registry,
            )
            assert dispatcher.get_p40_failure_count("test-goal-3a", "coding") == 1
            assert dispatcher.get_p40_failure_count("test-goal-3b", "coding") == 0


class TestP75Escalation:
    """Tests for escalation after P40 exhaustion."""

    def test_escalate_to_secondary_after_three_substantive_failures(self):
        """Test escalation to Second Shift after 3 substantive failures."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)

            registry = {
                "models": [
                    {"id": "p40", "status": "active", "credit_status": "available"},
                    {"id": "north-mini-code", "status": "active", "credit_status": "available"},
                    {"id": "copilot", "status": "standby", "credit_status": "available"},
                ],
            }

            # Simulate 3 substantive failures
            for i in range(3):
                registry = dispatcher.route_coding_request(
                    goal_id="test-goal-4",
                    goal_class="coding",
                    registry=registry,
                )
                registry = dispatcher.record_attempt_result(
                    goal_id="test-goal-4",
                    goal_class="coding",
                    model_id="p40",
                    provider="llama.cpp",
                    classification="incorrect_code" if i < 2 else "tests_failed",
                    credit_status="available",
                    reviewer="codex",
                    review_outcome="rejected",
                    reviewer_evidence=f"failure {i+1}",
                    evidence=[f"fail-{i}"],
                    registry=registry,
                )

            # Count should be 3
            assert dispatcher.get_p40_failure_count("test-goal-4", "coding") == 3

            # Next route should go to secondary
            registry = dispatcher.route_coding_request(
                goal_id="test-goal-4",
                goal_class="coding",
                registry=registry,
            )

            dispatcher_state = dispatcher.get_dispatcher_state(registry)
            assert dispatcher_state["selected_model"] == "north-mini-code"

    def test_select_secondary_by_lowest_failure_count(self):
        """Test that secondary selection favors lowest failure count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)

            registry = {
                "models": [
                    {"id": "p40", "status": "active", "credit_status": "available"},
                    {"id": "model-a", "status": "active", "credit_status": "available", "failure_counts_by_goal": {"coding": 0}},
                    {"id": "model-b", "status": "active", "credit_status": "available", "failure_counts_by_goal": {"coding": 2}},
                ],
            }

            # Simulate 3 substantive failures to exhaust P40
            for i in range(3):
                registry = dispatcher.route_coding_request(
                    goal_id="test-goal-5",
                    goal_class="coding",
                    registry=registry,
                )
                registry = dispatcher.record_attempt_result(
                    goal_id="test-goal-5",
                    goal_class="coding",
                    model_id="p40",
                    provider="llama.cpp",
                    classification="incorrect_code",
                    credit_status="available",
                    reviewer="codex",
                    review_outcome="rejected",
                    reviewer_evidence="buggy",
                    evidence=[f"fail-{i}"],
                    registry=registry,
                )

            # Should select model-a (lowest failure count)
            registry = dispatcher.route_coding_request(
                goal_id="test-goal-5",
                goal_class="coding",
                registry=registry,
            )

            dispatcher_state = dispatcher.get_dispatcher_state(registry)
            assert dispatcher_state["selected_model"] == "model-a"

    def test_no_usable_secondary_raises_error(self):
        """Test that error is raised when no usable secondary models exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)

            registry = {
                "models": [
                    {"id": "p40", "status": "active", "credit_status": "available"},
                    {"id": "copilot", "status": "unusable", "credit_status": "exhausted"},
                ],
            }

            # Exhaust P40
            for i in range(3):
                registry = dispatcher.route_coding_request(
                    goal_id="test-goal-6",
                    goal_class="coding",
                    registry=registry,
                )
                registry = dispatcher.record_attempt_result(
                    goal_id="test-goal-6",
                    goal_class="coding",
                    model_id="p40",
                    provider="llama.cpp",
                    classification="incorrect_code",
                    credit_status="available",
                    reviewer="codex",
                    review_outcome="rejected",
                    reviewer_evidence="buggy",
                    evidence=["fail"],
                    registry=registry,
                )

            # No usable models should raise error
            with pytest.raises(ValueError, match="No usable Second Shift members"):
                dispatcher.route_coding_request(
                    goal_id="test-goal-6",
                    goal_class="coding",
                    registry=registry,
                )


class TestP75FailureClassification:
    """Tests for substantive vs non-substantive failure classification."""

    def test_substantive_failures_count_toward_threshold(self):
        """Test that substantive failures count toward the threshold."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)

            registry = {
                "models": [
                    {"id": "p40", "status": "active", "credit_status": "available"},
                ],
            }

            # Route and record substantive failures
            for i in range(3):
                registry = dispatcher.route_coding_request(
                    goal_id="test-goal-7a",
                    goal_class="coding",
                    registry=registry,
                )
                registry = dispatcher.record_attempt_result(
                    goal_id="test-goal-7a",
                    goal_class="coding",
                    model_id="p40",
                    provider="llama.cpp",
                    classification="incorrect_code",
                    credit_status="available",
                    reviewer="codex",
                    review_outcome="rejected",
                    reviewer_evidence="buggy",
                    evidence=[f"fail-{i}"],
                    registry=registry,
                )

            assert dispatcher.get_p40_failure_count("test-goal-7a", "coding") == 3

    def test_non_substantive_failures_do_not_count(self):
        """Test that non-substantive failures do not count toward threshold."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)

            registry = {
                "models": [
                    {"id": "p40", "status": "active", "credit_status": "available"},
                ],
            }

            # Route once
            registry = dispatcher.route_coding_request(
                goal_id="test-goal-7b",
                goal_class="coding",
                registry=registry,
            )

            # Record various non-substantive failures
            non_substantive = [
                "no_code",
                "timeout",
                "endpoint_unavailable",
                "authentication",
                "credit_exhausted",
                "rate_limited",
                "policy_refusal",
                "context_limit",
                "unknown",
            ]

            for classification in non_substantive:
                registry = dispatcher.record_attempt_result(
                    goal_id="test-goal-7b",
                    goal_class="coding",
                    model_id="p40",
                    provider="llama.cpp",
                    classification=classification,
                    credit_status="available",
                    reviewer="codex",
                    review_outcome="rejected",
                    reviewer_evidence=f"{classification} occurred",
                    evidence=[f"{classification}-evidence"],
                    registry=registry,
                )

            # Should still be 0 substantive failures (non-substantive don't count)
            assert dispatcher.get_p40_failure_count("test-goal-7b", "coding") == 0

    def test_can_route_to_secondary_check(self):
        """Test can_route_to_secondary returns correct status."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)

            registry = {
                "models": [
                    {"id": "p40", "status": "active", "credit_status": "available"},
                ],
            }

            # Before exhaustion
            assert dispatcher.can_route_to_secondary("test-goal-8a", "coding") is False

            # Route and record 3 substantive failures
            for i in range(3):
                registry = dispatcher.route_coding_request(
                    goal_id="test-goal-8a",
                    goal_class="coding",
                    registry=registry,
                )
                registry = dispatcher.record_attempt_result(
                    goal_id="test-goal-8a",
                    goal_class="coding",
                    model_id="p40",
                    provider="llama.cpp",
                    classification="incorrect_code",
                    credit_status="available",
                    reviewer="codex",
                    review_outcome="rejected",
                    reviewer_evidence="buggy",
                    evidence=["fail"],
                    registry=registry,
                )

            assert dispatcher.can_route_to_secondary("test-goal-8a", "coding") is True


class TestP75ReviewerChain:
    """Tests for the reviewer chain workflow."""

    def test_record_attempt_persists_all_fields(self):
        """Test that all attempt fields are persisted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)

            registry = {
                "models": [
                    {"id": "p40", "status": "active", "credit_status": "available"},
                ],
            }

            registry = dispatcher.route_coding_request(
                goal_id="test-goal-9",
                goal_class="reasoning",
                registry=registry,
            )

            registry = dispatcher.record_attempt_result(
                goal_id="test-goal-9",
                goal_class="reasoning",
                model_id="p40",
                provider="ollama",
                classification="partial_code",
                credit_status="low",
                reviewer="codex",
                review_outcome="needs_revision",
                reviewer_evidence="missing edge cases",
                evidence=["test-case-1-failed"],
                registry=registry,
            )

            history = dispatcher.get_attempt_history(goal_id="test-goal-9")
            assert len(history) == 1
            attempt = history[0]
            assert attempt["goal_id"] == "test-goal-9"
            assert attempt["goal_class"] == "reasoning"
            assert attempt["model_id"] == "p40"
            assert attempt["provider"] == "ollama"
            assert attempt["classification"] == "partial_code"
            assert attempt["credit_status"] == "low"
            assert attempt["reviewer"] == "codex"
            assert attempt["review_outcome"] == "needs_revision"
            assert attempt["is_substantive_failure"] is True

    def test_attempt_coding_task_full_workflow(self):
        """Test the full coding task workflow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)

            registry = {
                "models": [
                    {"id": "p40", "status": "active", "credit_status": "available"},
                    {"id": "codex", "status": "standby", "credit_status": "available"},
                ],
            }

            # Candidate review is not final acceptance: Codex review must
            # be followed by an explicit Codex verification record.
            registry = dispatcher.attempt_coding_task(
                goal_id="test-goal-10",
                goal_class="coding",
                model_id="p40",
                provider="llama.cpp",
                classification="tests_failed",
                credit_status="available",
                reviewer="codex",
                review_outcome="approved",
                reviewer_evidence="all tests pass",
                evidence=["test-1", "test-2"],
                registry=registry,
            )

            history = dispatcher.get_attempt_history(goal_id="test-goal-10")
            assert len(history) == 1
            attempt = history[0]
            assert attempt["final_decision"] == "needs_codex_verification"
            assert attempt["review_outcome"] == "approved"
            assert attempt["reviewer"] == "codex"

            registry = dispatcher.record_final_verification(
                goal_id="test-goal-10",
                goal_class="coding",
                provider="codex-cli",
                classification="success",
                credit_status="available",
                reviewer_evidence="Codex verified the corrected implementation",
                evidence=["final-tests-pass"],
                registry=registry,
            )
            history = dispatcher.get_attempt_history(goal_id="test-goal-10")
            assert len(history) == 2
            assert history[-1]["final_decision"] == "accepted"
            assert history[-1]["is_final_verification"] is True


class TestP75StatePersistence:
    """Tests for state persistence across calls."""

    def test_dispatcher_state_persists_across_instances(self):
        """Test that dispatcher state persists across StateFile instances."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf1 = StateFile(tmpdir)
            dispatcher1 = DefaultCodingDispatcher(sf1)

            registry = {
                "models": [
                    {"id": "p40", "status": "active", "credit_status": "available"},
                ],
            }

            registry = dispatcher1.route_coding_request(
                goal_id="test-goal-11",
                goal_class="coding",
                registry=registry,
            )

            # Create new instance and verify state persists
            sf2 = StateFile(tmpdir)
            dispatcher2 = DefaultCodingDispatcher(sf2)

            assert dispatcher2.get_p40_failure_count("test-goal-11", "coding") == 0

    def test_attempt_history_persists_across_instances(self):
        """Test that attempt history persists across StateFile instances."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf1 = StateFile(tmpdir)
            dispatcher1 = DefaultCodingDispatcher(sf1)

            registry = {
                "models": [
                    {"id": "p40", "status": "active", "credit_status": "available"},
                ],
            }

            registry = dispatcher1.route_coding_request(
                goal_id="test-goal-12",
                goal_class="coding",
                registry=registry,
            )

            registry = dispatcher1.record_attempt_result(
                goal_id="test-goal-12",
                goal_class="coding",
                model_id="p40",
                provider="llama.cpp",
                classification="incorrect_code",
                credit_status="available",
                reviewer="codex",
                review_outcome="rejected",
                reviewer_evidence="buggy",
                evidence=["fail"],
                registry=registry,
            )

            # Verify persistence
            sf2 = StateFile(tmpdir)
            dispatcher2 = DefaultCodingDispatcher(sf2)

            history = dispatcher2.get_attempt_history(goal_id="test-goal-12")
            assert len(history) == 1


class TestP75NonSubstantiveClassification:
    """Tests for the non-substantive classification set."""

    def test_no_code_is_non_substantive(self):
        """Test that no_code is classified as non-substantive."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)

            assert dispatcher.classify_as_substantive("no_code") is False
            assert dispatcher.classify_as_substantive("timeout") is False
            assert dispatcher.classify_as_substantive("endpoint_unavailable") is False

    def test_substantive_classifications(self):
        """Test that substantive classifications are correctly identified."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)

            assert dispatcher.classify_as_substantive("partial_code") is True
            assert dispatcher.classify_as_substantive("incorrect_code") is True
            assert dispatcher.classify_as_substantive("tests_failed") is True

    def test_unknown_is_non_substantive(self):
        """Test that unknown is classified as non-substantive."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)

            assert dispatcher.classify_as_substantive("unknown") is False


class TestP75CreditStatus:
    """Tests for credit status handling."""

    def test_credit_status_is_recorded(self):
        """Test that credit status is recorded in attempts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(sf)

            registry = {
                "models": [
                    {"id": "p40", "status": "active", "credit_status": "exhausted"},
                ],
            }

            registry = dispatcher.route_coding_request(
                goal_id="test-goal-13",
                goal_class="coding",
                registry=registry,
            )

            registry = dispatcher.record_attempt_result(
                goal_id="test-goal-13",
                goal_class="coding",
                model_id="p40",
                provider="llama.cpp",
                classification="timeout",
                credit_status="exhausted",
                reviewer=None,
                review_outcome=None,
                reviewer_evidence=None,
                evidence=[],
                registry=registry,
            )

            history = dispatcher.get_attempt_history(goal_id="test-goal-13")
            assert history[0]["credit_status"] == "exhausted"


class TestP75AcceptanceGates:
    def test_arbitrary_classification_does_not_count_as_code_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dispatcher = DefaultCodingDispatcher(StateFile(tmpdir))
            registry = {"models": [{"id": "p40", "status": "active"}]}
            for _ in range(3):
                registry = dispatcher.record_attempt_result(
                    goal_id="goal", goal_class="coding", model_id="p40",
                    provider="llama.cpp", classification="provider_warning",
                    credit_status="unknown", reviewer=None, registry=registry,
                )
            assert dispatcher.get_p40_failure_count("goal", "coding") == 0

    def test_codex_review_and_fresh_final_verification_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dispatcher = DefaultCodingDispatcher(StateFile(tmpdir))
            registry = {"models": [{"id": "p40", "status": "active"}]}
            with pytest.raises(ValueError, match="Codex review"):
                dispatcher.record_attempt_result(
                    goal_id="goal", goal_class="coding", model_id="p40",
                    provider="llama.cpp", classification="incorrect_code",
                    credit_status="available", reviewer="copilot",
                    registry=registry,
                )
            with pytest.raises(ValueError, match="explicit repair"):
                dispatcher.record_attempt_result(
                    goal_id="goal", goal_class="coding", model_id="codex",
                    provider="codex-cli", classification="success",
                    credit_status="available", reviewer="codex",
                    registry=registry,
                )

    def test_attempt_number_and_registry_are_persisted_with_ledger_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = StateFile(tmpdir)
            dispatcher = DefaultCodingDispatcher(state)
            registry = {"models": [{"id": "p40", "status": "active"}]}
            for _ in range(2):
                registry = dispatcher.record_attempt_result(
                    goal_id="goal", goal_class="coding", model_id="p40",
                    provider="llama.cpp", classification="no_code",
                    credit_status="unknown", reviewer=None, registry=registry,
                )
            history = dispatcher.get_attempt_history(goal_id="goal")
            assert [entry["attempt_number"] for entry in history] == [1, 2]
            persisted = state.get_registry()["coding_dispatcher"]
            assert persisted["selected_model"] == "p40"


# Run tests
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
