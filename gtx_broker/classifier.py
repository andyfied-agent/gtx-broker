"""Task classification for GTX broker routing.

Classifies incoming tasks into routing categories based on:
- Task description and requirements
- Context size needs
- GPU memory constraints
- Quality vs. speed preferences

Author: GTX Broker P7.5
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, List
from enum import Enum

from .state import TaskClassification, GTXBrokerState


class GtxModelConfig(Enum):
    """Available GTX model configurations."""
    GTX_IQ3_XS = "gtx_iq3_xs"
    GTX_Q2_K = "gtx_q2_k"
    P40_QWEN35 = "p40_qwen35"


@dataclass
class GtxModelProfile:
    """GPU model profile with constraints."""
    model_id: str
    name: str
    vram_required_gb: float
    max_context: int
    is_quality_focused: bool
    speed_tier: str  # "slow", "medium", "fast", "fastest"
    endpoints: List[str]

    # Performance characteristics
    tokens_per_second: float = 0.0
    load_time_seconds: float = 0.0


class GtxTaskClassifier:
    """Classify tasks for GTX broker routing decisions.

    Maps task requirements to appropriate GPU/model configuration:
    - Bounded implementation tasks -> P40 (GTX scopes, P40 implements)
    - Large context tasks (>64k) -> P40
    - Benchmark validation -> P40 (qualification evidence required)
    - Complex reasoning -> Escalate to Codex
    """

    # GPU memory constraints (from compute01-gtx-selection-2026-09-22.md)
    GTX_1650_VRAM_GB = 4.0
    GTX_1650_SAFE_HEADROOM_GB = 0.3  # Keep 300MB headroom

    # Context size thresholds
    SMALL_CONTEXT_THRESHOLD = 32000  # Tasks under this fit both GPUs
    LARGE_CONTEXT_THRESHOLD = 65536  # Tasks needing >64k context (P40 only)
    MAX_CONTEXT_P40 = 262144  # P40's maximum context

    # Quality vs. speed thresholds
    QUALITY_IMPORTANCE_HIGH = 0.6  # Above this, prefer quality-focused models
    QUALITY_IMPORTANCE_MEDIUM = 0.5

    def __init__(self):
        """Initialize with model profiles."""
        self.model_profiles = {
            GtxModelConfig.GTX_IQ3_XS: GtxModelProfile(
                model_id="gtx_iq3_xs",
                name="GTX IQ3_XS",
                vram_required_gb=3.7,
                max_context=65536,
                is_quality_focused=True,
                speed_tier="medium",
                endpoints=["http://127.0.0.1:8081/v1"],
                tokens_per_second=40.7,
                load_time_seconds=2.07,
            ),
            GtxModelConfig.GTX_Q2_K: GtxModelProfile(
                model_id="gtx_q2_k",
                name="GTX Q2_K",
                vram_required_gb=3.6,
                max_context=65536,
                is_quality_focused=False,
                speed_tier="fastest",
                endpoints=["http://127.0.0.1:8081/v1"],
                tokens_per_second=52.1,
                load_time_seconds=2.07,
            ),
            GtxModelConfig.P40_QWEN35: GtxModelProfile(
                model_id="p40_qwen35",
                name="P40 Qwen3.5",
                vram_required_gb=15.0,  # Estimate for 35B model
                max_context=262144,
                is_quality_focused=True,
                speed_tier="slow",
                endpoints=["http://127.0.0.1:11436/v1"],
                tokens_per_second=25.0,
                load_time_seconds=15.0,
            ),
        }

    def classify_task(
        self,
        task_id: str,
        task_description: str,
        context_size: int,
        quality_importance: float = 0.5,
        requires_benchmark_evidence: bool = False,
    ) -> GTXBrokerState:
        """Classify a task and return the recommended routing state.

        Args:
            task_id: Unique identifier for the task
            task_description: Task description or goal text
            context_size: Required context size in tokens
            quality_importance: 0.0-1.0 importance of quality vs. speed
            requires_benchmark_evidence: Whether task needs P40 qualification

        Returns:
            GTXBrokerState with recommended routing decision
        """
        # Step 1: Determine task classification
        task_class = self._determine_task_class(
            task_description,
            context_size,
            requires_benchmark_evidence,
        )

        # Step 2: Select model based on constraints
        model_id = self._select_model(
            task_class,
            context_size,
            quality_importance,
            requires_benchmark_evidence,
        )

        # Step 3: Build state object
        state = GTXBrokerState(
            selected_model=model_id,
            workflow=None,  # Will be set by dispatcher
            task_id=task_id,
            task_description=task_description,
            task_class=task_class.value,
            context_size=context_size,
            quality_preference="quality" if quality_importance >= 0.5 else "speed",
            evidence=[
                f"classification={task_class.value}",
                f"context_size={context_size}",
                f"quality_importance={quality_importance}",
                f"requires_benchmark={requires_benchmark_evidence}",
            ],
        )

        return state

    def _determine_task_class(
        self,
        task_description: str,
        context_size: int,
        requires_benchmark_evidence: bool,
    ) -> TaskClassification:
        """Determine task classification from description and constraints."""
        task_desc_lower = task_description.lower()

        # Rule 1: Requires benchmark evidence -> benchmark_validation
        if requires_benchmark_evidence:
            return TaskClassification.BENCHMARK_VALIDATION

        # Rule 2: Large context needs (>64k) -> P40 only
        if context_size > self.LARGE_CONTEXT_THRESHOLD:
            return TaskClassification.LARGE_CONTEXT

        # Rule 3: Keyword-based classification
        # Benchmark keywords (check AFTER context check)
        # Use more specific patterns to avoid false positives
        if self._has_keyword(task_desc_lower, [
            "deterministic benchmark", "qualification test", "benchmark run",
            "score", "metric", "eval", "reproducible", "validation suite"
        ]):
            return TaskClassification.BENCHMARK_VALIDATION

        if self._has_keyword(task_desc_lower, [
            "large context", "256k", "262k", "very long", "extended context"
        ]):
            return TaskClassification.LARGE_CONTEXT

        if self._has_keyword(task_desc_lower, [
            "reasoning", "analysis", "architectural", "design",
            "strategy", "planning", "complex"
        ]):
            return TaskClassification.COMPLEX_REASONING

        if self._has_keyword(task_desc_lower, [
            "fix", "repair", "correct", "adjust", "minor", "bounded",
            "small", "quick", "simple", "typo"
        ]):
            return TaskClassification.REPAIR_LOCAL

        # Default: bounded implementation (most common)
        return TaskClassification.BOUNDED_IMPLEMENTATION

    def _select_model(
        self,
        task_class: TaskClassification,
        context_size: int,
        quality_importance: float,
        requires_benchmark_evidence: bool,
    ) -> str:
        """Select the most appropriate model for the task.

        Args:
            task_class: Classification of the task
            context_size: Required context size
            quality_importance: 0.0-1.0 quality vs. speed tradeoff
            requires_benchmark_evidence: Whether P40 qualification is needed

        Returns:
            Model ID string (e.g., "gtx_iq3_xs", "p40_qwen35")
        """
        # Rule 1: Benchmark validation always uses P40
        if task_class == TaskClassification.BENCHMARK_VALIDATION:
            return "p40_qwen35"

        # Rule 2: Large context >64k requires P40
        if context_size > self.LARGE_CONTEXT_THRESHOLD:
            return "p40_qwen35"

        # Rule 3: Complex reasoning escalates to Codex
        if task_class == TaskClassification.COMPLEX_REASONING:
            return "codex"

        # Rule 4: GTX is the scoping/controller layer.  The existing direct
        # escalation workflow gives implementation work to the P40, including
        # bounded repairs, so the GTX models never become coding workers.
        if task_class in {
            TaskClassification.BOUNDED_IMPLEMENTATION,
            TaskClassification.REPAIR_LOCAL,
        }:
            return "p40_qwen35"

        # Keep a deterministic P40 fallback for any future task class added
        # without an explicit routing rule.
        return "p40_qwen35"

    def _has_keyword(self, text: str, keywords: List[str]) -> bool:
        """Check if text contains any of the given keywords."""
        for keyword in keywords:
            if keyword in text:
                return True
        return False

    def get_model_constraints(self, model_id: str) -> dict:
        """Get VRAM and context constraints for a model."""
        # Try to match by string first, then by enum
        profile = None
        for key, prof in self.model_profiles.items():
            if key.value == model_id or key == model_id:
                profile = prof
                break

        if not profile:
            return {}

        return {
            "model_id": profile.model_id,
            "name": profile.name,
            "vram_required_gb": profile.vram_required_gb,
            "max_context": profile.max_context,
            "is_quality_focused": profile.is_quality_focused,
            "speed_tier": profile.speed_tier,
            "tokens_per_second": profile.tokens_per_second,
        }

    def validate_task_feasibility(
        self,
        context_size: int,
        quality_importance: float,
    ) -> tuple[bool, str]:
        """Check if a task is feasible with available resources.

        Returns:
            Tuple of (feasible, reason)
        """
        # Check if context size is within P40's max
        if context_size > self.MAX_CONTEXT_P40:
            return False, f"Context size {context_size} exceeds P40 max ({self.MAX_CONTEXT_P40})"

        # Check if quality_importance is valid
        if not 0.0 <= quality_importance <= 1.0:
            return False, f"Quality importance {quality_importance} must be 0.0-1.0"

        return True, "Task is feasible"

    def route_task_standalone(
        self,
        task_id: str,
        task_description: str,
        context_size: int,
        quality_importance: float = 0.5,
        requires_benchmark_evidence: bool = False,
    ) -> tuple[str, dict, list]:
        """Route a task without persisting state (stateless).

        Useful for calling from within StateFile.update_state() context
        to avoid lock deadlocks.

        Args:
            task_id: Unique identifier for the task
            task_description: Task description or goal text
            context_size: Required context size in tokens
            quality_importance: 0.0-1.0 importance of quality vs. speed
            requires_benchmark_evidence: Whether task needs P40 qualification

        Returns:
            Tuple of (selected_model, model_config, evidence)
        """
        # Classify and route
        state = self.classify_task(
            task_id=task_id,
            task_description=task_description,
            context_size=context_size,
            quality_importance=quality_importance,
            requires_benchmark_evidence=requires_benchmark_evidence,
        )

        # Get model constraints
        model_config = self.get_model_constraints(state.selected_model)

        return state.selected_model, model_config, state.evidence


__all__ = [
    "GtxModelConfig",
    "GtxModelProfile",
    "GtxTaskClassifier",
]
