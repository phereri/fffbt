# Instagram Auto-Registration Agent Briefing

You are setting up and running an Instagram auto-registration system on a PC
that is on the SAME LAN as ~200 Android phones. The code is in a GitHub repo.

## CRITICAL SAFETY RULES

1. **NEVER run bare `adb` commands without `-s <serial>`** — there are ~200
   devices connected. A bare `adb shell` hits a random device.
2. **NEVER run `adb kill-server`, `adb start-server`, or `adb disconnect`
   (without a specific serial)** — this drops ALL 200 devices.
3. **NEVER run `adb connect` to any IP not listed below.**
4. **Only interact with the 3 AUTHORIZED test devices** (see below).
5. **Always use `adb -s <serial>` for every ADB command.**

## Setup (One-Time)

### 1. Clone the repo

```powershell
git clone -b feat/instagram-autoreg https://github.com/phereri/fffbt.git C:\fffbt-ar
cd C:\fffbt-ar
```

### 2. Create Python venv and install dependencies

```powershell
python -m venv .venv
.venv\Scripts\pip install mobilerun pydantic
```

`mobilerun` (v0.6.x from PyPI) pulls in llama-index, httpx, adbutils, etc.

### 3. Create `.env`

The operator will provide a `.env` file in the repo root with these keys:
- `GOOGLE_API_KEY` — Gemini LLM (agent brain, Google AI Studio)
- `FIVESIM_API_KEY` — 5sim.net SMS provider (primary)
- `SMSPOOL_API_KEY` — smspool.net SMS provider (fallback)

See `.env.example` for the template. `_run_loop.py` loads `.env` automatically
and sets `OPENAI_API_KEY = GOOGLE_API_KEY` (Gemini uses the OpenAI-compat endpoint).

### 4. Create the clean Instagram backup

The registration ladder restores a clean (no-account) Instagram backup before
each attempt. Capture it once from a device that has Instagram installed but is
NOT logged in:

```powershell
cd C:\fffbt-ar
.venv\Scripts\python -m src.registration.backup 192.168.4.169:5555 --clear
```

This creates `clean_backups/com.instagram.android/clean_install/` with
`data.tgz` + `manifest.json` (~320KB).

If the backup script fails, do it manually:
```powershell
mkdir clean_backups\com.instagram.android\clean_install
adb -s 192.168.4.169:5555 shell "genfarmer -c 'tar czf /data/local/tmp/ig_clean.tgz -C /data/data/com.instagram.android .'"
adb -s 192.168.4.169:5555 pull /data/local/tmp/ig_clean.tgz clean_backups\com.instagram.android\clean_install\data.tgz
```

## Authorized Test Devices

| LAN IP (use for ADB) | Model | Android | HW Serial | Instagram |
|---|---|---|---|---|
| 192.168.4.169:5555 | SM-N970F | 12 | ceeccda5750864ceccec | YES |
| 192.168.4.161:5555 | SM-G781B | 12 | ce8fb4b49e27b7c763ed | YES |
| 192.168.4.123:5555 | RMX2040 | 10 | 988a983843484f534e30 | NO |

**Primary target**: SM-N970F at `192.168.4.169:5555`.
RMX2040 is Android 10 (ChangeDevice needs 12+) and has no Instagram — skip it.

First connect to the target:
```powershell
adb connect 192.168.4.169:5555
adb -s 192.168.4.169:5555 shell getprop ro.product.model
# Should print: SM-N970F
```

## GenRouter Proxy API (localhost:9000)

Assign a Vietnam SOCKS5 proxy to the device via its **LAN IP**:

```
curl -X POST "http://localhost:9000/api/update_proxy?force=true" -H "Content-Type: application/json" -d "{\"<DEVICE_LAN_IP>\": {\"is_change\": true, \"type\": \"socks5\", \"protocol\": \"socks5\", \"server\": \"<PROXY_IP>\", \"port\": <PROXY_PORT>, \"username\": \"<USER>\", \"password\": \"<PASS>\"}}"
```

Proxy list is in `.env` or provided by the operator.

Verify proxy is active:
```powershell
adb -s 192.168.4.169:5555 shell curl -s https://api.ipify.org
# Should return the proxy IP, NOT the farm's shared IP
```

To remove proxy:
```
curl -X POST "http://localhost:9000/api/update_proxy?force=true" -H "Content-Type: application/json" -d "{\"192.168.4.169\": {\"is_change\": false}}"
```

## Operational Flow

### Step 1: Assign proxy

Use the GenRouter API above with one of the Vietnam SOCKS5 proxies.
Wait 10 seconds, then verify egress with `curl -s https://api.ipify.org`.

### Step 2: Rotate android_id

```powershell
$newId = -join ((0..9 + 'a','b','c','d','e','f') | Get-Random -Count 16)
adb -s 192.168.4.169:5555 shell settings put secure android_id $newId
adb -s 192.168.4.169:5555 shell settings get secure android_id
```

### Step 3: Run the registration ladder

```powershell
cd C:\fffbt-ar
.venv\Scripts\python.exe -X utf8 -u _run_loop.py
```

The ladder tries these SMS recipes in order:
1. **5sim austria/virtual51** (proven — created a real account before)
2. 5sim luxembourg
3. 5sim croatia/virtual4
4. 5sim czech/virtual34
5. smspool USA (real-SIM fallback)

### What happens during the run

- Each attempt: restore clean backup -> snapshot fingerprint -> LLM agent drives
  Instagram signup on the phone -> buy SMS number -> enter code -> create account
- On SMS failure: advance to next recipe (different country/provider)
- On success: save app backup + fingerprint + credentials to `app_backups/`
- Results append to `accounts.csv`

### Monitoring

- Agent logs stream to stdout (`-u` = unbuffered)
- Screenshots: `artifacts/registration/<device>/<timestamp>/`
- If the agent gets stuck: it writes `operator_request.txt` in the artifacts dir.
  Answer by creating `operator_answer.txt` in the same dir with instructions.

## ChangeDevice (Optional — Identity Rotation)

For stronger anti-detection, rotate the device model/fingerprint before
registration. Requires the GenBR tools (`genfarmer_change_device.py`):

```powershell
python genfarmer_change_device.py apply --random --serial 192.168.4.169:5555
```

Device reboots (~90s). Reconnect: `adb connect 192.168.4.169:5555`

## Success

```
LADDER: success
  #0 [5sim austria/virtual51 (proven)] -> OK (success)
```

`accounts.csv` gets a new row. App backup in `app_backups/<username>/`.

## Key Files

| File | Purpose |
|---|---|
| `_run_loop.py` | Launcher (loads .env, runs the ladder) |
| `.env` | API keys (GOOGLE_API_KEY, FIVESIM_API_KEY, SMSPOOL_API_KEY) |
| `src/registration/cli.py` | CLI + RegistrationRunner |
| `src/registration/ladder.py` | RecipeLadder self-recovery engine |
| `src/registration/goal.py` | Agent goal (anti-detection built in) |
| `config/mobilerun/config.yaml` | MobileRun LLM config (Gemini profiles) |
