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

BIRTHDAY DATE-PICKER (the "What's your birthday?" / "Set date" screen)
- The birthday is a 3-column scroll-wheel: Month | Day | Year. Only the YEAR
  needs changing; it defaults to the CURRENT year (e.g. 2026), which is invalid
  (age 0). You must lower the Year to make the account ~18–30 years old — i.e. a
  year roughly 18–30 before the current year (e.g. if it is 2026, choose ~2000).
- HOW (do this, do NOT scroll): TAP the Year value in the rightmost column — that
  makes it an EDITABLE text field. Clear it and TYPE the target year directly,
  e.g. "2000". (The Month and Day columns work the same way — tap then type — but
  usually only the Year needs changing.) Do not waste steps swiping the wheel.
- After typing the year, verify the Year shows the value you typed, then tap
  "SET" (or "OK"/"Done"). Do NOT tap CANCEL.
- Only if tapping does not make the field editable, fall back to swiping the Year
  column to decrease the year, re-reading the centered value after each swipe.

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
- CONFIRMATION CODE — FORCE SMS, NOT WHATSAPP. The number you bought can receive
  SMS/text only; it has NO WhatsApp. On the "Enter the confirmation code" screen,
  read HOW Instagram says it sent the code:
  * If it says the code was sent by SMS / text message, call ``get_sms_code()``.
  * If it says the code was sent "via WhatsApp" (or anything that is NOT SMS/text),
    do NOT wait and do NOT call ``get_sms_code`` yet — that code will never arrive.
    Tap "I didn't get the code" (or "Didn't get a code?" / "Resend"), then from the
    options choose the one that sends the code by SMS / TEXT MESSAGE (e.g. "Send
    code in a text message", "Text me the code", "Resend via SMS"). Do NOT pick
    "Send via WhatsApp" and do NOT pick "Call me". Only AFTER the screen confirms
    the code was sent by SMS/text, call ``get_sms_code()``.
  * If "I didn't get the code" offers ONLY WhatsApp / phone-call options (no SMS /
    text option at all), this number cannot be SMS-verified — treat it as an SMS
    failure (follow the timeout/retry rule below).
- ``get_sms_code()`` polls for ~30 seconds per call (it does NOT block for minutes).
  Three possible replies:
  * A line starting "SMS code: <digits>" — type those digits in the code field.
  * A line starting "NO CODE YET ..." — the code has not arrived. Do NOT type
    anything. Tap "I didn't get the code" → "Resend code to SMS", then call
    ``get_sms_code()`` AGAIN. Keep doing this resend-then-poll loop until you get a
    code or get the cancellation message below.
  * A "Failed: ... order cancelled. You may buy a new number." — the whole budget
    is spent; follow the new-number rule below.
- NEW NUMBER: after a number is cancelled, you may call ``buy_phone_number`` again
  for a fresh number and retry ONCE (so at most TWO numbers total).
- SMS-FAILURE STOP (do this autonomously, do NOT call ask_operator): if
  ``get_sms_code`` has timed out / been cancelled on TWO numbers, stop and finish
  with success=false and failure_reason="phone_verification_failed".
- AUTOMATIC PHONE-CALL screen ("Confirm your account automatically with a phone
  call", asking for call-log permissions): do NOT use it (the number cannot
  receive a call) and do NOT ask the operator — treat it as SMS failure and stop
  with failure_reason="phone_verification_failed".
- Do NOT use "Sign up with email" — there is no way to read the email inbox. If
  phone verification is impossible, stop with failure_reason="phone_verification_failed".
  Only call ``ask_operator`` for genuinely unexpected screens not covered above.

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
