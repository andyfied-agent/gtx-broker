"""Model registry operations for the Second Shift fallback group."""

from __future__ import annotations

import copy
from enum import Enum
from typing import Dict, List, Optional

from .state import StateFile


class ModelStatus(Enum):
    ACTIVE = "active"
    UNUSABLE = "unusable"
    BLOCKED = "blocked"
    STANDBY = "standby"
    UNKNOWN = "unknown"


class GoalClass(Enum):
    CODING = "coding"
    REASONING = "reasoning"
    REVIEW = "review"
    GENERAL = "general"


class ModelRegistry:
    """Pure registry transformations plus durable load/save helpers."""

    def __init__(self, state_file: StateFile):
        self.state = state_file

    def load_registry(self) -> Dict:
        return self.state.get_registry()

    def save_registry(self, registry: Dict) -> None:
        self.state.save_registry(registry)

    def get_model(self, model_id: str, registry: Dict) -> Optional[Dict]:
        for model in registry.get("models", []):
            if model.get("id") == model_id:
                return copy.deepcopy(model)
        return None

    def get_all_models(self, registry: Dict) -> List[Dict]:
        return [copy.deepcopy(model) for model in registry.get("models", [])]

    def update_model(self, registry: Dict, model_id: str, updates: Dict) -> Dict:
        result = copy.deepcopy(registry)
        for model in result.get("models", []):
            if model.get("id") == model_id:
                model.update(copy.deepcopy(updates))
                break
        return result

    def get_substantive_failure_count(self, model_id: str, goal_class: str, registry: Dict) -> int:
        model = self.get_model(model_id, registry)
        return (model or {}).get("failure_counts_by_goal", {}).get(goal_class, 0)

    def increment_substantive_failure(self, model_id: str, goal_class: str, registry: Dict) -> Dict:
        result = copy.deepcopy(registry)
        for model in result.get("models", []):
            if model.get("id") != model_id:
                continue
            counts = model.setdefault("failure_counts_by_goal", {})
            count = counts.get(goal_class, 0) + 1
            counts[goal_class] = count
            if count >= self.state.SUBSTANTIVE_FAILURE_THRESHOLD and model.get("status") == "active":
                model.update({
                    "status": "unusable",
                    "quarantined_at": self.state.timestamp_now(),
                    "quarantine_reason": f"{self.state.SUBSTANTIVE_FAILURE_THRESHOLD} substantive failures for {goal_class}",
                    "reviewer_required": True,
                })
            break
        return result

    def decrement_substantive_failure(self, model_id: str, goal_class: str, registry: Dict) -> Dict:
        result = copy.deepcopy(registry)
        for model in result.get("models", []):
            if model.get("id") == model_id:
                counts = model.setdefault("failure_counts_by_goal", {})
                counts[goal_class] = max(0, counts.get(goal_class, 0) - 1)
                break
        return result

    def set_failure_count(self, model_id: str, goal_class: str, count: int, registry: Dict) -> Dict:
        if count < 0:
            raise ValueError("failure count cannot be negative")
        result = copy.deepcopy(registry)
        for model in result.get("models", []):
            if model.get("id") != model_id:
                continue
            model.setdefault("failure_counts_by_goal", {})[goal_class] = count
            if count >= self.state.SUBSTANTIVE_FAILURE_THRESHOLD and model.get("status") == "active":
                model.update({
                    "status": "unusable",
                    "quarantined_at": self.state.timestamp_now(),
                    "quarantine_reason": f"{self.state.SUBSTANTIVE_FAILURE_THRESHOLD} substantive failures for {goal_class}",
                    "reviewer_required": True,
                })
            break
        return result

    def reset_failure_counts(self, model_id: str, registry: Dict, *, reviewer: Optional[str] = None, reviewer_evidence: Optional[str] = None) -> Dict:
        result = copy.deepcopy(registry)
        model = self.get_model(model_id, result)
        if model and model.get("status") == "unusable":
            return self.reactivate_model(model_id, reviewer, result, reviewer_evidence=reviewer_evidence)
        for item in result.get("models", []):
            if item.get("id") == model_id:
                item["failure_counts_by_goal"] = {}
                break
        return result

    def record_last_successful_artifact(self, model_id: str, artifact_ref: str, registry: Dict) -> Dict:
        if not artifact_ref or not artifact_ref.strip():
            raise ValueError("artifact reference is required")
        result = copy.deepcopy(registry)
        for model in result.get("models", []):
            if model.get("id") == model_id:
                model["last_successful_artifact"] = artifact_ref
                model["last_success_at"] = self.state.timestamp_now()
                break
        return result

    def get_last_successful_artifact(self, model_id: str, registry: Dict) -> Optional[str]:
        model = self.get_model(model_id, registry)
        return model.get("last_successful_artifact") if model else None

    def is_model_quarantined(self, model_id: str, registry: Dict) -> bool:
        model = self.get_model(model_id, registry)
        return bool(model and model.get("status") == "unusable")

    def is_model_active(self, model_id: str, registry: Dict) -> bool:
        model = self.get_model(model_id, registry)
        return bool(model and model.get("status") == "active")

    def quarantine_model(self, model_id: str, reason: str, registry: Dict) -> Dict:
        if not reason or not reason.strip():
            raise ValueError("quarantine reason is required")
        return self.update_model(registry, model_id, {
            "status": "unusable", "quarantined_at": self.state.timestamp_now(),
            "quarantine_reason": reason, "reviewer_required": True,
        })

    def reactivate_model(self, model_id: str, reviewer: Optional[str], registry: Dict, *, reviewer_evidence: Optional[str] = None) -> Dict:
        if not reviewer or not reviewer.strip():
            raise ValueError("reviewer identity is required")
        if not reviewer_evidence or not reviewer_evidence.strip():
            raise ValueError("explicit reviewer evidence is required")
        result = copy.deepcopy(registry)
        for model in result.get("models", []):
            if model.get("id") != model_id:
                continue
            if model.get("status") != "unusable":
                raise ValueError("only quarantined models require reactivation")
            model.update({
                "status": "active", "reviewer_required": False,
                "reviewed_by": reviewer, "reviewed_at": self.state.timestamp_now(),
                "reviewer_evidence": reviewer_evidence,
                "quarantined_at": None, "quarantine_reason": None,
                "failure_counts_by_goal": {},
            })
            break
        return result

    def select_best_model(self, goal_class: str, registry: Dict) -> Optional[str]:
        candidates = [m for m in self.get_all_models(registry) if m.get("status") == "active"]
        candidates.sort(key=lambda m: (m.get("failure_counts_by_goal", {}).get(goal_class, 0), m.get("id", "")))
        return candidates[0].get("id") if candidates else None

    def get_pool_stats(self, registry: Dict) -> Dict:
        stats = {"active": 0, "unusable": 0, "blocked": 0, "standby": 0}
        for model in self.get_all_models(registry):
            if model.get("status") in stats:
                stats[model["status"]] += 1
        return stats
