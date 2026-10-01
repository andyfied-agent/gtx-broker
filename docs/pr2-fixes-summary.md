# PR #2 fixes

This document records the broker-owned fixes for PR #2 (`feat: vault tools and
README discovery`, Issues #22 and #23). Scheduler/orchestration and image-task
lifecycle work are excluded because Paperclip owns task scheduling now.

## Fixed

- Hermes tool imports target `gtx_broker.vault_readers` and
  `gtx_broker.repo_readers`.
- `gtx_broker.tools` is included in the wheel and `requests` is declared as a
  runtime dependency.
- Direct integration tests cover public tool definitions and handlers.
- Vault write-directory containment is checked before `mkdir()`, including
  traversal and symlinked-parent cases.
- Repository README discovery accepts an explicit JSON registry through
  `RepositoryTools(registry_path=...)` or
  `GTX_BROKER_REPOSITORY_REGISTRY`, reports configured failures, and parses
  normal HTTPS GitHub URLs.

## Verification

- Hermes, repository, and vault focused tests pass.
- Wheel build and installed-wheel Hermes imports pass.
- `git diff --check` passes before delivery.

## Scope

Paperclip owns task scheduling and orchestration. This broker PR does not send
Telegram messages or implement Teapot/GTX conversation routing. The route
`Telegram → I'm a Little Teapot → Hermes–Teapot → GTX` belongs to its owning
client/runtime projects.
