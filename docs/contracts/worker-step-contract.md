# Worker step contract — `StepResult` / `WorkerStep`

- Status: draft (MVP)
- Owner: Architect / Tech Lead
- Related: [`job-state-machine.md`](./job-state-machine.md),
  [`instagram-worker-state-machine.md`](./instagram-worker-state-machine.md),
  [`account-environment-application.md`](./account-environment-application.md),
  [`retry-failure-policy.md`](./retry-failure-policy.md)
- Reference: ADR [`0002-mobilerun-first-worker.md`](../decisions/0002-mobilerun-first-worker.md)

## Purpose

Every publishing job is executed as a fixed, ordered sequence of **runtime
steps**. This document defines the uniform boundary between the orchestrator
(launcher / worker driver) and each step:

- `WorkerStep` — the interface every step implements.
- `StepResult` — the structured value every step returns.

The contract is **backend-agnostic on purpose**. The `mobile_ui_automation`
and `verification` steps are implemented by the `MobileWorker` (ADR 0002,
FFF-28); whether the adapter behind it is Mobilerun (MVP) or a future Appium
fallback, it returns the same `StepResult`. The orchestrator never sees
Mobilerun- or Appium-specific shapes.

This document defines the *boundary*, not the *internals*. How a step does its
work is owned by that step's own contract (see *Runtime steps* below).

## Runtime steps

A job is exactly these five steps, always in this order:

| # | Step (`step` value) | Job state | Internal flow owned by |
|---|---|---|---|
| 1 | `environment_apply` | `preparing_device` | [`account-environment-application.md`](./account-environment-application.md) |
| 2 | `video_preparation` | `publishing` (entry) | [`instagram-worker-state-machine.md`](./instagram-worker-state-machine.md) — `prepare_video_for_android`, `push_video_to_gallery` |
| 3 | `mobile_ui_automation` | `publishing` | [`instagram-worker-state-machine.md`](./instagram-worker-state-machine.md) — `open_instagram` … `share_and_confirm` |
| 4 | `verification` | `verifying` | [`instagram-worker-state-machine.md`](./instagram-worker-state-machine.md) — `optional_capture_post_url`, `verification` |
| 5 | `cleanup` | _(runs on every job exit)_ | [`retry-failure-policy.md`](./retry-failure-policy.md) — terminal resource cleanup |

The runtime steps are the **orchestration-level** decomposition of a job. Each
step is internally implemented however its owner contract specifies — the
Instagram worker state machine, for example, is the fine-grained internal flow
of steps 2–4. The `StepResult` is the only thing the orchestrator consumes.

## `WorkerStep` interface

```
interface WorkerStep:
    name: StepName            # one of the five `step` values above
    run(ctx: StepContext) -> StepResult
```

### `StepContext` (input)

Read-only. The orchestrator builds it from the `automation.jobs` row.

| Field | Type | Meaning |
|---|---|---|
| `job_id` | uuid | `automation.jobs.id`. |
| `video_id` | uuid | `automation.videos.id`. |
| `account_id` | uuid | `automation.accounts.id`. |
| `account_environment_id` | uuid | `automation.account_environments.id` — the identity bundle (invariant I3). |
| `device_id` | uuid | `automation.physical_devices.id` of the reserved phone. |
| `mode` | enum | `proof_of_posting` \| `mvp` \| `production`. Consumed by `environment_apply`; passed to every step so results are self-describing. |
| `settings` | object | Resolved timeouts / limits from `automation.global_settings`. No hard-coded timeouts inside a step. |

### Behavioural contract

Every `WorkerStep` implementation **must**:

1. **Be idempotent / re-runnable.** A step may be invoked again on the same job
   after a retryable failure. It re-checks current state and skips work already
   satisfied (reporting `skipped`). Re-running a step must never double-post or
   corrupt state.
2. **Be single-device scoped.** Every command targets only `ctx.device_id`.
   A step never enumerates or touches other phones.
3. **Be bounded.** A step runs under a timeout from `ctx.settings`. On timeout
   it returns `status = "failed"`, `code = "TIMEOUT"`.
4. **Emit progress as `job_events`.** Per-step progress is written as
   `heartbeat` rows on `automation.job_events` with the `step` name, `mode`,
   and step-specific payload. The step does **not** write `status_changed` or
   `error` rows — those belong to the orchestrator's transition functions.
5. **Persist artifacts.** Screenshots, logs, and trajectories are inserted into
   `automation.artifacts` and referenced from `StepResult.artifacts`.
6. **Not interpret retry policy.** A step reports *what happened* (`status` +
   `code`); it never decides whether the job retries, never calls
   `transition_job_status()` or `process_job_error()` itself. The orchestrator
   owns routing (see *Orchestration*).
7. **Return a `StepResult` for every outcome** — success, failure, or skip.
   A step must not raise an uncaught exception across the boundary; an
   unhandled error is caught and returned as `status = "failed"`,
   `code = "UNKNOWN"`.

## `StepResult` (output)

The structured value every step returns. JSON shape:

```json
{
  "step": "mobile_ui_automation",
  "status": "failed",
  "code": "share_did_not_register",
  "message": "tap_share_and_confirm completed but share_button still present after 3 checks",
  "retryable": false,
  "warnings": [
    { "code": "post_url_capture_skipped", "step": "verification", "detail": "clipboard read returned empty" }
  ],
  "artifacts": [
    { "artifact_id": "…", "artifact_type": "screenshot", "label": "on_error" },
    { "artifact_id": "…", "artifact_type": "trajectory", "label": "mobilerun_run" }
  ],
  "details": null
}
```

### Fields

| Field | Type | Required | Meaning |
|---|---|---|---|
| `step` | enum | yes | Which runtime step produced this result (the five values above). |
| `status` | enum | yes | `ok` \| `skipped` \| `failed` \| `needs_review`. See below. |
| `code` | string \| null | conditional | Machine code. **Mandatory when `status` is `failed` or `needs_review`** — must be an `automation.error_catalog` code. `null` on `ok`. Optional reason code on `skipped`. |
| `message` | string | yes | Human-readable detail for logs and review. Never contains secrets (no proxy credentials, no tokens). |
| `warnings` | array | yes | List of non-blocking warnings (possibly empty). Each: `{ code, step, detail }`. Warnings never change `status`. |
| `artifacts` | array | yes | List of artifact references (possibly empty). Each: `{ artifact_id, artifact_type, label }`, pointing at an `automation.artifacts` row. |
| `retryable` | bool \| null | optional | Advisory hint only — see *The `retryable` flag*. `null`/absent when the step has no strong local signal. |
| `details` | object \| null | optional | Step-specific structured payload. `null` for most steps; `environment_apply` uses it to carry its per-sub-step breakdown (see *Reconciliation*). |

### `status` values

| Value | Meaning | Effect on the job |
|---|---|---|
| `ok` | Step completed successfully (or was already satisfied and produced its expected outcome). | Job advances to the next step. |
| `skipped` | Step intentionally not run — already satisfied on an idempotent re-run, or `deferred`/`forbidden` for the active `mode`. `message` states why. | Job advances to the next step. |
| `failed` | Step did not achieve its outcome. `code` is set. | Job stops; `code` routed through `process_job_error()`. |
| `needs_review` | Step positively recognized an ambiguous state that needs a human / automated reviewer. `code` is set. | Job stops; `code` routed through `process_job_error()`. |

`status` describes the **step's own observation** at face value. It is *not*
the job's final disposition — that is always decided by `process_job_error()`
from `code`. A `failed` step carrying a non-retryable code (e.g. `logged_out`)
still ends the job terminally; a `failed` step carrying a retryable code
(e.g. `device_offline`) gets re-queued. The step does not need to know which.

### The `retryable` flag

`retryable` is **advisory only**. The authoritative retry decision is
`automation.error_catalog` via `process_job_error()` — see
[`retry-failure-policy.md`](./retry-failure-policy.md). The orchestrator
**must not** branch on `retryable`.

It exists so a step can surface a strong *local* signal it already has (e.g.
"this ADB call timed out, which I know is transient") into logs and review,
without that signal silently overriding catalog policy. When a step has no
such signal it leaves `retryable` `null`. If `retryable` and the catalog
category ever disagree, the catalog wins.

### `warnings`

A warning is a non-blocking issue: something a best-effort sub-step could not
do, or a degraded-but-acceptable condition. Warnings are recorded for
observability and **never** change `status` — a step with warnings can still
return `ok`. The job-level warning list is the union of all steps' warnings.
Each warning is also written to `automation.job_events` as a `heartbeat` row so
there is a durable record behind the in-memory list.

Examples: `proxy_deferred` (proxy not applied on a best-effort env run),
`post_url_capture_skipped` (best-effort URL capture hit friction).

### `artifacts`

Each entry references a row already inserted into `automation.artifacts`
(`artifact_type IN ('screenshot','log','video_thumbnail','trajectory','gif','other')`).
The step inserts the row and the file (or its URL), then lists the reference
here so the orchestrator and dashboard can find evidence per step. A Mobilerun
run's trajectory is an artifact with `artifact_type = 'trajectory'`; an Appium
adapter would attach screenshots/logs the same way — the orchestrator sees no
difference.

## Orchestration

The orchestrator (launcher / worker driver) runs the steps and owns all job
state transitions:

1. **Forward sequence.** Run steps 1→4 in order. Before each step it transitions
   the job into that step's state (`transition_job_status()`); after each `ok`
   it advances.
2. **Stop on failure.** When a step returns `failed` or `needs_review`, the
   forward sequence stops. Steps not reached are treated as `skipped` (the
   orchestrator may synthesize `skipped` `StepResult`s for the audit record).
3. **`cleanup` always runs.** Step 5 runs on every job exit — success *or*
   failure. It releases the reserved physical device and finalizes resources.
   It is best-effort and idempotent; a `cleanup` failure is logged but never
   blocks (the device release in `process_job_error()` is also idempotent, so a
   missed `cleanup` is recovered).
4. **Route the outcome.** After `cleanup`:
   - all steps `ok`/`skipped` → transition the job to `done`.
   - a step `failed`/`needs_review` → call
     `automation.process_job_error(job_id, code, message)` with the failing
     step's `code` and `message`. That function encodes category, retry limits,
     account side effects, and terminal cleanup.
5. **Aggregate.** The job-level result is the failing `StepResult` (or the last
   `ok` one on success), plus the union of every step's `warnings`.

The orchestrator never re-implements retry policy and never reads step
internals — it consumes `status` + `code` and delegates.

## Reconciliation with existing contracts

This contract generalizes shapes that already exist in two sibling documents.
They are consistent; the mapping is:

- **`environment_apply` ↔ `ApplyResult`.** `account-environment-application.md`
  defines `ApplyResult` for the environment step. It **is** the `StepResult`
  for `step = "environment_apply"`, with this field mapping:

  | `ApplyResult` | `StepResult` |
  |---|---|
  | `status: ready` | `status: ok` |
  | `status: needs_review` | `status: needs_review` |
  | `status: failed` | `status: failed` |
  | `error_code` | `code` |
  | `error_message` | `message` |
  | `warnings` | `warnings` |
  | `steps[]` (per-sub-step breakdown) | `details.steps` |

  The inner per-sub-step result in that document (with statuses
  `ok`/`skipped`/`unverified`/`deferred`/`failed`) is a **sub-step** detail of
  the single `environment_apply` runtime step — it is not a top-level
  `StepResult`. `unverified` and `deferred` are best-effort sub-step states; at
  the runtime-step level a deferred sub-step surfaces as a `warning` and the
  step still returns `ok`.

  > Follow-up: `account-environment-application.md` currently names its
  > sub-step type `StepResult`, which collides with the top-level type defined
  > here. Renaming it (e.g. `EnvSubStepResult`) is a small documentation
  > follow-up, tracked separately — it does not change behaviour.

- **`mobile_ui_automation` / `verification` ↔ Instagram worker state machine.**
  `instagram-worker-state-machine.md` defines the fine-grained states inside
  steps 2–4. Its terminal exit signals map to `StepResult.status`:
  `FAILED → failed`, `NEEDS_REVIEW → needs_review`, normal completion → `ok`.
  The worker's `update_job_result` state is the moment the step returns its
  `StepResult` to the orchestrator. The worker-level error→code table in that
  document is the `code` source for these steps.

## Error and warning codes

`StepResult.code` values are `automation.error_catalog` codes — this contract
introduces **no new codes**. The catalog is the single source of truth for
category, target status, and retry limits
([`retry-failure-policy.md`](./retry-failure-policy.md)). Codes proposed but
not yet in the catalog (`caption_mismatch`, `share_did_not_register`,
`environment_failed`, `account_mismatch`, …) are tracked by their originating
contracts and must be added by the migration those contracts call for before a
step emits them.

Warning codes (e.g. `proxy_deferred`, `post_url_capture_skipped`) are **not**
error codes — they never reach `process_job_error()` and are not stored in the
error catalog. They live only in `StepResult.warnings` and `job_events`.

## Open questions

1. **Synthesized `skipped` results.** Should the orchestrator persist
   synthetic `skipped` `StepResult`s for steps a failure prevented from
   running, or is the absence of a `heartbeat` for that step enough? Affects
   only the audit record, not control flow. *Assumption: persist them, for a
   complete per-job step record.*
2. **`cleanup` on success vs. `process_job_error()`.** On a terminal `failed`/
   `needs_review` job, `process_job_error()` already releases the device
   (idempotently). On a `done` job there is no such function — the `cleanup`
   step is the only releaser. The division is intentional but should be
   re-confirmed when the launcher contract is written (FFF launcher issue).
3. **Per-step timeout values.** Concrete timeouts belong in
   `automation.global_settings`, not this contract. The key names are defined
   by the launcher contract.
