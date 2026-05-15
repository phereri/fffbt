# Architecture

Status: **draft / baseline**
Scope: Instagram Reels Trial posting (MVP).

This document captures the high-level architecture and the contracts that
component-owning agents must respect. It is intentionally short — details
belong in ADRs (`docs/decisions/`) or in the relevant component's own README.

## 1. Domain model (high level)

- **Video** — drives the queue. Sourced from Google Drive
  `instagram/<category-folder>/videos/*.mp4`. Each video produces zero or more
  posting jobs.
- **Account** — identity bundle. Owns exactly one of each: proxy, device
  profile, GPS location, app/session state.
- **Device** — physical Android phone. Interchangeable executor; receives an
  account's environment just before a job runs.
- **Job** — a unit of work (e.g. "post this video as a Reels Trial from this
  account"). Async, queue-driven, never blocks the global queue.
- **Event** — observability record: status transitions, errors, screenshots,
  device telemetry.

## 2. Data layout

- The existing `fffbt` Supabase schema is **reference-only**. Do not modify it.
- All new automation tables live in a separate schema named `automation`.
- Cross-schema reads from `automation` to `fffbt` are allowed via views/RPC.
  Writes from `automation` into `fffbt` are not.

Concrete tables, columns, and migrations live in `supabase/`; ADRs in
`docs/decisions/` describe the rationale for non-obvious choices.

## 3. Components

| Component | Owner (agent role) | Lives in |
|---|---|---|
| Drive ingestion | Backend / ingestion agent | `src/` (TBD) |
| Queue & scheduler | Backend / queue agent | `src/`, `supabase/` |
| Account eligibility | Backend / accounts agent | `src/`, `supabase/` |
| Device pool & provisioning | Devices agent | `src/`, `scripts/` |
| Reels Trial automation | Mobile automation agent | `src/` (Appium) |
| Observability | Platform agent | `src/`, `supabase/` |
| Dashboard | Frontend agent | `src/` (TBD) |
| Skills / agent prompts | Architect | `skills/` |
| Docs & ADRs | Architect | `docs/`, `docs/decisions/` |

Component contracts (queue API, worker contract, device-provisioning contract,
verification contract) are tracked as separate documents under `docs/` once
each component begins.

## 4. Execution model

- Jobs are asynchronous. The scheduler picks an eligible (account, video) pair,
  acquires a free physical device, provisions it with the account's
  environment, and dispatches a worker.
- The global queue must remain non-blocking: a slow or stuck job never holds up
  unrelated work.
- Verification uses **option A**: the physical device stays reserved until
  verification completes, then is released back to the pool.

## 5. Out of scope (MVP)

- Account onboarding, avatar/profile setup, professional-mode switching.
- SMM follower ordering.
- 24–72h analytics-based decisions.
- Publishing successful Trial videos to the profile grid.
- Comments and DMs automation.

These will be picked up in later phases once Reels Trial posting is stable.

## 6. Open questions

Tracked in `docs/decisions/` as ADR drafts (status: `open question`). The
architect agent owns the resolution path for each.
