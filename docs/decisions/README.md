# Architecture Decision Records

This folder holds ADRs for the `fffbt` automation project.

## Format

One file per decision, numbered sequentially:

```
NNNN-short-slug.md
```

Each ADR follows this lightweight template:

```markdown
# NNNN — <title>

- Status: proposed | accepted | superseded by NNNN | open question
- Date: YYYY-MM-DD
- Owner: <agent or person>

## Context
What problem are we solving? What constraints apply?

## Decision
What did we decide? Be specific.

## Consequences
What does this enable, prevent, or imply? Migration / rollback notes.

## Alternatives considered
Briefly list and why they were rejected.
```

## Index

- `0001-architecture-baseline.md` — initial baseline (this bootstrap).
- `0002-mobilerun-first-worker.md` — Mobilerun-first Instagram worker behind a
  shared interface; Appium fallback (resolves FFF-49).

New ADRs are added per change. Use status `open question` for decisions that
are not yet resolved; the architect agent is responsible for moving them to
`proposed` → `accepted`.
