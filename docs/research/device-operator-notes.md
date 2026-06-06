# Device operator notes — practical lessons from the real farm

- Status: research, imported 2026-06-06 from the working Mobilerun farm
  snapshot (`fffbt-mobilerun` repo, `docs/OPERATOR.md`).
- Audience: operators preparing a physical Android device and account
  before a `proof_of_posting` job. Agents driving Instagram via the
  AppCard inherit the same constraints.
- Scope: device-side concerns only — proxy state, MockGPS, GPS region,
  IME for clipboard, the per-device account binding. Everything that
  belongs in the per-job goal is in
  [`config/mobilerun/app_cards/instagram.md`](../../config/mobilerun/app_cards/instagram.md).

This document is a **reference**, not a runbook step list. The
authoritative runbook is `docs/ops/mvp-rollout-runbook.md`.

---

## 1. Proxy state — manual on the phone, do not toggle from the worker

The real farm runs each phone with a SOCKS/VPN tunnel that the operator
brings up by hand (via the proxy app's UI, or an already-active system
tunnel). The worker — and the MobileRun agent driving Instagram —
**must not** toggle that proxy or run a ProxyConnector CONNECT/CHECK
during login, post, or verify unless the operator explicitly asked for it.

Why the constraint exists:

- Touching the proxy mid-job kills sockets the IG session is using, which
  in turn surfaces as `action_blocked` or `logged_out` on the next UI step
  even though the account is fine.
- ProxyConnector CHECK calls go out from the host. On a phone with a
  manual tunnel that does not match the host's egress, the CHECK reports
  failure and the job stops on a transport problem that isn't real.

What this means for FFFBT:

- Treat the device's proxy as **pre-applied environment state**, owned
  by the operator and recorded in `automation.account_environments` (the
  `proxy` row). The worker only verifies it is `active`; it does not
  drive the proxy app.
- Honor `FARM_SKIP_PROXY_CHECK=1` if any helper still ships a CHECK call;
  treat that as the default for `proof_of_posting`.
- The Instagram AppCard's "Proxy (farm)" block at the top documents the
  same rule for the LLM agent.

---

## 2. MockGPS — install + grant the AppOp once per device

GPS spoofing in the working farm uses the third-party `com.lilstiffy.mockgps`
app driven via ADB. The FFFBT MVP decision is `io.appium.settings/.LocationService`
as the primary path (see
[`docs/research/mockgps-integration.md`](mockgps-integration.md) §3),
with `com.lilstiffy.mockgps` as a documented fallback when Appium is not
installed. Either way, the device-side pregrant is the same one-time
step:

1. Install the APK on the phone (GenFarmer Install APK, `adb install`,
   or via the `mockgps_vn` helper). The vetted build the real farm uses
   is `1.0.4(5)` from the upstream MockGPS repo.
2. Grant the mock-location AppOp **once** per device — do not bake the
   tap coordinates into a script; use the ADB pregrant:

   ```bash
   adb shell appops set com.lilstiffy.mockgps android:mock_location allow
   adb shell settings put secure mock_location_app com.lilstiffy.mockgps
   ```

3. Open the app once, enter the target coordinates (default
   Vietnam / HCMC `10.820000,106.630000` in the real farm; for FFFBT use
   the account-specific row in `automation.account_gps_locations` once
   that table is populated), and tap **FAKE**.
4. Verify in Google Maps that the blue dot lands on the entered point
   before running any IG flow.

For FFFBT specifically:

- Per-device coordinates must come from `automation.account_gps_locations`,
  **never** from a hard-coded Python constant like the legacy
  `FLEET_MOCK_BY_SERIAL`. That data is account-sensitive and belongs in
  the database.
- The host worker only reads the active row and emits an
  `automation.job_events` entry (`event_type='gps_apply'`); the actual
  ADB calls live in the GPS step. See FFF-25 for the interface contract.

---

## 3. GPS region must match the account's home region

A logged-in Instagram account remembers the IP/GPS region of past sessions.
The real-farm finding: VN accounts that were briefly used from a US-routed
device (NYC GPS on serial ending `.127`) tripped IG's risk model. The
recovery cost was an `action_blocked` cooldown for hours.

What this means for FFFBT:

- Before running `proof_of_posting`, both the **proxy** and the **MockGPS
  pin** must be consistent with the account's stated home region (recorded
  in `automation.accounts`).
- The timezone matters too — VN accounts run on `Asia/Ho_Chi_Minh`. The
  preflight step should not change the system timezone mid-job, but the
  device should already be on the correct one.

This is a **data hygiene** constraint, not a UI step. If you cannot
guarantee region consistency for a job, do not start it — the worker
will not detect the mismatch up front and the failure will look like an
IG rate limit when the cause is actually region drift.

---

## 4. Clipboard / IME — Mobilerun Portal IME, not just AdbKeyboard

The real farm learned that AdbKeyboard's `ADB_INPUT_B64` broadcast alone
**reports success but leaves the placeholder visible** on the IG Share
caption field. Paste only registers reliably through the Mobilerun
Portal IME (`content://com.mobilerun.portal/keyboard/input`), which is
installed and enabled as part of GenFarmer provisioning.

Implications:

- The Mobilerun Portal IME must be **installed and enabled** on every
  device the worker uses. There is no fallback that posts captions
  cleanly on IG Share without it.
- Reading the clipboard back (for the verify pass) also benefits from
  the Mobilerun IME — `dumpsys clipboard` is not always populated; the
  IME path is.
- The AppCard's caption sequence — *click index 12 → fresh tree → paste
  via Mobilerun Keyboard at the resolved `caption_input_text_view` index*
  — is non-negotiable. Treat anything else as a bug to fix in the
  agent's tooling, not a tactic to try.

---

## 5. One device = one account at a time

The IG session on the phone is the single source of truth for which
account is being operated. The real farm learned to:

- Set `posted_by` (or the FFFBT equivalent: `automation.jobs.account_id`
  → `automation.accounts.username`) to the username currently logged in
  on the device, not the username the launcher thought was assigned.
- Never reuse a captured post URL from the clipboard across two posts —
  each Share produces a new link; the previous one in the clipboard
  belongs to the previous post.

For FFFBT this is enforced by the scheduler — `find_eligible_account()`
plus the device → environment binding gives one account per device per
job. The verify step must still re-read the username from the Profile
header before promoting the row, to catch the case where the operator
manually switched accounts on the phone.

---

## 6. Login challenges — 2FA / "Try anyway" / "This was me"

Even on a healthy account, IG will occasionally surface a login challenge
on the next session start: 2FA input, "Try anyway", "This was me /
Wasn't me". The FFFBT MVP rule is:

- **Hard stop** on every challenge. `MobileUIAutomationStep` returns
  `login_challenge` (or `logged_out` if the login screen itself appears).
- The operator finishes the challenge by hand on the phone before any
  further runs.
- Do **not** attempt to type a TOTP code from the worker or carry over a
  recovery code. Account-recovery flows are out of MVP scope.

The real farm has scripts that automate TOTP entry; we intentionally
skip them until login automation has its own design pass.

---

## 7. Where the real-farm code lives (for porting)

The full module-by-module map is in
[`mobilerun-real-repo-task-map.md`](mobilerun-real-repo-task-map.md). Of
the device-side concerns covered above:

| Concern | Real-repo file | FFFBT target |
|---|---|---|
| Proxy hands-off rule | `farm/proxyconnector.py` (do not port; honor `FARM_SKIP_PROXY_CHECK=1`) | Environment apply step ignores proxy app |
| MockGPS one-time grant | `farm/mockgps_vn.py` (ADB calls only, not the FLEET constant) | FFF-25 (`apply_gps`) |
| Mobilerun Portal IME / paste | `farm/tools.py::paste_text` | FFF-50 (port) |
| Username read-back | `farm/tools.py::tap_share_and_confirm` preamble | Verify step |
| Login-challenge hard stop | `scenarios/post_ig_trial_reel.py` goal hard-stops | Already enforced in `mobile_ui_automation._HARD_STOP_PATTERNS` |

If a concern in this doc grows into a code change, link it to the
corresponding FFF issue rather than expanding the doc.
