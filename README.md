# fffbt

Instagram **Trial Reels** posting automation. Videos enter a queue, the
scheduler picks an account + device, a worker prepares the video and drives
Instagram via **MobileRun TCP**, then verifies and releases the device.

This README is the operator + developer cheatsheet. Deeper docs live under
[`docs/`](docs/). For the full staged rollout playbook see
[`docs/ops/mvp-rollout-runbook.md`](docs/ops/mvp-rollout-runbook.md).

---

## 0. Current status (read this first)

- MVP mode: **`proof_of_posting`** — one already-prepared device, one already
  logged-in Instagram account, one validation video. No production launcher.
- Backend, scheduler, and worker pipeline are in place. MobileRun TCP works.
  Video preparation + push to phone works. Instagram launches.
- **Current blocker:** the available test phone is logged out of Instagram.
  Until a human logs the account in (and grants Trial Reels access), the
  worker stops cleanly on `logged_out` and persists screenshot + UI dump.
- Once the account is logged in: re-run a controlled `proof_of_posting` per
  stages 1–2 of [`docs/ops/mvp-rollout-runbook.md`](docs/ops/mvp-rollout-runbook.md).
  Do **not** start the launcher before those stages pass.

---

## 1. What this project does (plain language)

1. **Videos enter the queue** — either downloaded from Google Drive
   (`scripts/sync_drive_videos.py`) or seeded locally as validation videos
   (`scripts/seed_validation_video.py`).
2. **The scheduler picks an account + device** — `automation.create_publishing_job()`
   reserves one eligible video, one eligible Instagram account, and one online
   physical Android device, atomically.
3. **The worker prepares the video** — `VideoPreparationStep` validates the
   `.mp4`, transcodes for Android if needed, pushes it to `/sdcard/DCIM/Camera`,
   and triggers MediaScanner.
4. **MobileRun controls Instagram** — `MobileUIAutomationStep` opens Instagram,
   navigates Profile → Professional dashboard → Trial Reels → create, fills the
   caption + hashtags, dismisses the keyboard, and taps the final OK to publish.
5. **Verification confirms the post** — `VerificationStep` does two-level
   verification: immediate (publish screen gone) and delayed (trial reel
   visible in the trials list).
6. **Job status, events, and artifacts are saved** — every step writes a
   heartbeat event into `automation.job_events`. Hard stops persist a screenshot
   + UI tree dump under `$ARTIFACTS_DIR/mobile_ui/<job_id>/`. The device is
   released back to `online` after success **or** failure.

---

## 2. MVP architecture (text diagram)

```
Google Drive *.mp4  ──sync_drive_videos.py──┐
Local validation .mp4 ──seed_validation_video.py──┘
                                            │
                                            ▼
                         automation.videos (status: new)
                                            │
                          ┌─────────────────┴─────────────────┐
                          ▼                                   ▼
            generic queue                       targeted (validation)
            reserve_next_video()                create-job --device-serial
            skips category='validation'         --video-id  --account-id
                          │                                   │
                          └─────────────────┬─────────────────┘
                                            ▼
                       automation.create_publishing_job()
                  reserves video + account + physical device
                                            │
                                            ▼
                            automation.jobs (queued)
                                            │
                                            ▼
                   run-job <id> --mode proof_of_posting
                                            │
                                            ▼
   ┌──────────────── pipeline (scheduler/pipeline.py) ─────────────────┐
   │  ProofOfPostingEnvironmentStep   no-op (no ChangeInfo/proxy yet)  │
   │  VideoPreparationStep            transcode + push + media scan    │
   │  MobileUIAutomationStep          MobileRun TCP drives Instagram   │
   │  VerificationStep                two-level (immediate + delayed)  │
   │  CleanupStep                     release device + audit event    │
   └──────────────────────────────────────────────────────────────────┘
                                            │
                                            ▼
                automation.job_events  +  $ARTIFACTS_DIR/mobile_ui/<job_id>/
                  (status transitions,   (screenshots, UI tree dumps)
                   artifacts refs)
```

The launcher (`scheduler/launcher.py`) runs the same pipeline for many jobs in
parallel; it is intentionally **not** the path used during pre-account-login
validation.

---

## 3. Main components

| Component | Where | What it does |
|---|---|---|
| Supabase `automation` schema | `supabase/migrations/*.sql` | Videos, jobs, accounts, environments, physical_devices, job_events, error_catalog, helper functions (`create_publishing_job`, `reserve_next_video`, `find_eligible_account`, `process_job_error`, `transition_job_status`). |
| Scheduler CLI | `src/scheduler/cli.py` | Operator entry point: status, create-job, run-job, run-launcher, cleanup-job, seed-validation-video, discover-devices, sync-drive. |
| Job launcher | `src/scheduler/launcher.py` | Async loop that dispatches many jobs through the pipeline. Supports `--max-parallel` override and `--max-jobs` stop-after-N. |
| Pipeline | `src/scheduler/pipeline.py` | Glue between launcher and worker steps. Owns state transitions, heartbeats, error routing. |
| Worker steps | `src/worker/steps/{video_preparation,mobile_ui_automation,verification}.py` | The actual posting flow. |
| MobileWorker session | `src/worker/session/{interface,types,mobilerun_adapter}.py` | `MobileWorker` abstract base + `MobilerunWorker` implementation that talks to MobileRun TCP / GenFarmer. |
| Worker tools | `src/worker/tools/{_adb,_ui,instagram,video,device}.py` | Lower-level primitives: ADB shell/push/tap, UI parsing, Instagram-specific helpers, video prep. |
| MobileRun config | `config/mobilerun/{config.yaml,app_cards/,platform_defaults.yaml,shopaikey_models.yaml}` | What MobileRun is told about Instagram and the LLM router. |
| Validation videos | `automation.videos` with `category='validation'` + `download_method='local_validation'` | Excluded from the generic queue (see migration `20260603130000`). |
| Artifacts | `$ARTIFACTS_DIR/mobile_ui/<job_id>/` | Screenshots and UI tree dumps for hard stops + on_error. |
| Runbook docs | `docs/ops/` | Operational runbooks (rollout, smoke test, MobileRun setup). |

---

## 4. Key directories

```
supabase/migrations/   schema, functions, error catalog, RLS — apply in order
src/scheduler/         CLI, launcher, pipeline, hashtag pool
src/worker/            MobileWorker, steps, tools, PoC scripts
scripts/               operational scripts (discover-devices, sync-drive,
                       seed-validation-video, seed-validation-accounts,
                       check_mobilerun_*, import_physical_devices)
config/mobilerun/      MobileRun config + Instagram AppCard
docs/architecture.md   system overview, invariants, high-level flow
docs/contracts/        environment, video source, worker step contract,
                       job state machine, retry/failure policy
docs/ops/              mvp-rollout-runbook, mobilerun-setup, runtime-smoke
docs/research/         GenFarmer/GenRouter/MockGPS API notes, MobileRun map
docs/decisions/        ADRs (0001 baseline, 0002 MobileRun-first)
docs/reports/          incident/runtime reports (created on demand)
.artifacts/            default $ARTIFACTS_DIR (gitignored)
trajectories/          MobileRun trajectory dumps (gitignored)
tests/                 pytest suites + bash integration tests
```

---

## 5. Environment setup

Copy `.env.example` to `.env` on the host machine and fill in the values
locally. **Never commit `.env`**, service-account JSON, model keys, or
GenFarmer credentials. The single source of truth is
[`docs/contracts/environment.md`](docs/contracts/environment.md); the
per-variable contract there overrides anything below.

### A. Required for any DB operation
- `SUPABASE_DB_URL` — direct Postgres DSN used by the runtime CLI.
- `PYTHONPATH=src` — so `python -m scheduler.cli ...` resolves.

### B. Required for MobileRun-driven posting
- `MOBILERUN_CONFIG=config/mobilerun/config.yaml`
- `MOBILERUN_TRAJECTORIES_DIR=trajectories`
- `MOBILERUN_USE_TCP=1` — pin the TCP path (primary on the VPS).
- `GOOGLE_API_KEY` — ShopAIKey/Gemini-style key for MobileRun's LLM router.
- `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` — Anthropic-compatible router.
- `ADB_PATH` (or `ANDROID_HOME`) — resolves to a working `adb`.
- `FFMPEG_PATH` / `FFPROBE_PATH` — required when validation videos need
  transcode for Android.

### C. Required for Drive ingestion
- `GOOGLE_APPLICATION_CREDENTIALS` — absolute path to the Drive service-account
  JSON (kept out of git).
- `VIDEO_DOWNLOAD_DIR` (default `./.artifacts/videos`).

### D. Optional / observability
- `ARTIFACTS_DIR` (default `./.artifacts`) — root for screenshots, UI dumps.
- `SCREENSHOTS_DIR` (default `./.artifacts/screenshots`).
- `LOG_LEVEL` (default `info`).
- `JOB_HEARTBEAT_INTERVAL_SECONDS`, `JOB_TIMEOUT_SECONDS` — override per-job
  heartbeat policy.

### E. Management API fallback (no DB password available)
- `SUPABASE_PAT` — personal access token from `supabase.com/dashboard/account/tokens`.
- `SUPABASE_PROJECT_REF` — the `<ref>` in `<ref>.supabase.co`.
- Pass `--via-management-api --project-ref <ref>` to CLI commands that support
  it (status, create-job, seed-validation-video, discover-devices, import_physical_devices).
- **Not supported** for `run-launcher`, `run-job`, or `cleanup-job` — those
  need a persistent direct connection.

### F. Deferred (do not set in the current MVP)
- `GENFARMER_BASE_URL`, `GENROUTER_BASE_URL`, `PROXY_*`, `APPIUM_BASE_URL`,
  `FARM_*`. The current proof_of_posting path uses MobileRun TCP at the
  default loopback URL; the other proxies/farms are scaffolding for later
  stages.

---

## 6. Setup commands

### 6.1 First-time pull / install (Windows VPS, PowerShell)

```powershell
cd C:\fffbt
git pull origin main
# Python venv lives at .venv\Scripts\python.exe; rebuild only if missing
.\.venv\Scripts\python.exe -m pip install -r scripts\requirements.txt
.\.venv\Scripts\python.exe scripts\check_mobilerun_setup.py
adb devices -l
```

### 6.2 Linux / macOS development

```bash
cd ~/code/fffbt
python3 -m venv .venv && source .venv/bin/activate
pip install -r scripts/requirements.txt
pip install pytest
adb devices -l
python scripts/check_mobilerun_setup.py
```

### 6.3 Tests

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m pytest tests\worker -q
.\.venv\Scripts\python.exe -m pytest tests\scheduler -q
```

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests -q
```

Some integration tests need Docker (they spin up an ephemeral Postgres 17
container — see `tests/scheduler/test_launcher.py`) or a real connected device
(`tests/devices/test_discovery_e2e.sh`, `tests/migrations/smoke_test.sh`).
Those are marked `@pytest.mark.integration` or live in bash files; CI is
expected to skip them when the prerequisites are missing.

---

## 7. MobileRun-first rule

**Instagram UI control must go through MobileRun TCP.** The runtime safety
contract is:

- Normal `proof_of_posting` UI actions (tap, swipe, type) **must not** use raw
  ADB. `MobileUIAutomationStep` constructs `MobilerunWorker(adb_fallback=False,
  use_tcp=True)`; if any step logs an `adb_fallback` action the result is
  forced to `needs_review` (`code='unknown_screen'`).
- ADB is allowed only for:
  - device connection (`adb connect`, `adb devices`)
  - app lifecycle (`am force-stop`, `am start`, `monkey -p ...`)
  - file push + MediaScanner (`adb push`, broadcast `android.intent.action.MEDIA_SCANNER_SCAN_FILE`)
  - diagnostics (`dumpsys`, `wm size`)
  - keyboard input only via the MobileRun Portal IME or `ADB_INPUT_B64`
    broadcast (validated paths in `src/worker/tools/instagram.py`).
- The MobileRun preflight (`scripts/check_mobilerun_setup.py`) must show
  `source=mobilerun_tcp`, `use_tcp=true`, `adb_fallback=false`.

If any new code wants to add raw ADB taps to a posting flow, that is a
deliberate policy change — gate it explicitly and document why.

---

## 8. Validation video workflow

Validation videos are MP4s seeded into `automation.videos` with
`category='validation'` and `download_method='local_validation'`. They are
**excluded from the generic queue** (`reserve_next_video()` filters them out
— see migration `20260603130000_reserve_next_video_skip_validation.sql`) and
can only be picked up by **targeted** `create-job` with `--video-id`.

### 8.1 Put the MP4 somewhere stable

```
C:\fffbt\artifacts\validation_videos\test_001.mp4
```

The absolute path becomes the identity key — do not move or rename it after
seeding.

### 8.2 Seed it

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe scripts\seed_validation_video.py `
    "C:\fffbt\artifacts\validation_videos\test_001.mp4"
# or via CLI passthrough:
.\.venv\Scripts\python.exe -m scheduler.cli seed-validation-video `
    "C:\fffbt\artifacts\validation_videos\test_001.mp4"
```

The script is idempotent: re-running on the same file refreshes the row in
place (same `video_id`, no duplicate).

### 8.3 Create a targeted job

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m scheduler.cli create-job `
    --device-serial <SERIAL> `
    --video-id <VIDEO_ID> `
    --account-id <ACCOUNT_ID> `
    --json
```

Notes:
- `--device-serial` accepts a USB serial, an `ip:port` connect target, or a
  bare Tailscale IPv4. The targeted SQL matches the device by `adb_serial`,
  `adb_connect_target`, or `tailscale_ipv4`.
- `--account-id` is required to use a validation/placeholder account
  (`accounts.is_validation = true`); the targeted SQL only opts back in to
  validation accounts when this flag is set.
- Without `--video-id`, the targeted path falls back to the generic queue
  filter — validation videos will **not** be picked.

### 8.4 Run the job

```powershell
.\.venv\Scripts\python.exe -m scheduler.cli run-job <JOB_ID> `
    --mode proof_of_posting --json
```

`--mode proof_of_posting` forces the real worker steps (no stubs). Without
it `run-job` runs the stub pipeline.

---

## 9. Scheduler commands cheatsheet

All commands accept `--db-url` or read `SUPABASE_DB_URL` from the environment.
Commands that need a persistent direct connection are flagged below.

| Command | Purpose | Safe use | Don't |
|---|---|---|---|
| `status [--events N] [--json]` | Counts of jobs / devices / videos; optionally the N most recent events. | Anytime, read-only. | — |
| `discover-devices [--source adb\|heartbeat\|both] [--dry-run] [--reassign-serial]` | Reconcile `automation.physical_devices` with live ADB / heartbeat state, mark stale rows offline. | After connecting a new phone, after Tailscale IP churn. Always `--dry-run` first on production. | Pass `--reassign-serial` unless you know the same physical device's USB serial really changed. |
| `reconnect-devices` | `adb connect` against offline rows that have an `adb_connect_target`. | When Tailscale comes back. | — |
| `sync-drive` | Pull new MP4s from Google Drive and insert them as `videos.status='new'`. | When new content is uploaded. | Run as part of validation — use `seed-validation-video` instead. |
| `seed-validation-video <path>` | Idempotently seed a local MP4 as a validation video. | Anytime during validation. | Point it at a moving / temp path. |
| `create-job [--device-serial S] [--video-id V] [--account-id A] [--json]` | Create one job. Without `--device-serial` it calls `create_publishing_job()` (generic). With it, the targeted SQL pins one device + (optional) video + (optional) account. | Targeted validation. | Use generic creation when validating — it will pick from the production queue. |
| `cleanup-job <uuid> [--json]` | Release a stuck job's device + reserved video, transition the job to `needs_review`, audit-log a `manual_cleanup` event. (Direct DB only.) | After a worker crash leaves a job pinned with no heartbeat. | Use it as a substitute for actual error handling — first try to understand why it's stuck. |
| `run-job <uuid> --mode proof_of_posting [--log-level info]` | Run one job end-to-end through the real worker pipeline. (Direct DB only.) | Stage 0a / 1 / 2 of the runbook. | Use without `--mode proof_of_posting` — that runs the stub pipeline and does not post. |
| `run-launcher [--max-parallel N] [--max-jobs M]` | Run the launcher loop, dispatching N jobs in parallel, stopping after M terminal jobs. (Direct DB only.) | Stages 3+ of the runbook, after stages 0a–2 have passed twice without manual cleanup. | Start it before validation has passed. Run unbounded (no `--max-jobs`) for testing. |

---

## 10. Job statuses and what they mean

### `automation.videos.status`
| Value | Meaning |
|---|---|
| `new` | Available for pickup. |
| `reserved` | A job picked it up; not yet finished. |
| `uploading` / `verifying` | Set by the worker mid-pipeline. |
| `released` | Successfully posted at least once. The generic queue does not pick `released`. |
| `failed` | Terminal failure; not auto-retried. |
| `needs_review` | Human must look at it. |

### `automation.jobs.status`
| Value | Meaning |
|---|---|
| `queued` | Created, waiting for the launcher / `run-job`. |
| `preparing_device` | `environment_apply` is running. |
| `publishing` | `video_preparation` and `mobile_ui_automation` are running. |
| `verifying` | `verification` step is running. |
| `done` | Posted and verified. |
| `failed` | Terminal failure per `error_catalog.target_job_status`. |
| `needs_review` | Soft failure; the operator decides what to do. |
| `cancelled` | Manually cancelled. |

### `automation.physical_devices.status`
| Value | Meaning |
|---|---|
| `online` | Reachable via ADB / heartbeat, available for new jobs. |
| `busy` | Pinned to `current_job_id`. Discovery will not flip this. |
| `offline` | Not seen within `--stale-seconds` (default 120s). |
| `maintenance` | Operator-set; discovery will not touch it. |

---

## 11. Error codes and common failures

Defined in `automation.error_catalog` (see migrations `20260520110000`,
`20260520120000`, `20260527120000`, `20260603120000`). The pipeline routes
each one through `automation.process_job_error()` which decides retry vs
terminal vs needs_review based on the row.

| Code | Category | What it means | What to check | Retry safe? |
|---|---|---|---|---|
| `logged_out` | non_retryable, account → `disabled` | The phone showed Instagram's login screen. | Log into Instagram by hand on the phone, complete any 2FA, dismiss "Save your login info". | No — re-run only after a human login. |
| `login_challenge` | (not catalogued → falls back to `max_retries_default`) | Two-factor / verify-your-identity / security-code dialog. | Complete the challenge by hand on the phone before retrying. | No. |
| `account_suspended` | (not catalogued → falls back to `max_retries_default`) | "Account suspended" or "your account has been disabled" copy on screen. | Stop using the account. Escalate. | No. |
| `action_blocked` | non_retryable | Instagram's "Action blocked / try again later." | Wait at least 24h before any further action on that account. Check whether other accounts are also blocked. | No. |
| `trial_reels_unavailable` | non_retryable | Professional Dashboard or Trial Reels tile is missing. | Confirm the account is Professional and Trial Reels is enabled in-app. | No. |
| `final_ok_did_not_register` | needs_review | The top-right OK on the New reel screen was tapped, but the screen did not transition. | `artifacts.screenshot`/`ui_dump` referenced by the job_event; confirm OK coords match this device. | Manual rerun only after investigating. |
| `share_did_not_register` | needs_review | Legacy Reels share screen: bottom `share_button` was tapped but never disappeared. | Same as above — check screenshot/UI dump. Almost always fixed by switching to the Trial Reels share path. | Manual rerun only after investigating. |
| `caption_mismatch` | needs_review | Caption verification did not match what we typed. | Check IME state, MobileRun keyboard install, and Unicode handling. | Manual rerun only. |
| `trial_reels_gallery_not_reached` / `share_screen_not_reached` / `editor_next_not_reached` / `next_button_inactive` | needs_review | Navigation got stuck mid-way through the editor. | Screenshot + UI dump; almost always a layout change. | Manual rerun only. |
| `unknown_screen` | needs_review | UI did not match any known state, or a disallowed ADB fallback was used. | Screenshot + UI dump + driver actions log. | Manual rerun only. |
| `verification_failed` | needs_review | Two-level verification could not confirm the post. | Check trial reels list in-app; confirm the post is actually live before re-running. | Manual rerun only. |
| `INFRA` | retryable (3 retries) | DB / OS / network error. | Check connectivity, then let the launcher retry. | Yes. |
| `TIMEOUT` | retryable (2 retries) | Job exceeded `JOB_TIMEOUT_SECONDS`. | Step-level timing in `job_events`. | Yes, but investigate slow steps. |
| `UNKNOWN` | needs_review | Unhandled exception in the worker. | Stack trace in `job_events.payload.message`. | No — fix the underlying bug. |
| `HEARTBEAT_TIMEOUT` | needs_review | Worker stopped emitting heartbeats. | Check if the worker process crashed or was killed. | No without explanation. |

> **Catalog gap:** `login_challenge`, `account_suspended`, and
> `unexpected_destructive_dialog` are emitted by the worker
> (`src/worker/steps/mobile_ui_automation.py::_HARD_STOP_PATTERNS`) but not
> yet present in `automation.error_catalog`. They fall through to
> `max_retries_default`. Add them to the catalog before relying on per-code
> retry/account side effects.

---

## 12. Current MVP rollout stages

Full text in [`docs/ops/mvp-rollout-runbook.md`](docs/ops/mvp-rollout-runbook.md).

- **Stage 0 — readiness.** Devices online, MobileRun TCP preflight clean,
  one validation video seeded, one validation account seeded with
  `is_validation=true`.
- **Stage 0a — pre-account-login.** No Instagram account logged in. Verify
  the controlled stop: `proof_of_posting` should hit `logged_out`, persist a
  screenshot + UI dump under `$ARTIFACTS_DIR/mobile_ui/<job_id>/`, write a
  `job_events` row with `payload.artifacts`, and release device + video. No
  retries, no posting. **You are here.**
- **Stage 1 — one controlled happy-path proof.** After a human logs the
  account in: one targeted `run-job --mode proof_of_posting`. Expected
  outcome `done` with a real post.
- **Stage 2 — error-path proof.** Same shape as Stage 1 but on a phone /
  account where Trial Reels is unavailable. Confirms `trial_reels_unavailable`
  routes cleanly and cleanup releases resources.
- **Stage 3 — `run-launcher --max-parallel 1 --max-jobs 1`.** One job through
  the launcher loop end-to-end.
- **Stage 4 — `run-launcher --max-parallel 2 --max-jobs 2`.** Two devices in
  parallel.
- **Stage 5 — scale.** Only after Stage 4 passes twice in a row without
  manual cleanup. Adds more devices, accounts, and videos. Still bounded by
  `--max-jobs` until the heartbeat/timeout/retry policy has been observed
  under load.

---

## 13. What is deferred (not in the MVP path)

- **Change Device / device profile mutation** — and BackupRestoreV2 — would
  rewrite the phone's identity. Change Device is Android 12+ only and is
  deferred for production-hardening.
- **GPS required mode** (MockGPS injection per job).
- **Proxy / GenRouter** production wiring.
- **Account onboarding, profile setup, professional-mode switching.**
- **SMM follower ordering, 24–72h analytics decisions.**
- **Comments / DMs** automation.
- **Publishing successful Trial videos to the profile grid.**
- **Full 24/7 launcher** without `--max-jobs`.
- **Dashboard polish**, multi-device production quotas.

---

## 14. Operator safety rules

- Do **not** run `run-launcher` unbounded yet. Always pass `--max-jobs`.
- Do **not** use the Google Drive production queue for tests; seed validation
  videos with `seed-validation-video`.
- Do **not** mutate the legacy `fffbt` schema. It is reference-only.
- Do **not** store Instagram passwords in the repo or any CLI argument.
- Do **not** commit `.env`, service-account JSON, `.artifacts/`,
  `trajectories/`, or any phone screenshot containing a logged-in account.
- Do **not** run BackupRestoreV2 / ChangeInfo without explicit approval.
- Do **not** bypass `logged_out`, `login_challenge`, `action_blocked`,
  `account_suspended`. The worker hard-stops on these on purpose.
- For destructive or shared-state operations (force-pushes, `db reset`,
  `cleanup-job` on a job you didn't open), confirm first.

---

## 15. Where to look for deeper docs

- [`docs/architecture.md`](docs/architecture.md) — system overview,
  invariants, high-level flow.
- [`docs/ops/mvp-rollout-runbook.md`](docs/ops/mvp-rollout-runbook.md) —
  staged rollout plan (current).
- [`docs/ops/mobilerun-setup.md`](docs/ops/mobilerun-setup.md) — how to
  install and verify MobileRun on the VPS.
- [`docs/ops/runtime-smoke-test-runbook.md`](docs/ops/runtime-smoke-test-runbook.md) —
  per-runtime-function smoke tests.
- [`docs/contracts/environment.md`](docs/contracts/environment.md) —
  per-variable contract for `.env`.
- [`docs/contracts/account-environment-application.md`](docs/contracts/account-environment-application.md)
- [`docs/contracts/instagram-worker-state-machine.md`](docs/contracts/instagram-worker-state-machine.md)
- [`docs/contracts/job-state-machine.md`](docs/contracts/job-state-machine.md)
- [`docs/contracts/retry-failure-policy.md`](docs/contracts/retry-failure-policy.md)
- [`docs/contracts/video-source.md`](docs/contracts/video-source.md)
- [`docs/contracts/worker-step-contract.md`](docs/contracts/worker-step-contract.md)
- [`docs/research/mobilerun-real-repo-task-map.md`](docs/research/mobilerun-real-repo-task-map.md)
- [`docs/research/genfarmer-operator-checklist.md`](docs/research/genfarmer-operator-checklist.md),
  [`docs/research/genrouter-operator-checklist.md`](docs/research/genrouter-operator-checklist.md)
- [`docs/decisions/0001-architecture-baseline.md`](docs/decisions/0001-architecture-baseline.md),
  [`docs/decisions/0002-mobilerun-first-worker.md`](docs/decisions/0002-mobilerun-first-worker.md)

---

## 16. Current blocker

The available test phone is logged out of Instagram. Until a human logs the
account in and confirms Trial Reels access in-app:

- `proof_of_posting` will stop on `logged_out`. That is expected and
  intentional.
- The hard stop persists `hard_stop_logged_out_<ts>.png` and
  `hard_stop_logged_out_<ts>.ui.json` under
  `$ARTIFACTS_DIR/mobile_ui/<job_id>/` and writes their paths into
  `automation.job_events.payload.artifacts` for the failing step.
- The device returns to `online` and (for the retryable codes) the video
  rolls back to `new`. Validation video rows for non-retryable codes stay
  `reserved` — clear them with `cleanup-job` before re-running.
- Do **not** start the launcher. Do **not** auto-create accounts. Do
  **not** publish.

Once a human has logged the account in:

1. Re-seed/refresh the validation video if it was moved.
2. `create-job --device-serial <serial> --video-id <uuid> --account-id <uuid>`.
3. `run-job <id> --mode proof_of_posting`.
4. Expected outcome: `done` with a real post visible in the Trial Reels
   list. Then proceed to Stage 1 of the rollout runbook.
