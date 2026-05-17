# Instagram AppCard Reference

Extracted from the Mobilerun `config/mobilerun/app_cards/instagram.md` operator
guide. This is the authoritative reference for the Appium Instagram worker's
posting logic on the fffbt device farm.

Applies to: **Instagram Reels Trial posting only** (MVP scope).

## 1. Trial Reel mandatory flow

Trial Reels are ALWAYS published via the Professional dashboard. Do NOT use the
bottom-nav "+" / "Create" button or look for a "Trial" toggle on the standard
Share screen.

Entry point:

```
Profile → Professional dashboard → Trial Reels tile → Create / + / Try it
```

The dashboard tile launches the composer in Trial mode automatically.

Step-by-step:

1. Navigate to the Profile tab.
2. Tap **"Professional dashboard"** (may appear as "Professional Tools" or
   "Pro dashboard" depending on locale).
3. Inside the dashboard, tap the **"Trial Reels"** tile (may be labelled
   "Trial reel", "Trial", or appear under "Tools to grow").
4. Tap the create entry: "Create", "Try it", "Get started", or a centred "+".
5. In the composer, switch to the gallery tab if needed, pick the most recent
   video (the file pushed by `push_video_to_gallery`).
6. Tap "Next" / arrow forward through editor screens. Do NOT enter "Edit cover".
7. On the Share screen: fill the caption and share (see sections 3 and 4 below).

On a successful share you land on one of:

- `com.instagram.android/.modal.ModalActivity` showing the `trials_list`
  (freshly posted reel is the first `trial_thumbnail_image` tile) — happy path.
- The home feed or profile.

In either case the post is LIVE — do not retry.

## 2. Hard stop conditions

Stop immediately and report `success=false` with a descriptive `failure_reason`
if any of the following are detected:

| Condition | `failure_reason` |
|-----------|-----------------|
| "Action blocked", "We restrict certain activity", "Try again later" | `action_blocked` |
| Two-factor / login challenge / email-code screen | `login_challenge` |
| Account suspended or checkpoint | `account_suspended` |
| Professional dashboard tile absent on the account | `trial_reels_unavailable` |
| Trial Reels tile absent inside the dashboard | `trial_reels_unavailable` |
| Account is logged out (login screen appears) | `logged_out` |
| Destructive dialog (Logout, Delete, Block) appears unexpectedly | `unexpected_destructive_dialog` |

When the Professional dashboard or Trial Reels tile is missing, do NOT fall
back to posting a normal Reel.

## 3. Caption field rules

The Share screen has a specific layout. Getting the caption target wrong is a
common failure mode.

### Locating the field

The real caption field is the **large multiline `AutoCompleteTextView`** with
resource-id `caption_input_text_view`, located **under the preview thumbnail**,
with hint text like **"Write a caption"**.

Below the caption field is a chip row with **Prompt**, **#** (Hashtags), and
**Link a reel**. These are NOT the caption — never tap them for caption entry.

### Entering text

On the fffbt farm (hard layout for Trial Reels):

- **No swipes / no scrolling** on the Share screen before caption entry. Any
  scroll remaps Mobilerun indices.
- Call **`paste_text` exactly once** with the full caption string, `clear=true`,
  `index=13`, and **omit `resource_id`**. The tool resolves
  `caption_input_text_view` + caption hint internally; index 13 is a fallback.
- Do NOT use separate `click` before or after `paste_text` for the caption.
- After `paste_text`, call **`verify_caption_text`** with the exact same
  caption. If verification fails, stop with `failure_reason="caption_mismatch"`.
  Do not publish.

### Text input general rules

- For text longer than ~5 characters, use `type_humanized` (per-keystroke
  jitter) — except on the Trial Reel Share caption where `paste_text` is used.
- After any text input, sleep ~700–1500 ms before pressing the next affirmative
  button (Save / Next / Share). Compose-based fields finalize state on focus
  loss.
- After typing into a caption / search field, always dismiss the IME (press
  Back or tap a non-input area) before scrolling or tapping the next control.
  Autocomplete popups frequently cover the bottom of the screen.
- Caption language: English only (Latin script).

## 4. Share confirmation rules

Never use raw `click {index}` for the Share button. Use dedicated tools:

- **`tap_share_and_confirm`** — the designated tool for the Share button. It:
  1. Auto-hides the Mobilerun Portal IME (which otherwise covers the bottom of
     the screen and silently swallows taps).
  2. Taps `com.instagram.android:id/share_button` with a real-finger swipe
     (90 ms touchscreen gesture, Compose-friendly).
  3. Verifies the post registered (Activity left `ModalActivity` or
     `share_button` is gone).
- If `tap_share_and_confirm` returns failure, stop with
  `failure_reason="share_did_not_register"`.

Other critical button tools (for non-Share buttons):

| Tool | Use case |
|------|----------|
| `tap_by_resource_id` | Taps by `resource-id`. Survives index churn. |
| `tap_by_text` | Fallback when no stable resource-id (e.g. "Allow", "Skip", "Done"). |
| `hide_ime` | Call before any bottom-of-screen tap if you just typed into a field. `tap_share_and_confirm` calls this internally; for other bottom buttons call it yourself. |

## 5. Index hygiene rules

UI element indices shown in the Mobilerun tree are tied to the snapshot for
that step only. They change after scrolls, swipes, keyboard events, layout
reflows, or even ~1 s of inactivity. Treat indices as **throwaway hints, not
durable identifiers**.

Events that invalidate all previously seen indices:

- Pressing system Back / Home / Recents.
- Typing or dismissing the IME.
- Tapping a button that opens/closes a sheet, dialog, or new screen.
- Any scroll or swipe (RecyclerView / gallery / feed re-layout).
- Waiting more than ~1 s without acting.

For critical buttons (Share / Publish / Confirm / Done / Allow / Skip), always
use a stable tool (`tap_by_resource_id`, `tap_share_and_confirm`, `tap_by_text`)
instead of `click {index}`.

Raw `click {index}` is acceptable only for non-critical UI (gallery thumbnails,
list rows) against the fresh snapshot of the current step.

## 6. Post URL — optional behavior

The post URL is **optional**. Once `tap_share_and_confirm` succeeds, the post
is live regardless of whether a URL is captured.

Constraints:

- Spend at most ~3 agent steps trying to capture the URL.
- Instagram does not expose a numeric URL for ~1–2 minutes after a fresh Trial
  Reel publishes. The reel can be visible in the dashboard list before its
  public link is reachable.
- If capture fails, leave `post_url` empty and continue with `success=true`.
- Do NOT re-share, navigate the gallery in circles, or retry the whole flow
  just because `read_clipboard` returned nothing.

Best-effort capture flow (skip on the slightest friction):

1. Profile tab → Reels grid. Tap the freshest thumbnail (top-left).
2. Open the share / overflow menu (`⋮` or paper-plane icon) → "Copy link".
3. Read the clipboard via the `read_clipboard` custom action.

## 7. Common failure modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Black-screen Reel preview after publish | 10-bit / yuv444 codec issue | Re-encode to H.264 yuv420p via `prepare_video_for_android` before posting. Do not retry the upload before the file is fixed. |
| Caption field opens "Edit cover" | Tapped too high on the thumbnail | Re-read the tree; the caption box is lower: `caption_input_text_view` under the preview, above the chip row. |
| Wrong widget focused (Prompt / # sheet opens) | Tapped a chip below the caption | The caption is the large `AutoCompleteTextView` above the chip row. Use only `paste_text` with `caption_input_text_view`. |
| Autocomplete popup hides bottom of Share screen | IME not dismissed | Dismiss the IME (Back or tap above popup). Do not scroll while it's open. Do not conclude "the toggle is missing". |
| Account-switching dialog after launch | Multiple accounts on device | Pick the username explicitly given by the task. Do not pick the first row blindly. |

## Platform defaults (from Mobilerun config)

For reference, the fffbt farm runs Instagram with these defaults:

- `stealth: true` — no rapid taps; `wait_for_stable_ui` between actions.
- `max_steps: 25` — per-task step budget.
- `manager_vision: false`, `executor_vision: false` — UI-tree only by default.
  Vision is an emergency fallback enabled per-run via overrides.
- Coordinate-based tools (`click_at`, `click_area`, `long_press_at`) are
  disabled globally.
- `save_trajectory: action` — trajectories saved per action for audit.
