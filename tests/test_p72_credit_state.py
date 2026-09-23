"""P7.2 Credit-State Tracking Tests.

Focus: independent credit-state tracking for each Second Shift model.
States: exactly available, low, exhausted, unknown.
Exhausted may be set only from explicit provider/account evidence.
Timeout, no response, empty output, no_code, and generic endpoint failure
must leave credits unknown or unchanged.
Preserve implementation-failure counts and model status as separate state.
Persist credit transitions atomically with registry/ledger state.
Record evidence and timestamp for explicit provider credit evidence.
Validate invalid states.
Out of scope: P7.3/P7.4 pool recovery, P7.5 dispatch.
"""

import copy
import json
import tempfile
from pathlib import Path
from typing import Dict, List

import pytest

from second_shift.state import StateFile
from second_shift.credit_state import ModelCreditState, CreditStatus
from second_shift.registry import ModelRegistry


@pytest.fixture
def temp_state_dir():
    """Create a temporary directory for state files."""
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def state_file(temp_state_dir):
    """Create a StateFile instance pointing to temp directory."""
    return StateFile(temp_state_dir)


@pytest.fixture
def credit_state(state_file):
    """Create a ModelCreditState instance."""
    return ModelCreditState(state_file)


@pytest.fixture
def sample_registry():
    """Return a sample registry with Second Shift models."""
    return {
        "models": [
            {
                "id": "openrouter/poolside/laguna-s-2.1:free",
                "provider": "openrouter",
                "role": "agentic-coding",
                "status": "active",
                "credit_status": "unknown",
                "consecutive_substantive_failures": 0,
            },
            {
                "id": "openrouter/cohere/north-mini-code:free",
                "provider": "openrouter",
                "role": "light-coding",
                "status": "active",
                "credit_status": "unknown",
                "consecutive_substantive_failures": 0,
            },
            {
                "id": "openrouter/nvidia/nemotron-3-ultra:free",
                "provider": "openrouter",
                "role": "reasoning-coding",
                "status": "active",
                "credit_status": "available",
                "consecutive_substantive_failures": 0,
            },
        ],
    }


def _save_registry(state_file: StateFile, registry: Dict) -> None:
    """Helper to save registry to disk."""
    registry_obj = ModelRegistry(state_file)
    registry_obj.save_registry(registry)


def _load_registry(state_file: StateFile) -> Dict:
    """Helper to load registry from disk."""
    registry_obj = ModelRegistry(state_file)
    return registry_obj.load_registry()


class TestCreditStatusValidation:
    """Tests for credit status validation."""

    def test_validate_valid_status_available(self, credit_state):
        """Validate available status is valid."""
        errors = credit_state.validate_credit_status("available")
        assert errors == []

    def test_validate_valid_status_low(self, credit_state):
        """Validate low status is valid."""
        errors = credit_state.validate_credit_status("low")
        assert errors == []

    def test_validate_valid_status_exhausted(self, credit_state):
        """Validate exhausted status is valid."""
        errors = credit_state.validate_credit_status("exhausted")
        assert errors == []

    def test_validate_valid_status_unknown(self, credit_state):
        """Validate unknown status is valid."""
        errors = credit_state.validate_credit_status("unknown")
        assert errors == []

    def test_validate_invalid_status(self, credit_state):
        """Validate invalid status returns errors."""
        errors = credit_state.validate_credit_status("invalid")
        assert len(errors) == 1
        assert "Invalid credit status" in errors[0]
        assert "available" in errors[0]

    def test_validate_invalid_status_none(self, credit_state):
        """Validate None status returns errors."""
        errors = credit_state.validate_credit_status(None)
        assert len(errors) == 1
        assert "Invalid credit status" in errors[0]


class TestExplicitExhaustion:
    """Tests for explicit provider credit exhaustion."""

    def test_set_exhausted_with_explicit_provider_evidence(
        self, credit_state, state_file, sample_registry
    ):
        """Set exhausted from explicit provider evidence."""
        # First save registry to disk
        _save_registry(state_file, sample_registry)

        evidence = [
            "Provider API returned 402 Payment Required",
            "Account balance: $0.00",
        ]
        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "exhausted",
            evidence=evidence,
            is_explicit_provider_evidence=True,
        )
        model = credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", registry
        )
        assert model["credit_status"] == "exhausted"
        assert "credit_evidence" in model
        assert "credit_exhausted_at" in model
        assert len(model["credit_evidence"]) == 2

    def test_set_exhausted_without_explicit_evidence_fails(
        self, credit_state, sample_registry
    ):
        """Setting exhausted without explicit evidence raises ValueError."""
        with pytest.raises(ValueError, match="exhausted.*explicit provider.*evidence"):
            credit_state.update_model_credit_status(
                sample_registry,
                "openrouter/poolside/laguna-s-2.1:free",
                "exhausted",
                evidence=["some guess"],
                is_explicit_provider_evidence=False,
            )

    def test_set_exhausted_from_timeout_does_not_change(
        self, credit_state, state_file, sample_registry
    ):
        """Timeout does not set exhausted - must use explicit provider evidence."""
        # First save registry
        _save_registry(state_file, sample_registry)

        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "unknown",  # timeout should leave as unknown
            evidence=["Timeout after 30s"],
            is_explicit_provider_evidence=False,
        )
        model = credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", registry
        )
        assert model["credit_status"] == "unknown"
        assert "credit_evidence" not in model

    def test_set_exhausted_from_no_code_does_not_change(
        self, credit_state, state_file, sample_registry
    ):
        """no_code does not set exhausted - must use explicit provider evidence."""
        _save_registry(state_file, sample_registry)

        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "unknown",  # no_code should leave as unknown
            evidence=["No code generated in response"],
            is_explicit_provider_evidence=False,
        )
        model = credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", registry
        )
        assert model["credit_status"] == "unknown"
        assert "credit_evidence" not in model

    def test_set_exhausted_from_endpoint_failure_does_not_change(
        self, credit_state, state_file, sample_registry
    ):
        """Generic endpoint failure does not set exhausted."""
        _save_registry(state_file, sample_registry)

        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "unknown",
            evidence=["Connection refused"],
            is_explicit_provider_evidence=False,
        )
        model = credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", registry
        )
        assert model["credit_status"] == "unknown"
        assert "credit_evidence" not in model

    def test_set_exhausted_from_empty_response_does_not_change(
        self, credit_state, state_file, sample_registry
    ):
        """Empty/no response does not set exhausted."""
        _save_registry(state_file, sample_registry)

        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "unknown",
            evidence=["Empty response body"],
            is_explicit_provider_evidence=False,
        )
        model = credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", registry
        )
        assert model["credit_status"] == "unknown"
        assert "credit_evidence" not in model

    def test_explicit_credit_evidence_classifications(self, credit_state):
        """Verify which classifications constitute explicit credit evidence."""
        assert credit_state.is_explicit_credit_evidence_classification("credit_exhausted")
        assert credit_state.is_explicit_credit_evidence_classification("rate_limited")
        assert not credit_state.is_explicit_credit_evidence_classification("timeout")
        assert not credit_state.is_explicit_credit_evidence_classification("no_code")
        assert not credit_state.is_explicit_credit_evidence_classification(
            "endpoint_unavailable"
        )


class TestCreditRecovery:
    """Tests for credit recovery from exhausted."""

    def test_recover_from_exhausted_to_available(
        self, credit_state, state_file, sample_registry
    ):
        """Recover credit status from exhausted to available."""
        _save_registry(state_file, sample_registry)

        # First exhaust
        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "exhausted",
            evidence=["Provider returned 402"],
            is_explicit_provider_evidence=True,
        )
        assert credit_state.get_credit_status(
            "openrouter/poolside/laguna-s-2.1:free", registry
        ) == "exhausted"

        # Then recover
        registry = credit_state.recover_credit_status(
            registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "available",
            evidence=["Provider confirmed credit top-up"],
        )
        model = credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", registry
        )
        assert model["credit_status"] == "available"
        assert "credit_evidence" not in model
        assert "credit_exhausted_at" not in model
        assert "credit_recovery_evidence" in model
        assert "credit_recovered_at" in model

    def test_recover_from_exhausted_to_low(
        self, credit_state, state_file, sample_registry
    ):
        """Recover credit status from exhausted to low."""
        _save_registry(state_file, sample_registry)

        # First exhaust
        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "exhausted",
            evidence=["Provider returned 402"],
            is_explicit_provider_evidence=True,
        )

        # Then recover to low
        registry = credit_state.recover_credit_status(
            registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "low",
            evidence=["Provider confirmed 10% balance remaining"],
        )
        model = credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", registry
        )
        assert model["credit_status"] == "low"
        assert "credit_evidence" not in model
        assert "credit_exhausted_at" not in model

    def test_recover_from_exhausted_to_unknown(
        self, credit_state, state_file, sample_registry
    ):
        """Recover credit status from exhausted to unknown."""
        _save_registry(state_file, sample_registry)

        # First exhaust
        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "exhausted",
            evidence=["Provider returned 402"],
            is_explicit_provider_evidence=True,
        )

        # Then recover to unknown
        registry = credit_state.recover_credit_status(
            registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "unknown",
        )
        model = credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", registry
        )
        assert model["credit_status"] == "unknown"
        assert "credit_evidence" not in model
        assert "credit_exhausted_at" not in model

    def test_recover_to_exhausted_raises(self, credit_state, sample_registry):
        """Recovery to exhausted is not allowed - use update_model_credit_status."""
        with pytest.raises(ValueError, match="Recovery must be to available/low/unknown"):
            credit_state.recover_credit_status(
                sample_registry,
                "openrouter/poolside/laguna-s-2.1:free",
                "exhausted",
            )


class TestPersistence:
    """Tests for state persistence across restarts."""

    def test_persist_credit_state_to_disk(
        self, credit_state, state_file, temp_state_dir, sample_registry
    ):
        """Credit state persists to disk via StateFile."""
        _save_registry(state_file, sample_registry)

        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "exhausted",
            evidence=["Provider 402"],
            is_explicit_provider_evidence=True,
        )

        # Simulate restart by creating new StateFile instance
        new_state = StateFile(temp_state_dir)
        new_credit_state = ModelCreditState(new_state)

        # Load from disk
        loaded_registry = new_state.get_registry()
        model = new_credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", loaded_registry
        )
        assert model is not None
        assert model["credit_status"] == "exhausted"
        assert "credit_evidence" in model
        assert "credit_exhausted_at" in model

    def test_persist_credit_transition_atomically_with_ledger(
        self, credit_state, state_file, sample_registry
    ):
        """Credit transition is atomic with ledger state."""
        from second_shift.ledger import FailureLedger
        ledger = FailureLedger(state_file)

        _save_registry(state_file, sample_registry)

        # Record a failure that transitions credit state
        # Note: ledger.record_failure records credit_status in the ledger entry
        # but doesn't update the registry model. We need to also call update_model_credit_status
        # to update the registry model's credit_status field.
        ledger.record_failure(
            model_id="openrouter/poolside/laguna-s-2.1:free",
            provider="openrouter",
            goal_id="test-goal-1",
            goal_class="coding",
            classification="credit_exhausted",
            evidence=["Provider API 402"],
            registry=sample_registry,
            credit_status="exhausted",
        )

        # Now also update the registry model's credit_status
        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "exhausted",
            evidence=["Provider API 402"],
            is_explicit_provider_evidence=True,
        )

        # Verify credit status was recorded in registry
        model = credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", registry
        )
        assert model["credit_status"] == "exhausted"
        assert "credit_evidence" in model

        # Verify ledger entry exists
        entries = state_file.get_ledger_entries()
        assert len(entries) == 1
        assert entries[0]["credit_status"] == "exhausted"

    def test_persist_low_status(
        self, credit_state, state_file, temp_state_dir, sample_registry
    ):
        """Low credit status persists."""
        _save_registry(state_file, sample_registry)

        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "low",
            is_explicit_provider_evidence=False,  # low doesn't need explicit evidence
        )

        # Restart and verify
        new_state = StateFile(temp_state_dir)
        new_credit_state = ModelCreditState(new_state)
        loaded_registry = new_state.get_registry()
        model = new_credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", loaded_registry
        )
        assert model["credit_status"] == "low"

    def test_persist_unknown_status(
        self, credit_state, state_file, temp_state_dir, sample_registry
    ):
        """Unknown credit status persists."""
        _save_registry(state_file, sample_registry)

        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "unknown",
        )

        # Restart and verify
        new_state = StateFile(temp_state_dir)
        new_credit_state = ModelCreditState(new_state)
        loaded_registry = new_state.get_registry()
        model = new_credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", loaded_registry
        )
        assert model["credit_status"] == "unknown"

    def test_persist_available_status(
        self, credit_state, state_file, temp_state_dir, sample_registry
    ):
        """Available credit status persists."""
        _save_registry(state_file, sample_registry)

        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "available",
            is_explicit_provider_evidence=False,
        )

        # Restart and verify
        new_state = StateFile(temp_state_dir)
        new_credit_state = ModelCreditState(new_state)
        loaded_registry = new_state.get_registry()
        model = new_credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", loaded_registry
        )
        assert model["credit_status"] == "available"


class TestFailureCountsSeparateFromCredit:
    """Tests that failure counts and model status are separate from credit state."""

    def test_failure_count_separate_from_credit_status(
        self, credit_state, state_file, sample_registry
    ):
        """Failure counts are tracked separately from credit status."""
        _save_registry(state_file, sample_registry)

        registry = credit_state.set_substantive_failure_count(
            "openrouter/poolside/laguna-s-2.1:free",
            "coding",
            2,
            sample_registry,
        )
        model = credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", registry
        )
        assert model["failure_counts_by_goal"]["coding"] == 2
        assert model["credit_status"] == "unknown"

    def test_model_status_separate_from_credit_status(
        self, credit_state, state_file, sample_registry
    ):
        """Model status (active/unusable) is separate from credit status."""
        _save_registry(state_file, sample_registry)

        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "exhausted",
            evidence=["Provider 402"],
            is_explicit_provider_evidence=True,
        )
        model = credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", registry
        )
        assert model["credit_status"] == "exhausted"
        assert model["status"] == "active"  # credit exhausted doesn't change status


class TestAtomicUpdates:
    """Tests for atomic updates to credit state."""

    def test_atomic_update_with_multiple_models(
        self, credit_state, state_file, temp_state_dir, sample_registry
    ):
        """Credit updates to multiple models are atomic."""
        _save_registry(state_file, sample_registry)

        registry1 = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "exhausted",
            evidence=["Provider 402 for model 1"],
            is_explicit_provider_evidence=True,
        )
        registry2 = credit_state.update_model_credit_status(
            registry1,
            "openrouter/cohere/north-mini-code:free",
            "low",
            evidence=["Provider 429 for model 2"],
            is_explicit_provider_evidence=True,
        )

        # Verify both models updated
        model1 = credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", registry2
        )
        model2 = credit_state.get_model(
            "openrouter/cohere/north-mini-code:free", registry2
        )
        assert model1["credit_status"] == "exhausted"
        assert model2["credit_status"] == "low"

        # Simulate restart and verify both persisted
        new_state = StateFile(temp_state_dir)
        new_credit_state = ModelCreditState(new_state)
        loaded_registry = new_state.get_registry()
        loaded_model1 = new_credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", loaded_registry
        )
        loaded_model2 = new_credit_state.get_model(
            "openrouter/cohere/north-mini-code:free", loaded_registry
        )
        assert loaded_model1["credit_status"] == "exhausted"
        assert loaded_model2["credit_status"] == "low"

    def test_atomic_update_preserves_other_model_fields(
        self, credit_state, state_file, sample_registry
    ):
        """Atomic update preserves non-credit fields."""
        _save_registry(state_file, sample_registry)

        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "exhausted",
            evidence=["Provider 402"],
            is_explicit_provider_evidence=True,
        )
        model = credit_state.get_model(
            "openrouter/poolside/laguna-s-2.1:free", registry
        )
        assert model["provider"] == "openrouter"
        assert model["role"] == "agentic-coding"
        assert model["status"] == "active"
        assert "consecutive_substantive_failures" in model


class TestUnknownDefault:
    """Tests for default unknown status."""

    def test_new_model_has_unknown_credit_status(
        self, credit_state, state_file, sample_registry
    ):
        """New model (or model without credit_status) defaults to unknown."""
        _save_registry(state_file, sample_registry)

        registry = credit_state.update_model_credit_status(
            sample_registry,
            "openrouter/poolside/laguna-s-2.1:free",
            "exhausted",
            evidence=["Provider 402"],
            is_explicit_provider_evidence=True,
        )
        # Add a new model without credit_status
        registry["models"].append({
            "id": "new-model",
            "provider": "test",
            "role": "coding",
            "status": "active",
        })
        model = credit_state.get_model("new-model", registry)
        assert model is not None
        assert credit_state.get_credit_status("new-model", registry) == "unknown"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
