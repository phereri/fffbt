# 0001 — Architecture baseline

- Status: accepted
- Date: 2026-05-15
- Owner: Architect / Tech Lead

## Context

The `fffbt` automation project is starting from an empty repository. Before any
component-owning agent begins implementation, we need an agreed-on baseline
that pins:

- the MVP scope (Instagram Reels Trial posting only),
- the data layout (new `automation` schema in Supabase, separate from the
  read-only `fffbt` schema),
- the execution model (async, video-driven queue, interchangeable physical
  devices, verification option A).

Without this baseline, agents will diverge on schema ownership, queue
semantics, and device-handling rules.

## Decision

Adopt the architecture described in `docs/architecture.md` as the baseline:

1. **Video drives the queue.** Each ingested video produces posting jobs.
2. **Account is an identity bundle.** One account owns exactly one proxy, one
   device profile, one GPS location, and one app/session state.
3. **Devices are interchangeable executors.** Before a job, a free physical
   phone receives the selected account's environment.
4. **Jobs are async.** The global queue must never wait on a single job.
5. **Data lives in a new `automation` schema.** The existing `fffbt` schema is
   reference-only.
6. **Verification uses option A.** The physical device remains reserved until
   verification completes.

Out-of-scope work (onboarding, profile setup, professional mode, SMM,
analytics-driven decisions, grid publishing, comments, DMs) is deferred.

## Consequences

- Component agents can begin work against a stable contract.
- Schema changes target `automation` only; PRs that touch `fffbt` are
  rejected by default.
- Device-pool design must support the "provision-on-pickup" model rather than
  long-lived device-to-account bindings.
- Verification option A constrains throughput per device; revisit if device
  utilization becomes a bottleneck (would supersede this ADR or follow up
  with a new one).

## Alternatives considered

- **Account-pinned devices** (each account permanently tied to one phone).
  Rejected: it does not scale and wastes device capacity.
- **Synchronous verification on a separate device** (option B). Rejected for
  the MVP — adds device-handoff complexity before we have data on failure
  modes. May revisit later.
- **Extending the existing `fffbt` schema** instead of creating `automation`.
  Rejected: muddies ownership and risks accidental writes to reference data.
