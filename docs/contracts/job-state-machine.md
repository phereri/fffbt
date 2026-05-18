# Job state machine

- Status: accepted (MVP)
- Owner: Queue / Scheduler Agent
- Migration: `supabase/migrations/20260518080000_job_state_machine.sql`

## States

| State | Description |
|---|---|
| `queued` | Job created, waiting for a free device to start. |
| `preparing_device` | Physical device is being provisioned with the account environment. |
| `publishing` | Device is executing the Instagram posting flow. |
| `verifying` | Post published; device is verifying the result. Device stays reserved. |
| `done` | Verification passed. Terminal state. |
| `failed` | Unrecoverable error at any stage. Terminal (retryable via re-queue). |
| `needs_review` | Ambiguous result requiring human or automated review. |
| `cancelled` | Job was cancelled before completion. Terminal. |

## Allowed transitions

```
queued ──────────────► preparing_device
  │                        │
  ├──► cancelled           ├──► publishing
  └──► failed              │       │
                           │       ├──► verifying
                           │       │       │
                           │       │       ├──► done
                           │       │       ├──► failed
                           │       │       └──► needs_review
                           │       │
                           │       ├──► failed
                           │       └──► needs_review
                           │
                           ├──► failed
                           ├──► needs_review
                           └──► cancelled

needs_review ──► queued        (retry after review)
needs_review ──► cancelled     (abandon after review)
needs_review ──► failed        (confirmed failure)

failed ──► queued              (retry)
```

### Transition table

| From | To |
|---|---|
| `queued` | `preparing_device`, `failed`, `cancelled` |
| `preparing_device` | `publishing`, `failed`, `needs_review`, `cancelled` |
| `publishing` | `verifying`, `failed`, `needs_review` |
| `verifying` | `done`, `failed`, `needs_review` |
| `needs_review` | `queued`, `cancelled`, `failed` |
| `failed` | `queued` |
| `done` | _(none — terminal)_ |
| `cancelled` | _(none — terminal)_ |

## Side effects

The `automation.transition_job_status` function enforces these rules:

1. **Transition validation.** Only transitions listed above are allowed;
   anything else raises an exception.
2. **Audit log.** Every transition inserts a `status_changed` row into
   `automation.job_events` with `from_status`, `to_status`, and an optional
   `payload`.
3. **Timestamps.**
   - `started_at` is set when the job first leaves `queued`.
   - `finished_at` is set when the job enters a terminal state (`done`,
     `failed`, `cancelled`).
4. **Error fields.** When transitioning to `failed`, callers should pass
   `error_code` and `error_message` inside the `payload` JSONB. The function
   copies them to `jobs.error_code` and `jobs.error_message`.

## Verification model

Verification option A (ADR 0001): the physical device remains reserved (`busy`)
throughout `verifying`. The device is released only after the job reaches
`done`, `failed`, or `needs_review`. Device release is the responsibility of
the worker/verification flow, not this state machine.

## Concurrency

The function uses `SELECT ... FOR UPDATE` on the job row, making it safe for
concurrent scheduler and worker processes. Callers should keep the enclosing
transaction short.
