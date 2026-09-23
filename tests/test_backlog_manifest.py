"""Tests for the backlog manifest contract.

Covers:
- Valid data acceptance
- Missing required fields (all fields)
- Duplicate issue IDs
- Invalid status values
- Invalid numeric values (context_size, quality_importance)
- Boolean benchmark flag parsing
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from second_shift.backlog_manifest import (
    BacklogItem,
    BacklogStatus,
    ManifestValidationError,
    P40_MAX_CONTEXT_SIZE,
    load_backlog_items,
    load_manifest,
    save_manifest,
    validate_backlog_item,
    validate_manifest,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def valid_item_data():
    """A valid minimal backlog item dictionary."""
    return {
        'issue_id': 'TASK-001',
        'title': 'Test Task',
        'description': 'This is a test task description.',
        'acceptance_criteria': ['Criterion 1', 'Criterion 2'],
        'context_size': 32768,
        'quality_importance': 0.75,
        'requires_benchmark_evidence': True,
        'status': 'queued',
    }


@pytest.fixture
def valid_item(valid_item_data):
    """A valid BacklogItem instance."""
    return BacklogItem.from_dict(valid_item_data)


@pytest.fixture
def valid_manifest_data(valid_item_data):
    """A valid manifest dictionary."""
    return {
        'items': [valid_item_data],
    }


@pytest.fixture
def temp_file():
    """Create a temporary file for save/load tests."""
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        temp_path = Path(f.name)
    yield temp_path
    if temp_path.exists():
        temp_path.unlink()


# =============================================================================
# Valid data tests
# =============================================================================

class TestValidData:
    """Tests for valid backlog items and manifests."""

    def test_valid_item_creation(self, valid_item):
        """Valid data should create without errors."""
        assert valid_item.issue_id == 'TASK-001'
        assert valid_item.title == 'Test Task'
        assert len(valid_item.acceptance_criteria) == 2

    def test_valid_manifest_load(self, valid_manifest_data):
        """Valid manifest data should validate without errors."""
        result = validate_manifest(valid_manifest_data)
        assert result == valid_manifest_data

    def test_all_statuses_allowed(self):
        """All defined statuses should be valid."""
        for status in BacklogStatus:
            item_data = {
                'issue_id': 'TASK-001',
                'title': 'Test Task',
                'description': 'Test description.',
                'acceptance_criteria': ['Done when complete'],
                'context_size': 32768,
                'quality_importance': 0.5,
                'requires_benchmark_evidence': False,
                'status': status.value,
            }
            item = BacklogItem.from_dict(item_data)
            assert item.status == status

    def test_boundary_context_size_values(self):
        """Boundary values for context_size should be valid."""
        # Minimum positive value
        item = BacklogItem(
            issue_id='TASK-MIN',
            title='Min Context',
            description='Test',
            acceptance_criteria=['Done'],
            context_size=1,
            quality_importance=0.5,
            requires_benchmark_evidence=False,
            status=BacklogStatus.QUEUED,
        )
        assert item.context_size == 1

        # Maximum allowed (P40 limit)
        item = BacklogItem(
            issue_id='TASK-MAX',
            title='Max Context',
            description='Test',
            acceptance_criteria=['Done'],
            context_size=P40_MAX_CONTEXT_SIZE,
            quality_importance=0.5,
            requires_benchmark_evidence=False,
            status=BacklogStatus.QUEUED,
        )
        assert item.context_size == P40_MAX_CONTEXT_SIZE

    def test_boundary_quality_importance_values(self):
        """Boundary values for quality_importance should be valid."""
        # Minimum: 0.0
        item = BacklogItem(
            issue_id='TASK-LOW',
            title='Low Priority',
            description='Test',
            acceptance_criteria=['Done'],
            context_size=1024,
            quality_importance=0.0,
            requires_benchmark_evidence=False,
            status=BacklogStatus.QUEUED,
        )
        assert item.quality_importance == 0.0

        # Maximum: 1.0
        item = BacklogItem(
            issue_id='TASK-HIGH',
            title='High Priority',
            description='Test',
            acceptance_criteria=['Done'],
            context_size=1024,
            quality_importance=1.0,
            requires_benchmark_evidence=False,
            status=BacklogStatus.QUEUED,
        )
        assert item.quality_importance == 1.0

    def test_boolean_parsing_true(self):
        """Boolean true values should parse correctly."""
        for true_value in [True, 'true', 'True', 'TRUE', 1]:
            if isinstance(true_value, bool):
                item_data = {
                    'issue_id': 'TASK-001',
                    'title': 'Test',
                    'description': 'Test',
                    'acceptance_criteria': ['Done'],
                    'context_size': 1024,
                    'quality_importance': 0.5,
                    'requires_benchmark_evidence': true_value,
                    'status': 'queued',
                }
                item = BacklogItem.from_dict(item_data)
                assert item.requires_benchmark_evidence is True

    def test_boolean_parsing_false(self):
        """Boolean false values should parse correctly."""
        for false_value in [False, 'false', 'False', 'FALSE', 0]:
            if isinstance(false_value, bool):
                item_data = {
                    'issue_id': 'TASK-001',
                    'title': 'Test',
                    'description': 'Test',
                    'acceptance_criteria': ['Done'],
                    'context_size': 1024,
                    'quality_importance': 0.5,
                    'requires_benchmark_evidence': false_value,
                    'status': 'queued',
                }
                item = BacklogItem.from_dict(item_data)
                assert item.requires_benchmark_evidence is False

    def test_save_and_load_manifest(self, temp_file, valid_item):
        """Save and load should preserve data."""
        save_manifest(temp_file, [valid_item])
        loaded = load_backlog_items(temp_file)
        assert len(loaded) == 1
        assert loaded[0].issue_id == valid_item.issue_id

    def test_save_rejects_duplicate_ids_before_writing(self, temp_file, valid_item):
        """Saving must validate the full serialized manifest."""
        with pytest.raises(ManifestValidationError, match='duplicate'):
            save_manifest(temp_file, [valid_item, valid_item])
        assert temp_file.read_text() == ''


# =============================================================================
# Missing required field tests
# =============================================================================

class TestMissingRequiredFields:
    """Tests for missing required fields."""

    def test_missing_issue_id(self, valid_item_data):
        """Missing issue_id should raise error."""
        del valid_item_data['issue_id']
        with pytest.raises(ManifestValidationError, match='issue_id') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))
        assert 'required' in str(exc.value).lower() or 'non-empty' in str(exc.value).lower()

    def test_missing_title(self, valid_item_data):
        """Missing title should raise error."""
        del valid_item_data['title']
        with pytest.raises(ManifestValidationError, match='title') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_missing_description(self, valid_item_data):
        """Missing description should raise error."""
        del valid_item_data['description']
        with pytest.raises(ManifestValidationError, match='description') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_missing_acceptance_criteria(self, valid_item_data):
        """Missing acceptance_criteria should raise error."""
        del valid_item_data['acceptance_criteria']
        with pytest.raises(ManifestValidationError, match='acceptance_criteria') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_missing_context_size(self, valid_item_data):
        """Missing context_size should raise error."""
        del valid_item_data['context_size']
        with pytest.raises(ManifestValidationError, match='context_size') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_missing_quality_importance(self, valid_item_data):
        """Missing quality_importance should raise error."""
        del valid_item_data['quality_importance']
        with pytest.raises(ManifestValidationError, match='quality_importance') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_missing_requires_benchmark_evidence(self, valid_item_data):
        """Missing requires_benchmark_evidence should raise error."""
        del valid_item_data['requires_benchmark_evidence']
        with pytest.raises(ManifestValidationError, match='requires_benchmark_evidence') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_missing_status(self, valid_item_data):
        """Missing status should raise error."""
        del valid_item_data['status']
        with pytest.raises(ManifestValidationError, match='status') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_empty_string_issue_id(self, valid_item_data):
        """Empty string issue_id should raise error."""
        valid_item_data['issue_id'] = ''
        with pytest.raises(ManifestValidationError, match='issue_id') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_whitespace_only_issue_id(self, valid_item_data):
        """Whitespace-only issue_id should raise error."""
        valid_item_data['issue_id'] = '   '
        with pytest.raises(ManifestValidationError, match='issue_id') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    @pytest.mark.parametrize('field_name', ['issue_id', 'title', 'description'])
    def test_non_string_required_fields(self, valid_item_data, field_name):
        """Non-string required fields must fail with the manifest error type."""
        valid_item_data[field_name] = 123
        with pytest.raises(ManifestValidationError, match=field_name):
            BacklogItem.from_dict(valid_item_data)


# =============================================================================
# Duplicate ID tests
# =============================================================================

class TestDuplicateIssueIds:
    """Tests for duplicate issue_id detection."""

    def test_exact_duplicate_ids(self, valid_item_data):
        """Exact duplicate IDs should be rejected."""
        manifest = {
            'items': [valid_item_data.copy(), valid_item_data.copy()],
        }
        with pytest.raises(ManifestValidationError, match='duplicate') as exc:
            validate_manifest(manifest)

    def test_different_case_ids_are_distinct(self, valid_item_data):
        """Issue ID uniqueness is case-sensitive."""
        valid_item_data['issue_id'] = 'Task-001'
        manifest = {
            'items': [
                valid_item_data.copy(),
                {**valid_item_data, 'issue_id': 'task-001'},
            ],
        }
        assert validate_manifest(manifest) == manifest

    def test_multiple_duplicates_in_manifest(self, valid_item_data):
        """Manifest with multiple duplicates should report first occurrence."""
        manifest = {
            'items': [
                {**valid_item_data, 'issue_id': 'A'},
                {**valid_item_data, 'issue_id': 'B'},
                {**valid_item_data, 'issue_id': 'A'},  # Duplicate
                {**valid_item_data, 'issue_id': 'C'},
                {**valid_item_data, 'issue_id': 'B'},  # Another duplicate
            ],
        }
        with pytest.raises(ManifestValidationError, match='duplicate') as exc:
            validate_manifest(manifest)
        assert 'A' in str(exc.value)

    @pytest.mark.parametrize('issue_id', [['TASK-001'], {'value': 'TASK-001'}])
    def test_unhashable_issue_id_is_validation_error(self, valid_item_data, issue_id):
        """List/object IDs must fail through validate_manifest cleanly."""
        manifest = {'items': [{**valid_item_data, 'issue_id': issue_id}]}
        with pytest.raises(ManifestValidationError, match='issue_id'):
            validate_manifest(manifest)


# =============================================================================
# Invalid status tests
# =============================================================================

class TestInvalidStatus:
    """Tests for invalid status values."""

    def test_invalid_status_string(self, valid_item_data):
        """Invalid status string should raise error."""
        valid_item_data['status'] = 'invalid_status'
        with pytest.raises(ManifestValidationError, match='status') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))
        assert 'queued' in str(exc.value).lower() or 'status' in str(exc.value).lower()

    def test_invalid_status_int(self, valid_item_data):
        """Integer status should raise error."""
        valid_item_data['status'] = 123
        with pytest.raises(ManifestValidationError, match='status') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_invalid_status_object(self, valid_item_data):
        """Object status should raise error."""
        valid_item_data['status'] = {'value': 'queued'}
        with pytest.raises(ManifestValidationError, match='status') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_missing_status_value(self, valid_item_data):
        """Empty string status should raise error."""
        valid_item_data['status'] = ''
        with pytest.raises(ManifestValidationError, match='status') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))


# =============================================================================
# Invalid numeric value tests
# =============================================================================

class TestInvalidNumericValues:
    """Tests for invalid numeric values."""

    def test_negative_context_size(self, valid_item_data):
        """Negative context_size should raise error."""
        valid_item_data['context_size'] = -1
        with pytest.raises(ManifestValidationError, match='context_size') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_zero_context_size(self, valid_item_data):
        """Zero context_size should raise error."""
        valid_item_data['context_size'] = 0
        with pytest.raises(ManifestValidationError, match='context_size') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_float_context_size(self, valid_item_data):
        """Float context_size should raise error (must be int)."""
        valid_item_data['context_size'] = 1024.5
        with pytest.raises(ManifestValidationError, match='context_size') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_context_size_exceeds_p40_max(self, valid_item_data):
        """context_size > P40_MAX_CONTEXT_SIZE should raise error."""
        valid_item_data['context_size'] = P40_MAX_CONTEXT_SIZE + 1
        with pytest.raises(ManifestValidationError, match='context_size') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))
        assert str(P40_MAX_CONTEXT_SIZE) in str(exc.value)

    def test_quality_importance_negative(self, valid_item_data):
        """negative quality_importance should raise error."""
        valid_item_data['quality_importance'] = -0.1
        with pytest.raises(ManifestValidationError, match='quality_importance') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_quality_importance_above_one(self, valid_item_data):
        """quality_importance > 1.0 should raise error."""
        valid_item_data['quality_importance'] = 1.1
        with pytest.raises(ManifestValidationError, match='quality_importance') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_quality_importance_float(self, valid_item_data):
        """Float quality_importance should be accepted."""
        valid_item_data['quality_importance'] = 0.5
        item = BacklogItem.from_dict(valid_item_data)
        assert item.quality_importance == 0.5

    def test_quality_importance_as_int_zero(self, valid_item_data):
        """quality_importance as integer 0 should be accepted."""
        valid_item_data['quality_importance'] = 0
        item = BacklogItem.from_dict(valid_item_data)
        assert item.quality_importance == 0

    def test_quality_importance_as_int_one(self, valid_item_data):
        """quality_importance as integer 1 should be accepted."""
        valid_item_data['quality_importance'] = 1
        item = BacklogItem.from_dict(valid_item_data)
        assert item.quality_importance == 1

    @pytest.mark.parametrize('field_name', ['context_size', 'quality_importance'])
    def test_json_boolean_is_not_numeric(self, valid_item_data, field_name):
        """JSON booleans must not pass Python's int/float checks."""
        valid_item_data[field_name] = True
        with pytest.raises(ManifestValidationError, match=field_name):
            BacklogItem.from_dict(valid_item_data)


# =============================================================================
# Acceptance criteria tests
# =============================================================================

class TestAcceptanceCriteria:
    """Tests for acceptance_criteria validation."""

    def test_empty_acceptance_criteria_list(self, valid_item_data):
        """Empty acceptance_criteria list should raise error."""
        valid_item_data['acceptance_criteria'] = []
        with pytest.raises(ManifestValidationError, match='acceptance_criteria') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_acceptance_criteria_not_list(self, valid_item_data):
        """Non-list acceptance_criteria should raise error."""
        valid_item_data['acceptance_criteria'] = "single criterion"
        with pytest.raises(ManifestValidationError, match='acceptance_criteria') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_empty_string_in_criteria(self, valid_item_data):
        """Empty string in acceptance_criteria should raise error."""
        valid_item_data['acceptance_criteria'] = ['Valid', '', 'Another']
        with pytest.raises(ManifestValidationError, match='acceptance_criteria') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_whitespace_only_in_criteria(self, valid_item_data):
        """Whitespace-only string in acceptance_criteria should raise error."""
        valid_item_data['acceptance_criteria'] = ['Valid', '   ', 'Another']
        with pytest.raises(ManifestValidationError, match='acceptance_criteria') as exc:
            validate_backlog_item(BacklogItem.from_dict(valid_item_data))

    def test_single_valid_criterion(self, valid_item_data):
        """Single valid criterion should pass."""
        valid_item_data['acceptance_criteria'] = ['Only criterion']
        item = BacklogItem.from_dict(valid_item_data)
        assert len(item.acceptance_criteria) == 1


# =============================================================================
# JSON parsing tests
# =============================================================================

class TestJsonParsing:
    """Tests for JSON loading and parsing."""

    def test_load_invalid_json_file(self, temp_file):
        """Invalid JSON file should raise error."""
        temp_file.write_text('not valid json {')
        with pytest.raises((json.JSONDecodeError, ManifestValidationError)):
            load_manifest(temp_file)

    def test_load_empty_manifest(self, temp_file):
        """Empty manifest (no items) should be valid."""
        save_manifest(temp_file, [])
        items = load_backlog_items(temp_file)
        assert len(items) == 0

    def test_load_manifest_missing_items_key(self, temp_file):
        """Manifest without items key should raise error."""
        temp_file.write_text(json.dumps({'not_items': []}))
        with pytest.raises(ManifestValidationError, match='items') as exc:
            load_manifest(temp_file)

    def test_load_non_dict_manifest(self, temp_file):
        """Non-dict manifest should raise error."""
        temp_file.write_text(json.dumps([]))
        with pytest.raises(ManifestValidationError, match='object') as exc:
            load_manifest(temp_file)

    def test_unknown_field_is_a_manifest_validation_error(self, valid_item_data):
        """Malformed schema should not escape as a raw dataclass TypeError."""
        malformed = {**valid_item_data, 'typo_field': 123}
        with pytest.raises(ManifestValidationError, match='unknown'):
            BacklogItem.from_dict(malformed)
        with pytest.raises(ManifestValidationError, match='unknown'):
            validate_manifest({'items': [malformed]})


# =============================================================================
# BacklogStatus enum tests
# =============================================================================

class TestBacklogStatusEnum:
    """Tests for BacklogStatus enum behavior."""

    def test_all_statuses_defined(self):
        """All expected statuses should be defined."""
        statuses = [s.value for s in BacklogStatus]
        assert 'queued' in statuses
        assert 'in_progress' in statuses
        assert 'completed' in statuses
        assert 'blocked' in statuses
        assert 'escalated' in statuses

    def test_status_to_dict_roundtrip(self, valid_item_data):
        """Status should serialize and deserialize correctly."""
        for status in BacklogStatus:
            valid_item_data['status'] = status.value
            item = BacklogItem.from_dict(valid_item_data)
            assert item.status == status
            serialized = item.to_dict()
            assert serialized['status'] == status.value
