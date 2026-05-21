# Instagram worker state machine

- Status: accepted (MVP)
- Owner: Appium / Instagram Worker Agent
- Scope: Instagram Reels Trial posting only
- Reference: `docs/instagram-appcard-reference.md`

## Overview

Defines the internal state machine of the Instagram worker — the step-by-step
flow executed on a single physical Android device for one publishing job. This
is **not** the job-level state machine (`docs/contracts/job-state-machine.md`);
the entire worker state machine runs within the job's `publishing` and
`verifying` stages.

## States

| State | Description |
|---|---|
| `prepare_video_for_android` | Re-encode video to H.264 yuv420p if needed. Ensures codec compatibility with Instagram on Android. |
| `push_video_to_gallery` | Push the prepared video file to the device's media gallery via ADB so it appears in Instagram's picker. |
| `open_instagram` | Launch `com.instagram.android` on the assigned device. Handle account-switching dialog if multiple accounts are present. |
| `verify_logged_in` | Confirm the correct account is active. Detect login screens, 2FA challenges, checkpoints, or suspension. |
| `open_profile` | Navigate to the Profile tab. |
| `open_professional_dashboard` | Tap "Professional dashboard" (or locale variant: "Professional Tools", "Pro dashboard"). Detect absence → `trial_reels_unavailable`. |
| `open_trial_reels` | Tap the "Trial Reels" tile inside the dashboard. Detect absence → `trial_reels_unavailable`. |
| `create_trial_reel` | Tap the create entry ("Create", "Try it", "Get started", or centred "+"). This launches the composer in Trial mode. |
| `select_latest_gallery_video` | In the composer, switch to gallery tab if needed and select the most recent video (pushed in `push_video_to_gallery`). |
| `navigate_editor_next_steps` | Tap "Next" / forward arrow through editor screens. Do NOT enter "Edit cover". |
| `fill_caption` | On the Share screen, enter caption via `paste_text` with `clear=true`, `index=13`. No swipes or scrolling before entry. Dismiss IME after entry. |
| `verify_caption` | Call `verify_caption_text` with the exact caption string. Mismatch → `caption_mismatch` error. |
| `share_and_confirm` | Call `tap_share_and_confirm`. Verify post registered (left `ModalActivity` or `share_button` gone). Failure → `share_did_not_register`. |
| `optional_capture_post_url` | Best-effort URL capture (max 3 steps): Profile → Reels grid → tap freshest thumbnail → copy link → read clipboard. Skip on any friction. |
| `verification` | Confirm the Trial Reel is visible in the trials list or profile. Device stays reserved until this completes. |
| `update_job_result` | Report final outcome (`done` / `failed` / `needs_review`) with error code, post URL (if captured), and artifact references. |

## Allowed transitions

```
prepare_video_for_android
    ├──► push_video_to_gallery          (video ready)
    └──► FAILED                         (encode error)

push_video_to_gallery
    ├──► open_instagram                 (video in gallery)
    └──► FAILED                         (adb push error)

open_instagram
    ├──► verify_logged_in               (app launched)
    └──► FAILED                         (app crash, device offline)

verify_logged_in
    ├──► open_profile                   (correct account active)
    └──► FAILED                         (logged_out, login_challenge,
                                         account_suspended, two_factor,
                                         checkpoint)

open_profile
    ├──► open_professional_dashboard    (profile tab reached)
    └──► FAILED                         (navigation failure)

open_professional_dashboard
    ├──► open_trial_reels               (dashboard opened)
    ├──► FAILED                         (trial_reels_unavailable:
    │                                    dashboard tile absent)
    └──► NEEDS_REVIEW                   (unknown_screen)

open_trial_reels
    ├──► create_trial_reel              (trial reels tile found)
    ├──► FAILED                         (trial_reels_unavailable:
    │                                    tile absent inside dashboard)
    └──► NEEDS_REVIEW                   (unknown_screen)

create_trial_reel
    ├──► select_latest_gallery_video    (composer launched)
    └──► NEEDS_REVIEW                   (unknown_screen)

select_latest_gallery_video
    ├──► navigate_editor_next_steps     (video selected)
    └──► NEEDS_REVIEW                   (unknown_screen,
                                         video not found in gallery)

navigate_editor_next_steps
    ├──► fill_caption                   (reached Share screen)
    └──► NEEDS_REVIEW                   (unknown_screen)

fill_caption
    ├──► verify_caption                 (text entered)
    └──► NEEDS_REVIEW                   (caption field not found)

verify_caption
    ├──► share_and_confirm              (caption matches)
    └──► FAILED                         (caption_mismatch)

share_and_confirm
    ├──► optional_capture_post_url      (post published)
    └──► FAILED                         (share_did_not_register,
                                         action_blocked)

optional_capture_post_url
    └──► verification                   (always — URL capture is best-effort)

verification
    ├──► update_job_result              (verified or timed out)
    └──► NEEDS_REVIEW                   (verification_failed)

update_job_result
    (terminal — reports to job state machine)
```

### Transition table

| From | To |
|---|---|
| `prepare_video_for_android` | `push_video_to_gallery`, `FAILED` |
| `push_video_to_gallery` | `open_instagram`, `FAILED` |
| `open_instagram` | `verify_logged_in`, `FAILED` |
| `verify_logged_in` | `open_profile`, `FAILED` |
| `open_profile` | `open_professional_dashboard`, `FAILED` |
| `open_professional_dashboard` | `open_trial_reels`, `FAILED`, `NEEDS_REVIEW` |
| `open_trial_reels` | `create_trial_reel`, `FAILED`, `NEEDS_REVIEW` |
| `create_trial_reel` | `select_latest_gallery_video`, `NEEDS_REVIEW` |
| `select_latest_gallery_video` | `navigate_editor_next_steps`, `NEEDS_REVIEW` |
| `navigate_editor_next_steps` | `fill_caption`, `NEEDS_REVIEW` |
| `fill_caption` | `verify_caption`, `NEEDS_REVIEW` |
| `verify_caption` | `share_and_confirm`, `FAILED` |
| `share_and_confirm` | `optional_capture_post_url`, `FAILED` |
| `optional_capture_post_url` | `verification` |
| `verification` | `update_job_result`, `NEEDS_REVIEW` |
| `update_job_result` | _(terminal)_ |

### Terminal outcomes

`FAILED` and `NEEDS_REVIEW` are not worker states — they are exit signals.
When the worker reaches either, it jumps directly to `update_job_result` to
report the error via `automation.process_job_error()`, then terminates.

## Error classification

Errors are classified per `docs/contracts/retry-failure-policy.md`. The worker
passes the error code to `process_job_error()`; the function handles retry
logic, account side effects, and job state transitions.

### Worker-level error → error code mapping

| Worker state | Condition | Error code | Category |
|---|---|---|---|
| `prepare_video_for_android` | Encode failure | `INFRA` | retryable |
| `push_video_to_gallery` | ADB push failure | `device_offline` | retryable |
| `open_instagram` | App crash / device unreachable | `device_offline` | retryable |
| `verify_logged_in` | Login screen appears | `logged_out` | non_retryable |
| `verify_logged_in` | 2FA challenge | `two_factor` | non_retryable |
| `verify_logged_in` | Checkpoint / security screen | `checkpoint` | non_retryable |
| `verify_logged_in` | Account suspended | `suspended` | non_retryable |
| `open_professional_dashboard` | Dashboard tile absent | `trial_reels_unavailable` | non_retryable |
| `open_professional_dashboard` | Unrecognized screen | `unknown_screen` | needs_review |
| `open_trial_reels` | Trial Reels tile absent | `trial_reels_unavailable` | non_retryable |
| `open_trial_reels` | Unrecognized screen | `unknown_screen` | needs_review |
| `create_trial_reel` | Unrecognized screen | `unknown_screen` | needs_review |
| `select_latest_gallery_video` | Video not in gallery | `unknown_screen` | needs_review |
| `select_latest_gallery_video` | Unrecognized screen | `unknown_screen` | needs_review |
| `navigate_editor_next_steps` | Unrecognized screen | `unknown_screen` | needs_review |
| `fill_caption` | Caption field not found | `unknown_screen` | needs_review |
| `verify_caption` | Caption text mismatch | `caption_mismatch` | needs_review |
| `share_and_confirm` | Share did not register | `share_did_not_register` | needs_review |
| `share_and_confirm` | "Action blocked" dialog | `action_blocked` | non_retryable |
| `verification` | Cannot confirm post exists | `verification_failed` | needs_review |
| _any state_ | Unexpected destructive dialog | `unknown_screen` | needs_review |
| _any state_ | Captcha challenge | `captcha` | needs_review |
| _any state_ | Step budget exceeded | `TIMEOUT` | retryable |
| _any state_ | Unhandled exception | `UNKNOWN` | needs_review |

### New error codes (not in existing catalog)

| Error code | Category | Target status | Description |
|---|---|---|---|
| `caption_mismatch` | needs_review | `needs_review` | `verify_caption_text` returned false; caption not entered correctly |
| `share_did_not_register` | needs_review | `needs_review` | `tap_share_and_confirm` succeeded but post not detected as published |

These must be added to the `automation.error_catalog` table.

## Screenshot points

Screenshots provide evidence for debugging and audit. The worker captures a
screenshot at each point marked below.

| Point | Worker state | Trigger | Purpose |
|---|---|---|---|
| `after_instagram_launch` | `open_instagram` | After app is foreground | Confirm app opened, detect account-switch dialog |
| `after_login_check` | `verify_logged_in` | After verifying account | Confirm correct account is active |
| `professional_dashboard` | `open_professional_dashboard` | After dashboard opens | Confirm dashboard visible, document tile layout |
| `trial_reels_tile` | `open_trial_reels` | After tapping Trial Reels | Confirm trial reels section reached |
| `gallery_selection` | `select_latest_gallery_video` | After video selected | Confirm correct video selected |
| `share_screen` | `fill_caption` | Before caption entry | Document Share screen layout |
| `caption_filled` | `verify_caption` | After caption verified | Confirm caption text matches |
| `post_result` | `share_and_confirm` | After share tap | Capture result screen (trials list or feed) |
| `verification_result` | `verification` | After verification | Confirm post visible in trials list / profile |
| `on_error` | _any state_ | On any error | Capture screen state at point of failure |

Screenshots are stored as job event attachments via `automation.job_events`
with `event_type = 'screenshot'`.

## Hard stop conditions

The worker must abort immediately on any of these (from AppCard §2):

| Condition | Error code |
|---|---|
| "Action blocked" / "We restrict certain activity" / "Try again later" | `action_blocked` |
| Two-factor / login challenge / email-code screen | `two_factor` |
| Account suspended or checkpoint | `suspended` / `checkpoint` |
| Professional dashboard tile absent | `trial_reels_unavailable` |
| Trial Reels tile absent inside dashboard | `trial_reels_unavailable` |
| Account logged out (login screen appears) | `logged_out` |
| Destructive dialog (Logout, Delete, Block) appears unexpectedly | `unknown_screen` |

On hard stop: capture screenshot, jump to `update_job_result`, call
`process_job_error()` with the appropriate error code.

## Mapping to job state machine

The worker state machine maps to job-level states as follows:

| Job state | Worker states |
|---|---|
| `preparing_device` | _(handled by Environment Loader, before worker starts)_ |
| `publishing` | `prepare_video_for_android` through `share_and_confirm` |
| `verifying` | `optional_capture_post_url`, `verification`, `update_job_result` |

The job transitions from `publishing` → `verifying` when `share_and_confirm`
succeeds. The worker handles both phases internally; the job state transition
is reported to the job state machine at the boundary.
