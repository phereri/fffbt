# Runtime smoke-test runbook

- Status: draft (MVP)
- Owner: Architect / Tech Lead
- Audience: a single human operator
- Last updated: 2026-05-22

This runbook verifies each MVP runtime function **independently**, before any
full automation run. Follow it top to bottom. Part A is local-machine testing,
Part B is VPS testing, Part C is the per-function smoke tests.

> **Read the safety section (Part D) before running anything.** The worker
> steps are still stubs and the launcher is scaffolding — see the warnings.

---

## How the runtime is invoked

All runtime functions are reachable two ways:

- The **CLI**: `python -m scheduler.cli <command>` (run from `src/`, or with
  `PYTHONPATH=src`). Commands: `discover-devices`, `reconnect-devices`,
  `sync-drive`, `create-job`, `run-launcher`, `run-job`, `status`.
- The **scripts** directly: `scripts/import_physical_devices.py`,
  `scripts/discover_physical_devices.py`, `scripts/reconnect_devices.py`,
  `scripts/sync_drive_videos.py`.

The CLI `discover-devices` / `reconnect-devices` / `sync-drive` commands just
forward to the matching script, so either form works. `import-devices` has no
CLI wrapper — call the script directly.

Every DB-touching command takes a connection either via `--db-url`
(or env `SUPABASE_DB_URL`) **or** `--via-management-api` with `SUPABASE_PAT`
and `--project-ref`. `run-launcher` and `run-job` require a direct `--db-url`
(the Management API has no persistent connections).

---

## Part A — Local machine setup

### A.1 Required environment variables

Copy the template and fill it in:

```
cp .env.example .env
git check-ignore .env        # must print ".env"
```

Per-variable contract: [`docs/contracts/environment.md`](../contracts/environment.md).
The minimum for local smoke testing:

| Variable | Needed for |
|---|---|
| `SUPABASE_DB_URL` | every DB command (point at a **local** Postgres — see A.4) |
| `GOOGLE_APPLICATION_CREDENTIALS` | Drive sync only |
| `VIDEO_DOWNLOAD_DIR` | Drive sync (defaults to `./.artifacts/videos`) |
| `ADB_BIN` / `ANDROID_HOME` | device discovery / reconnect (real ADB) |
| `LOG_LEVEL` | optional, defaults to `info` |

Load the file into your shell before running commands:

```
set -a; source .env; set +a
```

### A.2 Install dependencies

```
# Python tooling for scripts + scheduler
python3 -m venv .venv && source .venv/bin/activate
pip install -r scripts/requirements.txt   # psycopg, google-auth, google-api-python-client
pip install pytest                        # only for the pytest-based tests

# Node tooling (Supabase CLI) — optional, only if you use `supabase` locally
npm install
```

Also required on the host: `docker` and `psql` (the test harnesses spin up
throwaway Postgres containers and talk to them with `psql`).

### A.3 Run the test suite

The repo has three kinds of tests. Run them from the repo root.

```
# 1. Pure-logic unit tests (no DB, no docker)
python -m unittest tests.devices.test_discovery
python -m unittest tests.devices.test_reconnect

# 2. Shell integration tests (each boots its own throwaway Postgres 17 container)
bash tests/migrations/smoke_test.sh
bash tests/scheduler/test_create_publishing_job.sh
bash tests/scheduler/test_account_eligibility.sh
bash tests/scheduler/test_video_reservation.sh
bash tests/scheduler/test_device_reservation.sh
bash tests/scheduler/test_process_job_error.sh
bash tests/devices/test_discovery_e2e.sh

# 3. Pytest integration tests (also use ephemeral Postgres containers)
PYTHONPATH=src pytest tests/scheduler/test_cli.py tests/scheduler/test_launcher.py tests/scheduler/test_pipeline.py
```

Expected: every suite ends with `PASS` (shell) or `passed` (pytest). Any
failure here must be fixed before continuing — the smoke tests below assume a
green suite.

### A.4 Set up a local Postgres and verify Supabase connectivity

**Do not point `SUPABASE_DB_URL` at the production project for smoke testing.**
Use a throwaway local container that mirrors the schema:

```
docker run -d --name fffbt_local -e POSTGRES_PASSWORD=postgres \
    -p 5432:5432 postgres:17-alpine
until docker exec fffbt_local pg_isready -U postgres; do sleep 1; done

# Apply every migration in name order, skipping the Supabase-only remote schema
export PGPASSWORD=postgres
PSQL="psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -v ON_ERROR_STOP=1"
for f in $(ls -1 supabase/migrations/*.sql | sort); do
    case "$f" in *_remote_schema.sql) continue;; esac
    $PSQL -f "$f"
done
$PSQL -f supabase/seed.sql

export SUPABASE_DB_URL="postgresql://postgres:postgres@127.0.0.1:5432/postgres"
```

Verify connectivity and that the `automation` schema is live:

```
PYTHONPATH=src python -m scheduler.cli status
```

Expected output — three sections, all empty on a fresh DB:

```
Jobs:
  (none)
Devices:
  (none)
Videos:
  (none)
```

If you see this, the runtime can reach the DB and the schema is correct.

### A.5 How to avoid touching production

- Local smoke testing runs **only** against the local container above. Keep
  the production `SUPABASE_DB_URL` out of `.env` while smoke testing.
- Before every DB command, confirm the target:
  `echo "$SUPABASE_DB_URL"` — it must contain `127.0.0.1` / `localhost`.
- Prefer `--dry-run` first on every script that supports it (all four scripts
  and `discover-devices` do).
- Never run `run-launcher` against a shared/production DB — see Part D.
- Tear the container down when finished: `docker rm -f fffbt_local`.

---

## Part B — VPS setup

The VPS runs the same code against the real Supabase `automation` schema.
Treat it as pre-production: schema is real, but with **no production Instagram
accounts** until explicitly approved (Part D).

### B.1 Required environment variables

Set these in the service env file (e.g. `/etc/fffbt/fffbt.env`), **not** in a
Multica custom env:

| Variable | Notes |
|---|---|
| `SUPABASE_URL` | `https://<ref>.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | server-only secret |
| `SUPABASE_DB_URL` | direct Postgres URL, must include `sslmode=require` |
| `GOOGLE_APPLICATION_CREDENTIALS` | absolute path to the service-account JSON |
| `GENFARMER_BASE_URL` / `GENROUTER_BASE_URL` | device/proxy backends |
| `ANDROID_HOME`, `ADB_BIN`, `APPIUM_BASE_URL` | Android automation |
| `VIDEO_DOWNLOAD_DIR`, `SCREENSHOTS_DIR`, `ARTIFACTS_DIR` | artifacts |
| `MODEL_PROVIDER`, `MODEL_API_KEY` | LLM provider |

Optional, for Management-API mode instead of a direct DB connection:
`SUPABASE_PAT` + `SUPABASE_PROJECT_REF`.

Full contract: [`docs/contracts/environment.md`](../contracts/environment.md).

### B.2 Required paths

- **Code**: the `fffbt` checkout, with `src/` importable
  (`PYTHONPATH=/opt/fffbt/src` or run from `src/`).
- **Videos**: `VIDEO_DOWNLOAD_DIR` — writable by the ingestion process,
  readable by the worker.
- **Artifacts / logs**: `ARTIFACTS_DIR` and `SCREENSHOTS_DIR` — created and
  writable by the runtime user. Both are under `.artifacts/` by default and
  are gitignored.
- **Google service-account key**: placed per
  [`docs/setup/credentials.md`](../setup/credentials.md) —
  `/etc/fffbt/credentials/google-drive.json`, mode `0640`, bind-mounted only.

### B.3 Android SDK / ADB checks

```
echo "$ANDROID_HOME"           # must be set
adb version                    # confirms adb is on PATH (or set ADB_BIN)
adb devices -l                 # lists devices reachable from the VPS
```

Expected: `adb version` prints a version banner; `adb devices` lists the
phones connected over ADB-over-TCP / Tailscale. An empty device list is fine
for the dry-run smoke tests but blocks the real discovery/reconnect runs.

### B.4 Google service account check

```
sudo -u fffbt test -r "$GOOGLE_APPLICATION_CREDENTIALS" && echo "key readable"
```

Expected: `key readable`. The runtime uses the Google SDK default-credentials
path — do not open or print the JSON. See
[`docs/setup/credentials.md`](../setup/credentials.md).

### B.5 Artifacts / logs directories

```
mkdir -p "${ARTIFACTS_DIR:-.artifacts}" "${SCREENSHOTS_DIR:-.artifacts/screenshots}" \
         "${VIDEO_DOWNLOAD_DIR:-.artifacts/videos}"
```

The launcher and pipeline emit structured JSONL events to **stderr** (one JSON
object per line); redirect stderr to a log file when running as a service.

### B.6 Supabase Management API vs service role

- The runtime uses the **direct `SUPABASE_DB_URL`** connection for all
  commands. `run-launcher` and `run-job` *require* it.
- `--via-management-api` (with `SUPABASE_PAT` + `SUPABASE_PROJECT_REF`) is a
  fallback for the stateless commands (`status`, `create-job`, `sync-drive`,
  `discover-devices`, `reconnect-devices`, `import_physical_devices.py`) when a
  direct DB connection is not available.
- `SUPABASE_SERVICE_ROLE_KEY` is for server-side Supabase API access; it is
  **server-only** and must never reach a dashboard frontend or a device.

---

## Part C — Manual smoke tests

Run these one at a time, in order. Each lists the **exact command**, the
**expected output**, the **expected DB changes**, **how to verify in Supabase**,
**rollback/cleanup**, and **common errors**.

For all verification SQL below, connect with
`psql "$SUPABASE_DB_URL"` (local) or the Supabase SQL editor (VPS).

A useful at-a-glance check after any step:

```
PYTHONPATH=src python -m scheduler.cli status --events 10
```

### C.1 Device import check

Imports a Tailscale "Devices" CSV export into `automation.physical_devices`.

```
# Dry run first — classifies each row, writes nothing
python scripts/import_physical_devices.py --csv /path/to/tailscale-devices.csv --dry-run

# Real import
python scripts/import_physical_devices.py --csv /path/to/tailscale-devices.csv
```

- **Expected output**: a per-row insert/update plan; a final count of
  inserted/updated devices.
- **Expected DB changes**: rows upserted into `automation.physical_devices`,
  every imported device with `status = 'offline'`, `tailscale_ipv4` and
  `adb_connect_target` populated.
- **Verify in Supabase**:
  ```sql
  SELECT alias, status, tailscale_ipv4, adb_connect_target
  FROM automation.physical_devices ORDER BY alias;
  ```
- **Rollback/cleanup**: `DELETE FROM automation.physical_devices WHERE ...;`
  (local container only — never delete VPS device rows that other steps rely on).
- **Common errors**:
  - *CSV column missing* — export must be the standard Tailscale Devices CSV
    (`Device name, Device ID, OS, OS Version, Tailscale IPs, Last seen`).
  - *No IPv4 extracted* — a device with only an IPv6 Tailscale address; expected,
    that device just has a null `tailscale_ipv4`.

### C.2 Device discovery check

Reconciles `physical_devices` rows with live ADB / heartbeat state.

```
# Dry run — prints the plan, rolls back
PYTHONPATH=src python -m scheduler.cli discover-devices --source heartbeat --dry-run

# Real run
PYTHONPATH=src python -m scheduler.cli discover-devices --source both
```

- **Expected output**: per-device decisions (online ↔ offline flips, serial
  backfills); a summary line.
- **Expected DB changes**: `physical_devices.status` flips for devices whose
  live state changed; a `device_events` row (`connected` / `disconnected`) per
  flip; `adb_serial` backfilled where discovered. `busy` devices are **never**
  touched.
- **Verify in Supabase**:
  ```sql
  SELECT status, count(*) FROM automation.physical_devices GROUP BY status;
  SELECT device_id, event_type, created_at FROM automation.device_events
  ORDER BY created_at DESC LIMIT 10;
  ```
- **Rollback/cleanup**: none needed — discovery is idempotent; a re-run with
  the same live state emits no duplicate events.
- **Common errors**:
  - *adb not found* — set `ADB_BIN` or add `adb` to `PATH`; or use
    `--source heartbeat` to skip ADB.
  - *No `public.device_heartbeats` table* — heartbeat source needs that table
    to exist; use `--source adb` if heartbeats are not wired up.

### C.3 ADB reconnect check

Reconnects offline devices via `adb connect <ip>:5555`.

```
# Dry run — prints planned actions, runs no adb, touches no DB
PYTHONPATH=src python -m scheduler.cli reconnect-devices --all --dry-run

# Single device, real run
PYTHONPATH=src python -m scheduler.cli reconnect-devices --device <alias-or-id>
```

- **Expected output**: per-device `adb connect` result (connected / failed).
- **Expected DB changes**: on success, the device's `status` moves toward
  `online` and a `device_events` row is recorded. None on dry run.
- **Verify in Supabase**:
  ```sql
  SELECT alias, status, last_seen_at FROM automation.physical_devices
  WHERE status = 'online';
  ```
- **Rollback/cleanup**: none — reconnecting is safe and idempotent.
- **Common errors**:
  - *No `adb_connect_target`* — the device row has no reconnect target; fix via
    a re-import (C.1).
  - *Timeout* — phone unreachable over Tailscale; raise `--adb-timeout` or check
    the device is powered on and on the tailnet.

### C.4 Google Drive sync check

Scans `instagram/<category>/videos/*.mp4` in Drive and registers new videos.

```
# Dry run — discovers and reports, no DB write, no download
PYTHONPATH=src python -m scheduler.cli sync-drive --dry-run --verbose

# Register metadata only, skip the file download
PYTHONPATH=src python -m scheduler.cli sync-drive --skip-download

# Full sync
PYTHONPATH=src python -m scheduler.cli sync-drive
```

- **Expected output**: a list of discovered videos with category and Drive file
  id; an inserted-count summary.
- **Expected DB changes**: new rows in `automation.videos` with
  `status = 'new'`; download columns populated on a full (non-`--skip-download`)
  run; `.mp4` files written under `VIDEO_DOWNLOAD_DIR`.
- **Verify in Supabase**:
  ```sql
  SELECT status, count(*) FROM automation.videos GROUP BY status;
  SELECT filename, category, status FROM automation.videos
  ORDER BY created_at DESC LIMIT 10;
  ```
- **Rollback/cleanup**: `DELETE FROM automation.videos WHERE status = 'new';`
  on the local container; delete files from `VIDEO_DOWNLOAD_DIR` as needed.
- **Common errors**:
  - *Credentials error* — `GOOGLE_APPLICATION_CREDENTIALS` unset or unreadable;
    see [`docs/setup/credentials.md`](../setup/credentials.md).
  - *No 'instagram' folder found* — the service account lacks access to the
    Drive tree, or the folder layout differs from
    [`docs/contracts/video-source.md`](../contracts/video-source.md).

### C.5 `create_publishing_job` check

Calls `automation.create_publishing_job()` — reserves one video + one eligible
account + one free device into a new job.

```
PYTHONPATH=src python -m scheduler.cli create-job --json
```

- **Expected output (success)**:
  `job created: id=... video=... account=... device=...`, or the full row with
  `--json`.
- **Expected output (no resources)**:
  `no job created — no eligible video, account, or device available.`
  (exit code 1) — this is normal when the DB has no `new` videos / eligible
  accounts / `online` devices.
- **Expected DB changes**: one row in `automation.jobs` (`status = 'queued'`);
  the chosen video → `status = 'reserved'`; the chosen device → `status =
  'busy'` with `current_job_id` set; a `job_events` row (`created` →
  `queued`) and a `device_events` row (`job_assigned`).
- **Verify in Supabase**:
  ```sql
  SELECT id, status, video_id, account_id, device_id FROM automation.jobs
  ORDER BY created_at DESC LIMIT 5;
  SELECT event_type, to_status FROM automation.job_events
  WHERE job_id = '<job-id>';
  ```
- **Prerequisite**: needs at least one `new` video (C.4), one `active` account
  with a full environment, and one `online` device (C.1–C.3). On a bare local
  container, seed fixtures the same way `tests/scheduler/test_create_publishing_job.sh`
  does, or run C.1–C.4 first.
- **Rollback/cleanup** (local container):
  ```sql
  UPDATE automation.jobs SET status = 'cancelled', finished_at = now()
  WHERE id = '<job-id>';
  UPDATE automation.videos SET status = 'new' WHERE id = '<video-id>';
  UPDATE automation.physical_devices SET status = 'online', current_job_id = NULL
  WHERE id = '<device-id>';
  ```
- **Common errors**:
  - *Returns NULL with resources present* — the account may be on cooldown or
    over its daily cap, or already has an active job; see
    [`docs/contracts/job-state-machine.md`](../contracts/job-state-machine.md).

### C.6 Run launcher in safe / stub mode

Starts the async job launcher loop. **All worker steps are stubs** — jobs walk
the full pipeline but each step returns OK without real device automation.

```
PYTHONPATH=src python -m scheduler.cli run-launcher --log-level info
```

- **Expected output**: a `WARNING` that worker steps are stubs, then JSONL
  events on stderr (`launcher_start`, `settings_loaded`, `job_created`,
  `worker_done`, `no_resources`, …). Stop it with `Ctrl-C`; it drains active
  jobs and prints a `summary` line.
- **Expected DB changes**: it repeatedly calls `create_publishing_job()` and
  runs each job through the stub pipeline to `done` (videos `reserved` →
  consumed, devices cycled `busy` → `online`, `job_events` heartbeats logged).
- **Verify in Supabase**:
  ```sql
  SELECT status, count(*) FROM automation.jobs GROUP BY status;
  ```
- **Run it ONLY against the local container** unless a real worker is plugged
  in — see Part D.
- **Rollback/cleanup**: stop the launcher; for the local container, reset with
  the C.5 cleanup SQL or just recreate the container.
- **Common errors**:
  - *`SUPABASE_DB_URL is not set`* — `run-launcher` needs a direct DB URL; the
    Management API is not supported here.
  - *Loops on `no_resources`* — expected when nothing is queued; seed videos/
    accounts/devices first.

### C.7 Single job through the generic pipeline (stub mobile step)

Runs one existing job through the worker pipeline once, without the launcher
loop. Steps are the same stubs as C.6.

```
# First create a job (C.5) and copy its id, then:
PYTHONPATH=src python -m scheduler.cli run-job <job-id> --log-level info
```

- **Expected output**: JSONL events — `pipeline_start`, `step_start`/`step_done`
  for `environment_apply` → `video_preparation` → `mobile_ui_automation` →
  `verification`, then `cleanup`, ending in `pipeline_done`.
- **Expected DB changes**: the job transitions
  `queued → preparing_device → publishing → verifying → done`; a `job_events`
  heartbeat row per step; the device is released (`status = 'online'`,
  `current_job_id = NULL`).
- **Verify in Supabase**:
  ```sql
  SELECT status FROM automation.jobs WHERE id = '<job-id>';
  SELECT event_type, payload->>'step', payload->>'status'
  FROM automation.job_events WHERE job_id = '<job-id>' ORDER BY created_at;
  ```
- **Rollback/cleanup**: a job that reached `done` is terminal — for the local
  container, recreate it or use a fresh job for the next run.
- **Common errors**:
  - *`job not found`* — wrong id, or the job is in another DB.
  - *Transition error* — the job was not in `queued`; `run-job` expects a fresh
    queued job. See [`docs/contracts/job-state-machine.md`](../contracts/job-state-machine.md).

### C.8 Mobilerun one-device PoC check

The Mobilerun-based Instagram worker is the planned MVP executor (ADR 0002),
behind the shared `MobileWorker` interface. **It is not yet implemented as a
runnable command** — the `mobile_ui_automation` and `verification` steps in
C.6/C.7 are stubs.

Until the `MobileWorker` lands (FFF-28), the "PoC check" is documentation-only:

- Confirm the operator can reach the Mobilerun environment for one device and
  has reviewed the references below.
- Read [`docs/decisions/0002-mobilerun-first-worker.md`](../decisions/0002-mobilerun-first-worker.md)
  and [`docs/instagram-appcard-reference.md`](../instagram-appcard-reference.md).
- Confirm `mode` stays `proof_of_posting` (the only runnable mode — Part D).

When the real Mobilerun step exists, this section will gain an exact command,
expected AppCard output, and the screenshot/artifact verification path. Until
then, treat C.8 as a **prerequisites-only** checkpoint, not an executable test.

---

## Part D — Safety warnings

Read before running anything in Part C.

- **The launcher is still scaffolding.** `run-launcher` and `run-job` execute
  the full pipeline, but every worker step (`environment_apply`,
  `video_preparation`, `mobile_ui_automation`, `verification`) is a **stub**
  that returns OK immediately. A job reaching `done` means *the pipeline ran*,
  **not** that anything was posted.
- **Do not run against a live queue without a real worker.** Running
  `run-launcher` against a shared or production DB while steps are stubs will
  march real jobs to `done` without posting anything — corrupting queue state.
  Use the local throwaway container for C.6/C.7.
- **Do not run ChangeInfo / device-profile mutation.** The GenFarmer ChangeInfo
  / device-profile mutation path is hazardous and is **forbidden** in
  `proof_of_posting` mode. Do not trigger it manually. See
  [`docs/research/genfarmer-changeinfo-hazard.md`](../research/genfarmer-changeinfo-hazard.md).
- **Proxy is deferred.** Proxy application is best-effort / deferred for the
  current phase; a missing or failed proxy must not block a smoke run.
- **`proof_of_posting` mode only.** It is the only runnable operation mode
  right now — it forbids the hazardous steps. Do not switch to `mvp` or
  `production` mode. See
  [`docs/contracts/account-environment-application.md`](../contracts/account-environment-application.md).
- **No production Instagram accounts** unless explicitly approved. Smoke
  testing uses local fixtures or throwaway accounts only. Never log into a
  real account during C.1–C.8.
- **Never commit secrets.** `.env`, the Google key, tokens, and real device
  serials stay out of git, issues, and comments.

---

## Reference documents

Deeper detail lives in these contracts and research notes — this runbook links
rather than duplicates.

- Architecture overview — [`docs/architecture.md`](../architecture.md)
- Environment variables contract — [`docs/contracts/environment.md`](../contracts/environment.md)
- Account environment application contract — [`docs/contracts/account-environment-application.md`](../contracts/account-environment-application.md)
- Job state machine — [`docs/contracts/job-state-machine.md`](../contracts/job-state-machine.md)
- Video source contract — [`docs/contracts/video-source.md`](../contracts/video-source.md)
- Worker step contract — [`docs/contracts/worker-step-contract.md`](../contracts/worker-step-contract.md)
- Retry / failure policy — [`docs/contracts/retry-failure-policy.md`](../contracts/retry-failure-policy.md)
- Instagram worker state machine — [`docs/contracts/instagram-worker-state-machine.md`](../contracts/instagram-worker-state-machine.md)
- Google Drive credentials setup — [`docs/setup/credentials.md`](../setup/credentials.md)
- GenFarmer research — [`api`](../research/genfarmer-api.md),
  [`operator checklist`](../research/genfarmer-operator-checklist.md),
  [`ChangeInfo hazard`](../research/genfarmer-changeinfo-hazard.md),
  [`asar findings`](../research/genfarmer-asar-findings.md),
  [`renderer findings`](../research/genfarmer-renderer-findings.md)
- GenRouter operator checklist — [`docs/research/genrouter-operator-checklist.md`](../research/genrouter-operator-checklist.md)
- MockGPS integration — [`docs/research/mockgps-integration.md`](../research/mockgps-integration.md)
- Mobilerun / AppCards — ADR [`0002-mobilerun-first-worker.md`](../decisions/0002-mobilerun-first-worker.md),
  [`docs/instagram-appcard-reference.md`](../instagram-appcard-reference.md)
