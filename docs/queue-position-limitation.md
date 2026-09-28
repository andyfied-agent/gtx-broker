# Queue Position API Limitation

**File**: `gtx_broker/status_api.py::_handle_queue_position()`  
**Date**: 2026-09-28  
**Status**: Known limitation, not a bug

## Current Behavior

The `/queue?task_id=...` endpoint calculates queue position by sorting all queued tasks by:

1. `priority` descending (higher priority first)
2. `created_at` ascending (earlier creation time first)

This matches the **priority ordering** used by the scheduler.

## Scheduler Dispatch Logic

The scheduler's `get_next_task()` method uses a more complex selection algorithm that considers:

- Priority ordering (same as API)
- Current scheduling window (immediate vs. batch vs. nightly)
- `schedule_type` (designated as immediate/batch/nightly)
- Review status (tasks awaiting review are excluded from normal dispatch)
- `task_kind` (vision, coding, data, maintenance)
- `mode` (interactive vs. queue mode)
- Worker availability and P40/GTX resource leases
- Stale claim detection

## Why They Don't Match

A task reported as position 1 by the API might not be dispatched first if:

1. It has `schedule_type=nightly` and the current time is in the immediate window (18:00-00:00)
2. It has `mode=queue` and there are `mode=interactive` tasks waiting
3. Its `task_kind=vision` but the P40 is currently unavailable (model not loaded)
4. It's in `claimed` or `running` state (excluded from queued count but not filtered by simple sort)
5. Another task has the same priority but was created earlier and passes all eligibility checks

## Design Trade-off

**Option A: Match scheduler exactly**

Pros:
- API response reflects actual dispatch order
- Consumers can trust position 1 = will be dispatched next

Cons:
- Requires duplicating scheduler's full eligibility logic in API
- Tightly couples API to scheduler's implementation details
- Harder to maintain and test
- Potential for race conditions if logic diverges

**Option B: Priority-only sorting (current)**

Pros:
- Simple, auditable, easy to test
- Reflects the primary scheduling dimension (priority)
- Decoupled from scheduler's complex eligibility logic
- Clear documentation of what position means

Cons:
- Position ≠ actual dispatch order in edge cases
- Consumers must understand the limitation

## Recommendation

**Keep Option B (current)** with explicit documentation.

The queue position is documented as a **priority-based ranking**, not a guaranteed dispatch order. Consumers who need exact dispatch order should:

1. Poll `/queue` statistics for overall state
2. Use `/status/{task_id}` for per-task state and events
3. Subscribe to events (future feature) for real-time state changes
4. Accept that position 1 is the highest-priority eligible task, but actual dispatch may differ based on scheduling windows and resource availability

## Future Work

If exact dispatch order is required, consider:

1. **Event-based subscriptions**: Push state changes to consumers instead of polling position
2. **Eligibility flag**: Add `is_eligible_for_dispatch` field to task metadata
3. **Separate endpoint**: `/queue/next` that returns the task ID that will be dispatched next
4. **Scheduler refactor**: Extract eligibility logic into a shared module

---

*This document was added as part of PR#12 fixes to clarify the queue position API limitation.*
