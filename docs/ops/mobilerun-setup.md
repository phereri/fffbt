# Mobilerun Setup

This runbook prepares the FFFBT MVP for Mobilerun-first Instagram automation.
It is safe to run before proof-of-posting because the checks below do not
create jobs, tap phones, type text, or publish.

## Config Layout

Mobilerun config lives in:

- `config/mobilerun/config.yaml` - main Mobilerun `MobileConfig` template.
- `config/mobilerun/platform_defaults.yaml` - per-platform runtime defaults.
- `config/mobilerun/shopaikey_models.yaml` - model names and provider base URLs.
- `config/mobilerun/app_cards/instagram.md` - Instagram Trial Reels AppCard.
- `trajectories/` - Mobilerun trajectory output directory.

All paths are repo-relative. Do not add `credentials.yaml`, `.env`, service
account JSON files, or production account/device data to git.

## Required Environment

Core runtime:

```powershell
SUPABASE_DB_URL=<postgres connection string>
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
GOOGLE_APPLICATION_CREDENTIALS=<absolute-path-to-google-drive-json>
PYTHONPATH=src
```

Mobilerun:

```powershell
MOBILERUN_CONFIG=config/mobilerun/config.yaml
MOBILERUN_TRAJECTORIES_DIR=trajectories
GOOGLE_API_KEY=<shopaikey-or-google-compatible-key>
ANTHROPIC_API_KEY=<shopaikey-anthropic-compatible-key>
ANTHROPIC_BASE_URL=https://api.shopaikey.com
FARM_USE_CLAUDE_TRAJECTORY=0
FARM_CLAUDE_RECOVERY_MODEL=claude-opus-4-6
```

Android/VPS tools:

```powershell
ADB_PATH=adb
FFMPEG_PATH=ffmpeg
FFPROBE_PATH=ffprobe
ARTIFACTS_DIR=artifacts
```

## Verify Paths

From the repo root:

```powershell
Test-Path $env:MOBILERUN_CONFIG
Test-Path config\mobilerun\app_cards\instagram.md
New-Item -ItemType Directory -Force $env:MOBILERUN_TRAJECTORIES_DIR
```

Or run the safe setup checker:

```powershell
python scripts\check_mobilerun_setup.py --create-dirs
```

The checker prints booleans and hostnames only. It must not print API key
values.

## Verify Model Keys Without Printing Values

PowerShell:

```powershell
[bool]$env:GOOGLE_API_KEY
[bool]$env:ANTHROPIC_API_KEY
([System.Uri]$env:ANTHROPIC_BASE_URL).Host
```

Expected:

- At least one model key is present for the selected Mobilerun profile.
- `ANTHROPIC_BASE_URL` host is `api.shopaikey.com`.
- No command prints the actual key value.

## Verify Python Dependencies

```powershell
python -c "import mobilerun; print('mobilerun import ok')"
python -c "import yaml; print('yaml import ok')"
```

If `mobilerun` is missing:

```powershell
pip install -r src\worker\requirements.txt
```

If `yaml` is missing:

```powershell
pip install PyYAML
```

## Verify MobileRun Portal And Accessibility

Use the real ADB serial shown by `adb devices -l`.

```powershell
adb devices -l
adb -s <SERIAL> shell pm list packages | findstr /i mobilerun
adb -s <SERIAL> shell settings get secure enabled_accessibility_services
adb -s <SERIAL> shell settings get secure default_input_method
```

Expected:

- `com.mobilerun.portal` is installed.
- `com.mobilerun.portal/com.mobilerun.portal.service.MobilerunAccessibilityService`
  is present in enabled accessibility services.
- The default input method can be switched to Mobilerun Portal IME when caption
  paste is required.

Optional read-only Portal state probe:

```powershell
adb -s <SERIAL> shell content query --uri content://com.mobilerun.portal/state
```

If Portal returns an empty tree but `uiautomator dump` works, the current
worker can use the ADB/uiautomator fallback path.

## Non-Posting Readiness Check

After ADB devices are online, run:

```powershell
python scripts\check_mobileworker_preflight.py --serial <SERIAL>
```

For two phones:

```powershell
python scripts\check_mobileworker_preflight.py `
  --serial <SERIAL_1> `
  --serial <SERIAL_2>
```

Expected:

- `ok: true`
- `ui_tree_available: true`
- `ui_tree_count` greater than zero

This check is non-posting. It connects the `MobilerunWorker`, reads the current
activity and UI tree, then disconnects.

## Safety Gate

Do not run posting, `run-job`, or `run-launcher` until:

- `python scripts\check_mobilerun_setup.py --create-dirs` succeeds.
- `adb devices -l` shows the target phones as `device`.
- `scripts\check_mobileworker_preflight.py` succeeds for the selected phone.
- The operator explicitly approves the first proof-of-posting attempt.
