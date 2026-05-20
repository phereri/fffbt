# MockGPS integration — research notes

- Status: research draft (no production code touched, no device accessed)
- Owner: Research Agent (FFF-24)
- Last updated: 2026-05-20
- Issue: FFF-24 — *MockGPS integration research*
- Scope: MVP — set per-account GPS coordinates on a physical Android phone as
  part of Environment Loader (`docs/architecture.md` §4, invariant I3).
- Companion: [`docs/research/genfarmer-api.md`](./genfarmer-api.md) §4.7 already
  established that GenFarmer exposes **no** MockGPS API — location is a
  device-side concern.

Every claim is tagged `confirmed`, `likely`, `assumption`, or `unknown`. No
phone was touched and no app traffic captured for this pass; findings come from
the Android platform docs, the `io.appium.settings` source, and public
mock-location app repos. Device verification is listed under §7.

---

## 1. TL;DR — recommendation

**Do not depend on the third-party "MockGPS" app.** Use the Appium helper app
`io.appium.settings`, which Appium already installs on every device the Poster
worker drives. It ships a headless `LocationService` that is controlled
entirely by ADB / the Appium driver — no UI, no map taps, no app-specific
intent reverse-engineering.

| Question from the brief | Answer | Confidence |
|---|---|---|
| Exact control method | `io.appium.settings` `LocationService` via `am start-foreground-service` (or `driver.set_location`) | confirmed |
| Verification method | `adb shell dumpsys location` + read-back; functional check in-app | confirmed (platform), likely (sufficiency) |
| Fallback via UI automation | Appium drives the chosen mock-GPS app's map/coordinate UI | likely |

The rest of this document explains why, and answers each sub-question for a
generic "MockGPS" app as the brief asked.

### 1.1 GPS requirement by phase — confirmed (FFF-24 clarification, 2026-05-20)

GPS is **mandatory for the real MVP and production path**. How strictly it
gates publishing depends on the rollout phase:

| Phase | GPS requirement | Behaviour on GPS failure |
|---|---|---|
| `proof_of_posting` (MVP-0) | **best-effort** | MockGPS apply may fail; the job may still publish. Log the failure, do not block. |
| `mvp` (MVP-1) | **required** — GPS apply **and** verification must succeed before publishing | If apply or verification fails, **abort the job before publish**. GPS provider is `io.appium.settings` (or whichever mock provider is selected per device). |
| `production` | **required** — same as `mvp`, plus later anti-detection hardening if needed | Abort before publish on failure. |

Accepted risk (explicit decision, do not silently re-assume): Android mock
locations are detectable via `Location.isMock()` (see §6). **Root / Magisk /
ROM-level hiding is out of current scope and must not be implemented now.**
"Later hardening if needed" in the production row refers to that work — it is
deferred, not in this MVP.

Implication for the Environment Loader: it needs the current phase as input so
it can decide whether a MockGPS failure is fatal. For `mvp`/`production` the
loader must run both the apply step (§3) and a verification read-back (§7
step 4) and treat either failure as a hard stop.

### 1.2 Mock-location state is not persistent — re-apply every job — confirmed

The mock-location setup is **not durable**. The selected-mock-app AppOp
(`appops set ... android:mock_location allow`), the location permission grant,
and the running `LocationService` are all runtime state that **does not survive
a device reboot** — and the foreground service also stops if it is killed or
the app is reinstalled.

**The Environment Loader must (re)apply the full §2 + §3 sequence at the start
of every job**, not once at fleet provisioning time. Do not assume a phone that
worked yesterday is still in a mock-capable state today. The verification
read-back (§7 step 4) is what proves the re-apply actually took for that job.

---

## 2. How Android mock location actually works — confirmed

This is the part that does **not** depend on which app you pick. Every
mock-location app on Android — `io.appium.settings`, `com.lexa.fakegps`,
`com.theappninjas.fakegpsjoystick`, the various open-source "MockGps" repos —
goes through the **same** platform mechanism:

1. **Developer Options enabled** on the phone — *confirmed*.
2. The app is granted the `android:mock_location` AppOp. Pre-Android-6 this was
   the global secure setting `mock_location`; from Android 6 (API 23) it is a
   per-app AppOp set by picking the app under *Developer options → Select mock
   location app*, or via ADB — *confirmed*.
3. The app declares `android.permission.ACCESS_MOCK_LOCATION` in its manifest
   and holds a normal location permission (`ACCESS_FINE_LOCATION`) — *confirmed*.
4. At runtime the app calls `LocationManager.addTestProvider(...)` then
   `setTestProviderEnabled(...)` and pushes coordinates with
   `setTestProviderLocation(...)` — *confirmed*.

Consequences that matter for our design:

- **Only one app can be the selected mock-location app at a time** — *confirmed*.
  If `io.appium.settings` is the mock app, a separate "MockGPS" app cannot
  inject simultaneously. Pick one provider per device. This alone argues for
  consolidating on `io.appium.settings`, since the Poster worker already needs
  it.
- A location pushed through the test-provider API is **flagged as mock**:
  `Location.isFromMockProvider()` (≤ API 30) / `Location.isMock()` (API 31+)
  returns `true` — *confirmed*. Any app, including Instagram, *can* read that
  flag. Whether Instagram does is **unknown** — see §6 (detectability risk).

The ADB commands to put a phone into this state (example for
`io.appium.settings`; substitute any package):

```sh
# one-time per device
adb shell settings put global development_settings_enabled 1
adb shell appops set io.appium.settings android:mock_location allow
adb shell pm grant io.appium.settings android.permission.ACCESS_FINE_LOCATION
```

`appops set ... allow` is the programmatic equivalent of picking the app in the
Developer Options dropdown — *confirmed*. It does **not** need root; ADB's
shell UID is allowed to set AppOps.

---

## 3. Exact control method — `io.appium.settings` (recommended) — confirmed

`io.appium.settings` is the open-source helper app the Appium Android driver
auto-installs (`appium/io.appium.settings`). It is already a dependency of the
Poster (Appium worker), so adopting it for GPS adds **zero new APKs** to the
device fleet.

It exposes a foreground `LocationService` driven purely by intent extras:

```sh
# API 26+ (all our target devices) — start continuous mock updates (~every 2s)
adb -s <serial> shell am start-foreground-service --user 0 \
  -n io.appium.settings/.LocationService \
  --es longitude <LON> --es latitude <LAT>

# optional extras: --es altitude <m> --es speed <m/s> \
#                  --es bearing <deg> --es accuracy <m>

# stop mocking + clean up
adb -s <serial> shell am stopservice io.appium.settings/.LocationService
```

- *confirmed* from the `io.appium.settings` README and `LocationService.java`.
- The service keeps re-emitting the location every ~2 s, which is what GPS
  consumers expect (a single fix goes stale) — *confirmed*.
- Equivalent from the Appium client: `driver.set_location(lat, lon, alt)` /
  the `mobile: setGeolocation` extension — *confirmed*. The driver shells out
  to the same `LocationService` under the hood — *likely*.

**Why this is the "exact control method":** it is fully programmatic, headless,
per-device (`-s <serial>`), idempotent, and needs no reverse-engineering. The
Environment Loader can call it directly while loading an account environment;
the value comes straight from `automation.gps_locations`
(`latitude`, `longitude`, `accuracy_meters` — see
`supabase/migrations/20260515091809_create_account_environment_tables.sql`).

Known caveat: `driver.setLocation()` has reported failures specifically on
**Android 10** with some Appium versions (works on 9, 11, 12) — *likely*. If
the fleet includes Android 10 devices, prefer the direct
`am start-foreground-service` call over the driver method and verify per §7.

---

## 4. The third-party "MockGPS" app — answering the brief's sub-questions

The brief asks specifically about "the MockGPS Android app". **There is no
single canonical app called "MockGPS"** — `genfarmer-api.md` §4.7 already flagged
this. Candidates seen in phone-farm setups: `com.lexa.fakegps` (Lexa FakeGPS),
`com.theappninjas.fakegpsjoystick` (Mock GPS with Joystick),
`ru.gavrikov.mocklocations`, and several open-source `MockGps` repos.

> **Action required (unknown):** the actual package on the `fffbt` device fleet
> is **unknown**. Confirm with `adb shell pm list packages | grep -iE 'gps|location|mock'`
> on a real device before coding against any app-specific behaviour.

With that caveat, the four sub-questions:

### 4.1 Intent / deeplink / API control — *likely no* (generic), *confirmed yes* (dev-oriented variants)

- Consumer, map-based mock-GPS apps (Lexa FakeGPS, Mock GPS with Joystick) are
  built around a touch UI and **do not document any intent or deeplink API** —
  *likely* (absence of documentation; not exhaustively reverse-engineered).
- *Developer-oriented* mock-location apps **do** expose broadcasts. Example —
  `amotzte/android-mock-location-for-development`:

  ```sh
  adb shell am broadcast -a send.mock -e lat <LAT> -e lon <LON> \
    [-e alt <ALT>] [-e accurate <ACC>]
  adb shell am broadcast -a stop.mock
  ```

  — *confirmed* from that repo. This is functionally identical to what
  `io.appium.settings` gives us, just from a less-maintained app.
- **Conclusion:** if the fleet's "MockGPS" app is a consumer GUI app, assume
  **no** programmatic API and fall back to §5. If it turns out to be a
  broadcast-driven dev app, it is usable — but `io.appium.settings` is still the
  better choice (maintained, already installed).

### 4.2 ADB app launch with extras — *likely no effect*

- You can always launch any app's activity with extras:
  `adb shell am start -n <pkg>/<activity> --es lat <LAT> --es lon <LON>`.
- But this only changes location **if the target activity reads those extras**.
  Consumer mock-GPS apps do not — they read coordinates from their own UI/map.
  So `am start --es ...` against a generic MockGPS app is *likely* a no-op for
  the coordinates — *likely*.
- `am start-foreground-service` against `io.appium.settings/.LocationService`
  (§3) is the case where extras **are** read — *confirmed*.

### 4.3 Settings storage — *likely not externally controllable*

- Mock-GPS apps persist their last-used coordinates in private
  `SharedPreferences` / an internal SQLite DB under
  `/data/data/<pkg>/` — *likely*.
- That path is **not readable or writable without root** (or an app built
  `debuggable`, via `adb shell run-as <pkg>`) — *confirmed*. Production phones
  are assumed non-rooted and the app non-debuggable.
- So "write the coordinate into the app's settings store, then launch it" is
  **not a viable control path** for a generic app — *likely*. There is no
  Android-wide "mock location coordinate" system setting either; `mock_location`
  is only an on/off-style flag, never a lat/lon pair — *confirmed*.

### 4.4 UI automation through Appium — *likely yes* (this is the fallback, §5)

Always available as long as the app has a usable UI. Covered in §5.

---

## 5. Fallback — UI automation via Appium — likely

If, after §7 verification, `io.appium.settings` is unusable on some device
(e.g. an Android-10 quirk, or a vendor ROM that blocks foreground services)
**and** the fleet ships a specific MockGPS GUI app, drive that app with Appium:

1. `adb shell appops set <mockgps-pkg> android:mock_location allow` (so the
   GUI app — not `io.appium.settings` — is the selected mock app).
2. Appium session against the MockGPS app
   (`appium:appPackage` / `appium:appActivity`).
3. Set coordinates. Most of these apps offer one of:
   - a search box → type an address / `lat,lon` string → select result;
   - a "set coordinates" dialog with two numeric fields;
   - long-press on the map (least reliable — needs pixel→geo math, avoid).
   Prefer the numeric-field or search path.
4. Tap the "Start" / play button to begin mocking.
5. Background the app and start the Instagram Appium session.

Cost vs. §3: brittle (depends on the app's exact layout and version), slower,
and needs a screenshot-based selftest per app version. **Use only if §3 fails.**
The element selectors are *unknown* until the fleet's actual app is identified
(§4 action item) and inspected with `appium inspector` / `uiautomator dump`.

---

## 6. Risks

| Risk | Severity | Note |
|---|---|---|
| **Mock flag detectability.** `Location.isMock()` is `true` for any test-provider location. If Instagram checks it, every approach in this doc (incl. `io.appium.settings`) is detected. | high if IG checks | *unknown* whether IG reads the flag. Defeating it reliably needs root / Magisk / a ROM-level hook — out of MVP scope and against the device-safety rules. Flag for a decision, do not silently assume it's fine. |
| Only one selected mock-location app per device. | medium | Design choice, not a bug: standardise on `io.appium.settings`. Don't install a second provider. |
| Android 10 + `driver.setLocation()` failures. | low | Use the direct `am start-foreground-service` call; verify per §7. |
| Mock provider dropped on reboot / Appium reinstall. | low | `appops`/`pm grant` state and the running service do not survive a reboot. Environment Loader must (re)apply §2 + §3 at the start of **every** job, not once — see §1.2. |
| GPS still shows real location if mocking is set up *after* IG already has a fix. | low | Start the `LocationService` **before** launching Instagram. |

---

## 7. Safe next steps (device verification)

All read-only or trivially reversible; none write production data, none touch
the `fffbt` schema, none run destructive device commands.

1. **Identify the app.** On one lab phone:
   `adb shell pm list packages | grep -iE 'gps|location|mock'` — resolves the
   §4 `unknown`.
2. **Confirm `io.appium.settings` is present** (it is, if Appium has ever run):
   `adb shell pm path io.appium.settings`.
3. **Dry-run §2 + §3** on one idle lab phone with a throwaway coordinate.
4. **Verify the mock took:**
   - `adb shell dumpsys location` → look for the test provider and a
     `last location` matching the coordinate;
   - Android 12+: `adb shell cmd location providers`;
   - functional check: open Google Maps, confirm the blue dot jumps.
5. **Functional check against Instagram** (the check that actually matters):
   on a non-production / disposable account, set a mock location, open
   Instagram, and see whether IG's location-tagging / suggestions reflect the
   mocked city — and whether anything flags it. Resolves the §6 detectability
   `unknown`.
6. Record exact commands + outputs, redact account identifiers, attach to
   FFF-24 or a follow-up issue.

Until step 5 is done, treat "Instagram accepts a test-provider mock location"
as an **assumption**, not a confirmed fact.
