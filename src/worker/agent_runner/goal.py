"""Goal-template builder for the Instagram Trial Reel scenario.

Ported from the real-repo ``scenarios/post_ig_trial_reel.py`` goal text,
stripped of legacy concerns (``poker_videos`` writes, ``crm.tasks`` rows,
``mark_video_verify``, Russian/Portuguese caption-language branches,
Claude-driven recovery hints).

The goal is *what* not *how*; tactics live in
``config/mobilerun/app_cards/instagram.md``.
"""

from __future__ import annotations

from pathlib import Path


HARD_STOP_RULES = (
    "HARD STOP CONDITIONS (set success=false, fill failure_reason exactly as listed):\n"
    "- Logged out / login screen visible → failure_reason=\"logged_out\".\n"
    "- Two-factor / security code / verify-your-identity → failure_reason=\"login_challenge\".\n"
    "- \"Action blocked\" / \"We restrict certain activity\" / \"Try again later\" → "
    "failure_reason=\"action_blocked\".\n"
    "- Account suspended / disabled / checkpoint → failure_reason=\"account_suspended\".\n"
    "- Trial Reels entry tile not present after paths A, B, and C exhausted → "
    "failure_reason=\"trial_reels_unavailable\".\n"
    "- Share button does not register / activity does not change after Share → "
    "failure_reason=\"share_did_not_register\".\n"
    "- Caption verification fails after one retry → failure_reason=\"caption_mismatch\".\n"
    "- Unexpected destructive dialog (logout, delete) → "
    "failure_reason=\"unexpected_destructive_dialog\".\n"
)


_GOAL_TEMPLATE = """\
You are operating the assigned Instagram account on the device {device_serial}.
This is the ONLY device you may interact with. Do NOT call ``open_app`` for any
other package than Instagram and do NOT touch other devices.

GOAL
- Publish the prepared video as an Instagram **Trial Reel**.
- Use the provided caption text exactly. Do not edit, paraphrase, translate,
  truncate, or split it across multiple ``type`` calls.

CONTEXT
{host_video_note}
- Caption (use EXACTLY this string — no edits):
  ---
  {caption_full}
  ---
- Account expected on the device: {expected_username}
- Video id (for caption verification): {video_id}

ACCOUNT POLICY
- Do NOT create a new account.
- Do NOT log in. If the app shows the login screen, stop immediately with
  failure_reason="logged_out".
- Do NOT switch between accounts; use the active one.
- Do NOT change profile settings, username, bio, avatar, or account type.

DEVICE-CONTROL POLICY
- Use Mobilerun TCP UI tools only (the AppCard names the specific helpers).
- Do NOT issue raw ADB tap/swipe coordinates.
- Do NOT take destructive actions (logout, delete, block) even if a dialog
  asks you to.

ENTRY PATHS (try in order — see the Instagram AppCard for tactics)
- Path A: Profile → Professional dashboard → Trial Reels → Create / Try it / +.
- Path B (only if A exhausted): Profile → top-right menu → Settings and activity →
  For professionals → Account type and tools → Trial reels → Create.
  If "Trial reels" opens the Ad tools screen instead, press Back and try Path C —
  never boost a post or pick ads.
- Path C (only if A and B exhausted): Profile → plus top-left (NOT bottom-nav
  Create) → menu Reel → gallery → Next through editor → on the Share screen turn
  the **Trial** toggle ON before caption + share.

STEPS
{prepare_push_steps}
3. Instagram should already be open on the device. Confirm login state from the
   current UI tree. If logged out → stop with failure_reason="logged_out".
   For ``account_username``: report it ONLY if you can read the active username
   verbatim from the UI (e.g. the profile/action-bar title). If you cannot read
   it with certainty, leave it empty — do NOT guess or invent a username. It is
   informational only and is not used to decide success.
4. Reach the Trial Reel composer using Path A, then B, then C.
5. Pick the most recent video in the gallery (the file we just pushed). If the
   clips timeline editor appears (filmstrip + "Try Edits" pill), tap the
   top-right Next arrow (drawer_next_button_layout) — NOT the top-left chevron.
   Tap Next through editor steps without entering "Edit cover".
6. Caption + Share — Trial Reel hard layout, see AppCard:
   - Tap the caption field (``caption_input_text_view``) to focus it, then use
     ``type`` to enter the full caption text exactly (resolve the index on a
     fresh UI tree). Do not paste via prompt.
   - Run ``verify_caption_text`` before Share to confirm the caption landed
     (the field must no longer show the "Write a caption…" placeholder). If it
     reports the placeholder, re-focus the field and ``type`` again.
   - Run ``hide_ime`` to dismiss the keyboard (best-effort). The Mobilerun
     Keyboard covers the Share button; ``hide_ime`` clears it by tapping a
     non-input area. If ``hide_ime`` reports it could not hide the IME, do NOT
     stop — ``tap_share_and_confirm`` dismisses the keyboard itself.
   - Run ``tap_share_and_confirm`` exactly once. Never raw ``click`` for Share.
     ALWAYS call ``tap_share_and_confirm`` to publish, even if ``hide_ime`` (or
     ``system_button`` BACK) reported failure — it re-dismisses the keyboard
     before tapping Share. Never conclude ``share_did_not_register`` without
     having called ``tap_share_and_confirm`` and seen IT fail.
   - If ``verify_caption_text`` fails, retry the paste once (no ``via_prompt``);
     never Share without a passing verify.
   - ``tap_share_and_confirm`` IS the complete publish action: it taps the
     bottom **Share** button on the Trial Reel "New reel" screen. On this build
     there is NO separate top-right "OK"/checkmark — do NOT tap the top-right
     after Share; that backs out of the composer and reverts the post (this is
     the #1 cause of false ``share_did_not_register``).
   - Success = ``tap_share_and_confirm`` reports the Share button gone / the
     activity changed, and you land on the Trial reels list (``trials_list``) or
     a post-processing screen. Treat that as published — stop and return.
7. After publish succeeds, return PostResult(success=true) immediately. Leave
   ``post_url`` null; the scheduler verification step copies the link.
8. Do NOT mark anything in the database; the host owns DB transitions.

{hard_stop_rules}
"""


_HOST_VIDEO_IN_GALLERY_NOTE = (
    "- HOST: Video is already prepared (H.264 yuv420p) and pushed to "
    "DCIM/Camera as {gallery_remote_name!r}. Do NOT call "
    "``prepare_video_for_android`` or ``push_video_to_gallery``. Pick the "
    "newest file in the gallery."
)

_HOST_LOCAL_VIDEO_NOTE = (
    "- Local video on host: {local_video_path}\n"
    "- Prepare with ``prepare_video_for_android``, then "
    "``push_video_to_gallery`` with the EXACT path the prep tool returned."
)


_HOST_SKIP_PREPARE_PUSH_STEPS = (
    "1. (skipped — host already pushed the video to gallery)\n"
    "2. (skipped)"
)

_HOST_PREPARE_PUSH_STEPS = (
    "1. Run ``prepare_video_for_android`` with source_path={local_video_path!r}. "
    "Use the EXACT path from the tool result (``transcoded -> …``) for push — "
    "never guess.\n"
    "2. Run ``push_video_to_gallery`` with that same prepared path."
)


def build_trial_reel_goal(
    *,
    device_serial: str,
    caption: str,
    hashtags: list[str] | None,
    expected_username: str | None,
    video_id: str | None,
    local_video_path: str | Path | None = None,
    host_video_in_gallery: str | None = None,
) -> str:
    """Render the natural-language goal handed to the MobileRun agent.

    The host has already prepared + pushed the video by the time the agent
    runs (``VideoPreparationStep`` precedes ``MobileUIAutomationStep``). Pass
    ``host_video_in_gallery`` with the gallery filename to instruct the agent
    to skip the prep/push tools; otherwise pass ``local_video_path`` and the
    goal tells the agent to run them itself.
    """
    hashtag_str = " ".join(f"#{h.lstrip('#')}" for h in (hashtags or []) if h.strip())
    caption_clean = (caption or "").rstrip()
    caption_full = caption_clean + (f"\n\n{hashtag_str}" if hashtag_str else "")
    caption_full = caption_full.strip()

    if host_video_in_gallery:
        host_note = _HOST_VIDEO_IN_GALLERY_NOTE.format(
            gallery_remote_name=host_video_in_gallery,
        )
        prep_steps = _HOST_SKIP_PREPARE_PUSH_STEPS
    else:
        resolved = ""
        if local_video_path is not None:
            resolved = str(Path(local_video_path).expanduser())
        host_note = _HOST_LOCAL_VIDEO_NOTE.format(local_video_path=resolved or "(unknown)")
        prep_steps = _HOST_PREPARE_PUSH_STEPS.format(local_video_path=resolved)

    return _GOAL_TEMPLATE.format(
        device_serial=device_serial,
        host_video_note=host_note,
        prepare_push_steps=prep_steps,
        caption_full=caption_full,
        expected_username=expected_username or "(unknown)",
        video_id=video_id or "(none)",
        hard_stop_rules=HARD_STOP_RULES,
    )


__all__ = ["build_trial_reel_goal", "HARD_STOP_RULES"]
