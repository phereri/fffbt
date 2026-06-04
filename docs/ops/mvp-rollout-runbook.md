# MVP rollout runbook

- Status: draft (MVP, pre-account-login)
- Owner: Architect / Tech Lead
- Audience: a single human operator
- Last updated: 2026-06-03 (post-PR #61)

This runbook drives the staged rollout of the Instagram Trial Reels MVP
from one controlled validation job to two concurrent devices. Each phase
is gated on the previous one passing. The goal is to catch real-device
regressions (publish target, video preparation, verification) before any
attempt to scale.

> **Not yet in scope.** ChangeInfo / GPS / proxy / Android 12+ Change Device
> are deferred. Real-device hardening (login/2FA flows, account recovery,
> live posting) requires human supervision and is **not** unlocked by this
> runbook on its own.

---

## 0. Phase ladder

0. **Pre-account-login stage** (you are here when no Instagram account is
   logged in on the available phone). Verify the controlled-stop path:
   the proof job must hit `logged_out` and produce a screenshot + UI
   dump + a `job_events` row for review. No retries, no posting.
1. One controlled validation job (one device, one local validation video,
   one Instagram account a human just logged in).
2. `run-launcher --max-parallel 1 --max-jobs 1` — one job through the full
   launcher loop end-to-end.
3. `run-launcher --max-parallel 2 --max-jobs 2` — two devices in parallel.
4. Scale beyond 2 devices (out of scope here; only after phase 3 passes
   twice in a row and the post-incident notes for phases 1–3 are clean).

Each phase **must** pass with `final_ok_did_not_register` not appearing
and no manual `cleanup-job` needed, before moving to the next.

---

## 0a. Pre-account-login stage

The VPS is currently in a controlled state: MobileRun TCP works, the
phone is reachable via ADB, but no Instagram account is logged in. The
goal of this stage is **not** to publish anything — it is to confirm
the system safely hard-stops on `logged_out` and persists the evidence
needed to debug it later.

What is guaranteed at this stage:

- The launcher's generic queue **will not** accidentally pick up
  validation/placeholder accounts (`accounts.is_validation = true`)
  even if they were seeded via `seed_validation_accounts.py`. The guard
  is in `automation.find_eligible_account()`; targeted `create-job
  --account-id <uuid>` still works for those accounts.
- The launcher's generic queue **will not** pick up validation videos
  (`videos.category = 'validation'` or `download_method =
  'local_validation'`). Only `create-job --device-serial --video-id` can.
- The proof_of_posting flow detects `logged_out` and stops with a
  `needs_review` outcome rather than retrying or escalating. The
  job's `device_id` and reserved `video_id` are released by the
  pipeline cleanup step.
- Every hard stop (`logged_out`, `action_blocked`, `login_challenge`,
  `account_suspended`, `unexpected_destructive_dialog`) writes a
  screenshot **and** a UI tree dump under
  `$ARTIFACTS_DIR/mobile_ui/<job_id>/` with timestamped names like
  `hard_stop_logged_out_20260603T091200Z.png` and `.ui.json`. The same
  artifact references land in `automation.job_events.payload.artifacts`
  for the failing step.

### 0a.1 What "controlled stop" looks like

1. `fffbt create-job --device-serial <serial> --video-id <validation-video-uuid> --account-id <validation-account-uuid>` — the only path that legitimately uses a validation account during this stage.
2. `fffbt run-job <job_id> --mode proof_of_posting`.
3. Expected outcome:
   - `step_done` event for `mobile_ui_automation` with status
     `needs_review` and code `logged_out`.
   - `job_events.payload.artifacts` contains a screenshot and a UI
     dump entry.
   - Job transitions to `needs_review`, not `failed` or `done`.
   - Device returns to `online`; the validation video may rebound to
     `new` if the per-error retry policy is `retryable`, otherwise it
     stays `reserved` (terminal codes do not roll back automatically).

If any of those expected outcomes is missing, **stop**. Either the
migration `20260603140000_account_validation_flag_and_device_unique.sql`
is not applied, the worker's `ARTIFACTS_DIR` is not writable, or the
device's actual screen does not match the hard-stop patterns in
`src/worker/steps/mobile_ui_automation.py::_HARD_STOP_PATTERNS` — open
an incident in `docs/reports/` before continuing.

### 0a.2 What to do once an account is manually logged in

1. **Open Instagram on the phone by hand** and complete the login + any
   2FA + any "Save your login info" prompt. The session must be left in
   a state where opening the app lands on the feed without further
   prompts.
2. **Verify a fresh proof job no longer hard-stops on logged_out**:

   ```bash
   PYTHONPATH=src python -m scheduler.cli create-job \
       --device-serial <serial> \
       --video-id <validation-video-uuid> \
       --account-id <validation-account-uuid>
   PYTHONPATH=src python -m scheduler.cli run-job <job_id> \
       --mode proof_of_posting --log-level info
   ```

   Expected: the step proceeds past `_open_instagram`, navigates to the
   share screen, fills the caption, and either reaches the final OK
   (success path) or fails with a specific non-`logged_out` code that
   you can then triage.
3. **Do not** convert the placeholder validation account into a real
   posting account. Once you have a logged-in phone, register the real
   account via the normal account-management flow (out of scope for this
   runbook) and clear the `is_validation` flag only on that real row.
4. **Do not** raise `--max-parallel` past 1 until phase 1 has passed
   twice in a row with the real account.

### 0a.3 How to rerun proof_of_posting after a logged-out incident

If a previous run left the job in `needs_review`:

```bash
PYTHONPATH=src python -m scheduler.cli cleanup-job <job_id>
# re-seed targeted job for the same device + video + account:
PYTHONPATH=src python -m scheduler.cli create-job \
    --device-serial <serial> \
    --video-id <validation-video-uuid> \
    --account-id <validation-account-uuid>
PYTHONPATH=src python -m scheduler.cli run-job <new_job_id> \
    --mode proof_of_posting --log-level info
```

The previous run's artifacts are kept under
`$ARTIFACTS_DIR/mobile_ui/<old_job_id>/` for as long as the operator
chooses to keep them — `cleanup-job` does **not** delete them.

---

## 1. Prerequisite checklist

Tick every item before phase 1. If any item is missing or unclear,
**stop** and do not advance.

- [ ] `SUPABASE_DB_URL` is set and points at the MVP database (not the
      legacy `fffbt` schema).
- [ ] All migrations applied: latest is
      `supabase/migrations/20260603140000_account_validation_flag_and_device_unique.sql`.
- [ ] `accounts.is_validation` column exists; seeded placeholder rows
      already carry `is_validation = true` (verify with
      `SELECT username, is_validation FROM automation.accounts WHERE
       username LIKE 'validation_%';`).
- [ ] At least one online physical device is registered (`fffbt status`
      shows `devices: online: N>=1`).
- [ ] At least one validation account exists with an active
      `account_environments` row (proxy, device_profile, gps, app_state
      all `active`).
- [ ] GenFarmer / MobileRun Portal is reachable from the worker host on
      the configured `genfarmer_url` (default `http://127.0.0.1:55554`).
      The phone is connected to GenFarmer and visible in `fffbt
      discover-devices`.
- [ ] `ADB_PATH` resolves to a working `adb` binary on the worker host.
- [ ] `FFMPEG_PATH` resolves to a working `ffmpeg` binary if any
      validation video needs transcode.
- [ ] A local `.mp4` (1KB–500MB) is available on the worker host for
      seeding as a validation video.
- [ ] No stuck rows in `automation.jobs` (no jobs in `running`,
      `in_progress`, or `verifying` older than `job_heartbeat_timeout_seconds`).
      If any, `fffbt cleanup-job <uuid>` for each before continuing.
- [ ] You have read `docs/ops/mobilerun-setup.md` and the safety section
      of `docs/ops/runtime-smoke-test-runbook.md`.
- [ ] `MOBILE_UI_EXECUTOR` is unset OR set to `mobilerun_agent` (the
      default). The primary proof_of_posting executor is the MobileRun AI
      agent driven by `config/mobilerun/app_cards/instagram.md`, not the
      hardcoded TCP coordinate path. If `mobilerun` is not installable on
      this host, set `MOBILE_UI_EXECUTOR=deterministic` *intentionally*
      and record that in the rollout report — the deterministic path is a
      fallback, not the supported MVP target.
- [ ] If the agent path is in use, `mobilerun` is importable from the
      worker venv (`python -c "from mobilerun import MobileAgent"` exits
      0) and the trajectories directory (`MOBILERUN_TRAJECTORIES_DIR`,
      default `trajectories/`) is writable.

---

## 2. Adding validation videos

Validation videos live in `automation.videos` with `category = 'validation'`
and `download_method = 'local_validation'`. The launcher's generic queue
**ignores them** (see migration
`20260603130000_reserve_next_video_skip_validation.sql`), so they only get
picked up via targeted job creation (`--video-id`).

1. Copy the `.mp4` to a stable absolute path on the worker host. The
   absolute path is the identity key; do not move/rename it after seeding.
2. Seed it:

   ```bash
   PYTHONPATH=src python -m scheduler.cli seed-validation-video \
       /abs/path/to/your_validation.mp4
   ```

   Output prints the assigned `video_id`. Save it.
3. Confirm:

   ```bash
   PYTHONPATH=src python -m scheduler.cli status
   ```

   `videos: new: N>=1` should include the seeded row. Cross-check by
   running the same seed command a second time — it must be idempotent
   (re-seeds in place, same `video_id`, no new row).

---

## 3. Seeding one validation video and one targeted job

After seeding (section 2), pin one job to one specific device + the
seeded video. This skips the generic queue entirely.

```bash
PYTHONPATH=src python -m scheduler.cli create-job \
    --device-serial <serial-or-connect-target> \
    --video-id <seeded-video-id>
```

Optional: also pin a specific account with `--account-id <uuid>`.

The targeted SQL **requires** the device serial; without it, the command
falls through to the generic `create_publishing_job()` path which will
not pick the validation video.

If `--device-serial` returns "no job created", check:
- the device's `last_seen_at` is within 300 seconds (`fffbt
  discover-devices` if not),
- the device is `online` with `current_job_id IS NULL`,
- there is at least one eligible account with no in-flight job.

---

## 4. Running one proof_of_posting job

Once `create-job` printed a `job_id`, run that job directly through the
worker pipeline:

```bash
PYTHONPATH=src python -m scheduler.cli run-job <job_id> \
    --mode proof_of_posting --log-level info
```

`run-job` always uses the real worker steps (video preparation →
MobileRun-driven UI → two-level verification) and never runs no-op
stubs, with or without `--mode`. Pass `--mode proof_of_posting`
explicitly for clarity; there is no accidental stub path.

Expected terminal output: `done`. The job should publish, then transition
to verification, and Level 2 should confirm the reel is visible on the
Trial Reels list.

If you see `final_ok_did_not_register` or `share_did_not_register`,
**stop**. The publish target detection is wrong for this device's UI;
collect the screenshot + UI dump from `automation.actions_log` and from
the worker's artifact directory, and do not move on to phase 2.

---

## 5. Inspecting `job_events` / artifacts

After a run, pull the event timeline + artifacts to confirm the publish
flow actually went through MobileRun TCP (not ADB tap fallback) and that
the final OK was tapped at the expected coordinates.

```bash
PYTHONPATH=src python -m scheduler.cli status --events 30
```

Or, by SQL:

```sql
SELECT created_at, event_type, from_status, to_status, payload
  FROM automation.job_events
 WHERE job_id = '<job_id>'
 ORDER BY created_at;
```

Things to confirm in the payloads:
- `mobile_driver.primary = 'mobilerun_tcp'`
- `mobile_driver.adb_fallback_used = false`
- The publish OK tap is logged as `Trial Reel published via accessible_node OK`
  (or `top_right_fallback`), not `share confirmed: share button disappeared`.

Screenshots are written under the worker's artifact directory
(`artifacts/<job_id>/...`) with labels like `caption_filled`,
`post_result`, `final_ok_did_not_register`.

---

## 6. Error-path test

Before moving from phase 1 to phase 2, run **one** error path manually
to confirm the cleanup + needs_review flow works.

1. Create a targeted job pointing at a video file path that does **not**
   exist on disk (seed a stub with a deliberately wrong `local_video_path`
   via a direct SQL update — leave the original validation video
   untouched).
2. Run `run-job <job_id> --mode proof_of_posting`. Expect `video_preparation`
   to fail with a non-retryable code.
3. Confirm `fffbt status` shows `jobs: needs_review: 1`, the device returns
   to `online`, and the video either rolls back to `new` (for transient
   codes) or stays `reserved` (for terminal codes).
4. Run `fffbt cleanup-job <job_id>` to verify the safe-cleanup CLI does
   release the device + video and emits a `manual_cleanup` event.

---

## 7. Launcher `--max-parallel 1`

After phases 1–6 pass cleanly:

```bash
PYTHONPATH=src python -m scheduler.cli run-launcher \
    --max-parallel 1 --max-jobs 1 --log-level info
```

- The launcher runs the **real** proof_of_posting steps by default
  (MobileRun agent executor) — the same code path `run-job` uses. It does
  **not** run stubs. (The `--stub` flag exists for tests only and must
  never be passed against the production queue.)
- The launcher picks **one** job from the generic queue (validation
  videos are excluded), dispatches it, drains, then exits.
- `--max-jobs 1` is the hard stop: once 1 job reaches a terminal state
  (`done` / `failed` / `timed_out`), the loop shuts down. Re-queued
  retries do **not** count against this limit; the eventual terminal
  outcome does.
- Verify with `fffbt status --events 30` that the run started, dispatched
  a job, drained, and emitted the `max_jobs_reached` event in the JSONL.

Expected outcome: `summary: created=1 done=1 failed=0 timed_out=0 retried=0`.
Anything else — review the events before proceeding.

---

## 8. Launcher `--max-parallel 2`

Only after phase 7 has passed twice in a row, without manual cleanup:

```bash
PYTHONPATH=src python -m scheduler.cli run-launcher \
    --max-parallel 2 --max-jobs 2 --log-level info
```

- Two devices must be online, two eligible accounts must exist, and the
  generic video queue must contain at least two Drive-sourced `new`
  rows.
- The launcher creates two jobs concurrently, drains both, then exits.
- The `semaphore` cap and the `--max-parallel` flag are both enforced;
  the override is logged in the `settings_loaded` JSONL event.

Expected outcome: `summary: created=2 done=2`.

**Stop conditions during this phase:**

- If either device produces `final_ok_did_not_register`, stop and
  bisect — the New reel publish path may be device-specific.
- If `process_job_error` events show `INFRA` codes, stop and resolve the
  infra issue (proxy, MobileRun connectivity) before retrying.
- If `at_capacity` events appear with `active < 2`, the heartbeat monitor
  may have stale state — investigate before increasing `--max-parallel`.

---

## 9. When NOT to continue

Do not move to the next phase if any of the following held during the
current phase:

- A job reached `needs_review` with a code other than the one you were
  deliberately testing in section 6.
- The `mobile_driver.adb_fallback_used` flag was `true` in any step's
  `details`.
- Any `final_ok_did_not_register` or `share_did_not_register` event
  fired.
- The verification step Level 2 returned `verification_failed`.
- `cleanup-job` was needed because a job hung without a heartbeat — fix
  the underlying hang before scaling further.
- A migration is pending in `supabase/migrations/` that has not yet been
  applied to the MVP database.
- The worker host's clock is off by more than 30 seconds (heartbeat
  freshness uses `now()` server-side, but artifact timestamps and the
  `last_seen_at` 300s window assume client/server clock agreement).

---

## 10. CLI reference (MVP-relevant subset)

All commands accept `--db-url` (or env `SUPABASE_DB_URL`).
Direct DB connection is required for `run-launcher`, `run-job`, and
`cleanup-job`. The Management API mode (`--via-management-api`) works
for `status`, `create-job`, `seed-validation-video`.

| Command | What it does |
|---|---|
| `status [--events N] [--json]` | Print counts for jobs/devices/videos; optionally the N most recent events. |
| `seed-validation-video <path>` | Idempotently seed a local MP4 into `automation.videos` with `category='validation'`. |
| `create-job [--device-serial S] [--account-id U] [--video-id U] [--json]` | Generic mode creates a job from the queue. With `--device-serial`, pins to one device; with `--video-id`, also pins one validation video. |
| `cleanup-job <uuid> [--json]` | Safely cleanup a stuck job: release the device, release the reserved video (if any), transition the job to `needs_review`, audit-log the cleanup. |
| `run-job <uuid> --mode proof_of_posting` | Run one job end-to-end through the real worker pipeline. |
| `run-launcher [--max-parallel N] [--max-jobs M] [--stub]` | Run the launcher loop using the real proof_of_posting steps (MobileRun agent) by default — never stubs. `--max-parallel` overrides `automation.global_settings.max_parallel_jobs`. `--max-jobs` stops after M terminal jobs and drains. `--stub` (test-only) runs no-op steps; never use against the production queue. |
| `discover-devices` / `reconnect-devices` / `sync-drive` | Maintenance forwards to the matching script. |

---

## 11. Remaining blockers before scaling to 2 devices

- **MobileRun agent path real-device validation.** The agent executor
  (`MOBILE_UI_EXECUTOR=mobilerun_agent`, default) is wired but has not
  yet been run end-to-end against a logged-in Instagram account from
  this repo. Phase 1 must explicitly capture the agent's trajectory
  files (under `MOBILERUN_TRAJECTORIES_DIR`) and the
  `details.mobile_driver.executor` field in the worker output; both
  must show `mobilerun_agent`. If the agent path is unavailable on the
  host (e.g. `mobilerun` package not installed), record that explicitly
  in the rollout report and fall back to `MOBILE_UI_EXECUTOR=deterministic`
  for the run — that is a known degradation, not the supported MVP target.
- **GenFarmer REST validation (deterministic fallback only).** Only
  `/backend/auth/me` and `/automation/apps` have been validated end-to-end
  against the deployed GenFarmer build. `/automation/run` falls back to
  the MobileRun TCP coord path. This blocker only applies when
  `MOBILE_UI_EXECUTOR=deterministic` — the agent executor builds its
  own `MobileAgent` and does not depend on GenFarmer's automation routes.
- **Per-device coordinate drift (deterministic fallback only).** The
  legacy executor's OK button position uses `width * 0.925` / `height *
  0.07` of the inferred UI size. On a phone with a different ratio or
  system bar, the fallback coords may miss. Real-device check on every
  new SKU is mandatory before adding it to the rotation. The agent
  executor does not depend on these coordinates — the AppCard names the
  resource id and the agent computes taps from the live UI tree.
- **No Change Device / GPS / proxy yet.** The current MVP assumes the
  device is already in the intended state. Do not enable these features
  in production until phase 4 planning is complete.
- **Stuck-job detection is heartbeat-driven, not idempotency-driven.** A
  worker that crashes between `transition_job_status` and `release_device`
  can leave a device pinned. The `cleanup-job` CLI is the safety valve;
  run it before any scale-up attempt.
- **Validation account quota.** Phase 8 needs two eligible accounts
  with active environments. Re-running phase 8 several times exhausts
  per-account once-per-day limits faster than the generic queue assumes.
