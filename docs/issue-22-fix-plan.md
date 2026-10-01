# PR #2 broker-only fix plan

## Scope

PR #2 is limited to broker-owned Hermes tools, vault access, repository README
discovery, packaging, and documentation. Scheduler/orchestration and image-task
lifecycle work are excluded: Paperclip is the authoritative task scheduler.

The Telegram/Teapot/GTX route remains outside this repository:
`Telegram → I'm a Little Teapot → Hermes–Teapot → GTX`.

## Prioritized work items

### P0 — Hermes tools and packaging

This is the first priority because broken imports, omitted packages, or missing
runtime dependencies make the broker tools unusable after installation.

- Correct public tool imports and handlers.
- Include `gtx_broker.tools` in the wheel.
- Declare runtime dependencies used by repository readers.
- Exercise the public tool handlers and verify an installed-wheel import.

### P0 — Vault boundary

This is the first security priority because a write-boundary failure can create
or modify files outside the configured vault.

- Resolve and validate canonical write destinations before any filesystem write.
- Reject traversal and symlinked write paths without creating directories outside
the vault.
- Keep read and write operations within the configured vault boundary.

### P1 — Repository discovery

Implement this after the installability and filesystem safety gates are green.
It is the main functional scope of the README-discovery change.

- Load repository mappings from an explicit JSON registry when configured.
- Support multiple configured local and remote repositories.
- Return per-repository success or failure rather than silently dropping errors.
- Keep local README reads within the configured repository boundary.

## Acceptance criteria

- Hermes tools import from source and from an installed wheel.
- The wheel includes `gtx_broker.tools` and declares all runtime dependencies.
- Vault traversal and symlink boundary tests pass.
- Configured multi-repository discovery and remote URL parsing tests pass.
- No scheduler, Paperclip, Telegram presentation, or Teapot/GTX implementation is
added to this broker PR.
