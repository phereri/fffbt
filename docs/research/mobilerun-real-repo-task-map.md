# Mobilerun real-repo → FFFBT MVP task map

- Status: research draft
- Owner: Architect / Tech Lead
- Issue: [FFF-58](mention://issue/c24ef4d0-45c7-4db3-938c-2d906735ee82)
- Last updated: 2026-05-25
- Source archive: `fffbt-mobilerun.zip` attached to FFF-58 (real `phereri/fffbt-mobilerun` snapshot, 2026-05-25)

This document inspects the **real, working** Mobilerun automation repo that
already publishes Instagram Trial Reels in production, and maps each useful
asset to the current FFFBT MVP issues. It is a research / planning document —
no code is copied here.

Every claim is tagged `confirmed` (read directly from the source files in this
archive) or `assumption` (filled in by analogy where the archive did not show
it). Names like `farm/tools.py:tap_share_and_confirm` are file + symbol
pointers, not import paths in FFFBT.

> **Reuse semantics used below**
> - **Copy** — file is generic enough to land in FFFBT verbatim (still subject
>   to repo style / lint).
> - **Port** — same idea, but rewrite to the new namespace / new contracts
>   (`automation` schema, `MobileWorker` interface, `StepResult`,
>   `automation.job_events`).
> - **Rewrite** — keep only the algorithm/observations; throw away the code.
> - **Reference** — do not bring code in; cite it from a doc / AppCard.

---

## 1. Useful files — what they are, where they go

### 1.1 `config/mobilerun/app_cards/instagram.md` — operator AppCard

**What it does (confirmed):** Mobilerun App Card that drives the LLM agent
when posting a Trial Reel. Contains: global stealth rules, index hygiene,
Trial Reel entry paths A/B/C, **farm-standard caption flow (click index 12 →
fresh tree → resolve `caption_input_text_view` → `paste_text` via Mobilerun
Keyboard)**, `tap_share_and_confirm` rules, verify-tab flow, hard-stop
conditions, common failure modes.

| Field | Value |
|---|---|
| FFF issue | [FFF-31](mention://issue/01ec6b9a-1fb1-435d-9d2a-b92948171fce) (upload flow MVP), [FFF-30](mention://issue/5681af3b-229d-43db-af1c-504bda621e73) (research) |
| Action | **Reference + port relevant fragments** into `docs/instagram-appcard-reference.md`. Most of the AppCard already lives there from FFF-48; **the farm-standard caption indices (12 focus / 8 paste) and the Mobilerun Keyboard requirement are new and must be merged in.** |
| Do NOT copy | Account-specific Vietnam coffee-shop / cafe pin metadata; the `poker_videos` table reference; trajectory file paths under `trajectories/_instructions/…`; any reference to `FARM_*` env vars without re-mapping to FFFBT names. |

### 1.2 `farm/agent.py` — Mobilerun `MobileAgent` factory

**What it does (confirmed):** Loads `config/mobilerun/config.yaml`, applies
per-platform + per-task overrides (`platform_defaults.yaml`), wires custom
tools from `farm.tools.build_custom_tools()`, applies driver patches
(`ensure_android_driver_uia_patch`), constructs a Mobilerun `MobileAgent`
bound to a single device serial (USB or `ip:port`). Per-device session, per
the FFF-28 contract.

| Field | Value |
|---|---|
| FFF issue | [FFF-28](mention://issue/e78c70d2-b847-4d5e-9573-818470404d7f) (Mobile UI session wrapper) |
| Action | **Port** — adapter behind the `MobileWorker` interface from FFF-28. The factory shape (build_agent(goal, device_serial, output_model, variables, overrides, timeout)) is the right signature for the Mobilerun adapter. |
| Do NOT copy | Hard-coded path `config/mobilerun/config.yaml` (point at FFFBT's own config dir); `FARM_*` env var names; the `shopaikey_llm` shim — that is account-specific shop-LLM key plumbing, out of MVP scope; any `crm/`, `accounts_db`, login-learn imports. |

### 1.3 `farm/tools.py` — Mobilerun custom-tool registry

**What it does (confirmed):** ~2,945 LOC of host-side and device-side custom
tools that solve the *genuinely hard parts* of IG Trial Reel posting. The
useful surface area for FFFBT (each is a separate registration in
`CUSTOM_TOOL_SPECS`):

- `prepare_video_for_android` — ffprobe + ffmpeg pipeline; idempotent
  re-encode to **H.264 / yuv420p / 8-bit / `+faststart`**, returns the new
  path or "already android-friendly".
- `push_video_to_gallery` — `adb push` into `/sdcard/DCIM/Camera/` then
  `am broadcast MEDIA_SCANNER_SCAN_FILE` so IG's picker sees it instantly.
- `paste_text` — focus the IG caption field (Mobilerun farm indices 12→8;
  resource-id `caption_input_text_view` + class filter `AutoCompleteTextView`
  + hint "Write a caption" to beat the chip row), then paste via
  **Mobilerun Portal IME** (`content://com.mobilerun.portal/keyboard/input`),
  fall back to AdbKeyboard's `ADB_INPUT_B64`, then `portal.input_text`,
  then `type_humanized`. Strict caption verify after paste.
- `verify_caption_text` — refreshes UI, resolves the caption AutoComplete
  node, compares to the expected caption (DB-sourced when `video_id` is
  passed). Sets a per-device gate that **blocks** `tap_share_and_confirm`
  until verification passes.
- `tap_share_and_confirm` — IG-specific Share button tap. (1) hide IME if
  shown, (2) snapshot `topResumedActivity`, (3) real-finger 90 ms
  touchscreen swipe on the lowest `share_button` node, (4) poll for
  activity change / `share_button` gone / `trials_list` visible for up to
  ~22 s, with retries (`shell_tap`, longer hold, last-resort
  `tap_by_resource_id`).
- `hide_ime` — checks `dumpsys input_method mInputShown=true`, dismisses
  IME with `KEYCODE_BACK`, re-checks until hidden. Required before any
  bottom-of-screen tap.
- `tap_by_resource_id` — resource-id lookup with optional `contains_text`
  and `class_name_contains` filters; for `caption_input_text_view` picks
  the largest `AutoCompleteTextView` by area; for `share_button` picks
  the lowest on screen (footer Share, not header duplicate).
- `tap_by_text` — visible-text fallback for "Next", "Allow", "Done", etc.
  `prefer="smallest"` (default, buttons) vs `prefer="largest"` (caption
  hints); `exclude_text_exact` to skip e.g. "Prompt".
- `advance_past_reel_timeline_editor` — handles the extra "clips
  timeline / Try Edits" editor screen between gallery pick and Share.
- `wait_trial_reel_settle` + `refresh_trial_reels_list` — 15–30 s wait
  after Share, navigate to `trials_list`, **pull-to-refresh** (IG hides a
  just-posted reel until refresh).
- `copy_reel_link` / `copy_reel_link_from_viewer` — paper-plane Share →
  "Copy link" → multi-strategy clipboard read (`dumpsys clipboard`,
  logcat scan, `uiautomator dump` XML scan, Chrome-paste fallback,
  AdbKeyboard broadcast). **Never** uses ⋮ More.
- `mock_location_status` / `set_mock_location_app` — `appops set
  android:mock_location allow` + `settings put secure mock_location_app`.
- `device_summary` / `list_farm_devices` — getprop / `adb devices` JSON
  summary.

| Field | Value |
|---|---|
| FFF issue | [FFF-50](mention://issue/c222ec7e-f0de-4c92-8000-b19d40fdd182) (port custom tools), [FFF-29](mention://issue/389b4cf9-aac8-434f-a93c-6321623770d6) (video transfer), [FFF-31](mention://issue/01ec6b9a-1fb1-435d-9d2a-b92948171fce) (upload MVP), [FFF-25](mention://issue/4d8a0305-e7f2-4ad7-87dd-7a044ca37a70) (GPS apply) |
| Action | **Port** the named tools individually under the new `MobileWorker` interface (FFF-28). Each tool retains its algorithm but: imports become FFFBT modules; DB calls go through the `automation` schema; events go to `automation.job_events`; errors raise the FFF-56 `StepResult.failed` shape. |
| Do NOT copy | The `update_video_post_in_supabase` function and **everything that touches `poker_videos`** — that is the legacy `fffbt` schema, which is read-only/reference for FFFBT. The `farm.videos_db` `mark_video_posted`/`mark_video_verify`/`list_verify_videos`/`prepare_video_for_publish` helpers. The whole `farm/shopaikey_llm.py` ShopAIKey key-management shim. `farm/claude_*` LLM-learn helpers (Claude post/verify/login/bio learn). `farm/ig_login_*`, `farm/ig_professional_*`, `farm/ig_profile_bio.py` — those are login / onboarding / professional-mode flows that the FFFBT MVP explicitly excludes. `farm/accounts_db.py` (account language lookup for caption locale). `farm/portal_state.py`, `farm/uiautomator_tree.py`, `farm/patch_portal_fetch.py` — Mobilerun internals to leave behind a clean adapter, not import as-is. The hard-coded `FLEET_MOCK_BY_SERIAL` mapping (serial → account + cafe pin) from `mockgps_vn.py`. Any `FARM_*` env var name — re-namespace under FFFBT names. The Telegram / RSS / football-news scripts. |

### 1.4 `farm/trial_reels_nav.py` — Trial Reels list navigation

**What it does (confirmed):** Deterministic, non-LLM navigation to
`trials_list`. Implements Path A (Profile → Professional dashboard → Trial
reels), Path B (Profile → burger menu → Settings → For professionals →
Account type and tools → Trial reels), the Ad-tools mis-tap recovery
(when Path B opens **Ad tools** instead — back out, never boost), the
`trials_list` strict detection (rid + title + `draft_entrypoint_container`),
the **pull-to-refresh** algorithm (`FARM_TRIAL_REEL_REFRESH_PULLS=2`),
opening a tile at 1-based position with black-preview retry, reading the
viewer caption, and `copy_reel_link_from_viewer` (paper-plane → Copy link →
clipboard, with DM-share-sheet dismissal).

| Field | Value |
|---|---|
| FFF issue | [FFF-32](mention://issue/46825e0e-a64b-4321-9ce7-0168d2e217e0) (verification option A), [FFF-31](mention://issue/01ec6b9a-1fb1-435d-9d2a-b92948171fce) (entry navigation) |
| Action | **Port** — this is the bulk of the deterministic verifier. Re-namespace under FFFBT; lift the `FARM_*` env vars to FFFBT config; keep the screen detection helpers (`on_trial_reels_list`, `on_profile_reels_grid`, `on_ad_tools_screen`, `describe_ig_screen`) verbatim in spirit. |
| Do NOT copy | Hard-coded swipe pixels (540, 1500 → 540, 650) — keep as defaults but treat as device-specific. Direct `subprocess.run` over `adb` — go through the FFF-28 session wrapper. |

### 1.5 `farm/trial_reel_settle.py` — post-publish settle wait

**What it does (confirmed):** Picks a random 15–30 s wait
(`FARM_TRIAL_REEL_SETTLE_MIN_S` / `FARM_TRIAL_REEL_SETTLE_MAX_S`) between
publish and the verify pass, with an optional fixed override. Both async
and sync variants. The sync variant is used inside `post_trial_reel_only.py`
when the next step is the deterministic verifier.

| Field | Value |
|---|---|
| FFF issue | [FFF-32](mention://issue/46825e0e-a64b-4321-9ce7-0168d2e217e0) (verification — `verification_delay_seconds` already exists in `automation.global_settings`) |
| Action | **Port** as a thin helper that reads `automation.global_settings.verification_delay_seconds` (FFF-8 already seeded the row). Keep the 15–30 s randomisation as a default; do not hardcode the 180 s value either. |
| Do NOT copy | `FARM_*` env-var names — replace with global-settings reads. |

### 1.6 `farm/verify_trial_tab.py` — deterministic verifier

**What it does (confirmed):** No LLM. Full verify loop on a device:
preflight (mockgps + IG foreground) + portal-tree check; opens
`trials_list` with pull-to-refresh; iterates the newest-N visible
`draft_entrypoint_container` tiles (default cap 30 across 3 scroll pages),
opens each in viewer, reads caption with retries (handles black-preview /
buffer-loading IG quirks), copies link via paper-plane (`direct_share_button`
→ "Copy link" → clipboard), matches to pending DB rows by **caption-text
equality** (`text_matches_expected`), promotes the matched DB row to
`posted` with the link, dedupes URLs/IDs across tiles, optionally runs a
Claude-based learn pass on failure, dumps the UI on failure for debugging.
Returns a `VerifyRunResult` with `exit_code()` mapping to operator-friendly
codes (`success=0`, `nav_trial_list=3`, `no_pending=4`).

| Field | Value |
|---|---|
| FFF issue | [FFF-32](mention://issue/46825e0e-a64b-4321-9ce7-0168d2e217e0) (verification option A) |
| Action | **Port** — this is the reference implementation of "verification option A". Re-namespace; replace `mark_video_posted` / `list_verify_videos` / `find_video_id_by_link` calls with the new FFFBT data-access layer (FFF-7's `automation.videos`); emit `automation.job_events` instead of `print()`; treat the device as "stays reserved" until the verifier returns (already the runtime behaviour — the function does not release before the result is computed). |
| Do NOT copy | Direct `poker_videos` writes via `mark_video_posted`; the Claude `verify_learn` recovery; the `posted_by`/`FARM_VERIFY_*` env-var contract; the assumption that "URL or caption alone is enough" (`FARM_VERIFY_WEAK_MATCH`) — FFFBT MVP should require **caption + link**, not weak-match. Direct file writes under `logs/verify_trial_tab_nav_fail.txt` — route through `automation.job_events` with a screenshot/artifact reference instead. |

### 1.7 `farm/mockgps_vn.py` — third-party MockGPS UI driver

**What it does (confirmed):** Uses the **third-party `com.lilstiffy.mockgps`**
app and drives it via ADB UI taps to set coffee-shop coordinates in Vietnam,
once per serial. `appops set ... android:mock_location allow` + `settings put
secure mock_location_app com.lilstiffy.mockgps`, force-stop + relaunch, tap a
search-field at (540,133), feed `lat<TAB>lon` via `input text`, tap "Start
mocking" at (603,1678), confirm by reading `uiautomator dump` XML for "Stop
mocking". Hard-coded `FLEET_MOCK_BY_SERIAL` mapping (8 production devices).

| Field | Value |
|---|---|
| FFF issue | [FFF-25](mention://issue/4d8a0305-e7f2-4ad7-87dd-7a044ca37a70) (GPS apply interface) |
| Action | **Reference only** for the FFFBT `apply_gps` contract — *do not port the code*. **The FFFBT decision is `io.appium.settings/.LocationService` (see [`docs/research/mockgps-integration.md`](./mockgps-integration.md) §3), not `com.lilstiffy.mockgps`.** Mobilerun's UI-driven approach is a *valid fallback* (the path described in §5 of the MockGPS research doc), so use this file as proof-of-concept that the fallback works — and as the source of the exact ADB commands to set the AppOp / mock-location-app — but the MVP code path should be the headless `am start-foreground-service`. Treat `mockgps_vn.py` as the **fallback** branch of the `MockWorker` interface, not the primary. |
| Do NOT copy | The `FLEET_MOCK_BY_SERIAL` constant (production account names, real cafe coordinates) — that data belongs in `automation.account_gps_locations`, not in checked-in source. Pixel coordinates for taps (`SEARCH_FIELD_TAP`, `START_MOCK_TAP`) — device-specific. The Vietnam-cafe theming. |

### 1.8 `farm/device_preflight.py` — pre-task device prep

**What it does (confirmed):** Two-phase prep. Lightweight `preflight_before_publish`
applies MockGPS then launches Instagram — **no Portal tree poll** to avoid
spurious tree-fetch errors. At task start (`ensure_portal_tree_for_task`)
it checks the Portal accessibility-tree via three fallbacks
(`content://com.mobilerun.portal/state` shell query → driver — →
`uiautomator dump`), with a soft retry (a11y restart), and on persistent
failure a bounded reboot (cooldown-gated). After a reboot, re-applies the
mock and re-opens IG. Maintains per-serial reboot history in
`logs/.portal_reboot_state.json` (cap: 1 reboot / 15 min).

| Field | Value |
|---|---|
| FFF issue | [FFF-31](mention://issue/01ec6b9a-1fb1-435d-9d2a-b92948171fce) (publish flow — preflight is its first step), [FFF-25](mention://issue/4d8a0305-e7f2-4ad7-87dd-7a044ca37a70) (GPS apply lives in preflight) |
| Action | **Port the structure** (the *order* — mock first, IG second; portal-tree check at *task start*, not on every tick; bounded-reboot cooldown), **rewrite the Portal-tree part** under the new `MobileWorker` interface. The Mobilerun Portal-tree specifics belong inside the Mobilerun adapter; the *outer* preflight (mock + IG foreground) belongs in the worker step. |
| Do NOT copy | The MockGPS package constant `com.lilstiffy.mockgps` (see §1.7); the `_REBOOT_STATE_PATH` JSON file under `logs/` — promote to `automation.devices.last_reboot_at` or similar (do not store runtime state in flat files). |

### 1.9 `scenarios/post_ig_trial_reel.py` — Mobilerun goal + scenario glue

**What it does (confirmed):** Builds the natural-language **goal string** for
the Mobilerun MobileAgent (Path A/B/C instructions, hard-stop conditions,
mandatory caption sequence `paste_text → verify_caption_text → hide_ime →
tap_share_and_confirm`, exact `video_id` to verify against), wires the
agent's structured output to a `PostResult` Pydantic model, optionally
inserts a `crm.tasks` row at start and updates at finish, then on success
calls `mark_video_verify` (NOT mark posted — host sets `status=verify` for
the verifier to promote), and finally force-stops Instagram.

| Field | Value |
|---|---|
| FFF issue | [FFF-31](mention://issue/01ec6b9a-1fb1-435d-9d2a-b92948171fce) (upload flow MVP), [FFF-30](mention://issue/5681af3b-229d-43db-af1c-504bda621e73) (research — confirms the goal text shape) |
| Action | **Port the goal-template structure**: it is the actual goal string that works on real devices today. Re-keyed for FFFBT (caption language read from FFFBT account record, not `farm.accounts_db`; `video_id` from `automation.videos`). The success path that sets the video to **`verifying`** (FFFBT) — not `verify` — is correct (see §3 contradictions). |
| Do NOT copy | `crm/repository.py` (`SupabaseRepo`, `insert_task`/`update_task`) — that's a separate "crm" mini-schema, not the FFFBT `automation` schema. `farm.videos_db.mark_video_verify` — replaced by an `automation.videos` update through the new data-access layer. The Russian "поставщик" / "Portuguese only" caption-language branch (`_caption_lang_note`) — FFFBT MVP is English-only. The `recovery_note` / `learn_from_failed_post` Claude-recovery branch — out of MVP scope; can be revisited post-MVP. The trial-banner pixel constants `IG_NEW_REEL_SHARE_CAPTION_TAP_*` (those are Path C fallback coordinates for a specific 1080×~1794 device — keep as a Mobilerun-internal default if at all, not a public contract). |

### 1.10 `scenarios/verify_ig_trial_reels.py` — LLM-driven verifier (legacy)

**What it does (confirmed):** Earlier LLM-agent-based verifier. Builds a
goal that asks the agent to open Trial reels, read top-N captions, copy
each link, return a `VerifyTrialReelsResult`. Host-side
`apply_verify_matches_to_db()` then promotes matched DB rows to `posted`.

| Field | Value |
|---|---|
| FFF issue | [FFF-32](mention://issue/46825e0e-a64b-4321-9ce7-0168d2e217e0) (verification — historical context only) |
| Action | **Reference only.** This file is **superseded by `farm/verify_trial_tab.py`** in the same repo (note `scripts/verify_trial_reels_from_trial_tab.py` explicitly says *"Agent mode removed from default path. Use deterministic flow only."*). Keep the structured-result Pydantic schema (`TrialReelObserved`, `TrialReelMatch`) as inspiration for the FFFBT `StepResult` payload, but do not bring the LLM-goal-template path into MVP. |
| Do NOT copy | The whole agent-driven verify path. The duplicated `SupabaseRepo` task plumbing. |

### 1.11 `scripts/post_trial_reel_only.py` — end-to-end posting entrypoint

**What it does (confirmed):** Production runner. Reads `FARM_DEVICE_SERIAL`,
optional `FARM_DEVICE_PIN`, optional `FARM_VIDEO_ID`. If pin set, unlocks
the device. Runs the preflight (MockGPS + IG). Claims (or fetches) one
`status=new` video row, downloads its `link_drive` MP4 to local disk,
prepares + pushes it (`prepare_video_for_android` + `push_video_to_gallery`),
checks the portal tree, then runs a **bounded retry ladder** through
`post_ig_trial_reel` (`baseline → vision_max32 → vision_max40 →
vision_max44_stable → vision_max48`, stop early on hard failures like
`logged_out` / `trial_reels_unavailable` / `checkpoint` / `2fa` /
`action_blocked`). On total failure, optional Claude-driven recovery
attempts. On success, optionally chains the deterministic verifier
(`verify_trial_reels_from_tab` with the post's `video_id`).

| Field | Value |
|---|---|
| FFF issue | [FFF-31](mention://issue/01ec6b9a-1fb1-435d-9d2a-b92948171fce) (upload flow MVP), [FFF-55](mention://issue/6dd97e41-63b2-42da-babf-817a6fdc30bc) (generic job runner — chaining post → verify is the prototype) |
| Action | **Rewrite** under the FFFBT job-runner shell (FFF-55). The *shape* — preflight → host video prep → mobile UI → verify, with `StepResult` between steps and `automation.job_events` for every transition — is the right shape. Keep the **strategy ladder idea** (baseline → escalate budgets / vision on retry) as a worker-internal retry policy. |
| Do NOT copy | `_bootstrap()` parsing `.env`; `_claim_next_video_row` against `poker_videos`; the `download_publish_mp4` Google-Drive flow that uses MinIO mirror (`farm/minio_media.py` is out of scope — FFFBT MVP uses `VIDEO_DOWNLOAD_DIR` per `docs/contracts/video-source.md`); `farm.caption.caption_full_text` and `prepare_video_for_publish` (caption locking lives in the FFFBT video pipeline now); the Claude-driven recovery; `farm.device_unlock` (account PIN unlock is out of MVP scope — assume the device is unlocked). |

### 1.12 `scripts/verify_trial_reels_from_trial_tab.py` — deterministic verifier entrypoint

**What it does (confirmed):** Thin wrapper that reads `FARM_DEVICE_SERIAL`,
`FARM_IG_USERNAME`, `FARM_VIDEO_ID` and invokes
`farm.verify_trial_tab.verify_trial_reels_from_tab`. Returns a non-zero
exit code on failure (`exit_code()` mapping above). Explicitly deprecates
the `FARM_VERIFY_USE_AGENT` agent-mode.

| Field | Value |
|---|---|
| FFF issue | [FFF-32](mention://issue/46825e0e-a64b-4321-9ce7-0168d2e217e0) (verification option A — entrypoint shape) |
| Action | **Port** as the FFFBT verification step inside the FFF-55 job runner. There is no "script" in the new architecture — the same code lives behind the `verify` hook in the generic job pipeline. |
| Do NOT copy | `FARM_*` env-var contract; the env-file bootstrap. |

---

## 2. Final MVP flow (extracted from the real repo)

This is the end-to-end posting flow as actually implemented in
`scripts/post_trial_reel_only.py` + `scenarios/post_ig_trial_reel.py` +
`farm/device_preflight.py` + `farm/verify_trial_tab.py`. It maps 1:1 to the
FFFBT `job_state_machine` (`docs/contracts/job-state-machine.md`) and the
worker state machine (`docs/contracts/instagram-worker-state-machine.md`).

| # | MVP step | Mobilerun real-repo source | FFFBT job state | FFFBT worker state | Owner / issue |
|---|---|---|---|---|---|
| 1 | **Preflight device** (MockGPS apply, Instagram foreground; *do not* poll Portal tree yet) | `farm/device_preflight.py::preflight_before_publish` | `preparing_device` | _(pre-worker)_ | [FFF-25](mention://issue/4d8a0305-e7f2-4ad7-87dd-7a044ca37a70), Environment Loader |
| 2 | **Prepare video** (ffmpeg H.264 / yuv420p / 8-bit / +faststart; idempotent) | `farm/tools.py::prepare_video_for_android` | `publishing` | `prepare_video_for_android` | [FFF-29](mention://issue/389b4cf9-aac8-434f-a93c-6321623770d6) |
| 3 | **Push video** to device gallery + `MEDIA_SCANNER_SCAN_FILE` broadcast | `farm/tools.py::push_video_to_gallery` | `publishing` | `push_video_to_gallery` | [FFF-29](mention://issue/389b4cf9-aac8-434f-a93c-6321623770d6) |
| 4 | **Portal-tree check at task start** (shell `/state` → driver → uiautomator fallback); bounded reboot if dead | `farm/device_preflight.py::ensure_portal_tree_for_task` | `publishing` | _(adapter-internal)_ | [FFF-28](mention://issue/e78c70d2-b847-4d5e-9573-818470404d7f) (inside Mobilerun adapter) |
| 5 | **Run Mobilerun agent** with Path A→B→C goal (Profile → Pro dashboard → Trial Reels → Create; gallery pick; Next through editor; `advance_past_reel_timeline_editor` if needed) | `scenarios/post_ig_trial_reel.py` + `config/mobilerun/app_cards/instagram.md` | `publishing` | `open_instagram` … `navigate_editor_next_steps` | [FFF-31](mention://issue/01ec6b9a-1fb1-435d-9d2a-b92948171fce) |
| 6 | **Caption paste** — farm-standard: focus index 12 → fresh tree → resolve `caption_input_text_view` → `paste_text` via **Mobilerun Keyboard** (`content://com.mobilerun.portal/keyboard/input`); never `via_prompt=true` on first paste | `farm/tools.py::paste_text` + `paste_ig_share_caption_farm_standard` | `publishing` | `fill_caption` | [FFF-31](mention://issue/01ec6b9a-1fb1-435d-9d2a-b92948171fce), [FFF-50](mention://issue/c222ec7e-f0de-4c92-8000-b19d40fdd182) |
| 7 | **Verify caption** against the DB caption (`video_id`) before Share; on mismatch retry paste once (no `via_prompt`), never publish without verify | `farm/tools.py::verify_caption_text` | `publishing` | `verify_caption` | [FFF-31](mention://issue/01ec6b9a-1fb1-435d-9d2a-b92948171fce), [FFF-50](mention://issue/c222ec7e-f0de-4c92-8000-b19d40fdd182) |
| 8 | **Hide IME** then **Share + confirm** — single call to `tap_share_and_confirm`; gated by the per-device caption-verified flag; success when activity changes / `trials_list` visible / `share_button` gone (Activity is the strongest signal) | `farm/tools.py::hide_ime` + `tap_share_and_confirm` | `publishing` | `share_and_confirm` | [FFF-31](mention://issue/01ec6b9a-1fb1-435d-9d2a-b92948171fce), [FFF-50](mention://issue/c222ec7e-f0de-4c92-8000-b19d40fdd182) |
| 9 | **Set job to `verifying`** — host transitions the job; device stays reserved (option A) | `scenarios/post_ig_trial_reel.py::mark_video_verify` (port) | `publishing` → `verifying` | `update_job_result` (interim) | [FFF-32](mention://issue/46825e0e-a64b-4321-9ce7-0168d2e217e0) |
| 10 | **Wait verification delay** (15–30 s default; `verification_delay_seconds` from `automation.global_settings`, FFF-8 already seeded with 180 s) | `farm/trial_reel_settle.py` | `verifying` | _(timing)_ | [FFF-32](mention://issue/46825e0e-a64b-4321-9ce7-0168d2e217e0) |
| 11 | **Deterministic verification** — open `trials_list` (Path A then B), pull-to-refresh, walk newest N tiles, read caption with retries, copy link via paper-plane, match to DB caption (strict — caption + link), promote matched row | `farm/verify_trial_tab.py::verify_trial_reels_from_tab` + `farm/trial_reels_nav.py` | `verifying` | `verification` | [FFF-32](mention://issue/46825e0e-a64b-4321-9ce7-0168d2e217e0) |
| 12 | **Terminal job state** — `done` (video `released`) / `failed` / `needs_review`; release device reservation; close Instagram | `farm/tools.py::close_instagram_serial_sync` + caller | `done` / `failed` / `needs_review` | `update_job_result` | [FFF-32](mention://issue/46825e0e-a64b-4321-9ce7-0168d2e217e0), [FFF-55](mention://issue/6dd97e41-63b2-42da-babf-817a6fdc30bc) |

---

## 3. Contradictions with current FFFBT docs (must be resolved)

The current FFFBT docs were written **before** this archive landed. The
following four discrepancies have to be reconciled — in every case the
real-repo behaviour is the one that demonstrably works in production, so the
docs should be amended (not the other way around).

### 3.1 MockGPS package / provider — **CONTRADICTION**

- **FFFBT docs say:** Use `io.appium.settings/.LocationService` driven by
  `am start-foreground-service` (`docs/research/mockgps-integration.md` §1
  and §3). Recommendation tagged *confirmed* in the research doc.
- **Real repo uses:** `com.lilstiffy.mockgps`, a third-party UI-driven app,
  with hard-coded coordinate strings and ADB taps (`farm/mockgps_vn.py`).
  Per-account cafe pins live in a `FLEET_MOCK_BY_SERIAL` constant.
- **Why both can be true:** `io.appium.settings` is only present once Appium
  has run on the device. The fffbt production farm runs Mobilerun + GenFarmer
  on devices that may **not** have Appium installed; the third-party MockGPS
  app fills that gap.
- **Resolution proposal:** FFFBT MVP keeps the **`io.appium.settings`** primary
  decision (it is correctly justified — same headless control method, no
  reverse-engineered tap coordinates). The third-party UI driver becomes the
  documented fallback (already covered by §5 of the MockGPS research doc).
  Update [FFF-25](mention://issue/4d8a0305-e7f2-4ad7-87dd-7a044ca37a70) to
  call out *both* paths explicitly so the implementation can fall back
  cleanly on devices without `io.appium.settings`. The account → coordinate
  mapping must come from `automation.account_gps_locations`, never from a
  hard-coded constant.

### 3.2 Caption paste method — **CONTRADICTION (docs are stale)**

- **FFFBT docs say:** "Call **`paste_text` exactly once** with the full
  caption string, `clear=true`, `index=13`, and **omit `resource_id`**."
  (`docs/instagram-appcard-reference.md` §3.) Worker state machine
  enshrines this as a single `fill_caption` state.
- **Real repo says (newer):** Farm-standard is now **a two-step protocol** —
  `click index 12` to focus the caption (forces Mobilerun Keyboard ON), then
  on a *fresh* UI snapshot resolve `caption_input_text_view` (usually
  index 8 after IME), and `paste_text` at the resolved index using the
  **Mobilerun Portal IME** (`content://com.mobilerun.portal/keyboard/input`).
  Index 13 is now a *legacy* fallback for the Path-A trial banner layout
  before keyboard. `ADB_INPUT_B64` alone reports success but leaves the
  placeholder visible on IG Share — must NOT be the primary method. The
  `paste_text` tool already implements the protocol internally; the AppCard
  describes it as "farm-standard, all devices, operator-verified 2026-05-15".
- **Resolution proposal:** Update
  [`docs/instagram-appcard-reference.md`](../instagram-appcard-reference.md) §3
  and the `fill_caption` row of
  [`docs/contracts/instagram-worker-state-machine.md`](../contracts/instagram-worker-state-machine.md)
  to reflect the **12-focus / 8-paste / Mobilerun-Keyboard / never-via-prompt-first**
  protocol. Index 13 stays as a legacy fallback note.

### 3.3 Status lifecycle — **PARTIAL CONTRADICTION (naming, not semantics)**

- **FFFBT docs say:** Video status uses
  `new → reserved → uploading → verifying → released | failed | needs_review`
  (`docs/architecture.md`, `docs/contracts/video-source.md`,
  [FFF-7](mention://issue/1e1ad05d-3d9e-438e-b519-5978527a5d45)). Job state
  machine adds `queued → preparing_device → publishing → verifying → done /
  failed / needs_review` (`docs/contracts/job-state-machine.md`).
- **Real repo says:** `poker_videos.status` uses
  `new → verify → posted` (and `null/error` failure). The host sets
  `status=verify` after `tap_share_and_confirm` (see
  `scenarios/post_ig_trial_reel.py::mark_video_verify`); the deterministic
  verifier promotes to `posted` once caption + link match.
- **Why this isn't a hard conflict:** the *flow* is identical — there is
  exactly one host-side transition between "share succeeded" and "verified
  in trials list". The FFFBT names (`uploading`, `verifying`, `released`)
  and the legacy names (`new`, `verify`, `posted`) are 1:1 mappings. FFFBT
  has more granular `reserved` / `uploading` states because the queue is
  decoupled from the worker.
- **Resolution proposal:** No doc change needed; the porting work in
  [FFF-50](mention://issue/c222ec7e-f0de-4c92-8000-b19d40fdd182) must
  **rewrite** the status update calls — never reuse the literal strings
  `"verify"` / `"posted"` against `automation.videos`. Use the FFFBT names.
  The legacy `farm.videos_db.mark_video_*` helpers are **not** to be ported.

### 3.4 Verification logic — **CLARIFICATION needed in docs**

- **FFFBT docs say:** Two-level verification — Level 1 is immediate publish
  confirmation in `share_and_confirm`; Level 2 is "verify the freshly posted
  Trial Reel is visible in Trial Reels / Professional Dashboard if possible"
  (`docs/contracts/instagram-worker-state-machine.md` `verification` state,
  [FFF-32](mention://issue/46825e0e-a64b-4321-9ce7-0168d2e217e0) brief).
- **Real repo says (concrete):** Level 2 is **deterministic**, not
  best-effort. It opens `trials_list` (Paths A then B), **pull-to-refresh**,
  walks the newest visible tiles, reads each caption with black-preview
  retries, copies each link via the paper-plane Share sheet (never ⋮ More
  — the More menu has no Copy link), strictly matches DB caption + link,
  promotes the matched row. Failure exit codes are explicit
  (`profile_reels_grid=3`, `nav_trial_list=3`, `no_pending=4`,
  `caption_no_match=3`). `FARM_VERIFY_WEAK_MATCH` (URL-only match) is OFF
  by default.
- **Resolution proposal:** Sharpen
  [`docs/contracts/instagram-worker-state-machine.md`](../contracts/instagram-worker-state-machine.md)
  `verification` row from "if possible" to "must succeed". Add three
  failure modes to the worker error table:
  `profile_reels_grid` (landed on Reels grid instead of Trial reels list),
  `nav_trial_list` (Paths A and B both failed), `caption_no_match` (tile
  caption did not match DB). Each is `needs_review`.

### 3.5 Appium vs Mobilerun — **alignment confirmed**

- **FFFBT docs say:** Mobilerun-first behind a shared `MobileWorker`
  interface; Appium is contingency only ([ADR
  0002](../decisions/0002-mobilerun-first-worker.md)).
- **Real repo confirms:** Mobilerun-only. No Appium code anywhere in the
  archive. No `appium` import. The whole stack is Mobilerun + GenFarmer +
  ADB.
- **Resolution proposal:** No change needed — the ADR is correct. *Note*:
  `docs/instagram-appcard-reference.md` still has the legacy phrase
  "Appium Instagram worker's posting logic" at the top — this should be
  changed to "Mobilerun Instagram worker's posting logic" as part of the
  FFF-31 doc refresh.

---

## 4. Safety — what must NOT be copied

This list is a hard contract for the port work in [FFF-50](mention://issue/c222ec7e-f0de-4c92-8000-b19d40fdd182), [FFF-31](mention://issue/01ec6b9a-1fb1-435d-9d2a-b92948171fce), and [FFF-32](mention://issue/46825e0e-a64b-4321-9ce7-0168d2e217e0). Reviewers
should reject PRs that bring any of these in.

1. **`.env`** at the archive root — contains real `SUPABASE_SERVICE_ROLE_KEY`,
   account credentials, TOTP secrets. **Do not open, do not commit, do not
   echo into a comment.**
2. **`credentials.yaml`** referenced by `farm/agent.py` (`credentials=…`
   parameter loaded from disk) — same as above.
3. **`supabase/`** schema dump under the archive — that is the *old*
   `fffbt` + `poker_videos` schema. FFFBT's production schema is the new
   `automation` schema ([FFF-4](mention://issue/c948886d-75a7-486b-8ebf-560899b146d8),
   [FFF-7](mention://issue/1e1ad05d-3d9e-438e-b519-5978527a5d45)). Do not
   apply the archive's migrations against the FFFBT Supabase project. The
   archive is for **reference only** when designing the equivalent
   `automation.*` tables.
4. **`farm.videos_db.update_video_post_in_supabase` /
   `mark_video_posted` / `mark_video_verify` / `list_verify_videos` /
   `find_video_id_by_link`** and every other helper that writes to
   `poker_videos`. The status lifecycle is similar but the table is the
   *legacy* `fffbt` table, which is read-only/reference for FFFBT.
5. **Login / onboarding** — `farm/ig_login_*`, `farm/ig_login_deterministic.py`,
   `farm/ig_professional_deterministic.py`, `farm/ig_profile_bio.py`,
   `scripts/login_ig_deterministic.py`, `scripts/check_ig_login_fleet.py`,
   `scripts/run_farm_login_*`, `scripts/set_ig_professional_account.py`,
   `scripts/set_ig_profile_bio.py`, `scripts/collect_fleet_profile_bios.py`.
   FFFBT MVP explicitly excludes account onboarding, avatar / profile setup,
   switching to professional mode.
6. **Analytics / 24–72h decisions** — `farm/analyze_*.py`,
   `farm/publish_count.py`, `farm/publish_day.py`,
   `scripts/show_publish_stats.py`, `scripts/regenerate_*.py`. Out of MVP
   scope.
7. **Comments / DM** — there are no first-class comments/DM modules in this
   archive, which is fine; do not add any (out of scope).
8. **Telegram, RSS, football-news** — `farm/telegram_notify.py`,
   `farm/tg_human_error.py`, `farm/football_news_rss.py`,
   `config/football_rss_feeds.yaml`, the `scripts/start_*.ps1`
   scheduler-launch scripts, `scripts/preview_rss_captions.py`,
   `scripts/refresh_shopaikey_models.py`. Out of MVP scope.
9. **Claude-driven recovery / "learn"** — `farm/claude_*.py`,
   `farm/post_recovery.py`, `farm/shopaikey_llm.py`. Out of MVP scope.
10. **Per-account specifics** — usernames in `mockgps_vn.py::FLEET_MOCK_BY_SERIAL`
    (`goalmoments.fc`, `pure.football.fc`, `only.foot.goals`,
    `matchmoments.fc`, `foot.moments.fc`, `nevgulbayam_`, `yaprakozgumusj`).
    These must come from `automation.accounts`, not source code.
11. **Logs and `.farm_run_log.txt`** at the archive root — may contain
    captions, video IDs, screenshots, post URLs. Do not copy.
12. **Mobilerun internals** that look tempting to lift — `farm.portal_state`,
    `farm.uiautomator_tree`, `farm.patch_portal_fetch`,
    `farm.overrides` — keep them on the *adapter* side of FFF-28 only.
    They are stable Mobilerun extension points, not application code.

---

## 5. Issue-by-issue recommendations

### [FFF-28](mention://issue/e78c70d2-b847-4d5e-9573-818470404d7f) — Mobile UI session wrapper

- **Status:** `done` per Linear. **No re-open needed**, but the
  Mobilerun adapter behind the interface should adopt the real-repo
  `farm/agent.py::build_agent` shape on the next port:
  `(goal, device_serial, output_model, variables, overrides, timeout)`,
  with per-task overrides merging after `platform_defaults.yaml`.
- **Action:** Description update — add a "real implementation reference"
  pointer to `farm/agent.py` so the next adapter PR has a starting
  point. **No status change.**

### [FFF-50](mention://issue/c222ec7e-f0de-4c92-8000-b19d40fdd182) — Port Mobilerun custom tools

- **Status:** `backlog`. **Can start now**, on top of FFF-28.
- **Action:** Description update — replace the bullet list of tool names
  with the canonical list from §1.3 (add `advance_past_reel_timeline_editor`,
  `wait_trial_reel_settle`, `refresh_trial_reels_list`,
  `copy_reel_link`/`copy_reel_link_from_viewer`, `paste_ig_share_caption_farm_standard`
  — they're missing from the current brief but they are part of the
  working MVP flow). Add the §4 "do not copy" list as an explicit
  guardrail. Confirm the rewrite-target is the **`automation` schema**, not
  `poker_videos`. **Promote to `todo` once FFF-31's caption-protocol doc
  refresh lands** (so the port writes the right caption flow).

### [FFF-29](mention://issue/389b4cf9-aac8-434f-a93c-6321623770d6) — Video transfer to device

- **Status:** `backlog`. **Can start now** — this is the easiest, lowest-risk
  port (just `prepare_video_for_android` + `push_video_to_gallery`,
  ~150 LOC together).
- **Action:** Description is already aligned. Add a pointer to the exact
  files in this archive and a one-line note: "*idempotent: if the file is
  already H.264 / yuv420p / 8-bit, return the source path; do not
  re-encode*". Promote to `todo`.

### [FFF-30](mention://issue/5681af3b-229d-43db-af1c-504bda621e73) — Instagram upload flow research

- **Status:** `backlog`. **Can be closed (mostly) on documentation grounds**:
  most of the research questions are already answered by this archive
  (Professional dashboard, Trial Reels tile, Share screen layout, success
  signal, `trials_list`). Index 13 is *no longer valid as the primary
  caption index* — the answer is the 12-focus / 8-paste protocol (§3.2).
- **Action:** Description update — replace the open research questions with
  the corresponding answers (linked to this doc and the AppCard reference).
  Add the remaining genuine unknowns: which IG build is being targeted,
  what locale variants are seen on the current production accounts, and
  whether the IG team has shipped any Share-screen layout changes since
  2026-05-15. Once the AppCard reference is refreshed (§3.2), this issue
  can be marked `done` without device work — the device-side learnings are
  already captured.

### [FFF-31](mention://issue/01ec6b9a-1fb1-435d-9d2a-b92948171fce) — Instagram upload flow MVP

- **Status:** `backlog`. **Should wait for FFF-28 (done), FFF-29
  (in-flight), FFF-50 (in-flight).**
- **Action:** Description update —
  - Replace point 7 ("Fill caption through paste_text-like mechanism") with
    the explicit 12-focus / 8-paste / Mobilerun-Keyboard protocol (cite
    §3.2 of this doc).
  - Replace point 8 ("Verify caption before Share") with the *blocking*
    contract: `tap_share_and_confirm` must be gated by
    `verify_caption_text` against the DB caption.
  - Add point 13: "Use the Mobilerun goal-template shape from
    `scenarios/post_ig_trial_reel.py` (Paths A/B/C, hard-stop list)" with a
    link to this doc.
  - Add point 14: "Use a bounded retry ladder (baseline → vision → larger
    `max_steps`) on transient failures; stop early on hard failures
    (`logged_out`, `trial_reels_unavailable`, `checkpoint`, `2fa`,
    `action_blocked`)."
- **Promote to `todo` only after FFF-50.**

### [FFF-32](mention://issue/46825e0e-a64b-4321-9ce7-0168d2e217e0) — Verification option A

- **Status:** `backlog`. **Should wait for FFF-31** (the verifier needs a
  job to verify against).
- **Action:** Description update —
  - Replace "verify the freshly posted Trial Reel is visible in Trial Reels
    / Professional Dashboard if possible" with the explicit deterministic
    flow from §3.4 (open `trials_list` via Paths A then B,
    pull-to-refresh, walk newest N tiles, paper-plane → Copy link,
    caption-equality match, promote on caption + link).
  - Forbid URL-only ("weak") matching by default.
  - Add the three new failure modes (`profile_reels_grid`,
    `nav_trial_list`, `caption_no_match`) from §3.4 as `needs_review`
    causes in the worker state machine.
  - Reuse `verification_delay_seconds` from `automation.global_settings`
    (FFF-8) rather than introducing a new env var.
- **Promote to `todo` after FFF-31.**

### [FFF-25](mention://issue/4d8a0305-e7f2-4ad7-87dd-7a044ca37a70) — GPS apply interface

- **Status:** `backlog`. **Can start now** — independent of the worker stack.
- **Action:** Description update —
  - Primary path: `io.appium.settings/.LocationService` via
    `am start-foreground-service` (no change from the MockGPS research doc).
  - Fallback path: `mockgps_vn.py`-style UI driving for a third-party
    mock-location app (e.g. `com.lilstiffy.mockgps`) when
    `io.appium.settings` is unavailable on a device. **The package is
    configurable per device, never hard-coded.**
  - Verify step: `dumpsys location` read-back; functional check after
    Instagram launch.
  - Coordinates source: `automation.account_gps_locations` (FFF-8 schema).
    Never a Python constant.
  - Errors emit `automation.job_events` rows with `event_type='gps_apply'`
    and pass through the FFF-56 `StepResult` shape.
- **Promote to `todo`.**

### What can start in parallel right now

| Issue | Why now |
|---|---|
| [FFF-29](mention://issue/389b4cf9-aac8-434f-a93c-6321623770d6) | Smallest port surface, no dependencies on the other in-flight work. |
| [FFF-25](mention://issue/4d8a0305-e7f2-4ad7-87dd-7a044ca37a70) | Independent stack; FFF-28 (done) provides the device session contract. |
| [FFF-50](mention://issue/c222ec7e-f0de-4c92-8000-b19d40fdd182) | Provides the building blocks used by FFF-31. Can start as soon as FFF-31's doc refresh lands (§3.2). |
| FFF-30 doc refresh | Tiny doc PR; no device work. Closes the research box. |

### What must wait

| Issue | Wait for |
|---|---|
| [FFF-31](mention://issue/01ec6b9a-1fb1-435d-9d2a-b92948171fce) | FFF-29 + FFF-50 (needs the prep/push tools and the caption + share tools). |
| [FFF-32](mention://issue/46825e0e-a64b-4321-9ce7-0168d2e217e0) | FFF-31 (needs published posts to verify) and FFF-50 (uses the verify-tab navigation tools). |

---

## 6. Open questions surfaced by this archive

These are **new** unknowns introduced by the real-repo evidence and not yet
covered in `docs/architecture.md` §7. Each should become its own issue if
picked up.

1. **Mobilerun Keyboard / Portal IME availability on FFFBT devices.** The
   farm-standard caption paste depends on `com.mobilerun.portal` being the
   active IME. The Portal IME is installed as part of GenFarmer / Mobilerun
   provisioning. FFFBT needs an explicit step that *guarantees* the Portal
   IME is installed and enabled on every assigned device, otherwise the
   caption protocol regresses to `ADB_INPUT_B64`. *Status: assumption,
   confirm with the Environment Loader.*
2. **Bounded reboot policy.** The real repo allows 1 reboot per 15 min per
   serial. FFFBT has no doc for that policy yet. *Status: open.*
3. **Black-preview retries** in the verifier — IG sometimes shows an empty
   tile thumbnail and the viewer takes ~10 s to surface the caption. The
   real repo retries up to 4× with `FARM_REEL_CAPTION_READ_ATTEMPTS=4`. The
   FFFBT verifier step must allow the same. *Status: confirmed pattern.*
4. **Caption equality semantics.** `text_matches_expected` in
   `farm/caption_match.py` is whitespace-tolerant and accepts truncated UI
   captions ("…"). FFFBT MVP should adopt the same definition explicitly in
   the worker state machine doc. *Status: confirmed pattern.*
5. **Trial Reel banner Path-A index drift.** The Path-A trial banner adds
   one row to the tree, which is why a *legacy* caption index of 13 still
   appears in places. New port code must not hardcode 13; it must use the
   12-focus / fresh-tree / paste-at-resolved-index protocol from §3.2.
   *Status: confirmed pattern.*

---

## 7. Pointers (read-only)

The following archive paths are referenced above. They live in
`fffbt-mobilerun.zip` attached to this issue; they are **not** in the FFFBT
tree.

```
config/mobilerun/app_cards/instagram.md
farm/agent.py
farm/tools.py
farm/trial_reels_nav.py
farm/trial_reel_settle.py
farm/verify_trial_tab.py
farm/mockgps_vn.py
farm/device_preflight.py
scenarios/post_ig_trial_reel.py
scenarios/verify_ig_trial_reels.py
scripts/post_trial_reel_only.py
scripts/verify_trial_reels_from_trial_tab.py
```
