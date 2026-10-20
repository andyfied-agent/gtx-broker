# Telegram image ingress

`gtx-image-ingress --json-stdin` is retained only as a fail-closed compatibility
entry point. It rejects every image and does not stage files, create scheduler
tasks, call P40, or mutate the GTX Broker database.

The active Hermes hook is the separate `paperclip-image-ingress --json-stdin`
command. It validates and atomically stages the image under
`/mnt/scratch/gtx-images/incoming/<task-id>/image.*`, records source metadata,
and returns the durable path. It is stage-only: it does not create a scheduler
task or claim that a worker processed the image.

The active staging command reads one JSON event from standard input and writes
one JSON result to standard output. A successful result has `status="saved"` and
an absolute `path`; a replay of the same Telegram chat/message/content returns
the existing durable path with `idempotent_replay=true`. The retired CLI's
rejection is intentional so a stale `gtx-image-ingress` launcher cannot silently
reactivate the old scheduler/P40 image path.

Historical event fields included `source_path`, `chat_id`, `message_id`,
`user_id`, `kind`, `caption`, `media_group_id`, `schema`, and `prompt`.
`schema` is `receipt` by default. Use `image_description` for non-receipt
images; the broker stores that selection in the task payload and validates the
corresponding structured response. Receipt tasks default to
`requires_review=true`; callers may explicitly set `requires_review=false` only
for workflows that have separately been approved for unattended processing. A
caller-provided `prompt` is permitted for an approved schema but does not bypass
output validation.

The former implementation validated the file with the storage contract, copied
it atomically below the configured storage root, recorded quality metrics, and
queued an idempotent nightly vision task. The active staging command retains the
validated atomic copy and source metadata but deliberately does not create a
scheduler task or run quality-gate/model work.

The Python `TelegramImageIngress.ingest()` API remains an explicitly constructed
internal compatibility surface for scheduler callers and is covered by the
scheduler-handler tests. It is not used by the active Hermes hook. The public
`gtx-image-ingress` CLI remains fail-closed so retired task scheduling cannot be
reactivated accidentally.
Older Hermes configurations referred to this as the
`platforms.telegram.extra.broker_image_ingress_command` hook. The configured
command now points to the stage-only `paperclip-image-ingress` entry point.

The broker's VisionReviewService is the storage/scheduler boundary for the
authorized review UI: it lists `awaiting_review` tasks and records an approval
or rejection before making the scheduler transition. A Telegram-facing adapter
must enforce the Hermes approval list before calling those methods.

The scheduler daemon moves accepted input from `incoming/` to `processing/`
before a worker reads it, then moves it to `processed/` on completion or back
to `incoming/` for retry. Retention cleanup remains a separate operational
task; this integration does not claim a fixed deletion interval.
