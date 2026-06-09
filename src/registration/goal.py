"""Goal-template builder for the Instagram account registration scenario.

The goal is *what* not *how*; on-screen tactics + the exact AppCard live in
``config/mobilerun/app_cards/instagram.md`` (to be extended for signup). The
agent invents its own identity and drives the 5sim phone-number lifecycle via
the custom tools (``buy_phone_number`` / ``get_sms_code``), and pauses for a
human via ``ask_operator`` whenever it hits an unexpected screen.
"""

from __future__ import annotations

_GOAL_TEMPLATE = """\
You are registering a BRAND-NEW Instagram account on the device {device_serial}.
This is the ONLY device you may interact with. Do NOT open any app other than
Instagram and do NOT touch other devices.

GOAL
- Create a fresh Instagram account from scratch (sign up — do NOT log into any
  existing account).
- Invent your own credentials and identity for this account:
  * username  — unique, plausible, lowercase letters/digits/._ only.
  * password  — strong: 12+ chars, mixing upper, lower, digits, and a symbol.
  * full name — a plausible human first + last name.
  * birthday  — a date of birth making the account holder at least 18 years old
    (18+ REQUIRED; pick an age roughly 18–45).
- Remember every value you choose; you must report them in the final result.

START
- The device may be on the home screen or have Instagram already open. First,
  open the Instagram app (package com.instagram.android). If a logged-in account
  or a previous session is showing, look for "Create new account" / "Sign up" —
  use the account-switcher or log out ONLY if needed to reach the signup screen;
  prefer the "Create new account" entry point on the login screen.

PHONE VERIFICATION (you own this via custom tools)
- When the signup flow asks for a phone number, call the tool
  ``buy_phone_number(country="{country}")``. It returns a real phone number —
  enter that exact number in the form.
- IMPORTANT — phone field entry: this signup screen has a SINGLE number field and
  NO country selector. Enter the FULL international number EXACTLY as returned by
  ``buy_phone_number`` — including the leading "+" and country code, e.g.
  "+31XXXXXXXXX". Do NOT strip the "+" and do NOT remove the country code.
- After typing, VERIFY the field actually shows the full number WITH the leading
  "+". The on-device keyboard can silently drop the "+" character; if the field is
  missing the "+", re-focus it and ensure the "+" is present before continuing.
- RATE LIMIT (read the message carefully): if the screen says "Please wait a few
  minutes before you try again" (or any wait / try-again-later message), that is
  Instagram rate-limiting THIS DEVICE — it is NOT an invalid number. STOP
  immediately, do NOT enter any more numbers on this device, and set success=false
  with failure_reason="rate_limited". Do not confuse this with "invalid".
- Only the message "Input Mobile number is invalid" means the number/format was
  rejected. If you see that, do NOT blindly buy another number — STOP and call
  ``ask_operator`` stating EXACTLY what the number field contains character by
  character (especially whether the leading "+" is present) so the operator can
  see the screenshot and decide.
- When Instagram says it sent an SMS / asks for the confirmation code, call
  ``get_sms_code()``. It blocks until the code arrives (or times out). Enter the
  returned code in the verification field.
- If ``get_sms_code`` reports a timeout/cancellation, you may call
  ``buy_phone_number`` again for a fresh number and retry once.
- Prefer phone verification. If Instagram offers email instead and phone is not
  available, call ``ask_operator`` to ask how to proceed.

PACE / STEALTH
- Behave like a human: do not rush. Allow brief natural pauses between actions.
- Do not spam taps; resolve each screen from a fresh UI tree before acting.

DEVICE-CONTROL POLICY
- Use Mobilerun TCP UI tools only (the AppCard names the specific helpers).
- Do NOT issue raw ADB tap/swipe coordinates.
- Do NOT take destructive actions.

WHEN YOU ARE STUCK
- If you reach an UNEXPECTED screen, an error you don't understand, a captcha,
  a suspicious-login / challenge screen, or anything the AppCard doesn't cover,
  STOP and call ``ask_operator("<clear description + what you see + options>")``.
  Wait for the operator's answer and follow it. Do NOT guess on unexpected
  screens — asking is always preferred over a blind tap.

HARD STOPS (set success=false and fill failure_reason)
- Account creation blocked / "We can't create your account right now" /
  repeated errors after retry → failure_reason="signup_blocked".
- Phone verification impossible after one fresh-number retry →
  failure_reason="phone_verification_failed".
- Account immediately suspended / disabled / checkpoint on creation →
  failure_reason="account_suspended".
- You explicitly asked the operator and were told to abort →
  failure_reason="operator_abort".

RESULT
- On success: return success=true with username, password, full_name, birthday,
  phone_number, phone_country, and fivesim_order_id filled in. Put anything
  noteworthy (unexpected screens seen, recovery steps) in notes.
- On failure: success=false with the failure_reason above and notes describing
  exactly where it failed.
"""


def build_registration_goal(
    *,
    device_serial: str,
    country: str = "any",
) -> str:
    """Render the natural-language goal handed to the registration agent."""
    return _GOAL_TEMPLATE.format(
        device_serial=device_serial,
        country=country,
    )


__all__ = ["build_registration_goal"]
