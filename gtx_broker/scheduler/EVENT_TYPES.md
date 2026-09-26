# Scheduler Event Types

## Overview

The scheduler logs all state transitions to the `task_events` table. Each event captures:
- `event_type`: What happened
- `from_state`: Previous state (nullable for initial events)
- `to_state`: New state
- `details`: Human-readable summary
- `created_at`: Timestamp

## Event Types

### Task Lifecycle Events

|| Event Type | Description | When Generated |
|------------|-------------|----------------|
| `task_added` | New task admitted to queue | `add_task()` succeeds |
| `task_claimed` | Task reserved for processing | `claim_task()` succeeds |
| `task_started` | Task execution begins | `start_task()` succeeds |
| `task_awaiting_review` | Task sent for human review | `transition_running_to_awaiting_review()` succeeds |
| `task_completed` | Task finished successfully/failed | `complete_task()` called |
| `task_cancelled` | Task cancelled by operator | `cancel_task()` succeeds |

### Worker Assignment Events

| Event Type | Description | When Generated |
|------------|-------------|----------------|
| `worker_selected` | Worker chosen for task | Before `start_task()` |
| `worker_failed` | Worker execution failed | In `complete_task()` with failure_class |

### Retry/Recovery Events

| Event Type | Description | When Generated |
|------------|-------------|----------------|
| `retry_scheduled` | Task queued for retry | `retry_wait` state transition |
| `task_requeued` | Task returned to queue | `awaiting_review` → `queued` |
| `attempt_limit_reached` | Max attempts exceeded | Auto-escalation |

## Event Details Format

### task_added
```json
{
  "details": "kind=vision, mode=batch, priority=10"
}
```

### task_started
```json
{
  "details": "worker=p40-vision, model=p40-vision-qwen35"
}
```

### task_completed
```json
{
  "details": "result=success"  // or "result=failure"
}
```

### task_failed
```json
{
  "details": "failure_class=timeout, error=Connection refused"
}
```

## Example Event Sequence

```
task_added: None -> queued
  |
  v
task_claimed: queued -> claimed
  |
  v
task_started: claimed -> running
  |
  v
task_completed: running -> succeeded
```

## Querying Events

```sql
-- Get all events for a task
SELECT * FROM task_events
WHERE task_id = 'task-abc123'
ORDER BY created_at DESC;

-- Get state transition history
SELECT 
  task_id,
  event_type,
  from_state,
  to_state,
  details,
  created_at
FROM task_events
WHERE task_id = 'task-abc123'
ORDER BY created_at ASC;

-- Count tasks by final state
SELECT 
  to_state,
  COUNT(*) as count
FROM task_events
WHERE event_type = 'task_completed'
GROUP BY to_state;
```

---

*Created: 2026-09-26*
