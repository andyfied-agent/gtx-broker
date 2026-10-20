# Scheduler Priority 3 implementation

This stage makes the execution policy durable in the broker rather than
leaving it only in the planning notes.

Implemented:

- task schedule metadata for immediate, batch, and nightly work;
- explicit review tags and review-worker selection;
- P40-aware queue ordering for coding and vision tasks when they are eligible;
- image-window gating so batch work waits while vision tasks remain pending;
- an explicit unavailable `air-review` worker profile, with no silent fallback
  to the P40 or GTX;
- durable batch epochs and a review barrier;
- persisted review evidence on an epoch.

The follow-on integration stage is included in the same branch:

- `ImageQualityGate` records dimensions, orientation, brightness, clipping,
  focus, content hash, and deterministic `pass`/`degraded`/`needs_review`/
  `reject` status;
- vision output contracts now distinguish receipt extraction from the opt-in
  general `image_description` schema, with actual image-content validation;
- receipt ingress defaults to durable human review, and
  `VisionReviewService` records approval or rejection before the scheduler
  reaches a terminal state;
- an opt-in P40 model-profile boundary restores the configured default profile
  in `finally`; it never targets the GTX worker;
- the former `gtx-image-ingress --json-stdin` implementation validated and
  atomically staged a Telegram image, recorded source metadata, applied the
  quality gate, and created an idempotent nightly vision task; the current
  compatibility command rejects events fail-closed; active Telegram image
  handling uses the separate stage-only Hermes hook and does not create a
  scheduler task or call P40;
- `CodingHandler` provides an explicit P40 executor/worktree/test boundary and
  rejects successful-but-dirty worktrees when a commit is required;
- `ReviewHandler` provides an explicit read-only Air-review command boundary,
  validates structured findings, and fails if the reviewer changes the
  worktree.
- daemon restart reconciliation keeps scheduler and durable storage outcomes
  recoverable when either side is interrupted;
- configurable retention removes only terminal processed/rejected inputs and
  stale staging directories.

P40-backed tasks are preferred within the eligible queue. The policy still
keeps image work ahead of ordinary batch work during 00:00-06:00, and general
conversation remains a GTX task. When
`GTX_P40_MODEL_SWITCH_COMMAND` is configured, the daemon may switch only the
P40 profile and restores the configured default after each task. It never
switches or touches the GTX service.

Not implemented by this stage:

- enabling the guarded switch command in the daemon's production environment;
- a live Air reviewer runtime;
- a configured production coding executor;
- the upstream Hermes adapter hook (the broker command is ready and the Hermes
  checkout documents the optional hook);
- a Telegram-facing review acknowledgement/approval adapter;
- live Air Reviewer/slow coder services;

Those remain separate stages with their own worker and integration tests.
