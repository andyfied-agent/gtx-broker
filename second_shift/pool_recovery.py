"""P7.3 staged Second Shift pool recovery.

Recovery state is kept inside the P7.1 registry transaction. A transition is
counted only when an observed pool crosses down to two or up to four usable
members; repeated observations and temporary health changes do not flap the
pool. Copilot and Codex are recovery members, never original members.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Dict, Optional, Set

from .state import StateFile


RECOVERY_MEMBERS = ("copilot", "codex")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_usable_member_ids(registry: Dict) -> Set[str]:
    """Return active models whose credit state is not explicitly exhausted."""
    return {
        model["id"] for model in registry.get("models", [])
        if model.get("status") == "active"
        and model.get("credit_status", "unknown") != "exhausted"
        and isinstance(model.get("id"), str)
    }


def _recovery_state(registry: Dict) -> dict:
    state = registry.get("pool_recovery")
    if not isinstance(state, dict):
        state = {}
    return {
        "usable_member_count": state.get("usable_member_count"),
        "last_usable_count": state.get("last_usable_count"),
        "transitions_to_two": int(state.get("transitions_to_two", 0)),
        "transitions_to_four": int(state.get("transitions_to_four", 0)),
        "recovery_members": list(state.get("recovery_members", [])),
        "original_members": list(state.get("original_members", [])),
        "last_transition_at": state.get("last_transition_at"),
        "last_observation_at": state.get("last_observation_at"),
        "transition_events": list(state.get("transition_events", [])),
    }


def _model(registry: dict, model_id: str) -> Optional[dict]:
    for model in registry.get("models", []):
        if model.get("id") == model_id:
            return model
    return None


def check_pool_recovery(state: StateFile, registry_seed: Optional[Dict] = None) -> tuple[Dict, Dict]:
    """Observe and apply one atomic Second Shift recovery transition.

    A new state directory may be seeded with ``registry_seed``. Once persisted,
    the registry on disk is authoritative. The returned events are descriptive
    only; all model/status and counter changes are persisted by the same
    ``StateFile.update_state`` transaction.
    """
    seed = copy.deepcopy(registry_seed or {})
    observed = {"actions": [], "drop_to_two": False, "rise_to_four": False}

    def mutate(registry: Dict, entries: list[dict]):
        registry = registry if registry.get("models") else seed
        registry = copy.deepcopy(registry)
        recovery = _recovery_state(registry)
        models = {m.get("id") for m in registry.get("models", [])}

        if not recovery["original_members"]:
            recovery["original_members"] = sorted(
                model_id for model_id in models if model_id not in RECOVERY_MEMBERS
            )

        usable_before = len(get_usable_member_ids(registry))
        previous = recovery["last_usable_count"]
        drop_to_two = previous is not None and previous > 2 and usable_before <= 2
        rise_to_four = previous is not None and previous < 4 and usable_before >= 4
        actions = []

        if drop_to_two:
            recovery["transitions_to_two"] += 1
            observed["drop_to_two"] = True
            target = RECOVERY_MEMBERS[recovery["transitions_to_two"] - 1] if recovery["transitions_to_two"] <= 2 else None
            target_model = _model(registry, target) if target else None
            if target_model is not None and target not in recovery["original_members"]:
                if target_model.get("status") != "active":
                    target_model["status"] = "active"
                    actions.append(f"activate_{target}")
                if target not in recovery["recovery_members"]:
                    recovery["recovery_members"].append(target)

        if rise_to_four:
            recovery["transitions_to_four"] += 1
            observed["rise_to_four"] = True
            target = RECOVERY_MEMBERS[2 - recovery["transitions_to_four"]] if recovery["transitions_to_four"] <= 2 else None
            target_model = _model(registry, target) if target else None
            if target_model is not None and target in recovery["recovery_members"] and target not in recovery["original_members"]:
                target_model["status"] = "standby"
                recovery["recovery_members"].remove(target)
                actions.append(f"deactivate_{target}")

        observed["actions"] = list(actions)

        if drop_to_two or rise_to_four:
            recovery["last_transition_at"] = _now()
            recovery["transition_events"].append({
                "kind": "drop_to_two" if drop_to_two else "rise_to_four",
                "actions": list(actions),
                "at": recovery["last_transition_at"],
            })
        recovery["usable_member_count"] = usable_before
        recovery["last_usable_count"] = usable_before
        recovery["last_observation_at"] = _now()
        registry["pool_recovery"] = recovery
        return registry, entries

    updated = state.update_state(mutate)
    recovery = _recovery_state(updated)
    events = {
        "actions_taken": observed["actions"],
        "transition_to_two": observed["drop_to_two"],
        "transition_to_four": observed["rise_to_four"],
        "new_usable_count": recovery["usable_member_count"],
        "transitions_to_two": recovery["transitions_to_two"],
        "transitions_to_four": recovery["transitions_to_four"],
        "recovery_members": set(recovery["recovery_members"]),
        "original_members": set(recovery["original_members"]),
    }
    return updated, events


def pool_recovery_status(state: StateFile) -> dict:
    registry = state.get_registry()
    recovery = _recovery_state(registry)
    return {
        **recovery,
        "recovery_members": set(recovery["recovery_members"]),
        "original_members": set(recovery["original_members"]),
    }
