# Instagram (com.instagram.android) — Operator Guide

You are operating an authentic Instagram account on a real Android phone in a
device farm. Behave like a careful human user, not a script. Always rely on the
**Mobilerun UI tree and flat indices** on the **current** screen (indices change
after scroll). Never tap raw coordinates.

**End-to-end checklist:** `docs/instagram-appcard-reference.md`

## Global rules

- Stealth on. Never chain rapid taps. Wait for `wait_for_stable_ui` between
  actions and add idle time after navigation.
- For ANY text longer than ~5 characters always use the custom action
  `type_humanized` (per-keystroke human jitter) **except** on Instagram **Trial
  Reel Share** captions: the farm task uses **`paste_text`** once with
  `caption_input_text_view` (see "Posting a Trial Reel"). The built-in `type`
  action pastes the whole word at once and reads as a robot — avoid it for long
  strings.
- After any text input always sleep ~700-1500 ms before pressing the next
  affirmative button (Save / Next / Share). Compose-based fields finalize
  state on focus loss.
- If a destructive dialog appears (Logout, Delete, Block), STOP and report
  unless the running task explicitly requested it.
- Auto-complete suggestion popups frequently cover the bottom of the screen
  and steal cursor focus. After typing into a caption / search field always
  dismiss the IME (press Back or tap a non-input area) before scrolling or
  tapping the next control.

### Index hygiene (CRITICAL)
UI element **indices** (the numeric ids Mobilerun shows in the tree) are tied
to the **snapshot for that step only**. They change after you **scroll or
swipe** (any list / gallery / feed motion), after a keyboard or autocomplete
sheet, or when the layout reflows — even on the **same** screen. Treat indices
as throwaway hints, not durable identifiers. Re-read the visible TEXT of the
element right before tapping it — if the text in the tree no longer matches what
the manager described, the index has drifted; use a stable selector instead.

Events that INVALIDATE every previously seen index:

- pressing system Back / Home / Recents
- typing or dismissing the IME
- tapping a button that opens / closes a sheet, dialog, or new screen
- ANY scroll **or swipe** (RecyclerView / gallery / feed re-layout — indices remap immediately)
- waiting more than ~1 s without doing anything

For critical buttons (Share / Publish / Confirm / Done / Allow / Skip)
ALWAYS use one of these custom tools instead of `click {index}`:

1. `tap_by_resource_id` — taps the centre of an element by its
   `resource-id` (full or trailing). Survives index churn entirely.
   Uses a real 90 ms touchscreen swipe (Compose-friendly).
2. `tap_share_and_confirm` — IG-only convenience for the Share button:
   auto-hides the IME, taps `share_button` with a real-finger swipe,
   and verifies the post registered (Activity changed OR `share_button`
   gone). Use this for Step 7 below — never `click` the Share button
   by index.
3. `tap_by_text` — fallback when there is no stable resource-id (e.g.
   "Share now", "Allow", "Skip", "Done"). Matches by visible text.
4. `hide_ime` — call before any bottom-of-screen tap (Share, Publish,
   Done, Save) if you have just typed into a field. The Mobilerun
   Portal IME does NOT auto-hide on focus loss — it sits on top of the
   share screen and silently swallows taps. `tap_share_and_confirm`
   already calls `hide_ime` internally; for any other bottom button
   you must call `hide_ime` yourself.
5. raw `click {index}` — only acceptable for non-critical UI (gallery
   thumbnails, list rows) AND only against the FRESH snapshot of the
   current step.

## Login state detection

1. Open Instagram. Wait until either the Home feed loads (bottom nav with
   Home / Reels / + / Reels / Profile) or the login screen appears.
2. If you see "Log in" / "Sign up" → account is logged out. Report and stop.
3. Otherwise read the active username from the Profile tab header
   (`@username` / `action_bar_title` element). Persist it in your scratchpad
   for downstream tools.

## Posting a Trial Reel — entry paths (try in order)

Full spec: `docs/instagram-appcard-reference.md`.

### Path A — primary

1. Profile tab.
2. **Professional dashboard** (card on profile).
3. **Trial Reels** tile (scroll dashboard if hidden).
4. **Create** / **Try it** / **+** / **Get started**.

### Path B — fallback (if Path A cannot find Trial Reels)

1. Profile tab.
2. Top-right **menu** (burger / three lines / **Options**).
3. **Account type and tools**.
4. **Trial reels** → **Create** / **+**.

### Path C — fallback (profile plus + Trial toggle)

1. Profile tab.
2. **Plus** top-left on profile (not bottom-nav Create).
3. **Reel** in the menu.
4. Gallery → newest pushed video → **Next** through editor.
5. On Share/caption screen: turn **ON** the **Trial** toggle, then caption + share.

### After composer is open (all paths)

1. Pick the most recent gallery video (`push_video_to_gallery`).
2. **Next** through editor; skip **Edit cover**.
   - **Extra editor screen (common on some devices):** after gallery pick IG may
     show the **clips timeline** editor (filmstrip, play time `0:xx / 0:xx`,
     **Try Edits** pill). Tap only the **top-right Next** arrow
     (`drawer_next_button_layout` / visible text **Next**) — **not** the
     top-left chevron. Farm tool: **`advance_past_reel_timeline_editor`**.
3. On the Share screen — **fffbt Trial Reel task (hard layout):**
   - **No swipes / no scroll** on Share before caption entry (indices remap).
   - **Caption (farm-standard, all devices):**
     1. **`click {index: 12}`** — focus caption, Mobilerun Keyboard ON.
     2. **Fresh UI tree** — resolve `caption_input_text_view` index (usually **8** after IME; **re-read, do not hardcode**).
     3. **`paste_text`** at resolved index with full DB caption — **Mobilerun Keyboard /
        `content://com.mobilerun.portal/keyboard/input`** (or `driver.input_text`). **Not**
        `ADB_INPUT_B64` alone (reports success but leaves placeholder on IG Share).
     4. **`verify_caption_text(video_id=...)`** before Share.
     See `docs/instagram-appcard-reference.md`. **No raw x/y.**
   - Call **`verify_caption_text(video_id=...)`** (must match the current
     `automation.jobs.caption` / task caption)
     before Share. **`hide_ime`** before Share (Mobilerun Keyboard covers the button).
     Do **not** tap top-right **OK** while caption is focused — it can jump to the clip
     editor. `tap_share_and_confirm` blocks Share until verify passes.
   - **Path A:** never `via_prompt=true` on first paste (trial banner layout).
   - **Fallback:** `via_prompt=true` only after one Mobilerun `paste_text`+verify failure.
   - Before Share: `verify_caption_text` → `hide_ime` → `tap_share_and_confirm`
     (never `system_button` BACK on Share).
   - Then **`tap_share_and_confirm`** (never raw `click` for Share).
   - If the **task goal text** in the run disagrees with this file, **follow the
     goal** (it wins for that job).

   On a successful share you will land on one of:
   - ``com.instagram.android/.modal.ModalActivity`` showing
     ``Trial reels`` list (resource-id ``trials_list``, the freshly
     posted reel is the first ``trial_thumbnail_image`` tile) — this is
     the Trial Reels happy path.
   - the home feed / profile.
   In either case the post is LIVE — do not retry.

## Verifying Trial Reels → `posted` (separate pass)

After publish the host sets `status=verify`. Confirmation happens only from the
**Trial Reels** list (`trials_list`), **not** the profile Reels grid.

| Step | Action | Mobilerun hints |
|------|--------|-----------------|
| 1 | Profile → Professional dashboard → **Trial reels** | ``action_bar_title`` = Trial reels; ``trials_list`` visible |
| 2 | Open **newest 2** tiles | ``draft_entrypoint_container`` — index 1 = top-left (newest) |
| 3 | Read caption on reel viewer | Match to the current `automation.jobs.caption` / task caption (truncated UI + ``...`` ok) |
| 4 | **Copy link** | ``direct_share_button`` → **Copy link** → ``read_clipboard`` — **not** ⋮ More |
| 5 | Back to list; repeat for 2nd tile | ``action_bar_button_back`` |
| 6 | DB | ``mark_video_posted`` — ``status=posted``, ``link_platform``, ``posted_by`` |
| 7 | **Re-check** | ``link_platform`` = URL from **newest** tile (position 1); older trials can match the same caption |
| 8 | Close app | ``close_instagram`` when post/verify pass finishes |

Caption matching should use the current worker verification step and
`docs/instagram-appcard-reference.md`; do not depend on legacy farm scripts.

## Capturing the post URL (publish pass — optional)

The post URL is OPTIONAL at publish time — once `tap_share_and_confirm` succeeds
the post is live. The verify pass above is the canonical place to copy links from
**Trial Reels** context (paper-plane, not More).

After `tap_share_and_confirm`, call **`wait_trial_reel_settle`** (wait + open Trial reels +
**pull-to-refresh**). If the newest tile is missing, call **`refresh_trial_reels_list`** again —
IG often does not show a just-published reel until the list is refreshed.

Instagram may not expose a public URL for ~1–2 minutes after publish; promote
to `posted` on caption match even when `link_platform` is null.

## Editing username (only when the task asks)

- Profile → "Edit profile" → "Username" row. The new flow uses an inline
  `EditText` that replaces the row label.
- Clear field with select-all + delete; do NOT rely on a single backspace.
  Then `type_humanized` the new value.
- After typing, wait 5-10 s for the availability check (a small spinner /
  red warning may appear under the field). Press the confirm checkmark only
  after the spinner disappears AND no error message is shown.
- "Username is unavailable" → do not retry the same value. Try variants
  with `_`, `__`, `.`, prefixes (`pro`, `hub`, `ft`) and postfixes
  (`fc`, `pro`).
- Username only. Do NOT touch the Display Name field unless the task asks.

## Common failure modes (and corrections)

- **Black-screen Reel preview after publish** → almost always a 10-bit /
  yuv444 codec issue. The host pipeline (`prepare_video_for_android`) must
  re-encode to H.264 yuv420p before posting. Do not retry the upload before
  the file is fixed.
- **Caption field opens "Edit cover"** — blind tap on video centre or wrong
  coordinates. Re-read Mobilerun tree; use **index** on `caption_input_text_view`
  (large `AutoCompleteTextView` under preview, above `# Hashtags` / Prompt row).
- **Wrong widget focused (Location / Prompt / # sheet)** — index drift or
  coordinate tap too low. Do **not** tap Add location row. Resolve caption index
  from fresh tree; `paste_text` with that index + Mobilerun Keyboard.
- **Text "pasted" but empty on Share** — used wrong IME or skipped verify.
  Enable Mobilerun Keyboard; `verify_caption_text` before Share.
- **Autocomplete popup hides bottom of Share screen** — dismiss the IME
  (Back, or tap above the popup). Do NOT scroll while it's open and do NOT
  conclude that "the toggle is missing".
- **Account-switching dialog after launch** — pick the username explicitly
  given by the task. Do not pick the first row blindly.

## Hard stop conditions

Stop and surface to the orchestrator if you see any of:

- "Action blocked", "We restrict certain activity", "Try again later".
- Two-factor / login challenge / email-code screen.
- Account suspended or checkpoint.
- Professional dashboard tile is absent on this account, OR the Trial Reels
  tile is absent inside the dashboard. Set
  `failure_reason="trial_reels_unavailable"` and do NOT fall back to a
  normal Reel.

Return a structured result with `success=false` and a clear `failure_reason`.
