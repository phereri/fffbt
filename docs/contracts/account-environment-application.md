# Account environment application contract

- Status: draft (MVP)
- Owner: Device Environment Agent
- Related: [`job-state-machine.md`](./job-state-machine.md),
  [`retry-failure-policy.md`](./retry-failure-policy.md),
  [`environment.md`](./environment.md)
- Research inputs: [`../research/genfarmer-api.md`](../research/genfarmer-api.md),
  [`../research/genfarmer-changeinfo-hazard.md`](../research/genfarmer-changeinfo-hazard.md),
  [`../research/genrouter-operator-checklist.md`](../research/genrouter-operator-checklist.md)

## Purpose

This document is the contract for `apply_account_environment` — the operation
that prepares a free physical Android phone with a selected account's
environment so a publishing job can post a Reels Trial video.

It defines:

1. the operation signature and its inputs/outputs;
2. the five ordered preparation steps and the MVP scope tier of each
   (MVP-required, best-effort, production-required, or deferred);
3. the pre-publish verification the worker performs before handing off to the
   Poster;
4. the output status values and error codes.

Device preparation only. This operation does not select accounts/videos,
create jobs, drive the Instagram UI, or verify publication.

## Where it runs

`apply_account_environment` is the body of the `preparing_device` job state
(see [`job-state-machine.md`](./job-state-machine.md)). The launcher calls it
after a free device has been reserved and an account chosen. On success the
job advances to `publishing`; otherwise it goes to `failed` or `needs_review`
via `automation.process_job_error()`.

```
queued ──► preparing_device ──[apply_account_environment]──► publishing
                   │                                            │
                   ├── failed (retryable)                        │
                   └── needs_review (ambiguous)                  ...
```

## Signature

```
apply_account_environment(device_id, account_environment_id) -> ApplyResult
```

### Inputs

| Param | Type | Meaning |
|---|---|---|
| `device_id` | uuid | `automation.physical_devices.id` of the reserved phone. Must be the phone already assigned to the job. |
| `account_environment_id` | uuid | `automation.account_environments.id`. Resolves to the full identity bundle. |

`account_environment_id` resolves (one-to-one, per invariant I3) to:

- `account` — `automation.accounts` row; `account.username` is the
  **expected username**.
- `proxy` — `automation.proxies` row.
- `device_profile` — `automation.device_profiles` row.
- `gps_location` — `automation.gps_locations` row.
- `app_state` — `automation.app_states` row (may be `expired`/`invalid`).

### Preconditions

The caller (launcher) must guarantee these before calling. They are *not*
re-checked as recoverable errors — a violation is a caller bug.

- The device row exists and `status = 'busy'` with `current_job_id` set to
  this job.
- The account environment exists and all five bundle members are present.
- A `jobs` row exists in state `preparing_device`.

### Scope and safety

- Every command is scoped to the single `device_id`. Never enumerate or touch
  other phones.
- Read-only checks run before any state-changing step.
- The operation **never** wipes, factory-resets, or clears device data. It
  never uses GenFarmer `ChangeDevice mode=reset` or `clearData=true`.
- Proxy credentials are never logged. Applied device-profile metadata *is*
  logged (non-secret).
- GenFarmer / GenRouter APIs are reached **locally on the GenFarmer host
  only** — `127.0.0.1:55554` has no auth and must not be exposed over
  Tailscale (research input, FFF-18).

### Idempotency

`apply_account_environment` is safe to call again on the same job after a
retryable failure. Each step re-checks current device state before acting and
skips a step whose target state is already satisfied (e.g. proxy already set
to the expected value). A step that already succeeded is reported `skipped`
on re-run.

## MVP scope — step tiers

The five steps below describe the **full production** operation. The first
MVP launch (Instagram Reels Trial posting) intentionally does not enforce all
of them. Every step carries one of four tiers:

| Tier | Meaning |
|---|---|
| **MVP-required** | Must run and pass for the job to reach `publishing`. A failure blocks posting. |
| **Best-effort (MVP)** | Attempted when possible; failure or unavailability is recorded as a warning/event and the job continues. Does not block posting on its own. |
| **Production-required** | Needed for production hardening but deferred for the first MVP launch — not enforced until the referenced work lands. |
| **Deferred / blocked** | Cannot be implemented yet — blocked on an open unknown (see Open questions). |

| Step | MVP tier | Behaviour for the MVP launch |
|---|---|---|
| 1 — apply device profile | Production-required (deferred) | Not enforced. The ChangeInfo apply is hazardous — one-way on Tailscale-only phones (FFF-18) — and its implementation is blocked on the unknown GenFarmer Task JSON shape. For MVP the phone posts with the fingerprint it already has. Reported `deferred`. |
| 2 — apply proxy | Best-effort | Applied when proxy config is available. If config is missing or the apply fails, a `proxy_deferred` warning/event is recorded and the job continues — **unless** the account environment is flagged `proxy_required`, which makes a `proxy_failed` blocking. Production hardening tracked in FFF-22 / FFF-23. |
| 3 — apply GPS | Best-effort | Applied / checked via MockGPS when available. If MockGPS is unavailable the check is `unverified`; it does not block. |
| 4 — restore / check app state | Verify: MVP-required · Restore: production-required (deferred) | The MVP gate is **verifying** an assigned account/session where possible. Full `BackupRestoreV2` cross-phone restore is deferred — blocked on unverified cross-phone restore semantics (FFF-18). |
| 5 — verify environment | MVP-required | Device-online verification and the Instagram account-identity check gate the transition to `publishing`. |

**Required MVP environment flow** (device-preparation scope only):

1. verify the physical device is online (ADB reachable);
2. verify the assigned account / session where possible;
3. apply / check GPS via MockGPS if available;
4. record `job_events` and artifacts for every step.

Selecting and publishing the Trial Reel itself is the Poster's responsibility
and is out of scope for this contract.

Because Steps 1, 2 and 4 all run as nodes of a GenFarmer automation Task —
and that Task's JSON shape is still an open unknown (FFF-18) — the practical
MVP path applies none of them: device profile and full restore are deferred,
and proxy is recorded `proxy_deferred`. MVP device preparation therefore
reduces to *verify device online → verify session where possible → apply GPS
if available → record events*. Resolving the GenFarmer Task JSON shape blocks
production hardening, not the MVP launch.

## Preparation steps

The operation runs five ordered steps. Each step produces a `StepResult`
(see Output). A failure in an **MVP-required** step stops the operation —
later steps are reported `skipped`. A **best-effort** step that fails or
cannot run does **not** stop the operation: it is reported `deferred`, a
warning is recorded, and the next step runs (see *MVP scope — step tiers*).

> Implementation note: steps 1, 2 and 4 are GenFarmer script-DSL nodes, not
> standalone REST calls. In practice they are submitted as one GenFarmer
> automation Task (`POST /automation/tasks` + `POST /automation/runs`) and
> their results polled via `GET /automation/runs/:id/storages` (research
> input, FFF-18). This contract still defines them as **discrete logical
> steps** with discrete results so failures map to specific error codes.

### Step order rationale

1. **Device profile** first — it is the most disruptive change and a
   connectivity barrier (see hazard below). Nothing else should run until the
   phone is confirmed reachable afterwards.
2. **Proxy** before any Instagram traffic — when proxy is applied it belongs
   to the account one-to-one and must be active before the session is touched.
   For the MVP launch proxy is best-effort and may be skipped (see *MVP scope
   — step tiers*).
3. **GPS** after proxy — the GPS location must correspond to the proxy's
   location.
4. **App/session state** last among the mutating steps — restored only once
   network identity is in place.
5. **Verify** confirms the whole bundle.

### Step 1 — apply device profile

*MVP tier: production-required — deferred for the MVP launch.*

- **Input**: `device_profile`.
- **Action**: apply the fingerprint via GenFarmer (`ChangeDevice` with
  `changeInfo` sub-mode). Build props, model/brand, locale, timezone, screen
  metrics come from the profile row.
- **Success criterion**: GenFarmer reports the profile applied; visible
  device properties (`ro.product.model`, locale, timezone) match the profile
  where readable via ADB.
- **Hazard barrier** (research input, FFF-18,
  `genfarmer-changeinfo-hazard.md`): ChangeInfo has been observed to drop both
  `adbd` (`:5555`) and `atx-agent` (`:7912`) on Tailscale-only phones with no
  proven remote recovery. After this step the operation **must** re-confirm
  reachability on `:5555` and `:7912` within a timeout before continuing. If
  either port is unreachable → stop with `device_unreachable_after_change_info`
  (`needs_review`). No blind retries.
- **Pre-flight**: capture `serial_no` + `android_id` before the step so an
  after-diff is possible.
- **Errors**: `device_profile_failed`, `device_unreachable_after_change_info`,
  `device_offline`.

### Step 2 — apply proxy

*MVP tier: best-effort. Proxy setup is **not** a hard blocker for the first
MVP launch.*

- **Input**: `proxy`.
- **Action**: apply the proxy through GenRouter for this device. GenRouter
  expects **colon-delimited** form, not a URL:
  `socks5://host:port:user:pass` (or `http(s)://…`) (research input, FFF-18).
- **Success criterion**: the device's effective external IP, checked through
  the device, resolves to the proxy egress (country code should match
  `proxy.country_code` when known).
- **MVP behaviour (best-effort)**: if proxy configuration is unavailable, or
  the apply or its verification fails, the step does **not** stop the
  operation. It reports `StepResult.status = "deferred"`, adds a
  `proxy_deferred` entry to `ApplyResult.warnings`, writes a `job_events` row
  with the reason, and the operation continues to Step 3. The job may still
  reach `publishing`.
- **Exception — `proxy_required` accounts**: if the account environment is
  flagged `proxy_required`, the proxy is mandatory for that account. A missing
  config or a failed apply is then a hard `proxy_failed` that stops the
  operation, exactly as in the production tier. `proxy_required` is a boolean
  that does **not** exist in the schema yet — a follow-up migration must add
  `automation.account_environments.proxy_required` (default `false`); until it
  lands, MVP treats every account as proxy-optional (see Open questions).
- **Production tier**: production hardening (FFF-22 / FFF-23) makes proxy
  required for every account, where a `proxy_failed` is always blocking.
- **Errors**: `proxy_failed` (blocking only for `proxy_required` accounts —
  otherwise downgraded to the `proxy_deferred` warning), `device_offline`.

### Step 3 — apply GPS

*MVP tier: best-effort.*

- **Input**: `gps_location`.
- **Action**: set the mock location via MockGPS. Prefer a direct
  intent/API; fall back to Appium/UI automation **on this device only** if no
  direct API exists.
- **Success criterion**: the device's reported location is within
  `accuracy_meters` of `(latitude, longitude)`; the location is consistent
  with the proxy country.
- **Errors**: `gps_failed`, `device_offline`.

### Step 4 — restore / check app state

*MVP tier: verifying the account/session is MVP-required; full backup-restore
is production-required and deferred.*

- **Input**: `app_state`, `account`.
- **Action**: restore the Instagram app/session state for the account onto
  the device. When a GenFarmer backup exists, this is a `BackupRestoreV2`
  (`mode=restore`, `withChangeInfo` per profile policy) node. If no usable
  backup or session exists, the worker falls back to a fresh login from
  `account` credentials during `publishing`.
- **Success criterion**: the Instagram app is installed and a session for the
  account is present.
- **Notes**:
  - `app_state.status` of `expired` or `invalid` is **not** a step failure
    here — it is a signal that re-login will be required; record it and
    continue.
  - A restore that fails because no change-info exists on the host
    (`"This device has no change info"`, observed — research input FFF-18)
    yields `app_state_missing`, not a hard crash.
- **Errors**: `app_state_missing`, `login_required`, `logged_out`,
  `device_offline`.

### Step 5 — verify environment

*MVP tier: MVP-required.*

Confirmation of the whole bundle, check by check. Each check that *can* run
does run; a check that cannot be performed is reported `unverified`, not
failed.

- **Device profile**: visible model/locale/timezone match the profile.
- **Proxy**: external IP egress matches the proxy. Skipped (not `unverified`)
  when proxy was deferred for the MVP tier.
- **GPS**: reported location matches `gps_location`.
- **Reachability**: `:5555` and `:7912` still respond.
- **Instagram account** (the pre-publish identity check, see next section):
  the account logged into the app matches `account.username`.
- **Errors**: `account_mismatch`, plus any of the per-resource codes above if
  verification reveals the applied state regressed.

## Pre-publish verification — Instagram account identity

Before the Poster begins publishing, the worker must confirm the correct
Instagram account is active on the device. This is part of Step 5 and is the
last gate before `publishing`.

Two checks, both **best-effort**:

1. **Correct account logged in** — the Instagram app has an authenticated
   session (not logged out, not on a login screen).
2. **Active username matches `expected_username`** — the username of the
   currently-active profile equals `account.username`.

`expected_username` is `automation.accounts.username` for the account behind
`account_environment_id`.

Outcomes:

| Observation | Result |
|---|---|
| Logged in **and** active username == `expected_username` | environment `ready` — job may proceed to `publishing` |
| Logged in but active username **!=** `expected_username` | stop with `account_mismatch` → `needs_review` |
| Not logged in / logged-out screen | `login_required` (retryable; re-login attempted) or `logged_out` (non-retryable) per what is observed |
| Username cannot be read at all (UI not reachable, ambiguous screen) | `unverified` for this check — does **not** by itself block; recorded for review. The job still proceeds, and the Poster re-checks identity in-app before posting. |

"Best-effort" means: a wrong account is always a hard stop
(`account_mismatch`), but inability to *read* the username is not — it is
logged and deferred to the Poster's own in-app check. This avoids burning a
job on a flaky screen read while still guaranteeing we never post from the
wrong identity once it is positively identified.

## Output — `ApplyResult`

`apply_account_environment` returns a structured result. Shape:

```json
{
  "status": "ready",
  "device_id": "…",
  "account_environment_id": "…",
  "expected_username": "…",
  "error_code": null,
  "error_message": null,
  "warnings": [
    { "code": "proxy_deferred", "step": "proxy",
      "detail": "no proxy config available; continued without proxy (MVP)" }
  ],
  "steps": [
    { "step": "device_profile", "status": "deferred",   "detail": "MVP: device profile not enforced" },
    { "step": "proxy",          "status": "deferred",   "detail": "MVP best-effort: proxy not applied" },
    { "step": "gps",            "status": "ok",         "detail": "lat/lon within 8m" },
    { "step": "app_state",      "status": "ok",         "detail": "session verified" },
    { "step": "verify",         "status": "ok",         "detail": "username matched" }
  ]
}
```

The example above is a typical **MVP-tier** run: device profile and proxy are
`deferred`, and the job still reaches `ready`. A full production run reports
every step `ok` with an empty `warnings` list.

`warnings` is a (possibly empty) list of non-blocking issues — each is a
best-effort step that was deferred or could not be verified. Warnings never
change the top-level `status`; they exist for observability and review.

### Top-level `status`

| Value | Meaning | Job transition |
|---|---|---|
| `ready` | All MVP-required steps passed and the environment is verified. Best-effort steps may be `deferred`; any deferral is listed in `warnings`. | `preparing_device → publishing` |
| `needs_review` | An ambiguous failure; human/automated review required. | `preparing_device → needs_review` |
| `failed` | A recoverable infrastructure failure; retry policy decides. | `preparing_device → failed` (may re-queue) |

The caller does **not** interpret `error_code` itself — it passes
`error_code` + `error_message` to `automation.process_job_error()`, which
encodes category, retry limits, and account side effects
(see [`retry-failure-policy.md`](./retry-failure-policy.md)).

### `StepResult.status`

| Value | Meaning |
|---|---|
| `ok` | Step applied and (where checkable) verified. |
| `skipped` | Step not run — either already satisfied (idempotent re-run) or a prior step failed. |
| `unverified` | Step applied but its effect could not be confirmed (best-effort check unavailable). |
| `deferred` | Step intentionally not enforced for the MVP launch (best-effort or production-required tier), or it failed without blocking. Recorded in `warnings` and `job_events`; does not stop the operation. |
| `failed` | Step failed; `error_code` on the result is set. |

## Error codes

All publishing-stage error handling goes through `process_job_error()`.
Codes already in `automation.error_catalog` are reused as-is; the catalog is
the source of truth for category and retry limits.

### Reused from the error catalog

| Error code | Category | Step | Notes |
|---|---|---|---|
| `device_profile_failed` | retryable (max 2) | 1 | Fingerprint injection failed. |
| `device_offline` | retryable (max 2) | 1–5 | Phone unreachable via ADB at any step. |
| `proxy_failed` | retryable (max 3) | 2 | Proxy connection / auth error. MVP: blocking only when the account environment is `proxy_required`; otherwise downgraded to a non-blocking `proxy_deferred` warning (see Step 2). |
| `gps_failed` | retryable (max 2) | 3 | MockGPS setup / injection failed. |
| `login_required` | retryable (max 1) | 4, 5 | Session expired; re-login needed. |
| `logged_out` | non-retryable | 4, 5 | Instagram forced logout; account → `disabled`. |

### Proposed new codes

These are specific to environment application and are **not yet** in the
catalog. They must be added via a migration that extends
`automation.error_catalog` before workers emit them (follow-up issue). Until
then, treat them as the listed fallbacks.

| Error code | Proposed category | Step | Fallback today | Description |
|---|---|---|---|---|
| `device_unreachable_after_change_info` | needs_review | 1 | emit `device_offline` | After ChangeInfo, `:5555`/`:7912` did not recover within timeout. One-way on Tailscale-only phones — no blind retry. |
| `app_state_missing` | needs_review | 4 | emit `login_required` | No usable session/backup to restore (e.g. host has no change-info). Worker may still fresh-login, but flag for review. |
| `account_mismatch` | needs_review | 5 | emit `unknown_screen` | Active Instagram username != `expected_username`. Never publish. |

### Non-blocking warning codes

These are **not** error codes — they never reach `process_job_error()` and do
not change the job status. They are recorded in `ApplyResult.warnings` and in
`job_events` for observability.

| Warning code | Step | Description |
|---|---|---|
| `proxy_deferred` | 2 | Proxy was not applied for an MVP best-effort run — config unavailable or apply failed on a non-`proxy_required` account. The job continues. |

Rationale for `needs_review` on all three error codes above: each indicates
the prepared
environment cannot be trusted for *this* account, but none is a clean
"retry the same thing" — they need a human or an automated reviewer to
decide (re-queue, re-assign device, or abandon).

## Events

`apply_account_environment` writes to the existing audit logs; it does not
own a new table.

- **`automation.job_events`** — one `status_changed` row for the
  `preparing_device` entry/exit transitions (written by
  `transition_job_status`). On failure, `process_job_error()` writes the
  `error` row. Per-step progress is recorded as `heartbeat` events with the
  step name and `StepResult` in `payload`. A best-effort step that is deferred
  (e.g. proxy under the MVP tier) is recorded as a `heartbeat` event whose
  `payload` carries the warning code (`proxy_deferred`) and the reason — this
  is the durable record behind `ApplyResult.warnings`.
- **`automation.device_events`** — `job_assigned` when the device is reserved
  (caller), `error` for a device-level failure during apply, `job_released`
  on terminal cleanup (written by `process_job_error()`).

Applied device-profile metadata (model, locale, timezone, applied build
props) is logged in the Step 1 `heartbeat` payload. Proxy host/port may be
logged; proxy **credentials** must not.

## Open questions

1. **GenFarmer Task JSON shape.** The exact `tasks.config` schema for
   programmatic submission is still an unknown (FFF-18). Steps 1/2/4 cannot be
   implemented until it is captured. Blocks implementation, not this contract.
2. **Cross-phone restore semantics.** `BackupRestoreV2 withChangeInfo=true`
   moving a session between phones is unverified in sandbox (FFF-18). Until
   confirmed, Step 4 should not assume a backup taken on phone A restores
   cleanly onto phone B.
3. **ChangeInfo recovery.** Is there *any* remote path to re-arm ADB-over-TCP
   after ChangeInfo drops it? If yes, `device_unreachable_after_change_info`
   could become retryable.
4. **Verify timeout values.** Concrete timeouts for the post-ChangeInfo
   reachability wait and for each verification check are left to the
   implementation issue; they belong in global settings, not hard-coded.
5. **`proxy_required` flag.** Per-account proxy enforcement needs a boolean
   that does not exist yet — proposed `automation.account_environments.proxy_required`
   (`NOT NULL DEFAULT false`). A follow-up migration must add it before any
   account can be marked proxy-mandatory; until then MVP treats every account
   as proxy-optional. Production-wide proxy enforcement is tracked in
   FFF-22 / FFF-23.
