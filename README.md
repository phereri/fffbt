# fffbt

Instagram Reels Trial posting automation.

This repository is the home of the `fffbt` automation project. It is bootstrapped
as an empty skeleton — see `docs/architecture.md` for the system overview and the
current MVP scope.

## Repository layout

```
docs/           architecture notes, decision records, contracts
docs/decisions/ ADRs (Architecture Decision Records)
src/            application code (workers, services, dashboard, integrations)
supabase/       Supabase project: migrations, seed, config (automation schema)
skills/         agent skills (prompts, instructions, tools)
scripts/        operational and developer scripts
tests/          automated tests
```

Each directory currently contains a `.gitkeep` placeholder. Production code is
introduced per-issue, not in this bootstrap.

## MVP scope

Current focus is **Instagram Reels Trial posting** end-to-end:

1. Google Drive ingestion of videos from `instagram/<category-folder>/videos/*.mp4`.
2. Supabase `automation` schema (new, separate from the read-only `fffbt` schema).
3. Video-driven queue, account eligibility, account environment preparation.
4. Physical Android device assignment (GenFarmer / GenRouter / ADB / MockGPS).
5. Appium-based Reels Trial posting with verification (option A: device stays
   reserved until verification completes).
6. Observability — job events, screenshots, errors, status tracking.

Out of scope right now: onboarding flows, profile setup, professional-mode
switching, SMM follower ordering, 24–72h analytics, publishing successful Trial
videos to the grid, comments, DMs.

## Conventions

- The existing `fffbt` Supabase schema is reference-only. New automation data
  lives in a separate `automation` schema.
- One account owns exactly one proxy, one device profile, one GPS location, and
  one app/session state.
- Physical phones are interchangeable executors; before any job, a free phone
  is provisioned with the selected account's environment.
- Jobs run asynchronously; the global queue must not block on a single job.

## Decisions

See `docs/decisions/` for ADRs. Start with `0001-architecture-baseline.md`.
