# Standalone Trial Reel poster (clone-and-run, no database)

The simplest possible way to post one video to Instagram **Trial Reels** on an
already-prepared phone. No Supabase, no GenFarmer launcher, no identity /
fingerprint / proxy / account changes. Designed so a controlling agent (or a
human) can drive specific devices on the LAN by serial.

> **This does NOT touch the main project.** It lives in `src/runner/` and only
> imports the worker steps that drive the phone. The DB pipeline
> (`src/scheduler/**`), the launcher, Google Drive sync, and the ChangeInfo
> identity flow are untouched and keep working exactly as before.

## What it does

For one device + one video + one caption:

1. **Prepare video** — accepts a local `.mp4` path *or* any `http(s)`/S3
   presigned URL. Downloads (if a URL), transcodes to H.264 yuv420p, and
   `adb push`es it to the phone gallery.
2. **Publish** — drives Instagram via the in-process Mobilerun agent + the
   Instagram AppCard to post the Trial Reel (Profile → Professional dashboard →
   Trial reels → Create → caption → Share).
3. **Verify** (default on) — waits, then re-opens the Professional dashboard and
   confirms the freshly posted reel is at the top of the Trial reels list.

## Prerequisites

On the PC that has the phones:

- **Python venv** with deps: `python -m venv .venv` then
  `.venv/Scripts/pip install -r requirements-runner.txt` (Windows) /
  `.venv/bin/pip install -r requirements-runner.txt` (Linux). This pulls
  `mobilerun` (the agent engine), `pydantic`, and `boto3`.
- **adb** on PATH (or set `ADB_PATH`). Prefer the GenFarmer-bundled adb 35.0.1
  to avoid the adb-server version war that makes TCP devices flap.
- **ffmpeg / ffprobe** on PATH (or set `FFMPEG_PATH` / `FFPROBE_PATH`).
- **Each phone already prepared**: Instagram installed and **logged in**, the
  Mobilerun **Portal** app installed with accessibility + keyboard bound, and
  the device reachable over adb (USB, LAN, or Tailscale TCP, e.g.
  `100.100.57.41:5555`).
- **An LLM key** in `runner.env` (`OPENAI_API_KEY` = the ShopAIKey token;
  base URL is set in `config/mobilerun/config.yaml`).

## Setup

```bash
git clone <repo> fffbt && cd fffbt
python -m venv .venv
# Windows: .venv/Scripts/pip install -r requirements-runner.txt
# Linux:   .venv/bin/pip install -r requirements-runner.txt
cp config/runner.env.example runner.env   # then edit runner.env
```

`requirements-runner.txt` is the only install the standalone path needs
(`mobilerun`, `pydantic`, `boto3`). Then fill in `runner.env` (at minimum
`OPENAI_API_KEY` for posting; the `FERMA_S3_*` block for S3 access). See the
comments in `config/runner.env.example`.

## Usage

### List / connect devices

```bash
# Windows
./scripts/post_one.ps1   # (or run the module directly, see below)
python -m runner devices
python -m runner devices --connect 100.100.57.41:5555
```

`devices` shows what adb sees, so the controlling agent can confirm which of the
human-authorized IPs are actually online before posting.

### Browse / pull videos from S3

The videos live in a TWC Storage S3 bucket: one folder per `video_id` under the
`ferma/` prefix, each containing many `VID_*.mp4` files plus a single `meta.json`
(`{"platform": [...], "category": "...", "caption": "..."}`) that applies to the
whole folder. Configure access with the `FERMA_S3_*` vars in `runner.env`.

```bash
python -m src.runner s3 ls                 # list video_id folders
python -m src.runner s3 ls Cowboy          # list one folder's videos + meta
python -m src.runner s3 ls Cowboy --json
python -m src.runner s3 meta Cowboy        # print just meta.json

# Download all videos of a folder (or the first N) into ./<video_id>/
python -m src.runner s3 pull Cowboy --limit 3
python -m src.runner s3 pull Cowboy --dest /tmp/cowboy

# Download one explicit key
python -m src.runner s3 pull --key ferma/Cowboy/VID_xxx.mp4 --dest /tmp/a.mp4
```

`s3_source.py` also exposes `delete` / `delete_folder` (used later for the
"posted to every platform → remove from S3" step). This layer is access-only:
category routing, daily limits, and caption uniquification are **not** here.

`post-one` accepts any of these as `--video`: a local path you pulled, or — since
`video_preparation` downloads any `http(s)` URL — an S3 presigned URL.

### Post one Trial Reel

Via the wrapper script (loads `runner.env`, sets `PYTHONPATH=src`):

```powershell
# Windows — local file
./scripts/post_one.ps1 --device 100.100.57.41:5555 --video C:\clips\a.mp4 --caption "my caption"

# Windows — S3 / URL source, with hashtags
./scripts/post_one.ps1 --device 100.100.57.41:5555 `
  --video "https://bucket.s3.amazonaws.com/a.mp4?X-Amz-Signature=..." `
  --caption "my caption" --hashtags trial,reels,fyp
```

```bash
# Linux/macOS
./scripts/post_one.sh --device 100.100.57.41:5555 --video /clips/a.mp4 --caption "my caption"
```

Or invoke the module directly (after loading env yourself / from the repo root):

```bash
PYTHONPATH=src python -m runner post-one \
  --device 100.100.57.41:5555 --video /clips/a.mp4 --caption "hi"
# or, no PYTHONPATH needed:
python -m src.runner post-one --device 100.100.57.41:5555 --video /clips/a.mp4 --caption "hi"
```

### Options

| Flag | Meaning |
|------|---------|
| `--device` | adb serial / TCP address (required) |
| `--video` | local `.mp4` path or http(s)/S3 URL (required) |
| `--caption` | caption body text (required) |
| `--hashtags` | comma/space separated, with or without `#` |
| `--expected-username` | informational; the IG username expected on the device |
| `--no-verify` | skip the delayed dashboard confirmation (publish only) |
| `--verify-delay` | seconds to wait before verification (default 180) |
| `--json` | emit a JSON result object |

## Exit codes & result

- **0 / SUCCESS** — published, and (unless `--no-verify`) confirmed in the
  Professional dashboard.
- **1 / FAILED** — one of:
  - `video_preparation_failed` — download/transcode/push problem (file missing,
    URL unreachable, device offline).
  - publish failure (`share_did_not_register`, `trial_reels_unavailable`,
    `logged_out`, `account_suspended`, …) — see the message.
  - `verification_failed` — the reel published but could **not** be confirmed in
    the dashboard within the timeout. The post may still be live; review
    manually. (Instagram sometimes takes 1–2 min to surface a new Trial Reel.)
- **2** — usage error (missing required arg, missing `runner.env`).

Trajectories for each agent run are written under `MOBILERUN_TRAJECTORIES_DIR`
(default `trajectories/`) for debugging.

## Notes for the controlling agent

- One device ↔ one account is assumed and **not** enforced here: whatever
  account is logged in on `--device` is the one that posts. Pick the device that
  belongs to the intended account.
- Only drive devices the human explicitly authorized. Use `runner devices` to
  confirm reachability first.
- This path never logs in, switches accounts, edits the profile, or changes
  device identity. If Instagram shows a login / 2FA / checkpoint / "action
  blocked" screen, the run stops and reports it — do not retry blindly.
