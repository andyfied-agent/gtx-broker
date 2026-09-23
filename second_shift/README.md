# Second Shift model registry, recovery, and dispatcher

Persistent state module with file-based locking for atomic updates
and concurrent writer support.

Features:
- Persist registry and failure records across restarts
- Count only substantive code failures per model and goal class
- Keep no-code/empty response, timeout, endpoint/auth/provider/rate-limit/refusal/credit events distinct and non-substantive
- Quarantine after three substantive failures
- Require explicit reviewer evidence before reactivation
- Preserve evidence references and last successful artifact
- Update state atomically for concurrent writers
- Track independent credit state with explicit exhaustion evidence

## Implemented scope

- P7.1 persistent registry and failure ledger;
- P7.2 independent credit-state tracking;
- P7.3 staged Second Shift pool recovery;
- P7.5 default P40-to-Second-Shift dispatcher and reviewer chain.

Chat Shift recovery is implemented separately under the root `chat_shift/`
package for P7.4. Candidate attempts receive routine independent Codex review
and accepted artifacts require a separate fresh Codex final verification.
Copilot is a coding/repair worker while available; provider or quota exhaustion
is recorded as an operational transition to Codex rather than an
implementation failure. `DefaultCodingDispatcher.route_coding_request()` takes
an explicit `workflow` (`layered-review` or `direct-escalation`) and `repair`
flag: Layered Review selects Second Shift after the P40 threshold, while Direct
Escalation selects Copilot when usable or Codex after Copilot failover. A Codex
repair is recorded as an implementation attempt and must be followed by a
separate `record_final_verification()` call. Copilot attempts classified as
explicit `credit_exhausted`, `endpoint_unavailable`, or `authentication`
results update persistent provider state atomically, so the next repair routes
to Codex and records the transition without counting an implementation failure.

The state directory defaults to `~/.hermes/second-shift`; callers may pass an
explicit directory in tests or an isolated runtime. Chat Shift must use its
own state directory and must not share this registry or ledger.

`BacklogExecutionAdapter` provides the durable ordered-backlog execution
boundary. Its backlog default is `gtx_direct_escalation`: GTX scopes the item,
P40 receives the full repository/worktree context, and Codex is used for
verification or exhaustion escalation. Worker, test, review, and verification
callbacks are injectable so the lifecycle and restart tests remain offline.
