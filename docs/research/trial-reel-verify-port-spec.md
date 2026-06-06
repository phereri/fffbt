# Trial Reel verification (option A) — port specification

- Status: design (algorithm distilled, no code yet)
- Source: `farm/verify_trial_tab.py` (~857 LOC) + `farm/trial_reels_nav.py`
  (~945 LOC) from the 2026-06-06 real-repo snapshot
  (`fffbt-mobilerun (1)`). **Do not copy verbatim** — these files write
  to the legacy `poker_videos` table via `farm.videos_db.mark_video_posted`,
  read `farm.caption_match`, and call `farm.tools.ensure_mobilerun_keyboard`.
  The FFFBT port replaces all of that with the `automation.videos`
  schema and `MobileWorker`-based UI control.
- Target FFFBT issue: FFF-32 (verification option A).
- Scope: this file is the **contract** the port PR has to satisfy. Once
  the port lands, this doc gets one line pointing at the implementation;
  nothing else changes here.

---

## 1. What "verification" means here

After `MobileUIAutomationStep` returns OK, the job is in `verifying`. The
video row is set to `verifying` and the device is **still reserved**.
The verifier's job is to confirm the post is actually live in IG's
Trial Reels list and then promote the video to `released` with the
captured `post_url`.

This is the **deterministic** path: no LLM agent. The real farm has both
agent-driven and deterministic verifiers; the deterministic one is the
one currently in production (the agent variant is marked deprecated
upstream).

Why deterministic: IG sometimes hides a just-posted reel until the
Trial list is pulled-to-refresh, and the visible thumbnail can stay
black for ~10s after publish. Both are easy to handle in a tight loop
with explicit retries, but waste a lot of agent budget if delegated.

---

## 2. End-to-end flow

The verifier runs as a step in the worker pipeline, called *once* after
the `verification_delay_seconds` settle wait. Pseudocode (~150 LOC in
the port, not counting the nav module):

```
async def verify_trial_reel(worker, account, video_row, *, settings):
    # 0. Early-out: row already promoted (idempotent re-run).
    if video_row.status == "released" and video_row.post_url:
        return ok(promoted=0, link=video_row.post_url)

    # 1. Preflight (lazy — see device-preflight-port-spec.md).
    portal_ready = await ensure_portal_tree_for_task(worker.serial)
    if portal_ready.rebooted:
        await apply_pre_publish_environment(worker.serial)
    await ensure_mobilerun_keyboard(worker)  # clipboard read needs it

    # 2. Navigate to Trial Reels list.
    nav_result = await navigate_to_trial_reels_list(worker)
    if nav_result.screen == "profile_reels_grid":
        return fail("profile_reels_grid")
    if nav_result.screen != "trial_reels_list":
        return fail("nav_trial_list")

    # 3. Pull-to-refresh; wait for at least one tile to appear.
    await wait_trial_list_with_refresh(worker, min_tiles=1)

    # 4. Walk newest N tiles.
    tiles = []
    seen_urls: set[str] = set()
    for page in range(SCROLL_PAGES):  # default 3
        for pos in newest_indices(worker.ui, limit=TILE_CAP):  # default 12
            tile = await inspect_tile(worker, pos, video_row, seen_urls)
            tiles.append(tile)
            if tile.matched_video_id:
                return ok(
                    promoted=1,
                    link=tile.post_url,
                    matched=tile.matched_video_id,
                    tiles=tiles,
                )
        await scroll_trial_reels_grid(worker)

    # 5. Out of tiles — caption never matched.
    return fail("caption_no_match", tiles=tiles)
```

`inspect_tile` is the per-tile sub-routine:

```
async def inspect_tile(worker, pos, video_row, seen_urls):
    await open_trial_reel_at_list_position(worker, pos)
    caption = await read_reel_caption(worker, retries=4)
    if not caption_matches(video_row.caption, caption):
        await back_to_trial_list(worker)
        return TileResult(pos, caption, None, None, note="caption_mismatch")
    ok, url = await copy_reel_link_from_viewer(worker)
    await back_to_trial_list(worker)
    if not ok or not _is_ig_post_url(url):
        return TileResult(pos, caption, None, None, note="copy_link_failed")
    if url in seen_urls:
        return TileResult(pos, caption, url, None, note="duplicate_url")
    seen_urls.add(url)
    return TileResult(pos, caption, url, video_row.id, note="match")
```

---

## 3. Navigation contract (port `farm/trial_reels_nav.py`)

The navigation module is the bulk of the deterministic flow. Each
function is testable in isolation with a fake UI source.

### 3.1 Screen classifiers (pure, given a UI node list)

| Function | Returns | What it checks |
|---|---|---|
| `on_trial_reels_list(elements, strict=True)` | bool | `action_bar_title == "Trial reels"` AND `trials_list` resource-id present AND `draft_entrypoint_container` count > 0 |
| `on_profile_reels_grid(elements)` | bool | Profile Reels grid markers — `reels_tab` / username-titled action bar — and **no** `trials_list` |
| `on_ad_tools_screen(elements)` | bool | `action_bar_title == "Ad tools"` — the Path B mis-tap recovery hook |
| `on_reel_viewer(elements)` | bool | Viewer-only nodes (`viewer_container` / `direct_share_button`) present |
| `trial_reels_title(elements)` | str | Returns the current action-bar title, lowercased. Used in error logs. |
| `describe_ig_screen(elements)` | str | Short human-friendly screen tag for logs (`trial_list:N tiles`, `profile_reels`, `viewer`, `unknown:<title>`) |
| `trial_thumb_indices(elements, *, limit=2)` | list[int] | Newest-first tile positions (1-based; position 1 = top-left) |

Pure functions stay pure — no `await`, no ADB. Move all I/O into the
async helpers below.

### 3.2 Navigation paths (async, drive the UI)

| Function | Behavior |
|---|---|
| `navigate_to_trial_reels_list(worker)` | Try **Path A** (Profile → Professional dashboard → Trial reels). If `on_trial_reels_list` is False after path A, try **Path B** (Profile → ⋯ menu → Settings → For professionals → Account type and tools → Trial reels). If Path B opens Ad tools, press Back and stop. Returns a result dataclass naming the final screen. |
| `refresh_trial_reels_list(worker)` | Pull-to-refresh on the Trial list. Real-farm config `FARM_TRIAL_REEL_REFRESH_PULLS=2`. Port keeps a `REFRESH_PULLS` constant (2). |
| `wait_trial_list_with_refresh(worker, *, min_tiles)` | Block until `len(trial_thumb_indices) >= min_tiles` OR the bounded refresh loop exhausts (default 3 refresh cycles with 1.5s waits). |
| `open_trial_reel_at_list_position(worker, pos)` | Tap the tile at 1-based position; retry once if the viewer shows a black preview for >5s. |
| `back_to_trial_list(worker)` | Tap `action_bar_button_back`, then verify the resulting screen is the list again. |
| `scroll_trial_reels_grid(worker)` | Vertical swipe on the trial-list to reveal the next page. Uses worker `swipe`; coordinates are derived from the grid bounds, not hardcoded pixels. |
| `read_reel_caption(worker, *, retries=4)` | Open Reel viewer, scroll inside if needed, read the caption text. `FARM_REEL_CAPTION_READ_ATTEMPTS=4` is the real-farm default; the port keeps the same constant. |
| `copy_reel_link_from_viewer(worker)` | Paper-plane share button → tap **Copy link** → read clipboard. Returns `(ok, url)`. Multi-strategy clipboard read: 1) `dumpsys clipboard` 2) logcat scan 3) `uiautomator dump` XML search 4) Chrome paste fallback 5) AdbKeyboard broadcast (already covered by Mobilerun Portal IME). |

### 3.3 Forbidden tactics

- **Never** use ⋮ More menu to grab a link — the More menu does not
  expose "Copy link" on the Trial Reel viewer; the paper-plane share
  sheet does. The real farm enforces this; the port must too.
- **Never** rely on a single hardcoded pixel swipe. `scroll_trial_reels_grid`
  must compute swipe coordinates from the bounding box of the visible
  list, falling back to `[540, 1500 → 540, 650]` only as a *default*
  on a 1080×1920 device.
- **Never** treat the Reels viewer URL as authoritative if the caption
  doesn't match. Caption + link both must match before promotion. The
  real farm's `FARM_VERIFY_WEAK_MATCH=1` URL-only fallback is **off**
  in the FFFBT port — do not expose the flag.

---

## 4. Caption matching

A pure function, ported from `farm/caption_match.py::text_matches_expected`:

- Strip surrounding whitespace.
- Collapse internal whitespace runs to a single space.
- Lowercase Unicode-normalise (NFC).
- Strip a trailing `…` ellipsis or `...` from the UI side (IG truncates
  long captions in the viewer).
- Truthy when the UI string is either an exact match OR a prefix of the
  expected string (after the same normalisation) with the suffix length
  ≤ 6 chars (the IG truncation indicator is 1 char visually but the
  invisible cut can vary).

The port also exposes the inverse for tests: `caption_mismatch_explanation`
returns a short human-readable diff suitable for `job_events`.

---

## 5. Result schema

```python
@dataclass(frozen=True)
class TileResult:
    position: int               # 1-based
    ui_caption: str | None
    post_url: str | None
    matched_video_id: str | None
    note: str                   # match | caption_mismatch | copy_link_failed | duplicate_url | open_failed


@dataclass(frozen=True)
class VerifyResult:
    success: bool
    promoted: int               # 0 or 1
    video_id: str | None        # promoted row id, if any
    post_url: str | None        # link captured for the promoted row
    failure_reason: str | None  # see §6
    tiles: list[TileResult]


def to_step_result(verify: VerifyResult) -> StepResult: ...
```

`to_step_result` maps to existing `StepResult` codes:

| `failure_reason` | `StepStatus` | `code` | Account side effect |
|---|---|---|---|
| `None` (success) | `OK` | — | — |
| `no_pending` | `OK` | `verify_no_pending` | — (idempotent re-run) |
| `profile_reels_grid` | `NEEDS_REVIEW` | `profile_reels_grid` | — |
| `nav_trial_list` | `NEEDS_REVIEW` | `nav_trial_list` | — |
| `caption_no_match` | `NEEDS_REVIEW` | `caption_no_match` | — |
| `copy_link_failed` | `NEEDS_REVIEW` | `verify_copy_link_failed` | — |

All three new `needs_review` codes need rows in
`automation.error_catalog` (target_job_status=`needs_review`, max_retries=0,
no account side effect). The port PR adds them in a single migration.

---

## 6. Failure modes (concrete)

- **`no_pending`** — the video row already has `status='released'` AND
  a non-null `post_url`. Idempotent re-run: returns OK with `promoted=0`
  and the existing link. Not an error.
- **`profile_reels_grid`** — Path A landed on the Profile Reels grid
  instead of the Trial Reels modal list. Usually means the device's IG
  build does not expose Trial Reels on this account, OR an account
  switch happened mid-job. Always `needs_review`.
- **`nav_trial_list`** — Paths A and B both failed to reach
  `on_trial_reels_list(strict=True)`. Persist the formatted UI tree as
  an artifact (`verify_nav_fail.json`) for triage.
- **`caption_no_match`** — Walked up to `SCROLL_PAGES × TILE_CAP` tiles
  (default 3×12=36), every caption matched a *different* pending row or
  none. Persist the tile-result list as an artifact for triage. The
  most common real cause is that the post never actually published
  (Share registered the event but IG silently dropped it) — `needs_review`
  with a hint suggesting the operator check the account's profile in IG.
- **`copy_link_failed`** — Caption matched but the clipboard read kept
  returning empty or a non-IG URL. Usually a Mobilerun Portal IME
  issue. Promote the row to `verifying` anyway and let the operator
  add the link manually, OR keep it as `needs_review` (the port PR
  picks one — recommend `needs_review` for safety).

---

## 7. Where the new code lives

| New module / file | Role |
|---|---|
| `src/worker/verify/screens.py` | Pure screen classifiers from §3.1. |
| `src/worker/verify/navigation.py` | Async path A/B + scroll + refresh + viewer helpers from §3.2. |
| `src/worker/verify/clipboard.py` | The multi-strategy clipboard read for `copy_reel_link_from_viewer`. |
| `src/worker/verify/caption_match.py` | `text_matches_expected` + `caption_mismatch_explanation`. |
| `src/worker/verify/runner.py` | The top-level `verify_trial_reel` orchestrator from §2. |
| `src/worker/steps/verification.py` | Replace existing stub with a thin wrapper around `verify_trial_reel`. |
| `tests/worker/verify/test_screens.py` | Screen-classifier table tests with fixture UI snapshots. |
| `tests/worker/verify/test_caption_match.py` | Caption matching truth table. |
| `tests/worker/verify/test_navigation.py` | Async nav tests with a fake `MobileWorker`. |
| `tests/worker/verify/test_runner.py` | End-to-end runner test with mocked nav + clipboard. |
| `supabase/migrations/<next>_verify_error_codes.sql` | Add `nav_trial_list`, `profile_reels_grid`, `caption_no_match`, `verify_copy_link_failed` to `automation.error_catalog`. |

The fixture UI snapshots come from real-device dumps captured during
the first Stage 1 happy-path run — they go under
`tests/worker/verify/fixtures/*.json` and are checked in.

---

## 8. Forbidden imports (hard contract)

The port PR **must not** import or reference:

- `farm.videos_db` — DB writes go through whatever
  `src/worker/db/videos.py` exposes (today: nothing; the port adds the
  `mark_video_released(video_id, *, post_url, account_username)`
  function as the only allowed write path).
- `farm.caption_match` — port `text_matches_expected` inline; it is
  ~30 LOC.
- `farm.tools.ensure_mobilerun_keyboard` — wrap the IME enable in
  `src/worker/verify/clipboard.py` (~20 LOC).
- `farm.device_preflight.preflight_before_publish` — call the FFFBT
  port from `device-preflight-port-spec.md` instead.
- `FARM_VERIFY_WEAK_MATCH` / `FARM_VERIFY_ANY_ACCOUNT` env vars — both
  intentionally absent; the FFFBT contract is strict caption+link match
  against the per-account pending row.
- `farm.claude_verify_learn` — Claude-recovery is out of MVP scope.

If a reviewer sees any of these in the port PR, the right action is to
push back, not to merge with a TODO.

---

## 9. Acceptance tests (write these alongside the port)

Each scenario is a single pytest case driving the runner with a fake
worker and a controlled UI fixture sequence.

1. **Happy path — first tile is a match.** One pending row, one tile
   in the list, caption matches, copy link succeeds. Asserts
   `promoted=1`, `post_url` set, `video_row.status` would be `released`
   (via the DB seam).
2. **Idempotent re-run.** Pending row already has `status='released'`
   and a `post_url`. Asserts no UI calls were made and result is OK
   with `promoted=0`.
3. **Pull-to-refresh recovers the freshly posted reel.** First UI
   snapshot has zero tiles; after one refresh, the tile appears and
   matches.
4. **Caption mismatch on first tile, match on second.** First tile's
   caption belongs to a different reel; second tile matches the pending
   row. Asserts `tiles` list has two entries and the second is the
   match.
5. **Path A lands on profile reels grid.** The fake nav returns the
   profile grid; verifier returns `failure_reason='profile_reels_grid'`
   and persists the UI dump as an artifact.
6. **Path A fails, Path B reaches the trial list.** Asserts the runner
   tried both paths in order.
7. **Path B opens Ad tools.** Recovery path: press Back, return
   `failure_reason='nav_trial_list'` after both paths exhausted (no Ad
   tap, no boost).
8. **Caption never matches across 3 scroll pages.** Asserts
   `failure_reason='caption_no_match'` and `len(tiles) >= 36 - tolerance`.
9. **Copy link returns empty clipboard.** Asserts
   `failure_reason='verify_copy_link_failed'` and the tile note is
   `copy_link_failed`.
10. **Duplicate URL across tiles.** Two tiles with matching caption
    return the same URL; only the first is treated as a match, the
    second has note `duplicate_url`. (Defends against an IG quirk where
    older trials can sometimes carry the same caption as the newest.)

All UI fixtures come from real-device dumps and are checked in. The
fake clock advances 100ms per `await asyncio.sleep` call, so the
tests run in well under a second each.

---

## 10. What is explicitly out of scope for the port PR

- **Agent-driven verifier.** The real-repo `scenarios/verify_ig_trial_reels.py`
  builds an LLM goal for the verifier — the FFFBT port skips it
  entirely. Even the agent-side scripts/post_trial_reel_only.py
  fallback (`FARM_VERIFY_USE_AGENT`) is explicitly off.
- **MockGPS-related verify retries.** The real farm reads
  `FARM_VERIFY_SKIP_MOCK=1` — that flag is meaningless until FFF-25
  lands; treat it as always-set for the port.
- **Profile Reels grid as a fallback verification source.** The real
  farm has `FARM_VERIFY_PROFILE_REELS_FALLBACK` — off by default and
  off in the port too. Trial Reels are verified from the Trial list
  only.
- **Multi-tile promotion.** The port promotes exactly one row per run.
  If two pending rows happen to match two visible tiles, that is two
  separate verify runs.
- **Telegram / Slack notifications.** Out of scope.
- **`farm/trial_reel_settle.py` randomized 15–30s wait.** The settle
  wait lives in the pipeline (already covered by
  `automation.global_settings.verification_delay_seconds`), not in the
  verifier itself.

---

## 11. Implementation order (for the port PR)

1. New migration adding the four `needs_review` error codes.
2. `src/worker/verify/screens.py` + tests. Self-contained.
3. `src/worker/verify/caption_match.py` + tests. Self-contained.
4. `src/worker/verify/clipboard.py` + tests with a fake ADB.
5. `src/worker/verify/navigation.py` + tests with a fake `MobileWorker`.
6. `src/worker/verify/runner.py` + the 10 acceptance tests from §9.
7. `src/worker/db/videos.py` — add `mark_video_released`.
8. `src/worker/steps/verification.py` — replace the existing stub.
9. Pipeline change: ensure the verification step is invoked
   post-`mobile_ui_automation` for `proof_of_posting`.
10. README §11 + runbook updates (verify codes table).

Each step is its own commit on the port branch; tests pass at every
commit. Total port size estimate: ~600 LOC code + ~800 LOC tests.
