# Architecture

Status: draft (MVP — Instagram Reels Trial posting only).
Owner: Architect / Tech Lead Agent.
Last updated: 2026-05-15.

## 1. MVP scope

Current MVP focus: **automated posting of Instagram Reels Trial videos** from
Google Drive, using physical Android devices.

In scope:

- Google Drive video ingestion from `instagram/<category-folder>/videos/*.mp4`.
- Supabase `automation` schema.
- Video queue.
- Account eligibility and scheduling.
- Account environment preparation (proxy, device profile, GPS, app/session state).
- Physical Android device assignment.
- GenFarmer / GenRouter / ADB / MockGPS integration.
- Appium-based Instagram Reels Trial posting.
- Verification — option A: device stays reserved until verification completes.
- Observability: job events, screenshots, errors, status tracking.

Out of scope (deferred):

- Account onboarding, avatar/profile setup, switching to professional mode.
- SMM follower ordering.
- 24–72h analytics decisions.
- Publishing successful Trial videos to the profile grid.
- Comments and Direct Messages automation.

## 2. Core invariants

These are the non-negotiable rules of the system. New work must not break them.

| #  | Invariant | Status |
|----|-----------|--------|
| I1 | **Videos drive the queue.** A video is the unit of work; everything else (account, device) is selected to serve a video job. | confirmed |
| I2 | **Account = identity bundle.** An account is a single coherent identity (login + cookies + bound infra). | confirmed |
| I3 | **One account owns exactly one** proxy, one device profile, one GPS location, and one app/session state. These travel with the account, not with the phone. | confirmed |
| I4 | **Physical Android phones are interchangeable executors.** A phone is a runtime sandbox; before each job the selected account environment is loaded onto whichever free phone is picked. | confirmed |
| I5 | **Jobs are asynchronous.** The global queue must not block on a single job. | confirmed |
| I6 | **Verification = option A.** When Instagram triggers a verification challenge, the physical device stays reserved to that account until verification finishes (success or failure). | confirmed (MVP) |
| I7 | **Existing `fffbt` schema is read-only / reference.** No automation writes touch it. | confirmed |
| I8 | **All new automation data lives in a separate Supabase schema named `automation`.** | confirmed |

## 3. High-level flow

End-to-end "video → posted Reel" path:

```
Drive folder              automation.videos                    automation.video_jobs
 .mp4 files     ──ingest──▶ (queued)        ──scheduler──▶      (scheduled, account_id, device_id)
                                                  │
                                                  ▼
                                         account eligibility
                                         (cooldown, daily caps,
                                          account healthy)
                                                  │
                                                  ▼
                                          claim free phone
                                                  │
                                                  ▼
                                  load account environment onto phone
                                  (proxy, GPS, device profile, app/session state)
                                                  │
                                                  ▼
                                       Appium → Instagram app
                                       post Reels Trial
                                                  │
                                          ┌───────┴────────┐
                                     no challenge       challenge
                                          │                │
                                          ▼                ▼
                                       success      device stays reserved
                                                    until verification done
                                          │                │
                                          └───────┬────────┘
                                                  ▼
                                       record outcome, events,
                                       screenshots, free device
```

## 4. Components

Logical building blocks. Each has a single owner contract; concrete
implementations live in their own issues.

| Component | Responsibility | Owns / writes |
|-----------|----------------|---------------|
| **Drive Ingestor** | Discover new `.mp4` files under `instagram/<category-folder>/videos/`, register them as videos. | `automation.videos` |
| **Video Queue** | Pending → scheduled → running → done/failed lifecycle for video jobs. | `automation.video_jobs` |
| **Account Registry** | Stores accounts and their bound infra (proxy, device profile, GPS, app/session state). | `automation.accounts`, `automation.account_*` |
| **Eligibility / Scheduler** | Decides *which* account posts *which* video *when*. Enforces cooldowns, daily caps, account health. | `automation.video_jobs.scheduled_at` |
| **Device Pool** | Tracks physical Android phones and their reservation state. | `automation.devices`, `automation.device_reservations` |
| **Environment Loader** | Prepares a phone for a chosen account: proxy, MockGPS, device profile, app/session state. Uses GenFarmer / GenRouter / ADB. | runtime only (no persistent schema yet) |
| **Poster (Appium worker)** | Drives Instagram UI to post a Reels Trial. Reports events. | `automation.job_events`, screenshots |
| **Verifier** | Detects and resolves Instagram verification challenges. Holds the device reservation while active (option A). | `automation.verifications` |
| **Observability** | Persists job events, screenshots, errors, status; surfaces them to the dashboard. | `automation.job_events`, attachment storage |

## 5. Data model boundaries

- `fffbt.*` — **read-only / reference**. No migrations, no writes from automation.
- `automation.*` — owned by this project. All new tables, views, functions, and
  policies live here.
- Cross-schema reads (e.g. automation looking up an existing account in `fffbt`)
  are allowed via views or read queries; the source of truth for automation
  state stays in `automation`.

Concrete table design is owned by the Supabase schema issue, not by this document.

## 6. Async & concurrency rules

- The queue is global. One slow job must not stall others.
- Device reservation is per-account-per-job, not per-batch.
- During verification (option A), the *device* is held; the *queue* keeps moving
  on other accounts/devices.
- Workers are stateless; all state lives in `automation.*` so any worker can
  pick up the next job.

## 7. Open questions

Each item below should be resolved before the related component is implemented
and should become its own issue when picked up.

1. **Account ↔ device pinning.** Is there ever a reason to pin an account to a
   specific phone (warm-up history, residual app data), or is the
   "interchangeable phone" rule absolute? *Assumption: absolute for MVP.*
2. **Proxy lifecycle.** Are proxies long-lived per account, or rotated? Where
   do credentials live, and how are they injected into the phone?
3. **Device profile fingerprint.** What exactly is the "device profile" — IMEI,
   build props, sensor noise? Which of these does GenFarmer / GenRouter
   actually control?
4. **App/session state migration.** How is Instagram session state moved between
   phones? Backup/restore via ADB? Re-login from stored credentials?
   Trade-offs on detectability.
5. **Verification UX.** Option A keeps the device reserved. What is the timeout
   before we mark the job failed and free the device? Does a human ever
   intervene?
6. **Cooldowns and caps.** What are the per-account posting limits (per hour,
   per day) for Reels Trial specifically?
7. **Drive ingestion idempotency.** How do we detect re-uploads, renames, and
   moves between category folders without re-posting the same video?
8. **Failure taxonomy.** Which failures retry automatically, which go to a
   human queue, which permanently burn the account?
9. **Observability scope.** How long do we keep screenshots and per-step
   events? Storage budget?
10. **Schema isolation enforcement.** Do we enforce "no writes to `fffbt`" via
    Postgres roles / grants, or only by convention?

## 8. Decisions log

Architecture decisions are recorded under `docs/decisions/` (ADR format). This
document references the current state; rationale and history live in the ADRs.
