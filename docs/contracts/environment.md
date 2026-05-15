# Environment variables contract

- Status: accepted (MVP)
- Owner: Architect / Tech Lead
- Template: [`.env.example`](../../.env.example)

This document is the contract for environment variables used by the `fffbt`
automation project. Every variable an agent reads from the environment must be
listed here. New variables go through a PR that updates both this file and
`.env.example`.

## Rules

1. **Secrets never go into git.** `.gitignore` already excludes `.env`,
   `.env.local`, and `.env.*.local`. `.env.example` carries variable names and
   safe defaults only — never real keys, URLs, or paths to private files.
2. **`.env.example` is authoritative for variable names.** If a name differs
   between this doc and the template, the template wins; open a PR to fix
   whichever is wrong.
3. **One reader, one owner.** Each variable lists the component that consumes
   it. Other components must not read it directly — go through that component
   or add a follow-up ADR.
4. **Server-only secrets stay server-side.** Anything marked _server-only_
   must never be shipped to the dashboard frontend or to a device.

## Variables

### Supabase

| Variable | Required | Sensitive | Owner | Purpose |
|---|---|---|---|---|
| `SUPABASE_URL` | yes | no | Backend | Project URL, e.g. `https://<ref>.supabase.co`. |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | yes (server-only) | Backend | Service role key for privileged server-side access. |
| `SUPABASE_DB_URL` | yes | yes | Migrations / scripts | Direct Postgres connection string used by `supabase` CLI and migration scripts. |

Notes:

- The dashboard, if/when it ships, will need an additional `SUPABASE_ANON_KEY`.
  Out of MVP scope; add it via PR when the dashboard begins.
- Connection strings must include `sslmode=require`.

### Google Drive ingestion

| Variable | Required | Sensitive | Owner | Purpose |
|---|---|---|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | yes | yes (path to secret file) | Drive ingestion | Absolute path to the service-account JSON key used to read `instagram/<category>/videos/*.mp4`. |

Notes:

- The variable name follows the Google client SDK convention. The original
  issue draft used `GOOGLE_DRIVE_CREDENTIALS_PATH`; renamed here so that the
  Google SDK auto-discovers the credentials without extra glue.
- The JSON file itself is a secret. Store it outside the repo; never commit it.

### Device / proxy backends

| Variable | Required | Sensitive | Owner | Purpose |
|---|---|---|---|---|
| `GENFARMER_BASE_URL` | yes | no | Devices | Base URL of the GenFarmer service (device profile generation). |
| `GENROUTER_BASE_URL` | yes | no | Devices | Base URL of the GenRouter service (proxy routing). |

Notes:

- Auth tokens for GenFarmer / GenRouter, if introduced, must be added as
  separate `*_API_KEY` variables marked sensitive.

### Android / Appium

| Variable | Required | Sensitive | Owner | Purpose |
|---|---|---|---|---|
| `ANDROID_HOME` | yes | no | Devices, mobile automation | Android SDK root; required for `adb` and Appium drivers. |
| `APPIUM_BASE_URL` | yes | no | Mobile automation | Base URL of the Appium server, e.g. `http://127.0.0.1:4723`. |

### Local artifacts

| Variable | Required | Sensitive | Owner | Purpose |
|---|---|---|---|---|
| `SCREENSHOTS_DIR` | no (defaults to `./.artifacts/screenshots`) | no | Observability | Where per-job screenshots are written. |
| `ARTIFACTS_DIR` | no (defaults to `./.artifacts`) | no | Observability | Root directory for per-job logs, dumps, and other artifacts. |

Notes:

- Relative paths resolve from the repo root.
- Both directories must be writable by the process running the job.

### Model provider

| Variable | Required | Sensitive | Owner | Purpose |
|---|---|---|---|---|
| `MODEL_PROVIDER` | yes | no | Platform | Which LLM provider to use: `openai` or `anthropic`. |
| `MODEL_API_KEY` | yes | yes (server-only) | Platform | API key for the selected `MODEL_PROVIDER`. |

Notes:

- Provider-specific names (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) are
  intentionally **not** used. Using `MODEL_PROVIDER` + `MODEL_API_KEY` lets us
  switch providers without code changes and avoids leaking provider choice
  into call sites.

### Logging

| Variable | Required | Sensitive | Owner | Purpose |
|---|---|---|---|---|
| `LOG_LEVEL` | no (defaults to `info`) | no | Platform | One of `debug`, `info`, `warn`, `error`. |

## Local setup

1. `cp .env.example .env`
2. Fill in real values from the appropriate secret store (1Password vault,
   Supabase dashboard, etc.). Do **not** paste secrets into chat or issues.
3. Confirm `.env` is ignored: `git check-ignore .env` should print `.env`.

## Changing this contract

- Adding a variable: PR updates `.env.example` first, then this file, then
  the code that reads it. The PR description must state owner, sensitivity,
  and whether the variable is required or optional.
- Renaming a variable: open an ADR under `docs/decisions/` describing the
  migration. Keep the old name working for one release if any deployed
  component already reads it.
- Removing a variable: only after confirming no code path still reads it
  (`grep -R '<NAME>' src/ scripts/ supabase/`).
