# ChangeDevice + account-pool integration — context & handoff

> Status: **design discussion, not started.** This is a handoff doc so a fresh
> agent can pick up the larger "phone is always working, cycling accounts via
> changeDevice" effort. The currently-running production flow is the *static*
> multi-thread fleet (one account per phone) committed on `main` — see
> `docs/standalone-trial-poster.md` and the `scripts/post_*.py` files.

## Goal (operator's vision)

Stop leaving a phone idle between an account's posts. Instead, keep **a pool of
accounts** and cycle them through each phone so the device is always working:

1. Bindings stored per account: **IG app backup + account + device fingerprint
   (Android 12+)**.
2. When starting work on a specific phone:
   1. apply the account's **fingerprint**,
   2. **reboot** and **verify the fingerprint** after the device is back,
   3. restore the **IG backup** for the linked account,
   4. verify **Mobilerun has accessibility-service access**,
   5. **start posting immediately**.
3. When a video is posted → put that account into a **wait/cooldown**, the phone
   immediately **picks the next eligible account** from the pool and repeats.

Effectively: a pool of accounts; when a device frees up, pick an account that is
allowed to work now and start.

## Good news: this is already designed

The operator's vision maps one-to-one onto an existing, written contract:

- **`docs/contracts/account-environment-application.md`** — defines
  `apply_account_environment(device_id, account_environment_id, mode) -> ApplyResult`.
  The **`account_environment` bundle** = `account` + `device_profile`
  (fingerprint) + `app_state` (IG backup/session) + `proxy` + `gps_location`.
  That bundle IS the operator's "backup + account + fingerprint" binding.
- The **`mvp` mode** in that contract is literally "one phone serves multiple
  accounts" — the pool-cycling model. (`proof_of_posting` mode = what we run
  today; device identity is *not* mutated.)
- The data model already exists in the **`automation.*` schema** (deferred for
  MVP-0 when the operator said "only `fffbt.videos`"): `automation.accounts`,
  `account_environments`, `device_profiles`, `app_states`, `proxies`,
  `gps_locations`, `physical_devices`. This phase = **activating that deferred
  architecture**, not inventing a new one.
- **`src/scheduler/launcher.py`** (`JobLauncher`) already reserves free physical
  devices, assigns accounts, and runs the `preparing_device → publishing` state
  machine with heartbeats/retries.
- **`src/genfarmer/changedevice.py`** (`ChangeDeviceClient`) already implements
  identity ops over adb + GenFarmer's local API (`127.0.0.1:55554`):
  `capture(serial) -> DeviceProfile`, `apply(serial, profile, clear_data=…)`
  (stages props, applies identity, reboots ~90 s), `wait_reconnect(serial)`,
  `random_profile(min_android=…)`. Saved profiles load from `.props`/`.json`,
  so **stored per-account fingerprints** (operator's preference) are supported —
  not only GenFarmer `/devices/random`.

## ⚠️ The critical blocker — ChangeInfo connectivity hazard

Applying a fingerprint uses GenFarmer **ChangeInfo**. Research input FFF-18
(`docs/research/genfarmer-changeinfo-hazard.md`) observed that on **Tailscale-only**
phones, ChangeInfo **dropped both `adbd` (:5555) and `atx-agent` (:7912) with no
proven remote recovery** — i.e. it can cut off remote access to the phone, then
only physical hands-on recovers it.

The contract therefore **gates the entire `mvp`/`production` mode** behind four
safety gates (G1–G4):

- **G1** — exercise ChangeInfo (+ `BackupRestoreV2 withChangeInfo=true`) in a
  **sandbox** (a dedicated test phone), not a production job, first.
- **G2** — sandbox uses a **throwaway** IG account; no production account logged
  in during the test.
- **G3** — prove the device reconnects over ADB (`:5555` **and** `:7912`) within
  a bounded timeout after ChangeInfo, **repeatably** (not one lucky run).
- **G4** — no production use until G1–G3 pass. Because device profile is required
  from `mvp` onward, **`mvp`/`production` do not run at all** until proven.

The IG-backup restore (`BackupRestoreV2`, Step 4b) is **also** ChangeInfo-bearing
and cross-phone restore semantics are **unverified** (contract Open question 2).

**Nuance in our favour:** the hazard was observed on **Tailscale-only** phones.
Our fleet runs over the **local LAN** (`192.168.5.x` adb) — after a reboot the
phone rejoins the LAN and adb reconnects on its own (we rely on exactly this in
the a11y reboot-recovery today). So the hazard is likely *milder* for us — **but
that must be proven in a sandbox, not assumed.**

## Proposed order of work

1. **Sandbox validation (G1–G3) — do this first.** One spare phone + a throwaway
   IG account. Prove the loop: `apply fingerprint → reboot → :5555 (and :7912 if
   used) recover within timeout → restore IG backup → verify a11y bound → verify
   the expected account is logged in`. Make it **repeatable**. This removes the
   single biggest risk and unblocks everything else.
2. **Data model.** Decide: (re)introduce `automation.*` (accounts /
   account_environments / device_profiles / app_states / physical_devices) — the
   contract was designed for it — **or** a lean custom store scoped to the pool.
3. **Orchestration.** A device-worker loop that **atomically claims an account**
   (same `FOR UPDATE SKIP LOCKED` pattern as the video claim, but on an
   accounts/environments table), runs `apply_account_environment`, posts via the
   proven publish path, then **releases the account to cooldown** and picks the
   next eligible one. Eligibility = not on challenge, under its 24 h cap, has a
   ready fingerprint + backup, cooldown elapsed. An account must never be loaded
   on two phones at once (the claim lock guarantees this).

## Open questions for the operator (raised, not yet answered)

1. **Sandbox**: is there a spare test phone + throwaway IG account to validate
   ChangeInfo/restore recovery safely? (Hard prerequisite — do not risk a real
   phone/account.)
2. **Network**: are target phones on local LAN (like the current fleet) or
   Tailscale-only? (Determines the recovery-risk profile.)
3. **Fingerprints**: stored per account (Android 12+) — initial source: `capture`
   from a real device once, or GenFarmer `/devices/random` filtered to ≥12?
4. **IG backups**: how are they produced and where stored (S3 like videos?), and
   restored via GenFarmer `BackupRestoreV2` or plain adb?
5. **Data store**: OK to (re)introduce `automation.*` for this phase, or prefer a
   lean custom store only for the pool?

Plus the contract's own Open questions 1–6 (GenFarmer Task JSON shape for
programmatic ChangeInfo submission; cross-phone restore semantics; ChangeInfo
recovery proof; verify timeouts; `proxy_required` flag; `mode` source).

## What exists today (starting point for the next agent)

- **Proven static fleet on `main`** (commit `9db6cb4`): `scripts/post_fleet.py`
  (supervisor) → `scripts/post_loop.py` (per-device loop, 15–45 min cadence,
  20/24 h cap, a11y reboot recovery, escalate after 5 consec) →
  `scripts/post_trial.py` (claim → caption → publish → confirm → writeback).
  Self-learning per account in `src/runner/account_memory.py`
  (`trial_reels_path`, `verify_path`). Telemetry in `src/runner/fleet_events.py`
  feeding `scripts/fleet_dashboard.py`. Account↔device binding (manual, IPs
  rotate on router reboot) in `data/device_accounts.json`.
- **LLM note (temporary):** `config/mobilerun/config.yaml` is *locally* pointed
  at Google's Gemini endpoint (OpenAI-compat for the agent, native `GoogleGenAI`
  for `structured_output`) because the shopaikey proxy token died; `.env` holds
  the funded Gemini key. Backups: `config.yaml.shopaikey.bak`, `.env.bak`.
  Revert to shopaikey when its token is replaced. This is **not** committed.
- **Account safety signal:** account `trungmaivuze962` hit an IG
  `login_challenge` under the static model — a reminder that posting cadence /
  identity hygiene matters, which is part of *why* changeDevice (per-account
  device identity) is wanted.
