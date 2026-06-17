# Standalone Trial Reel poster (`scripts/post_trial.py`) — MVP notes & roadmap

This documents the DB-backed standalone posting flow used in MVP and records
operator-mandated constraints that must survive into the production design.

> Status: MVP. The flow runs on one prepared device against the Supabase
> `fffbt.videos` table. It deliberately bypasses the `automation.*` schema,
> GenFarmer identity rotation, ChangeDevice, backups, and reboots (one account
> per phone in MVP).

## Flow

```
fffbt.videos (status=new, category=trend)
   │  atomic claim: UPDATE ... FOR UPDATE SKIP LOCKED  → status='posting'
   ▼
resolve link_drive (s3://neiroslop/ferma/<batch>/..) → presigned https URL
   │  read batch meta.json caption from S3, uniquify via LLM (last 5 #tags kept)
   ▼
VideoPreparationStep  (download presigned → transcode H.264 → push to gallery)
   ▼
MobileUIAutomationStep (in-process MobileRun agent drives Instagram)
   │  on publish → status='verify' (+ published_at)
   ▼
dashboard verification + reel-URL capture
   │  success → status='posted' (+ link_platform, posted_by, published_at)
   │  failure → status back to 'new'
```

- Status vocabulary (DB CHECK `videos_status_check`): `new`, `posting`,
  `verify`, `posted`, `error`, `cancel`. `posting` was added to the constraint
  on 2026-06-16 to give the claim a distinct lock state.
- Caption: always from the S3 batch `meta.json`, uniquified per reel by an LLM
  (operator copywriter prompt) — the **last 5 hashtags are preserved verbatim**;
  on any LLM failure the original caption is posted unchanged.

## Operator-mandated constraints (keep through to production)

### C1 — Verification timing
Do **not** block 180s before verifying. Use a short initial settle, then a few
quick retries:
- initial wait: **30s**
- then up to **3 verification attempts**, **15s** apart, stop on first success.

(Reel-URL capture stays best-effort: IG often withholds the public link for
1–2 min; a `null` link on a confirmed post is normal and must not fail the run.)

### C2 — Per-account Trial Reels path self-learning
The agent tries Trial Reels entry paths A → B → C in order, which wastes steps.
For each account, **record which path last succeeded** and instruct the agent to
**try that path first** next time, with the remaining paths as fallback. This
makes the agent self-learn per account.
- MVP: store locally (JSON, `data/account_memory.json`, gitignored).
- Production (future, to push to git): move this memory into the DB
  (per-account column / table) so it is shared across hosts, and record not just
  the entry path but other learned per-account quirks (e.g. dashboard layout,
  share-screen variant).

### C3 — Mobilerun Keyboard must be dismissed before Share
The Mobilerun Portal Keyboard is a custom IME that ignores focus-loss and BACK,
so it can sit on top of the Share button and silently swallow taps (observed as
a false `share_did_not_register`). `dismiss_keyboard` must therefore fall back to
`ime disable <MobilerunKeyboardIME>` (Android falls back to the stock IME →
overlay gone); the driver's `connect()/setup_keyboard` re-activates it next run.
See `src/worker/tools/instagram.py::dismiss_keyboard`.

### C4 — Device-readiness recovery (a11y "enabled but not bound")
If MobileRun reports `Accessibility service not available` while the service is
enabled in settings, recover by: (1) toggle the Mobilerun a11y service off→on,
(2) reboot the device and wait for it to reappear. (Reboot is allowed only as
this explicit recovery — not part of the normal posting flow.)

### C5 — Deterministic Trial-Reel link capture (no LLM)
The LLM URL-capture goal is unreliable: the MobileRun driver has **no
clipboard-read API**, so the agent cannot actually read a copied link and will
**hallucinate** a plausible-but-wrong URL. Capture the link deterministically
instead — `src/worker/tools/instagram.py::capture_trial_reel_link`:

1. Profile tab → Reels sub-tab.
2. Tap the first tile's **thumbnail** ("Drafts and trial reels") → selector.
3. Tap **"Trial reels"** (row tap may need a retry).
4. Tap the **first (newest) tile** in the trials list.
5. Tap **Share** (`direct_share_button`; first tap may miss — retry).
6. Tap **"Copy link"**.
7. Focus the share-sheet search box (`search_edit_text`), send **KEYCODE_PASTE
   (279)**, and read the URL from the a11y tree; strip `?igsh=…`.

This reads a REAL field value, so it cannot hallucinate. `post_trial`'s URL
capture uses it (retry-then-null preserved).

> **Device note:** the `input_tap` helper (scaled `touchscreen swipe` based on
> `wm size`) **misfires on some devices** (e.g. SM-F700F) — every tap silently
> no-ops. `capture_trial_reel_link` uses **raw `input tap`** instead. The agent
> publish path is unaffected (it taps via the Portal `/tap` endpoint). Worth a
> broader fix to `input_tap` later.

## Future work (push to git)
- Implement C2 in the DB and wire it into the production launcher.
- Add `path_used` to the publish agent's structured output so the worked path is
  captured reliably (not parsed from logs). *(done for the standalone runner.)*
- Fold the deterministic link capture (C5) into the production `VerificationStep`
  (replace the LLM `_GOAL_CAPTURE_URL`).
- Fix the `input_tap` helper's swipe-scaling so deterministic taps work on all
  devices without a raw-`input tap` workaround.
- Catalogue `share_did_not_register` false-negatives and confirm-on-device logic
  to avoid duplicate re-posts when a publish actually succeeded.
