"""Tests for GTX broker deterministic controller.

Tests task classification, escalation policy, and routing decisions.
"""

import pytest
import sys
from pathlib import Path
from datetime import datetime, timezone

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Import from gtx_broker module
from gtx_broker.state import (
    GTXBrokerState,
    GTXBrokerRegistry,
    GTXBrokerWorkflow,
    ProviderTransition,
    ProviderStatus,
)
from gtx_broker.classifier import (
    GtxTaskClassifier,
    GtxModelProfile,
    GtxModelConfig,
    TaskClassification,
)
from gtx_broker.escalation_policy import (
    GtxEscalationPolicy,
    EscalationResult,
    RoutingDecision,
    FailureClassification,
)
from gtx_broker.controller import (
    GtxBrokerDirector,
    RoutingRequest,
    BrokerStatus,
)


class TestGTXBrokerState:
    """Tests for GTXBrokerState dataclass."""

    def test_create_state(self):
        """Test creating a basic state."""
        state = GTXBrokerState(
            selected_model="gtx_iq3_xs",
            workflow=GTXBrokerWorkflow.DIRECT_ESCALATION,
            task_id="task-001",
            task_class="coding",
            context_size=65536,
            quality_preference="quality",
        )

        assert state.selected_model == "gtx_iq3_xs"
        assert state.workflow == GTXBrokerWorkflow.DIRECT_ESCALATION
        assert state.task_id == "task-001"
        assert state.context_size == 65536

    def test_state_to_dict(self):
        """Test serialization to dictionary."""
        state = GTXBrokerState(
            selected_model="p40_qwen35",
            workflow=GTXBrokerWorkflow.LAYERED_REVIEW,
            task_id="task-002",
            task_class="reasoning",
            context_size=262144,
            quality_preference="quality",
            attempt_number=2,
            evidence=["test1", "test2"],
        )

        data = state.to_dict()

        assert data["selected_model"] == "p40_qwen35"
        assert data["workflow"] == "layered_review"
        assert data["task_id"] == "task-002"
        assert data["context_size"] == 262144
        assert data["attempt_number"] == 2
        assert len(data["evidence"]) == 2

    def test_state_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "selected_model": "gtx_q2_k",
            "workflow": "direct_escalation",
            "task_id": "task-003",
            "task_class": "general",
            "context_size": 32000,
            "quality_preference": "speed",
            "attempt_number": 1,
            "evidence": [],
            "provider_transition": None,
            "last_routing_at": datetime.now(timezone.utc).isoformat(),
            "gtx_substantive_failures": 0,
            "p40_substantive_failures": 0,
            "is_final_verification": False,
        }

        state = GTXBrokerState.from_dict(data)

        assert state.selected_model == "gtx_q2_k"
        assert state.workflow == GTXBrokerWorkflow.DIRECT_ESCALATION
        assert state.task_id == "task-003"

    def test_provider_transition_serialization(self):
        """Test ProviderTransition serialization."""
        transition = ProviderTransition(
            from_worker="gtx_iq3_xs",
            to_worker="p40_qwen35",
            reason="context_size_exceeded",
            timestamp=datetime.now(timezone.utc).isoformat(),
            evidence=["context=262144"],
        )

        state = GTXBrokerState(
            selected_model="p40_qwen35",
            workflow=GTXBrokerWorkflow.DIRECT_ESCALATION,
            task_id="task-004",
            task_class="large_context",
            context_size=262144,
            quality_preference="quality",
            provider_transition=transition,
        )

        data = state.to_dict()
        assert data["provider_transition"]["from_worker"] == "gtx_iq3_xs"
        assert data["provider_transition"]["to_worker"] == "p40_qwen35"


class TestGTXTaskClassifier:
    """Tests for GtxTaskClassifier."""

    def test_classify_bounded_implementation(self):
        """Test classification of bounded implementation tasks."""
        classifier = GtxTaskClassifier()

        state = classifier.classify_task(
            task_id="task-001",
            task_description="Implement login feature",
            context_size=8192,
            quality_importance=0.7,
            requires_benchmark_evidence=False,
        )

        # GTX scopes the task; the P40 is the implementation worker.
        assert state.task_class == "bounded_implementation"
        assert state.selected_model == "p40_qwen35"
        assert state.context_size == 8192

    def test_classify_large_context(self):
        """Test classification of large context tasks."""
        classifier = GtxTaskClassifier()

        state = classifier.classify_task(
            task_id="task-002",
            task_description="Analyze this 200k token document",
            context_size=200000,
            quality_importance=0.5,
            requires_benchmark_evidence=False,
        )

        assert state.task_class == "large_context"
        assert state.selected_model == "p40_qwen35"  # P40 required
        assert state.context_size == 200000

    def test_classify_benchmark_validation(self):
        """Test classification of benchmark validation tasks."""
        classifier = GtxTaskClassifier()

        state = classifier.classify_task(
            task_id="task-003",
            task_description="Run deterministic benchmark on this model",
            context_size=65536,
            quality_importance=0.5,
            requires_benchmark_evidence=True,
        )

        assert state.task_class == "benchmark_validation"
        assert state.selected_model == "p40_qwen35"

    def test_classify_complex_reasoning(self):
        """Test classification of complex reasoning tasks."""
        classifier = GtxTaskClassifier()

        state = classifier.classify_task(
            task_id="task-004",
            task_description="Design architectural pattern for distributed system",
            context_size=16384,
            quality_importance=0.9,
            requires_benchmark_evidence=False,
        )

        assert state.task_class == "complex_reasoning"
        assert state.selected_model == "codex"  # Escalate to Codex

    def test_classify_repair_local(self):
        """Test classification of repair tasks."""
        classifier = GtxTaskClassifier()

        state = classifier.classify_task(
            task_id="task-005",
            task_description="Quick fix for typo in error message",
            context_size=1024,
            quality_importance=0.2,
            requires_benchmark_evidence=False,
        )

        assert state.task_class == "repair_local"
        assert state.selected_model == "p40_qwen35"

    def test_classify_quality_vs_speed(self):
        """Test quality vs speed preference."""
        classifier = GtxTaskClassifier()

        # Quality-focused
        state_quality = classifier.classify_task(
            task_id="task-006",
            task_description="High-quality code generation",
            context_size=32000,
            quality_importance=0.9,
            requires_benchmark_evidence=False,
        )
        assert state_quality.selected_model == "p40_qwen35"

        # Speed-focused
        state_speed = classifier.classify_task(
            task_id="task-007",
            task_description="Fast prototyping",
            context_size=32000,
            quality_importance=0.2,
            requires_benchmark_evidence=False,
        )
        assert state_speed.selected_model == "p40_qwen35"

    def test_model_constraints(self):
        """Test model constraint retrieval."""
        classifier = GtxTaskClassifier()

        constraints = classifier.get_model_constraints("gtx_iq3_xs")
        assert constraints["model_id"] == "gtx_iq3_xs"
        assert constraints["vram_required_gb"] == 3.7
        assert constraints["max_context"] == 65536
        assert constraints["is_quality_focused"] == True
        assert "name" in constraints  # Verify new field exists

        constraints = classifier.get_model_constraints("gtx_q2_k")
        assert constraints["speed_tier"] == "fastest"
        assert constraints["tokens_per_second"] == 52.1

        # Non-existent model returns empty dict
        assert classifier.get_model_constraints("unknown") == {}

    def test_validate_task_feasibility(self):
        """Test task feasibility validation."""
        classifier = GtxTaskClassifier()

        # Feasible
        feasible, reason = classifier.validate_task_feasibility(
            context_size=65536,
            quality_importance=0.5,
        )
        assert feasible == True

        # Infeasible context size
        feasible, reason = classifier.validate_task_feasibility(
            context_size=300000,
            quality_importance=0.5,
        )
        assert feasible == False
        assert "exceeds P40 max" in reason

        # Invalid quality importance
        feasible, reason = classifier.validate_task_feasibility(
            context_size=32000,
            quality_importance=1.5,
        )
        assert feasible == False
        assert "must be 0.0-1.0" in reason


class TestGtxEscalationPolicy:
    """Tests for GtxEscalationPolicy."""

    def test_handle_success(self):
        """Test handling of successful implementation."""
        policy = GtxEscalationPolicy()

        result = policy.evaluate_failure(
            classification="success",
            findings=[],
            attempt_number=1,
            gtx_failures=0,
            p40_failures=0,
        )

        assert result.decision == RoutingDecision.ACCEPT_NEEDS_VERIFICATION
        assert result.target_worker == "codex"
        assert result.requires_fresh_verification == True

    def test_handle_partial_code_retry(self):
        """Test handling of partial code (retry local)."""
        policy = GtxEscalationPolicy()

        result = policy.evaluate_failure(
            classification="partial_code",
            findings=["missing_assertion_in_test.py:42"],
            attempt_number=1,
            gtx_failures=0,
            p40_failures=0,
        )

        assert result.decision == RoutingDecision.RETRY_LOCAL
        assert result.target_worker == "p40_qwen35"

    def test_handle_architectural_error_escalation(self):
        """Test escalation for architectural errors."""
        policy = GtxEscalationPolicy()

        result = policy.evaluate_failure(
            classification="incorrect_code",
            findings=["wrong_design_pattern.py"],
            review_evidence="The worker misunderstands the architectural pattern",
            is_architectural=True,
            attempt_number=1,
            gtx_failures=0,
            p40_failures=0,
        )

        assert result.decision == RoutingDecision.ESCALATE_TO_CODEX
        assert result.target_worker == "codex"
        assert "architectural_misunderstanding" in result.reason

    def test_handle_cross_file_escalation(self):
        """Test escalation for cross-file contract breaks."""
        policy = GtxEscalationPolicy()

        result = policy.evaluate_failure(
            classification="incorrect_code",
            findings=["api.py", "database.py", "models.py"],
            review_evidence="Multiple files affected, interface mismatch",
            is_cross_file=True,
            attempt_number=1,
            gtx_failures=0,
            p40_failures=0,
        )

        assert result.decision == RoutingDecision.ESCALATE_TO_CODEX
        assert "cross_file_contract_breakage" in result.reason

    def test_handle_repeated_error_escalation(self):
        """Test escalation for repeated conceptual errors."""
        policy = GtxEscalationPolicy()

        result = policy.evaluate_failure(
            classification="tests_failed",
            findings=["test_logic.py"],
            review_evidence="Same error appears again",
            is_repeated=True,
            attempt_number=2,
            gtx_failures=0,
            p40_failures=0,
        )

        assert result.decision == RoutingDecision.ESCALATE_TO_P40
        assert "repeated_conceptual_error" in result.reason

    def test_handle_non_substantive_timeout(self):
        """Test handling of non-substantive timeout."""
        policy = GtxEscalationPolicy()

        result = policy.evaluate_failure(
            classification="timeout",
            findings=[],
            attempt_number=1,
            gtx_failures=0,
            p40_failures=0,
        )

        assert result.decision == RoutingDecision.RETRY_LOCAL
        assert "Non-substantive" in result.reason

    def test_non_substantive_retry_limit_escalates_to_codex(self):
        """Repeated operational failures must not retry P40 forever."""
        policy = GtxEscalationPolicy()

        result = policy.evaluate_failure(
            classification="timeout",
            findings=["P40 worker timed out"],
            attempt_number=4,
            gtx_failures=0,
            p40_failures=0,
            workflow="direct_escalation",
        )

        assert result.decision == RoutingDecision.ESCALATE_TO_CODEX
        assert result.target_worker == "codex"
        assert "non-substantive P40 retry limit" in result.reason

    def test_handle_exceed_attempt_limit(self):
        """Test escalation when attempt limit exceeded."""
        policy = GtxEscalationPolicy()

        result = policy.evaluate_failure(
            classification="tests_failed",
            findings=["failing_test.py"],
            attempt_number=4,  # Exceeds limit of 3
            gtx_failures=3,
            p40_failures=0,
            workflow="direct_escalation",
        )

        assert result.decision == RoutingDecision.ESCALATE_TO_CODEX
        assert "Exceeded P40 attempt limit" in result.reason

    def test_is_substantive_failure(self):
        """Test substantive failure detection."""
        policy = GtxEscalationPolicy()

        assert policy.is_substantive_failure("partial_code") == True
        assert policy.is_substantive_failure("incorrect_code") == True
        assert policy.is_substantive_failure("tests_failed") == True
        assert policy.is_substantive_failure("timeout") == False
        assert policy.is_substantive_failure("no_code") == False

    def test_retry_guidance(self):
        """Test retry guidance generation."""
        policy = GtxEscalationPolicy()

        guidance = policy.get_retry_guidance([
            "Add missing assertion in test_login.py:42",
            "Fix off-by-one error in validation.py:15",
        ])

        assert "Retry with same goal" in guidance
        assert "test_login.py" in guidance
        assert "validation.py" in guidance


class TestGtxBrokerDirector:
    """Tests for GtxBrokerDirector controller."""

    @pytest.fixture
    def mock_state_file(self):
        """Create a mock StateFile for testing."""
        class MockStateFile:
            def __init__(self):
                self._registry = {
                    "gtx_broker": {
                        "current_state": None,
                        "task_history": [],
                        "provider_transitions": [],
                        "settings": {
                            "gtx_failure_threshold": 3,
                            "p40_failure_threshold": 3,
                            "default_quality_preference": "quality",
                            "allow_direct_escalation": True,
                        }
                    }
                }

            def get_registry(self):
                return self._registry

            def save_registry(self, registry):
                self._registry.update(registry)

            def update_state(self, mutate_fn):
                registry = self._registry
                entries = []
                current, entries = mutate_fn(registry, entries)
                self._registry = current
                return current

            def get_ledger_entries(self):
                return []

            def append_ledger_entry(self, entry):
                pass

        return MockStateFile()

    def test_route_first_attempt(self, mock_state_file):
        """Test routing for first attempt."""
        director = GtxBrokerDirector(mock_state_file)

        request = RoutingRequest(
            task_id="task-001",
            task_description="Implement login feature",
            context_size=8192,
            quality_importance=0.7,  # Quality preference is retained as metadata
            workflow="direct_escalation",
        )

        response = director.route_request(request)

        assert response.task_id == "task-001"
        assert response.target_worker == "p40_qwen35"
        assert response.attempt_number == 1
        assert response.gtx_failures == 0
        assert response.workflow == "direct_escalation"

    def test_route_large_context(self, mock_state_file):
        """Test routing for large context task."""
        director = GtxBrokerDirector(mock_state_file)

        request = RoutingRequest(
            task_id="task-002",
            task_description="Analyze 200k token document",
            context_size=200000,
            quality_importance=0.5,
            workflow="direct_escalation",
        )

        response = director.route_request(request)

        assert response.target_worker == "p40_qwen35"
        assert "context_size" in response.reason

    def test_route_with_previous_failure(self, mock_state_file):
        """Test routing after previous failure."""
        director = GtxBrokerDirector(mock_state_file)

        # First attempt
        request1 = RoutingRequest(
            task_id="task-003",
            task_description="Implement feature",
            context_size=32000,
            quality_importance=0.7,
            workflow="direct_escalation",
        )
        response1 = director.route_request(request1)
        assert response1.target_worker == "p40_qwen35"

        # Second attempt with failure
        request2 = RoutingRequest(
            task_id="task-003",
            task_description="Implement feature",
            context_size=32000,
            previous_classification="tests_failed",
            previous_findings=["failing_test.py"],
            previous_attempt_number=1,
            previous_p40_failures=1,
            workflow="direct_escalation",
        )
        response2 = director.route_request(request2)

        assert response2.attempt_number == 2
        assert response2.p40_failures == 1

    def test_record_attempt_result(self, mock_state_file):
        """Test recording attempt result."""
        director = GtxBrokerDirector(mock_state_file)

        # Route initial task
        request = RoutingRequest(
            task_id="task-004",
            task_description="Implement feature",
            context_size=32000,
            quality_importance=0.7,  # Quality-focused
            workflow="direct_escalation",
        )
        director.route_request(request)

        # Record success
        response = director.record_attempt_result(
            task_id="task-004",
            classification="success",
            findings=[],
            workflow="direct_escalation",
        )

        assert response.decision == RoutingDecision.ACCEPT_NEEDS_VERIFICATION
        assert response.target_worker == "codex"

    def test_get_current_state(self, mock_state_file):
        """Test getting current state."""
        director = GtxBrokerDirector(mock_state_file)

        request = RoutingRequest(
            task_id="task-005",
            task_description="Test task",
            context_size=16384,
            workflow="direct_escalation",
        )
        director.route_request(request)

        state = director.get_current_state()
        assert state is not None
        assert state.task_id == "task-005"

    def test_settings(self, mock_state_file):
        """Test settings management."""
        director = GtxBrokerDirector(mock_state_file)

        settings = director.get_settings()
        assert settings["gtx_failure_threshold"] == 3
        assert settings["allow_direct_escalation"] == True

        new_settings = director.update_settings({
            "gtx_failure_threshold": 5,
        })
        assert new_settings["gtx_failure_threshold"] == 5


class TestIntegration:
    """Integration tests for full workflow."""

    @pytest.fixture
    def mock_state_file(self):
        """Create a mock StateFile for testing."""
        class MockStateFile:
            def __init__(self):
                self._registry = {
                    "gtx_broker": {
                        "current_state": None,
                        "task_history": [],
                        "provider_transitions": [],
                        "settings": {
                            "gtx_failure_threshold": 3,
                            "p40_failure_threshold": 3,
                            "default_quality_preference": "quality",
                            "allow_direct_escalation": True,
                        }
                    }
                }

            def get_registry(self):
                return self._registry

            def save_registry(self, registry):
                self._registry.update(registry)

            def update_state(self, mutate_fn):
                registry = self._registry
                entries = []
                current, entries = mutate_fn(registry, entries)
                self._registry = current
                return current

            def get_ledger_entries(self):
                return []

            def append_ledger_entry(self, entry):
                pass

        return MockStateFile()

    def test_full_direct_escalation_workflow(self, mock_state_file):
        """Test complete direct escalation workflow."""
        director = GtxBrokerDirector(mock_state_file)

        # Step 1: GTX scopes the task and routes implementation to P40.
        request = RoutingRequest(
            task_id="feature-001",
            task_description="Implement login feature",
            context_size=32000,
            quality_importance=0.7,
            workflow="direct_escalation",
        )
        response1 = director.route_request(request)
        assert response1.target_worker == "p40_qwen35"
        assert response1.attempt_number == 1

        # Step 2: First attempt fails with tests_failed
        response2 = director.record_attempt_result(
            task_id="feature-001",
            classification="tests_failed",
            findings=["login_test.py:42 failed"],
            workflow="direct_escalation",
        )
        assert response2.decision == RoutingDecision.RETRY_LOCAL
        assert response2.attempt_number == 2

        # Step 3: Second attempt fails with architectural error
        response3 = director.record_attempt_result(
            task_id="feature-001",
            classification="incorrect_code",
            findings=["architecture.py"],
            review_evidence="Worker misunderstands the pattern",
            is_architectural=True,
            workflow="direct_escalation",
        )
        assert response3.decision == RoutingDecision.ESCALATE_TO_CODEX
        assert response3.target_worker == "codex"

        # Verify state was persisted
        state = director.get_current_state()
        # Both failures were made by P40, even though the second escalated to Codex.
        assert state.p40_substantive_failures == 2
        assert state.gtx_substantive_failures == 0
        assert state.attempt_number == 3  # Two attempts made + escalation


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
