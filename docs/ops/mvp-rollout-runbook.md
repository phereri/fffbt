# MVP rollout runbook

- Status: draft (MVP)
- Owner: Architect / Tech Lead
- Audience: a single human operator
- Last updated: 2026-06-03

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

1. One controlled validation job (one device, one local validation video).
2. `run-launcher --max-parallel 1 --max-jobs 1` — one job through the full
   launcher loop end-to-end.
3. `run-launcher --max-parallel 2 --max-jobs 2` — two devices in parallel.
4. Scale beyond 2 devices (out of scope here; only after phase 3 passes
   twice in a row and the post-incident notes for phases 1–3 are clean).

Each phase **must** pass with `final_ok_did_not_register` not appearing
and no manual `cleanup-job` needed, before moving to the next.

---

## 1. Prerequisite checklist

Tick every item before phase 1. If any item is missing or unclear,
**stop** and do not advance.

- [ ] `SUPABASE_DB_URL` is set and points at the MVP database (not the
      legacy `fffbt` schema).
- [ ] All migrations applied: latest is
      `supabase/migrations/20260603130000_reserve_next_video_skip_validation.sql`.
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

`--mode proof_of_posting` forces the real worker steps (video
preparation → MobileRun-driven UI → two-level verification). Without
this flag, `run-job` uses the stub steps and will not exercise the
device.

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
| `run-launcher [--max-parallel N] [--max-jobs M]` | Run the launcher loop. `--max-parallel` overrides `automation.global_settings.max_parallel_jobs`. `--max-jobs` stops after M terminal jobs and drains. |
| `discover-devices` / `reconnect-devices` / `sync-drive` | Maintenance forwards to the matching script. |

---

## 11. Remaining blockers before scaling to 2 devices

- **GenFarmer REST validation.** Only `/backend/auth/me` and
  `/automation/apps` have been validated end-to-end against the
  deployed GenFarmer build. `/automation/run` falls back to the
  MobileRun TCP coord path. Confirm `mobilerun_tcp_trial_reels_path`
  appears in the navigation success payload on every phase-7/8 run.
- **Per-device coordinate drift.** The OK button position uses
  `width * 0.925` / `height * 0.07` of the inferred UI size. On a phone
  with a different ratio or system bar, the fallback coords may miss.
  Real-device check on every new SKU is mandatory before adding it to
  the rotation.
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
