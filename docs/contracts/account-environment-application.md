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
2. the three operation **modes** (`proof_of_posting`, `mvp`, `production`) and,
   for each mode, which of the five preparation steps is required,
   best-effort, deferred, or forbidden;
3. the ChangeInfo / BackupRestoreV2 safety gates that must pass before the
   `mvp` and `production` modes may run;
4. the pre-publish verification the worker performs before handing off to the
   Poster;
5. the output status values and error codes.

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
apply_account_environment(device_id, account_environment_id, mode) -> ApplyResult
```

### Inputs

| Param | Type | Meaning |
|---|---|---|
| `device_id` | uuid | `automation.physical_devices.id` of the reserved phone. Must be the phone already assigned to the job. |
| `account_environment_id` | uuid | `automation.account_environments.id`. Resolves to the full identity bundle. |
| `mode` | enum | `proof_of_posting` \| `mvp` \| `production`. Selects which steps are required, best-effort, deferred, or forbidden (see *Operation modes*). Supplied by the launcher from deployment/runtime config — it is not a per-account field. During the current project phase the only runnable mode is `proof_of_posting` (see *Mode readiness*). |

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
- `mode` is a valid enum value, and if `mode` is `mvp` or `production` the
  *Mode readiness* gates for that mode have passed.

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
- ChangeInfo-bearing operations (Step 1 device profile, Step 4 full restore)
  are additionally constrained by the *ChangeInfo / BackupRestoreV2 safety
  gates* below.

### Idempotency

`apply_account_environment` is safe to call again on the same job after a
retryable failure. Each step re-checks current device state before acting and
skips a step whose target state is already satisfied (e.g. proxy already set
to the expected value). A step that already succeeded is reported `skipped`
on re-run.

## Operation modes

`apply_account_environment` runs in one of three modes. The mode is **not** a
property of an account — it is a project-phase setting passed by the launcher.
Device profile is a permanent part of the production model: one physical
phone must carry a different account-specific device identity before each
job, so device profile is **required** from `mvp` onward and is *not* optional
forever. It is skipped only for the very first proof-of-posting run.

### Step tiers

For each mode every step carries exactly one tier:

| Tier | Meaning |
|---|---|
| **required** | Must run and pass for the job to reach `publishing`. A failure stops the job — in `mvp`/`production` with `environment_failed` → `needs_review`. |
| **best-effort** | Attempted when possible; failure or unavailability is recorded as a warning/event and the job continues. Does not block posting on its own. |
| **deferred** | Intentionally not run in this mode — not yet enforced, or blocked on an open unknown (see *Open questions*). Reported as a `skipped` StepResult. |
| **forbidden** | Must **not** run in this mode — a safety constraint. Reported as a `skipped` StepResult with the forbidding reason. |

### Mode × step matrix

| Step | `proof_of_posting` | `mvp` | `production` |
|---|---|---|---|
| 1 — apply device profile (ChangeInfo) | **forbidden** | **required** | **required** |
| 2 — apply proxy | best-effort | best-effort¹ | **required** |
| 3 — apply GPS (MockGPS) | best-effort | **required** | **required** |
| 4a — verify app/session | **required** | **required** | **required** |
| 4b — full restore (BackupRestoreV2) | **forbidden** | deferred² | **required** |
| 5 — verify environment | **required** | **required** | **required** |

¹ Proxy stays best-effort for `mvp` per the earlier architecture decision,
**except** when the account environment is flagged `proxy_required` — then it
is `required` for that account. See Step 2 and Open question 5.

² `mvp` full restore is deferred only until cross-phone restore semantics are
verified (Open question 2); the production model treats it as `required`.

### Mode descriptions

**`proof_of_posting` (MVP-0 / first proof-of-posting).** A single controlled
proof that Instagram Reels Trial posting works end-to-end on **one device that
is already prepared**. Device identity is not mutated: the device profile step
and full restore are *forbidden* — running ChangeInfo here is out of scope and
its recovery path is still unproven (see safety gates). Proxy and GPS are
best-effort. The flow is: *verify device online → verify session where
possible → apply GPS if available → record events*.

**`mvp` (MVP-1 / actual MVP).** The real MVP model where one phone serves
multiple accounts. Device profile application is **required** — the account
environment must carry a device profile, and if it cannot be applied the job
stops with `environment_failed` → `needs_review`. GPS via MockGPS is
**required**: if MockGPS cannot be applied, the job does **not** proceed.
Proxy stays best-effort (Open question 5). Full cross-phone restore is
deferred; verifying the assigned session is still required.

**`production`.** Full hardening: device profile, proxy, GPS, and app/session
state are all applied and verified. Every step is `required`.

### Mode readiness

A mode is *runnable* only when its prerequisites are met:

- **`proof_of_posting` — runnable now.** It forbids the hazardous ChangeInfo
  operations, so it depends on nothing unresolved.
- **`mvp` and `production` — not yet runnable.** Both require Step 1 (device
  profile / ChangeInfo). That step is blocked on two open unknowns:
  1. the GenFarmer Task JSON shape needed to submit it programmatically
     (Open question 1), and
  2. the ChangeInfo safety gates below — sandbox validation and a proven
     ADB/Tailscale recovery path (Open question 3).

  Until **both** are resolved, the launcher must not invoke
  `apply_account_environment` in `mvp` or `production` mode. The modes are
  fully specified here so implementation can target them, but the contract is
  explicit that they are gated, not live.

This is the deliberate resolution of a tension in the requirements: `mvp`
*requires* device profile, yet device profile *requires* a proven-safe
ChangeInfo. The contract does not make device profile optional — it gates the
whole `mvp` mode behind the safety proof instead.

## ChangeInfo / BackupRestoreV2 safety gates

Step 1 (device profile) uses GenFarmer `ChangeDevice` with the `changeInfo`
sub-mode, and Step 4b (full restore) uses `BackupRestoreV2` with
`withChangeInfo=true`. Both mutate synthetic device identity and have been
observed to be hazardous: on a Tailscale-only phone, ChangeInfo dropped both
`adbd` (`:5555`) and `atx-agent` (`:7912`) with **no proven remote recovery**
(research input, FFF-18, `genfarmer-changeinfo-hazard.md`).

Before `apply_account_environment` may run ChangeInfo against any account in
`mvp` or `production` mode, all four gates below must pass. Until they do,
ChangeInfo is forbidden outside the sandbox and only `proof_of_posting` mode
may run.

| Gate | Requirement |
|---|---|
| **G1 — sandbox validation first** | ChangeInfo and `BackupRestoreV2 withChangeInfo=true` must be exercised in a sandbox (a dedicated test phone, not a production job) before any live run. |
| **G2 — no production Instagram account** | The ChangeInfo sandbox test must use a throwaway / test Instagram account. No production account may be logged in on the device during ChangeInfo testing. |
| **G3 — recoverability proof** | The sandbox test must demonstrate that the device reconnects over ADB/Tailscale — `:5555` **and** `:7912` both responsive — within a bounded timeout after ChangeInfo. The proof must be repeatable, not a single lucky run. |
| **G4 — no production use until proven** | If G3 is not proven, the `mvp` and `production` paths must **not** use ChangeInfo. Because device profile is required from `mvp` onward, this means `mvp` and `production` modes do not run at all until G1–G3 pass. |

Operational consequence inside Step 1: even after the gates pass, every live
ChangeInfo run still re-confirms `:5555` and `:7912` reachability within a
timeout and stops with `device_unreachable_after_change_info` →
`needs_review` if either port does not recover. No blind retries.

## Preparation steps

The operation runs five ordered steps (Step 4 has a verify sub-step 4a and a
restore sub-step 4b). Each step produces a `StepResult` (see Output). A
failure in a step that is **required for the active mode** stops the operation
— later steps are reported `skipped`. A **best-effort** step that fails or
cannot run does **not** stop the operation: it is reported `deferred`, a
warning is recorded, and the next step runs. A step that is **deferred** or
**forbidden** in the active mode is not run and is reported `skipped`.

> Implementation note: steps 1, 2 and 4 are GenFarmer script-DSL nodes, not
> standalone REST calls. In practice they are submitted as one GenFarmer
> automation Task (`POST /automation/tasks` + `POST /automation/runs`) and
> their results polled via `GET /automation/runs/:id/storages` (research
> input, FFF-18). This contract still defines them as **discrete logical
> steps** with discrete results so failures map to specific error codes.

### Step order rationale

1. **Device profile** first — it is the most disruptive change and a
   connectivity barrier (see safety gates above). Nothing else should run
   until the phone is confirmed reachable afterwards.
2. **Proxy** before any Instagram traffic — when proxy is applied it belongs
   to the account one-to-one and must be active before the session is touched.
3. **GPS** after proxy — the GPS location must correspond to the proxy's
   location.
4. **App/session state** last among the mutating steps — restored only once
   network identity is in place.
5. **Verify** confirms the whole bundle.

### Step 1 — apply device profile

*Tier: `proof_of_posting` → forbidden · `mvp` → required · `production` →
required.*

- **Input**: `device_profile`.
- **Action**: apply the fingerprint via GenFarmer (`ChangeDevice` with
  `changeInfo` sub-mode). Build props, model/brand, locale, timezone, screen
  metrics come from the profile row.
- **Success criterion**: GenFarmer reports the profile applied; visible
  device properties (`ro.product.model`, locale, timezone) match the profile
  where readable via ADB.
- **`proof_of_posting` (forbidden)**: not run. The proof posts on an
  already-prepared device with the fingerprint it already has; mutating device
  identity during the proof is out of scope and the ChangeInfo recovery path
  is unproven. Reported `skipped`.
- **`mvp` / `production` (required)**: must succeed. The account environment
  must carry a device profile. If the profile cannot be applied — or the
  hazard barrier below trips — the operation stops with `environment_failed` →
  `needs_review` (the step-specific cause code is recorded alongside).
- **Hazard barrier** (research input, FFF-18,
  `genfarmer-changeinfo-hazard.md`): ChangeInfo has been observed to drop both
  `adbd` (`:5555`) and `atx-agent` (`:7912`) on Tailscale-only phones with no
  proven remote recovery. After this step the operation **must** re-confirm
  reachability on `:5555` and `:7912` within a timeout before continuing. If
  either port is unreachable → stop with `device_unreachable_after_change_info`
  (`needs_review`). No blind retries. Live ChangeInfo is permitted only after
  the *ChangeInfo / BackupRestoreV2 safety gates* G1–G3 pass.
- **Pre-flight**: capture `serial_no` + `android_id` before the step so an
  after-diff is possible.
- **Errors**: `device_profile_failed`, `device_unreachable_after_change_info`,
  `device_offline` — in `mvp`/`production` any of these escalates the job
  outcome to `environment_failed` → `needs_review`.

### Step 2 — apply proxy

*Tier: `proof_of_posting` → best-effort · `mvp` → best-effort (required if
account is `proxy_required`) · `production` → required.*

- **Input**: `proxy`.
- **Action**: apply the proxy through GenRouter for this device. GenRouter
  expects **colon-delimited** form, not a URL:
  `socks5://host:port:user:pass` (or `http(s)://…`) (research input, FFF-18).
- **Success criterion**: the device's effective external IP, checked through
  the device, resolves to the proxy egress (country code should match
  `proxy.country_code` when known).
- **`proof_of_posting` / `mvp` (best-effort)**: if proxy configuration is
  unavailable, or the apply or its verification fails, the step does **not**
  stop the operation. It reports `StepResult.status = "deferred"`, adds a
  `proxy_deferred` entry to `ApplyResult.warnings`, writes a `job_events` row
  with the reason, and the operation continues to Step 3.
- **Exception — `proxy_required` accounts**: if the account environment is
  flagged `proxy_required`, the proxy is mandatory for that account even in
  `mvp` mode. A missing config or a failed apply is then a hard `proxy_failed`
  → `environment_failed` → `needs_review`. `proxy_required` is a boolean that
  does **not** exist in the schema yet — a follow-up migration must add
  `automation.account_environments.proxy_required` (default `false`); until it
  lands, `mvp` treats every account as proxy-optional (see Open questions).
- **`production` (required)**: proxy is mandatory for every account; a
  `proxy_failed` always stops the operation. Production proxy hardening is
  tracked in FFF-22 / FFF-23.
- **Errors**: `proxy_failed` (blocking in `production`, and in `mvp` only for
  `proxy_required` accounts — otherwise downgraded to the `proxy_deferred`
  warning), `device_offline`.

### Step 3 — apply GPS

*Tier: `proof_of_posting` → best-effort · `mvp` → required · `production` →
required.*

- **Input**: `gps_location`.
- **Action**: set the mock location via MockGPS. Prefer a direct
  intent/API; fall back to Appium/UI automation **on this device only** if no
  direct API exists.
- **Success criterion**: the device's reported location is within
  `accuracy_meters` of `(latitude, longitude)`; the location is consistent
  with the proxy country.
- **`proof_of_posting` (best-effort)**: applied/checked via MockGPS when
  available. If MockGPS is unavailable the check is `unverified`; it does not
  block the proof.
- **`mvp` / `production` (required)**: GPS is mandatory. MockGPS must be
  applied successfully. If MockGPS cannot be applied, the job does **not**
  proceed — the operation stops with `gps_failed` → `environment_failed` →
  `needs_review`.
- **Errors**: `gps_failed` (blocking in `mvp`/`production`), `device_offline`.

### Step 4 — restore / check app state

*Tier — sub-step 4a (verify session): required in all modes. Sub-step 4b
(full BackupRestoreV2 restore): `proof_of_posting` → forbidden · `mvp` →
deferred · `production` → required.*

- **Input**: `app_state`, `account`.
- **4a — verify session (all modes)**: confirm the Instagram app is installed
  and a session for `account` is present on the device. This is the gate that
  feeds the pre-publish identity check in Step 5.
- **4b — full restore (`production`)**: restore the Instagram app/session
  state for the account onto the device via a `BackupRestoreV2`
  (`mode=restore`, `withChangeInfo` per profile policy) node. This is
  ChangeInfo-bearing and is subject to the *ChangeInfo / BackupRestoreV2
  safety gates*.
  - **`proof_of_posting` (forbidden)**: not run — the proof uses the session
    already on the device.
  - **`mvp` (deferred)**: not run yet — cross-phone restore semantics are
    unverified (Open question 2). The worker relies on the session already
    present and falls back to a fresh login during `publishing` if needed.
- **Success criterion**: the Instagram app is installed and a session for the
  account is present (4a); for `production`, the backup restored cleanly (4b).
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

*Tier: required in all modes.*

Confirmation of the whole bundle, check by check. Each check that *can* run
does run; a check that cannot be performed is reported `unverified`, not
failed. Checks for steps that were deferred or forbidden in the active mode
are skipped (not `unverified`).

- **Device profile**: visible model/locale/timezone match the profile.
  Skipped in `proof_of_posting` (profile not applied).
- **Proxy**: external IP egress matches the proxy. Skipped when proxy was
  deferred for the active mode.
- **GPS**: reported location matches `gps_location`. In `mvp`/`production` a
  mismatch is a failure; in `proof_of_posting` it is `unverified`.
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
  "mode": "proof_of_posting",
  "device_id": "…",
  "account_environment_id": "…",
  "expected_username": "…",
  "error_code": null,
  "error_message": null,
  "warnings": [
    { "code": "proxy_deferred", "step": "proxy",
      "detail": "no proxy config available; continued without proxy" }
  ],
  "steps": [
    { "step": "device_profile", "status": "skipped",  "detail": "forbidden in proof_of_posting" },
    { "step": "proxy",          "status": "deferred", "detail": "best-effort: proxy not applied" },
    { "step": "gps",            "status": "ok",       "detail": "lat/lon within 8m" },
    { "step": "app_state",      "status": "ok",       "detail": "session verified" },
    { "step": "verify",         "status": "ok",       "detail": "username matched" }
  ]
}
```

The example above is a typical **`proof_of_posting`** run: device profile is
`skipped` (forbidden), proxy is `deferred`, and the job still reaches `ready`.
An `mvp` run additionally requires device profile and GPS to be `ok`; a
`production` run reports every step `ok` with an empty `warnings` list.

`mode` echoes the input mode so the result is self-describing for logs and
review. `warnings` is a (possibly empty) list of non-blocking issues — each is
a best-effort step that was deferred or could not be verified. Warnings never
change the top-level `status`.

### Top-level `status`

| Value | Meaning | Job transition |
|---|---|---|
| `ready` | All steps required for the active mode passed and the environment is verified. Best-effort steps may be `deferred`; any deferral is listed in `warnings`. | `preparing_device → publishing` |
| `needs_review` | An ambiguous failure, or a mode-required step failed (`environment_failed`); human/automated review required. | `preparing_device → needs_review` |
| `failed` | A recoverable infrastructure failure; retry policy decides. | `preparing_device → failed` (may re-queue) |

The caller does **not** interpret `error_code` itself — it passes
`error_code` + `error_message` to `automation.process_job_error()`, which
encodes category, retry limits, and account side effects
(see [`retry-failure-policy.md`](./retry-failure-policy.md)).

### `StepResult.status`

| Value | Meaning |
|---|---|
| `ok` | Step applied and (where checkable) verified. |
| `skipped` | Step not run — already satisfied (idempotent re-run), a prior step failed, or the step is `deferred`/`forbidden` in the active mode. `detail` states which. |
| `unverified` | Step applied but its effect could not be confirmed (best-effort check unavailable). |
| `deferred` | A best-effort step that was not applied or failed without blocking. Recorded in `warnings` and `job_events`. |
| `failed` | Step failed; `error_code` on the result is set. In `mvp`/`production` a failed required step escalates the job to `environment_failed` → `needs_review`. |

## Error codes

All publishing-stage error handling goes through `process_job_error()`.
Codes already in `automation.error_catalog` are reused as-is; the catalog is
the source of truth for category and retry limits.

### Reused from the error catalog

| Error code | Category | Step | Notes |
|---|---|---|---|
| `device_profile_failed` | retryable (max 2) | 1 | Fingerprint injection failed. In `mvp`/`production`, escalates to `environment_failed` once retries are exhausted. |
| `device_offline` | retryable (max 2) | 1–5 | Phone unreachable via ADB at any step. |
| `proxy_failed` | retryable (max 3) | 2 | Proxy connection / auth error. Blocking in `production` and for `proxy_required` accounts in `mvp`; otherwise downgraded to a non-blocking `proxy_deferred` warning. |
| `gps_failed` | retryable (max 2) | 3 | MockGPS setup / injection failed. Blocking in `mvp`/`production` (GPS required) — escalates to `environment_failed`; non-blocking in `proof_of_posting`. |
| `login_required` | retryable (max 1) | 4, 5 | Session expired; re-login needed. |
| `logged_out` | non-retryable | 4, 5 | Instagram forced logout; account → `disabled`. |

### Proposed new codes

These are specific to environment application and are **not yet** in the
catalog. They must be added via a migration that extends
`automation.error_catalog` before workers emit them (follow-up issue). Until
then, treat them as the listed fallbacks.

| Error code | Proposed category | Step | Fallback today | Description |
|---|---|---|---|---|
| `environment_failed` | needs_review | 1–5 | emit the step-specific code | A step **required for the active mode** (`mvp` or `production`) failed and the prepared environment cannot be trusted. The step-specific cause code (`device_profile_failed`, `gps_failed`, …) is recorded in `error_message`. |
| `device_unreachable_after_change_info` | needs_review | 1 | emit `device_offline` | After ChangeInfo, `:5555`/`:7912` did not recover within timeout. One-way on Tailscale-only phones — no blind retry. |
| `app_state_missing` | needs_review | 4 | emit `login_required` | No usable session/backup to restore (e.g. host has no change-info). Worker may still fresh-login, but flag for review. |
| `account_mismatch` | needs_review | 5 | emit `unknown_screen` | Active Instagram username != `expected_username`. Never publish. |

### Non-blocking warning codes

These are **not** error codes — they never reach `process_job_error()` and do
not change the job status. They are recorded in `ApplyResult.warnings` and in
`job_events` for observability.

| Warning code | Step | Description |
|---|---|---|
| `proxy_deferred` | 2 | Proxy was not applied for a best-effort run — config unavailable or apply failed on a non-`proxy_required` account in `proof_of_posting`/`mvp` mode. The job continues. |

Rationale for `needs_review` on the new error codes above: each indicates the
prepared environment cannot be trusted for *this* account, and none is a clean
"retry the same thing" — they need a human or an automated reviewer to decide
(re-queue, re-assign device, or abandon).

## Events

`apply_account_environment` writes to the existing audit logs; it does not
own a new table.

- **`automation.job_events`** — one `status_changed` row for the
  `preparing_device` entry/exit transitions (written by
  `transition_job_status`). On failure, `process_job_error()` writes the
  `error` row. Per-step progress is recorded as `heartbeat` events with the
  step name, the active `mode`, and the `StepResult` in `payload`. A
  best-effort step that is deferred (e.g. proxy under `mvp`) is recorded as a
  `heartbeat` event whose `payload` carries the warning code (`proxy_deferred`)
  and the reason — this is the durable record behind `ApplyResult.warnings`.
- **`automation.device_events`** — `job_assigned` when the device is reserved
  (caller), `error` for a device-level failure during apply, `job_released`
  on terminal cleanup (written by `process_job_error()`).

Applied device-profile metadata (model, locale, timezone, applied build
props) is logged in the Step 1 `heartbeat` payload. Proxy host/port may be
logged; proxy **credentials** must not.

## Open questions

1. **GenFarmer Task JSON shape.** The exact `tasks.config` schema for
   programmatic submission is still an unknown (FFF-18). Steps 1/2/4 cannot be
   implemented until it is captured. Blocks the `mvp` and `production` modes,
   not `proof_of_posting`.
2. **Cross-phone restore semantics.** `BackupRestoreV2 withChangeInfo=true`
   moving a session between phones is unverified in sandbox (FFF-18). Until
   confirmed, Step 4b stays deferred for `mvp` and cannot be assumed for
   `production`.
3. **ChangeInfo recovery (safety gate G3).** Is there a reliable, repeatable
   path for the device to reconnect over ADB/Tailscale after ChangeInfo? This
   must be proven in the sandbox before `mvp`/`production` modes may run. If a
   remote re-arm path exists, `device_unreachable_after_change_info` could
   become retryable.
4. **Verify timeout values.** Concrete timeouts for the post-ChangeInfo
   reachability wait and for each verification check are left to the
   implementation issue; they belong in global settings, not hard-coded.
5. **`proxy_required` flag.** Per-account proxy enforcement needs a boolean
   that does not exist yet — proposed `automation.account_environments.proxy_required`
   (`NOT NULL DEFAULT false`). A follow-up migration must add it before any
   account can be marked proxy-mandatory; until then `mvp` treats every account
   as proxy-optional. Production-wide proxy enforcement is tracked in
   FFF-22 / FFF-23.
6. **`mode` source.** `mode` is passed by the launcher from deployment/runtime
   config. The exact config key and how it is set per project phase belong to
   the launcher contract, not this document.
