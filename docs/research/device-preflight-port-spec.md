# Device preflight — port specification

- Status: design (algorithm distilled, no code yet)
- Source: `farm/device_preflight.py` from the 2026-06-06 real-repo snapshot
  (`fffbt-mobilerun (1)`), ~545 LOC. **Do not copy verbatim** — the source
  imports `farm.mockgps_vn.FLEET_MOCK_BY_SERIAL` (a hardcoded account →
  cafe-pin map), `farm.adb_app_launch`, `farm.uiautomator_tree`, and
  writes reboot history to `logs/.portal_reboot_state.json`. The FFFBT
  port replaces all of those with the `automation` schema and the
  `MobileWorker` interface.
- Target FFFBT issues: FFF-31 (publish flow preflight), FFF-25 (GPS apply),
  feeds into the environment-apply step (`ProofOfPostingEnvironmentStep`).
- Scope: this document is the **contract** the port PR has to satisfy.
  When the port PR lands, this file gets a one-line "implemented in
  `<path>`" pointer; nothing else moves.

---

## 1. Why this matters

Today the FFFBT preflight is almost empty — `ProofOfPostingEnvironmentStep`
is a no-op, and `MobilerunWorker.preflight_ui_tree()` is one shallow
probe. The real farm has *three years* of hard-won lessons baked into
`device_preflight.py`:

- IG mid-job tap failures are usually the Portal accessibility tree going
  empty ("No active window / root filtered out"), **not** an actual UI
  problem. The right fix is a probe-restart-retry-reboot ladder, not a
  job retry.
- MockGPS pins must be applied **after** any reboot — reboot clears the
  mock UI state and a stale pin reads as "GPS missing" from IG's side.
- Reboots have to be **bounded** (≤1 per 15 min per serial) or a flaky
  Portal will burn the whole fleet's job throughput on reboot loops.

Without this, scaling past one device safely is not realistic — the
launcher will see a flake and either retry forever or burn a job into
`needs_review` for what should be a 30-second self-heal.

---

## 2. Two-phase contract

The port exposes exactly two entry points. **Do not** merge them — they
run at different times and protect against different failures.

### 2.1 `apply_pre_publish_environment(serial) -> EnvironmentApplyResult`

Lightweight, runs once per job during `preparing_device`. Order is
load-bearing:

1. **(Deferred — FFF-25)** Apply MockGPS for the assigned account
   (`automation.account_gps_locations` row). Until FFF-25 lands, this is
   a no-op that returns OK + logs "gps_skipped_proof_of_posting".
2. Bring Instagram to the foreground if it is not already there. Use the
   `MobileWorker` open-app path (which goes through ADB
   `am start -n com.instagram.android/...LauncherActivity` per the
   MobileRun-first rule). Sleep `FARM_PORTAL_IG_SETTLE_S` (default 3.1s)
   after launch.
3. **Do NOT** poll the Portal accessibility tree here. Polling every
   scheduler tick burns time when nothing is wrong. The tree check
   belongs at task start, not job start.

Emits `automation.job_events` with `event_type='environment_apply'` and
status `ok` / `failed` / `skipped`. On failure, returns a step-result
with `code='environment_apply_failed'` and `retryable=True` (transient).

### 2.2 `ensure_portal_tree_for_task(serial) -> PortalReadyResult`

Heavy, runs once per task right before `mobile_ui_automation` does its
first UI read. Returns `(message, rebooted: bool)` on success or raises
`PortalTreeUnavailableError` on terminal failure.

The ladder (each step short-circuits on success):

1. **Probe** — `_check_portal_tree(serial)`:
   - Try the content-provider shell path first
     (`adb shell content query --uri content://com.mobilerun.portal/state`).
     Works when the TCP driver flakes.
   - Else try the Mobilerun TCP driver (`AndroidDriver.get_ui_tree()`).
   - Else, if `uiautomator` fallback is enabled, try
     `uiautomator dump`. (Optional — gated by a config flag, off by
     default for FFFBT.)
   - Result is OK iff the parsed payload has a non-empty `a11y_tree`.
2. **Soft retry** — up to `FARM_PORTAL_MAX_SOFT_RETRIES` (default 7),
   each attempt:
   - Test if the failure looks transient (`"no active window"`,
     `"root filtered out"` in the error) AND the Portal accessibility
     service is enabled (`settings get secure
     enabled_accessibility_services` contains
     `com.mobilerun.portal/...MobilerunAccessibilityService`).
   - Bring IG to the foreground again (this alone fixes most transients).
   - On attempt ≥4, bounce the Portal a11y service: settings put 0 → wait
     → write `enabled_accessibility_services` back → settings put 1 →
     wait 1.5s. This is the "Portal a11y restart" Mobilerun does
     internally and it usually unsticks the driver without a reboot.
   - Re-probe.
3. **A11y enable** — if `portal_a11y_service_enabled(serial)` is False
   AND we have reboot budget, enable it first. Skipping this step burns
   a reboot on what is just an `adb shell settings put` problem.
4. **Bounded reboot** — last resort. Up to
   `FARM_PORTAL_MAX_REBOOTS_PER_SESSION` (default **1**), gated by
   `FARM_PORTAL_REBOOT_COOLDOWN_S` (default **900** = 15 min) per
   serial. `adb reboot`, wait 100s, then `adb get-state` poll loop
   (up to 12×10s) until the device is back, then re-probe.
5. **Terminal** — out of retries and reboot budget exhausted: raise
   `PortalTreeUnavailableError`. The pipeline maps this to a
   `needs_review` step result with `code='portal_tree_unavailable'`.

Order rule: portal-tree check happens **before** any other
environment-time mutation. A reboot during step 4 clears MockGPS, so
GPS must be re-applied **after** a successful return. Concretely:
`_run_proof_of_posting` calls `ensure_portal_tree_for_task` first, and
if it returns `rebooted=True`, calls `apply_pre_publish_environment`
again before the worker step.

---

## 3. Reboot state — where it lives

The real farm stores reboot history in
`logs/.portal_reboot_state.json`. The FFFBT port must NOT use a flat
file. Reboot history is observable runtime state about a device — it
goes in the database, where the launcher and dashboard can see it:

- Add column `automation.physical_devices.last_reboot_at TIMESTAMPTZ`
  (nullable).
- The cooldown check reads it inline:
  `now() - last_reboot_at < interval '15 minutes'` → blocked.
- A successful reboot writes `last_reboot_at = now()` in the same
  transaction that flips the device back to `online`.

Migration shape (new file
`supabase/migrations/<next>_physical_devices_last_reboot_at.sql`):

```sql
ALTER TABLE automation.physical_devices
    ADD COLUMN IF NOT EXISTS last_reboot_at TIMESTAMPTZ;

COMMENT ON COLUMN automation.physical_devices.last_reboot_at IS
    'Most recent forced reboot by the worker (Portal tree recovery). '
    'Used to enforce a per-device cooldown — see '
    'docs/research/device-preflight-port-spec.md §3.';
```

The cooldown window itself stays a runtime constant
(`PORTAL_REBOOT_COOLDOWN_SECONDS = 900`) — not a setting, because
changing it from a dashboard is the wrong control surface; if the
default is wrong, fix it in code with a PR.

---

## 4. Where the new code lives

| New module / file | Role |
|---|---|
| `src/worker/preflight/portal_tree.py` | Pure functions: `check_portal_tree`, `_probe_shell`, `_probe_driver`, `_probe_uiautomator`, `portal_a11y_service_enabled`. No state. |
| `src/worker/preflight/recovery.py` | `restart_portal_a11y`, `reboot_device`, cooldown check against the DB. Talks to ADB through `MobilerunWorker._adb_shell` (no direct subprocess). |
| `src/worker/steps/environment_apply.py` (replace existing stub) | `ProofOfPostingEnvironmentStep` becomes `EnvironmentApplyStep` and calls `apply_pre_publish_environment`. |
| `src/worker/preflight/portal_ready.py` | `ensure_portal_tree_for_task` — the top-level ladder. Returns `PortalReadyResult` dataclass. |
| `tests/worker/preflight/test_portal_tree.py` | Probe-chain unit tests with fake ADB + driver. |
| `tests/worker/preflight/test_portal_ready.py` | Ladder tests: soft-retry success, a11y-restart success, reboot path, cooldown blocked path, terminal failure. |

The port DOES NOT touch the existing `MobilerunWorker` directly — the
preflight modules call the worker's existing `_adb_shell` / `screenshot`
/ `page_source`. The worker is the *adapter*; preflight is *application
logic* on top.

---

## 5. Forbidden imports (hard contract)

The port PR **must not** import or reference:

- `farm.mockgps_vn.FLEET_MOCK_BY_SERIAL` — that constant carries
  production usernames + cafe coordinates and belongs in
  `automation.account_gps_locations`.
- `farm.adb_app_launch` — use `MobilerunWorker.open_app` instead.
- `farm.uiautomator_tree` — port the gated `uiautomator dump` fallback
  inline in `portal_tree.py` (~10 LOC); do not import the legacy module.
- The literal `logs/.portal_reboot_state.json` path — see §3.

If a reviewer sees any of these in the port PR, the right action is to
push back, not to merge with a TODO.

---

## 6. Acceptance tests (write these alongside the port)

Each scenario is a single pytest case that drives the ladder with a fake
ADB / driver and asserts the observable side effects.

1. **Happy path — first probe succeeds.** No retries, no reboot, no
   `last_reboot_at` write. Returns `rebooted=False`.
2. **Soft retry success.** Shell probe fails twice with a transient
   marker; third attempt succeeds after the foreground bounce. Asserts
   `last_reboot_at` is unchanged.
3. **A11y restart success.** Soft retry exhausts at attempt 4; a11y
   restart fixes it on attempt 5. Asserts the a11y `settings put`
   triplet was issued in order.
4. **Reboot path.** All soft retries fail; reboot is allowed (no
   prior `last_reboot_at` within 15 min); reboot succeeds; subsequent
   probe passes. Asserts `last_reboot_at` is updated to ~now (within 5s
   tolerance, using a clock injected via dependency).
5. **Reboot blocked by cooldown.** All soft retries fail; cooldown
   check returns blocked; a11y restart + shell probe + uia probe also
   fail. Asserts `PortalTreeUnavailableError` is raised and
   `last_reboot_at` is NOT modified.
6. **Already healthy after reboot but MockGPS still stale.** Reboot
   path returns `rebooted=True`; caller (a higher-level test) verifies
   that `apply_pre_publish_environment` is invoked a second time.
7. **A11y service disabled at start.** First probe fails with
   "accessibility service not available"; pre-reboot a11y enable fixes
   it; no reboot needed.

The clock and `last_reboot_at` lookup go through small interfaces
(`Clock.now()`, `DeviceRebootStore.get/set`) so the tests can be
hermetic — they never touch a real DB or run an `adb reboot`.

---

## 7. What is explicitly out of scope for the port PR

- Multi-host coordination of reboots. The real farm runs one operator
  PC; the cooldown is per-serial in a single DB row. Cross-host
  reconciliation belongs in the autonomous-supervisor design (see
  `docs/AUTONOMOUS_POSTING_SERVICE.md` in the source archive — not yet
  ported here).
- The Claude-driven recovery (`farm/post_recovery.py`, `claude_*_learn`).
  Stays out of MVP scope.
- The `mobilerun_app_post_recovery` LLM call. The whole port is
  deterministic — no LLM in the preflight ladder.
- Reboot-policy tuning from the operator dashboard. Constants in code
  until we have a real reason to change them at runtime.

---

## 8. Implementation order (for the port PR)

1. New migration adding `last_reboot_at`.
2. `src/worker/preflight/portal_tree.py` + its tests.
3. `src/worker/preflight/recovery.py` + its tests.
4. `src/worker/preflight/portal_ready.py` (ladder) + its tests.
5. `src/worker/steps/environment_apply.py` rewrite + tests.
6. Pipeline change: call `ensure_portal_tree_for_task` from the worker
   step entry instead of the environment-apply step. Plumb `rebooted`
   back through so the environment step can re-apply MockGPS if/when
   FFF-25 lands.
7. README §3 and §11 updates.

Each step is its own commit on the port branch; tests pass at every
commit.
