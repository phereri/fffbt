# Proof-of-posting (Instagram Trial Reel) — agent publish learnings

Captured 2026-06-08, the session that produced the **first verified** agent-driven
Trial-Reel publish (`uctamdoan.83862`, job `0f0e2ed5`, device `100.100.57.41:5555`).
Fixes are in commit `fix/proof-of-posting-trial-reel`. This is the content to fold
into the `fffbt-mobilerun-proof-of-posting` skill.

## What was broken (in order of discovery)

1. **LLM billing.** ShopAIKey gateway 401'd (dead token); migrated to Google AI
   Studio Gemini (`config/mobilerun/config.yaml`, OpenAI-compat endpoint
   `generativelanguage.googleapis.com/v1beta/openai`). Then the Gemini project's
   prepaid credits were depleted → `429 RESOURCE_EXHAUSTED`. Small probe calls
   pass while a full agent run 429s — probe with `scripts/probe_gemini.py`.
   MobileRun's OpenAILike reads `OPENAI_API_KEY`.

2. **Wrong publish action (goal).** The goal told the agent the Trial-Reel publish
   action was a "top-right OK". It is the **bottom Share button**; the OK tap
   reverts the composer → false `share_did_not_register`.

3. **Custom tools never registered.** The goal + AppCard tell the agent to call
   `hide_ime`, `tap_share_and_confirm`, `verify_caption_text`, … but those helpers
   (`src/worker/tools/instagram.py`) were never wired into the MobileRun agent →
   "Unknown tool", fallback to raw clicks. Fixed by `custom_tools.py` +
   `tools=` in `MobileRunAgentRunner.build_request()`. MobileRun calls a tool as
   `fn(**args, ctx=ctx)` and accepts a `(success, summary)` tuple
   (`mobilerun.agent.tool_registry`). Bind `serial`/`video_id`/`caption` at
   registration so the agent passes no device-specific args.

4. **Mobilerun Keyboard covers Share and ignores BACK.** The custom IME does not
   hide on KEYCODE_BACK, so the Share tap is swallowed. The reliable dismissal is
   to **clear caption focus by tapping the Trial banner** ("This is a trial reel…
   non-followers"). See `dismiss_keyboard()` in `instagram.py`.

5. **UI reads must use the portal content provider.** A standalone
   `MobilerunWorker.page_source()` returns **empty** outside the agent's GenFarmer
   runtime. Read the a11y tree via
   `content query --uri content://com.mobilerun.portal/state` and `walk_plain_ui`
   (`custom_tools._read_ui` / `_parse_portal_state`).

6. **`paste_text` regressed caption entry** (field stuck on the "Write a caption…"
   placeholder). Dropped it from the agent toolset; the agent uses stock `type`.

7. **Verification step false-failed.** Level 1 (a 30s immediate LLM glance)
   short-circuited to `verification_failed` in ~13s, skipping the delay + the
   authoritative dashboard check — failing reels that were in fact live. Now Level 1
   is best-effort (never short-circuits); always wait, then Level 2 navigates
   Profile → Professional dashboard → **Trial reels** with a pull-to-refresh.

8. **Agent hallucinates `account_username`.** It reported `ecolillyspa.canada` for
   the real `uctamdoan.83862`. The field gates nothing — the goal now tells the
   agent to leave it null if not read verbatim.

## Verifying a Trial Reel posted

Trial Reels do **not** appear on the main profile grid. Verify via:
Profile → **Professional dashboard** → **Trial reels** → the freshest tile (top-left)
→ open it → caption shows under the username, labelled **"Trial reel · View insights"**.

## Device recovery

A long agent run can wedge the device (system_server / SurfaceFlinger hang):
`am`, `input`, `screencap`, and the portal `content query` time out while
`getprop` still works. Recover with `adb reboot` (the Note10 takes ~9 min to
rejoin Tailscale). IG login and the posted reel survive the reboot. `screencap`
also hangs while a reel viewer holds a secure surface — go Home first.
