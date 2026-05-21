# 0002 — Mobilerun-first Instagram worker, behind a shared interface

- Status: accepted
- Date: 2026-05-20
- Owner: Architect / Tech Lead
- Resolves: FFF-49 (Evaluate Mobilerun-first vs Appium-first worker decision)

## Context

The Poster component (`docs/architecture.md` §4) drives the Instagram app to
publish a Reels Trial. Its execution backend was not yet pinned. Three options
were on the table:

- **A. Mobilerun-first** — single Mobilerun-based worker.
- **B. Appium-first** — single Appium-based worker.
- **C. Shared interface** — one worker contract, Mobilerun primary, Appium
  fallback.

The two backends are different paradigms, not interchangeable libraries:

- **Mobilerun** is an LLM-agent UI-automation framework (manager/executor,
  AppCards, custom tools, optional vision, per-step budget, trajectory logging).
  It runs on physical devices as a GenFarmer Automation App.
- **Appium** is deterministic scripted automation over UiAutomator2 — explicit
  resource-ids / XPath, one server per device.

The decision had to account for what already exists in this project.

## Decision

Adopt **option C, with Mobilerun as the primary (and only MVP) executor**:

1. The Poster is implemented as a **Mobilerun worker** for the MVP.
2. All callers go through a single `MobileWorker` interface (FFF-28). The
   Mobilerun specifics (sessions, AppCards, custom tools) stay behind the
   adapter and must not leak into the scheduler/launcher.
3. **Appium is a contingency fallback, not MVP work.** The interface keeps the
   door open; the Appium adapter is *not* built now. It is implemented only if
   a concrete trigger appears (see Consequences).

Rationale — every existing asset points to Mobilerun:

- **AppCards.** FFF-48 imported the Instagram Trial Reel AppCard
  (`docs/instagram-appcard-reference.md`). It encodes the entire mandatory
  flow, hard-stop conditions, caption rules, and failure modes — in Mobilerun
  form. Appium-first throws this away and re-derives it from scratch.
- **Custom tools.** FFF-50 lists ~11 Mobilerun custom tools
  (`prepare_video_for_android`, `push_video_to_gallery`, `paste_text`,
  `verify_caption_text`, `hide_ime`, `tap_by_resource_id`, `tap_by_text`,
  `tap_share_and_confirm`, `device_summary`, `mock_location_status`,
  `set_mock_location_app`). These solve the genuinely hard parts of Trial Reel
  posting — Compose-friendly real-finger taps, IME hiding, index churn,
  caption verification, H.264/yuv420p video prep. Raw Appium re-implements all
  of them.
- **GenFarmer / MobileRun setup.** The farm already runs GenFarmer; Mobilerun
  executes as a GenFarmer Automation App (`docs/research/genfarmer-api.md`).
  Mobilerun is the native fit; Appium is a parallel, separately-managed stack.
- **Per-device workers.** A Mobilerun MobileAgent session is scoped to one
  device, which is exactly the FFF-28 wrapper contract. Appium also supports
  per-device work but adds server-process and port management per device.
- **Trajectories / logging.** Mobilerun ships `save_trajectory: action` and
  structured per-step logs out of the box — this directly satisfies the MVP
  observability requirement (job events, screenshots, errors). Appium provides
  none of this; it would be built by hand.
- **Schema integration.** The Mobilerun custom tools already perform Supabase
  updates; FFF-50 only has to repoint them at the `automation` schema. An
  Appium worker is a clean-slate integration.
- **UI-drift robustness.** The AppCard reference repeatedly notes locale and
  label variance ("may appear as…", "may be labelled…"). An LLM-agent executor
  tolerates this far better than scripted XPath.

## Consequences

Enables / directs downstream issues:

- **FFF-27** (Mobile UI automation PoC): scope to a Mobilerun device
  connection. The Appium connection check becomes optional and may be deferred
  or dropped.
- **FFF-28** (Mobile session wrapper): implement as the `MobileWorker`
  interface with a Mobilerun adapter. Keep the interface backend-agnostic so a
  future Appium adapter is a drop-in.
- **FFF-30 / FFF-31** (upload flow research + MVP): proceed on Mobilerun +
  AppCards + custom tools, as already written.
- **FFF-50** (port custom tools): unblocked — proceed.
- `docs/architecture.md` §4 — the "Poster (Appium worker)" row is renamed to a
  backend-agnostic "Poster (Mobile worker)"; §1 wording updated accordingly.

Costs / risks accepted:

- Mobilerun runs are **non-deterministic and token-metered**. The `max_steps`
  budget (25) and `stealth` defaults bound this, but per-run cost is real.
- A Mobilerun/vendor outage has **no fallback in the MVP** — the queue stalls
  for posting until it recovers. Accepted for MVP; mitigated by the interface.

Triggers that would justify building the Appium fallback adapter (revisit
this ADR if any occur):

- Mobilerun per-run cost or latency becomes a throughput bottleneck.
- A stable, high-frequency sub-step is cheaper run deterministically.
- Mobilerun availability/support becomes a sustained operational risk.

## Alternatives considered

- **A. Pure Mobilerun-first, no shared interface.** Rejected: FFF-28 explicitly
  requires the worker behind a backend-agnostic interface, and leaking
  Mobilerun APIs into the scheduler would make a later fallback a rewrite. The
  interface is cheap insurance.
- **B. Appium-first.** Rejected: discards the imported AppCards, the ~11
  custom tools, native GenFarmer integration, and built-in trajectory logging;
  contradicts every downstream issue (FFF-27/28/30/31/50). No offsetting
  benefit for the MVP.

## Note on a documentation inconsistency

`docs/architecture.md` and the repo `CLAUDE.md` MVP-scope lists currently read
"**Appium**-based Instagram Reels Trial posting". This predates the FFF-48
AppCard import and is stale. `docs/architecture.md` is corrected as part of
this ADR. `CLAUDE.md` is outside the Architect's edit boundary and must be
updated by the workspace owner to keep the agent runtime config consistent.
