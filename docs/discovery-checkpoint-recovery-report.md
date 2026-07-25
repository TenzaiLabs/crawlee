# Discovery Checkpoint and Recovery Report

Generated: `2026-07-24`

## Result

The crawler now persists two atomic recovery points:

1. the immutable controlled-known-file, standard-Katana, pure-headless-Katana,
   and passive-CDP baseline; and
2. the latest validated browser-guided discovery sitemap, evidence, and progress.

All compare-and-set transitions, checkpoint publications, latest-checkpoint
finalization, and atomic completion are owned by `job_persistence.py`. The
obsolete completion path that could publish a sitemap without discovery
metadata has been removed.

The API persists the validated discovery configuration and does not expose
Chrome/CDP executable choices. A real server accepted a discovery-disabled job
with lower client budgets, persisted its baseline, finalized it with outcome
`disabled`, and returned the same metadata after restart.

During discovery, a checkpoint is written after direct browser evidence, after
each successful enrichment lane, and at the end of each round. If later model,
browser, or Katana work fails, finalization prefers this partial checkpoint and
falls back to the immutable baseline only when the partial value is absent or
invalid.

Startup recovery follows the same rule:

- an interrupted `discovering` job with a valid partial checkpoint completes
  with `server_restarted_after_discovery_checkpoint` and retains its additions;
- otherwise a valid baseline completes with outcome `interrupted`;
- a job interrupted before either checkpoint becomes `failed_interrupted`.

The whole job has a 3,600-second wall deadline. A timeout before a valid
checkpoint fails the job; a timeout afterward completes the newest checkpoint
with outcome `budget_exhausted` and stop reason `job_time_budget`.

Operator cancellation follows the same checkpoint preference without changing
the public terminal status: after a checkpoint, the job is `cancelled` and the
API returns the newest valid sitemap with outcome `interrupted` and stop reason
`cancelled_after_checkpoint`; before a checkpoint, the cancelled job has no
sitemap. A server-shutdown task interruption remains active for startup
recovery and is not mislabeled as operator cancellation.

Chrome and every active Katana process group share a 2-GiB RSS budget. Crossing
it kills the registered process groups. Before the baseline this fails the job;
afterward the latest checkpoint completes with `partial_failure` and stop reason
`job_memory_budget`.

The jobs schema deliberately rejects an older table and asks the operator to
recreate the database. It does not migrate or backfill legacy rows.

## Verification

- Deterministic tests cover request validation, atomic baseline and partial
  writes, corrupt-partial fallback, cancelled checkpoint publication and API
  retrieval, cancellation-before-checkpoint behavior, restart at each checkpoint
  boundary, whole-job timeout, and shared process-memory cleanup.
- A real-server Site E regression completed with 13 entries, found its declared
  browser-only endpoint and request sequence, survived restart retrieval, and
  left no Chrome or Katana process behind.
- A real-server Site B job was cancelled as soon as its public status reached
  `discovering`. It returned status `cancelled`, a 13-entry baseline sitemap,
  outcome `interrupted`, and stop reason `cancelled_after_checkpoint`; after a
  full server shutdown and restart, `GET /jobs/{job_id}` returned the identical
  cancelled sitemap. Job ID `79c99000-841e-43f4-aeed-eb3cdd655897`; retained
  artifacts: `/tmp/crawler-cancel-checkpoint-rnitd6zw`.
- The complete 24-target real-server qualification retrieved every persisted
  job after a server restart with zero cross-job isolation violations.
