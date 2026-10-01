# PR #2 broker-only completion note

The broker-owned portion of PR #2 covers Hermes tool packaging/imports, vault
write-boundary enforcement, configurable repository README discovery, and their
tests.

Completed broker work:

- Hermes tools import through the public `gtx_broker.tools` package.
- The wheel includes Hermes tools and declares the repository-reader dependency.
- Vault write destinations are checked canonically before filesystem writes;
  traversal and symlink boundary regressions are covered.
- Repository discovery uses configured registry data and reports per-repository
  failures without silently dropping them.

Scheduler and image-task orchestration are deliberately not part of this PR.
Paperclip owns that lifecycle now. The broker does not send Telegram or Teapot
replies and does not implement the `Telegram → I'm a Little Teapot →
Hermes–Teapot → GTX` route.
