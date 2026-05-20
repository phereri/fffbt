# Retry and failure policy

- Status: draft (MVP)
- Owner: Queue / Scheduler Agent
- Migration: `supabase/migrations/20260520110000_retry_failure_policy.sql`
- Resolves: architecture.md open question #8 (Failure taxonomy)

## Overview

Defines what happens when a publishing job encounters an error. Each error
code maps to a category, a target job status, retry limits, and optional
account side effects. Workers call a single function
(`automation.process_job_error`) that encodes the entire policy.

## Error categories

### 1. Retryable

Infrastructure or transient failures. The scheduler automatically re-queues
the job up to `max_retries` times. If retries are exhausted the job stays
`failed` (terminal).

### 2. Needs review

Ambiguous failures requiring human or automated review. The job moves to
`needs_review`. A reviewer decides: re-queue (`needs_review → queued`),
confirm failure (`needs_review → failed`), or cancel
(`needs_review → cancelled`).

### 3. Non-retryable

Hard business failures indicating an account-level or platform-level
problem. The job moves to `failed` as a terminal state. No automatic retry.
Some errors trigger account side effects.

## Error catalog

| Error code | Category | Target status | Max retries | Account side effect | Description |
|---|---|---|---|---|---|
| `proxy_failed` | retryable | `failed` | 3 | — | Proxy connection or authentication error |
| `device_profile_failed` | retryable | `failed` | 2 | — | Device fingerprint injection failed |
| `gps_failed` | retryable | `failed` | 2 | — | MockGPS setup or injection failed |
| `login_required` | retryable | `failed` | 1 | — | Instagram session expired, re-login needed |
| `upload_failed` | retryable | `failed` | 3 | — | Instagram upload error (network/timeout) |
| `device_offline` | retryable | `failed` | 2 | — | Physical device unreachable via ADB |
| `captcha` | needs_review | `needs_review` | — | — | Captcha challenge detected |
| `verification_failed` | needs_review | `needs_review` | — | — | Could not confirm post was published |
| `unknown_screen` | needs_review | `needs_review` | — | — | Unrecognized Instagram UI state |
| `logged_out` | non_retryable | `failed` | 0 | → `disabled` | Instagram forced logout |
| `trial_reels_unavailable` | non_retryable | `failed` | 0 | — | Trial Reels feature not available for account |
| `suspended` | non_retryable | `failed` | 0 | → `suspended` | Instagram suspended the account |
| `checkpoint` | non_retryable | `failed` | 0 | → `disabled` | Instagram checkpoint/security verification |
| `two_factor` | non_retryable | `failed` | 0 | → `disabled` | 2FA challenge encountered |
| `action_blocked` | non_retryable | `failed` | 0 | — | Instagram action block (temporary) |
| `INFRA` | retryable | `failed` | 3 | — | Launcher infrastructure error (connection, OS) |
| `TIMEOUT` | retryable | `failed` | 2 | — | Job exceeded the launcher timeout |
| `UNKNOWN` | needs_review | `needs_review` | — | — | Unhandled exception in worker |
| `HEARTBEAT_TIMEOUT` | needs_review | `needs_review` | — | — | No heartbeat received within timeout window |

## Error code → stage mapping

Not every error can occur at every stage. This table shows where each error
is expected.

| Error code | `preparing_device` | `publishing` | `verifying` |
|---|---|---|---|
| `proxy_failed` | yes | — | — |
| `device_profile_failed` | yes | — | — |
| `gps_failed` | yes | — | — |
| `login_required` | yes | yes | — |
| `upload_failed` | — | yes | — |
| `device_offline` | yes | yes | yes |
| `captcha` | — | yes | — |
| `verification_failed` | — | — | yes |
| `unknown_screen` | — | yes | yes |
| `logged_out` | yes | yes | yes |
| `trial_reels_unavailable` | — | yes | — |
| `suspended` | yes | yes | yes |
| `checkpoint` | yes | yes | yes |
| `two_factor` | yes | yes | — |
| `action_blocked` | — | yes | — |
| `INFRA` | yes | yes | yes |
| `TIMEOUT` | yes | yes | yes |
| `UNKNOWN` | yes | yes | yes |
| `HEARTBEAT_TIMEOUT` | yes | yes | yes |

## Retry behavior

When a retryable error occurs and `retry_count < max_retries`:

1. Job transitions to `failed` (records error_code, error_message, finished_at).
2. `retry_count` increments.
3. Job immediately transitions back to `queued` (clears finished_at).
4. Both transitions are recorded in `job_events` as separate `status_changed`
   entries. The re-queue event payload includes `"retry": true`.
5. The scheduler picks up the re-queued job normally. A new device is assigned;
   the same video and account are reused.

When retries are exhausted (`retry_count >= max_retries`):

- Job stays `failed` (terminal).
- The video and account are released by the standard cleanup flow.

## Account side effects

Some non-retryable errors indicate the Instagram account itself is
compromised or blocked. The `process_job_error` function automatically
updates the account status:

| Error code | Account status set to | Effect |
|---|---|---|
| `logged_out` | `disabled` | Account excluded from scheduling until manually re-enabled |
| `suspended` | `suspended` | Account excluded from scheduling; may be permanent |
| `checkpoint` | `disabled` | Account needs manual checkpoint resolution |
| `two_factor` | `disabled` | Account needs 2FA resolution |

Errors without account side effects (`proxy_failed`, `device_profile_failed`,
`gps_failed`, `upload_failed`, `device_offline`, `captcha`,
`verification_failed`, `unknown_screen`, `login_required`,
`trial_reels_unavailable`, `action_blocked`) leave the account status
unchanged.

`action_blocked` does not disable the account because the block is temporary.
The scheduler's existing cooldown logic (post interval settings) provides
natural spacing. If action blocks become frequent, this should be revisited.

## Worker integration

Workers **must** use `automation.process_job_error()` for all error handling
instead of calling `transition_job_status()` directly:

```sql
SELECT automation.process_job_error(
    p_job_id       := '...',
    p_error_code   := 'proxy_failed',
    p_error_message := 'Connection refused: 192.168.1.100:8080'
);
```

Returns a JSONB result describing what happened:

```json
{"action": "retried", "retry_count": 1, "max_retries": 3}
{"action": "retries_exhausted", "retry_count": 3, "max_retries": 3}
{"action": "needs_review"}
{"action": "terminal_failure"}
```

The worker does not need to know the retry policy — the function encodes it.

## Global settings

| Key | Default | Description |
|---|---|---|
| `max_retries_default` | `3` | Fallback max retries if error code not in catalog |

Per-error-code limits are stored in `automation.error_catalog` and take
precedence over the global default.

## Concurrency

`process_job_error` acquires a `FOR UPDATE` lock on the job row, making it
safe for concurrent workers. The function delegates to `transition_job_status`
for state changes, preserving the existing audit trail.

## Future considerations

- **Backoff**: Currently retries are immediate (re-queued). Consider adding
  a `retry_delay_seconds` per error code if instant retries cause repeated
  transient failures.
- **Circuit breaker**: If multiple accounts hit the same error in a short
  window, consider pausing the scheduler globally.
- **`action_blocked` cooldown**: May need a dedicated cooldown duration
  longer than the standard post interval.
