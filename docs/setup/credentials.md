# Google Drive Credentials Setup

- Status: accepted (MVP)
- Owner: Architect / Tech Lead
- Related: [`docs/contracts/environment.md`](../contracts/environment.md), `GOOGLE_APPLICATION_CREDENTIALS`

## What you need

A Google service account JSON key with **read-only** access to the
`instagram/<category-folder>/videos/` tree on the project's shared Drive.
Scope: `https://www.googleapis.com/auth/drive.readonly`. No write scopes.

## VPS placement (production)

1. Copy the key to the VPS as the deploy user:
   ```
   scp google-drive.json vps:/tmp/google-drive.json
   ```
2. Move it into place with restricted permissions:
   ```
   sudo install -d -m 0750 -o root -g fffbt /etc/fffbt/credentials
   sudo install -m 0640 -o root -g fffbt /tmp/google-drive.json /etc/fffbt/credentials/google-drive.json
   shred -u /tmp/google-drive.json
   ```
3. Confirm the runtime process can read it:
   ```
   sudo -u fffbt cat /etc/fffbt/credentials/google-drive.json >/dev/null && echo OK
   ```
4. Set in the service env file (NOT in Multica custom env):
   ```
   GOOGLE_APPLICATION_CREDENTIALS=/etc/fffbt/credentials/google-drive.json
   ```

## Local development

- Request a personal-scope key from the project owner. Do **not** reuse the
  production key.
- Place it under `.secrets/google-drive.json` (already gitignored).
- Export the path before running ingestion:
  ```
  export GOOGLE_APPLICATION_CREDENTIALS="$(pwd)/.secrets/google-drive.json"
  ```

## Rotation

- Revoke the old key in the GCP console **after** the new key is in place on
  the VPS and a smoke run succeeds.
- Rotate at least every 90 days, and immediately if any team member with VPS
  access leaves.

## What MUST NOT happen

- Pasting JSON content into a Multica custom env, comment, attachment, or
  autopilot configuration. Multica env may contain only the **path**.
- Logging the JSON content, the key id, or the full path at any log level.
  Errors that reference the path must truncate it to the first 32 characters.
- Mounting the key into containers via build args or image layers — runtime
  bind-mount only.

## Consumer contract

Components that need Drive access (today: Video Ingestion Agent, FFF-39):

- Read the env var `GOOGLE_APPLICATION_CREDENTIALS`. Fail fast at startup with
  a clear error if it is unset or the file is unreadable.
- Use the SDK default credentials path (e.g. `google.auth.default()`). Do
  **not** open or parse the JSON file directly.
- Never write the credential value (path or contents) into `automation.*`
  tables, `job_events`, screenshot artifacts, or logs.
