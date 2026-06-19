"""Runtime humanization of the MobileRun agent's device I/O (anti-detection).

Operator spec (2026-06):
  1. The caption is typed **character-by-character** (MobileRun's StealthDriver
     ships word-by-word typing — we replace it).
  2. The per-character delay is a **per-device base in [620, 1040] ms** plus a
     **±20 ms jitter**. The base is chosen **once per device** — derived
     deterministically from the serial so the same phone keeps a stable
     "typing speed" personality across runs.
  3. The delay **between any agent actions** is randomized to **[7, 15] s**.

These are applied as runtime monkeypatches from
``mobilerun_agent_runner._build_real_agent`` so the behaviour lives in our repo,
not in an edited site-package (which a reinstall would wipe). ``apply_humanization``
is idempotent and best-effort: a patch that cannot be applied logs a warning and
the agent still runs (just without that humanization).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random

logger = logging.getLogger("post_trial")

# (2) per-character typing delay
CHAR_BASE_MIN_MS = 620
CHAR_BASE_MAX_MS = 1040
CHAR_JITTER_MS = 20

# (3) inter-action delay
ACTION_MIN_S = 7.0
ACTION_MAX_S = 15.0

# A unique sentinel we set as the agent's ``after_sleep_action``. The patched
# ``asyncio.sleep`` recognises exactly this value and substitutes a fresh random
# [7,15] s, leaving every other sleep in the process untouched. Chosen to be a
# value no real code path sleeps for.
ACTION_DELAY_SENTINEL = 8.314703


def device_char_base_ms(serial: str) -> float:
    """Per-device base inter-keystroke delay (ms), chosen once per device.

    Deterministic from the serial: the same phone always gets the same base
    speed (a stable personality), within [CHAR_BASE_MIN_MS, CHAR_BASE_MAX_MS].
    """
    h = int(hashlib.sha256((serial or "").encode("utf-8")).hexdigest(), 16)
    span = CHAR_BASE_MAX_MS - CHAR_BASE_MIN_MS
    return float(CHAR_BASE_MIN_MS + (h % (span + 1)))


_applied = False


def apply_humanization() -> None:
    """Install the typing + action-delay patches once per process."""
    global _applied
    if _applied:
        return
    _applied = True

    # (1)(2) character-by-character caption typing on the StealthDriver.
    # DISABLED by default: per-character commits do NOT land in Instagram's
    # caption AutoCompleteTextView (chars are POSTed to the Portal IME and return
    # 200 OK but the field stays empty → verify_caption_text fails → the agent
    # gets stuck "typing" forever). The stock StealthDriver word-by-word typing
    # lands correctly. Re-enable only if a per-char path that actually commits is
    # found: HUMANIZE_PER_CHAR_TYPING=1.
    _per_char = os.environ.get("HUMANIZE_PER_CHAR_TYPING", "0").strip().lower() in ("1", "true", "yes")
    if _per_char:
        try:
            from mobilerun.tools.driver import stealth as _stealth

            async def _human_input_text(self, text, clear=False):  # noqa: ANN001
                inner = self.inner
                serial = (
                    getattr(inner, "serial", None)
                    or getattr(getattr(inner, "device", None), "serial", "")
                    or ""
                )
                base = device_char_base_ms(serial)
                typed_any = False
                first = True
                for ch in text:
                    try:
                        ok = await inner.input_text(ch, clear=(clear and first))
                        typed_any = typed_any or bool(ok)
                    except Exception as exc:
                        logger.debug("humanize: char input failed (%r): %s", ch, exc)
                    first = False
                    delay_ms = base + random.uniform(-CHAR_JITTER_MS, CHAR_JITTER_MS)
                    await asyncio.sleep(max(0.0, delay_ms) / 1000.0)
                return typed_any or text == ""

            _stealth.StealthDriver.input_text = _human_input_text
            logger.info("humanize: per-character typing ENABLED (base %d-%d ms +-%d ms)",
                        CHAR_BASE_MIN_MS, CHAR_BASE_MAX_MS, CHAR_JITTER_MS)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("humanize: could not patch StealthDriver typing: %s", e)
    else:
        logger.info("humanize: per-character typing OFF — stock word-by-word typing")

    # (3) randomize the inter-action delay via the sentinel.
    try:
        _orig_sleep = asyncio.sleep

        async def _human_sleep(delay, *args, **kwargs):  # noqa: ANN001
            try:
                if isinstance(delay, (int, float)) and abs(
                    float(delay) - ACTION_DELAY_SENTINEL
                ) < 1e-6:
                    delay = random.uniform(ACTION_MIN_S, ACTION_MAX_S)
            except Exception:
                pass
            return await _orig_sleep(delay, *args, **kwargs)

        asyncio.sleep = _human_sleep  # type: ignore[assignment]
        logger.info(
            "humanize: randomized inter-action delay [%.0f,%.0f]s enabled",
            ACTION_MIN_S, ACTION_MAX_S,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("humanize: could not patch action delay: %s", e)


__all__ = ["apply_humanization", "device_char_base_ms", "ACTION_DELAY_SENTINEL"]
