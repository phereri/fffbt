# VPS Rollout Runbook — Windows VPS + WSL2 (fffbt Instagram auto-registration)

Fast path to stand up the dev environment on the Windows VPS that hosts
GenFarmer and the tailscale-connected Android phones. The repo is **public**, so
the Windows side needs no git and no manual file copying.

```
Windows host ── GenFarmer.exe (REST 127.0.0.1:55554) ── Tailscale ── 📱 phones (adb :5555)
     └── WSL2 (Ubuntu) ── adb + repo + .venv + Hermes
```

---

## TL;DR (3 commands + auth)

**1. Windows — elevated PowerShell (Run as Administrator):**
```powershell
irm https://raw.githubusercontent.com/phereri/fffbt/feat/instagram-autoreg/scripts/win_vps_setup.ps1 | iex
```
> Installs WSL2 + Ubuntu, writes the correct `.wslconfig` (mirrored on Win build
> 22621+, NAT otherwise), clones the repo into WSL, builds the venv, and runs the
> 52 unit tests. **If it says "reboot required," reboot and run the same line again.**

**2. WSL — connect the phones:**
```bash
wsl -d Ubuntu -u root
cd /root/code/fffbt && source .venv/bin/activate
export FIVESIM_API_KEY=***
./scripts/connect_phones.sh 100.x.y.z 100.x.y.w     # the phones' tailnet IPs
```

**3. WSL — install + auth Hermes:**
```bash
curl -fsSL https://claude-code.nousresearch.com/install.sh | bash
# restore config/secrets from the Mac bundle OR re-auth:
hermes setup        # authenticate the anthropic provider
```
Then start Hermes in `/root/code/fffbt` — `.hermes.md` auto-loads as project context.

---

## What each script does

| Script | Runs on | Purpose |
|---|---|---|
| `scripts/win_vps_setup.ps1` | Windows (admin PS) | WSL2 install, networking-mode detection + `.wslconfig`, Ubuntu install, repo clone, invokes the Linux bootstrap. Reboot-aware + idempotent. |
| `scripts/vps_bootstrap.sh` | WSL (Ubuntu) | apt deps, `.venv`, `requirements-dev.txt`, optional `mobilerun` (`MOBILERUN_SRC=...`), runs unit tests, optional 5sim balance smoke. |
| `scripts/connect_phones.sh` | WSL (Ubuntu) | `adb connect <ip>:5555` for each phone, readiness probe, GenFarmer REST check. |

---

## Networking: mirrored vs NAT (auto-detected)

- **Win11 22H2 / Server 2025 (build ≥ 22621):** mirrored mode. `localhost:55554`
  (GenFarmer) and the phones' tailnet IPs are reachable directly from WSL. Nothing
  extra to do.
- **Server 2019/2022 / Win10 (build < 22621):** mirrored unavailable. The script
  writes a NAT `.wslconfig` and prints the fallback — run **tailscale inside WSL**
  so it becomes its own tailnet node:
  ```bash
  curl -fsSL https://tailscale.com/install.sh | sh && tailscale up
  ```
  For GenFarmer REST under NAT (only needed later, for device rotation), bind
  GenFarmer to `0.0.0.0` or add a Windows portproxy:
  ```powershell
  netsh interface portproxy add v4tov4 listenport=55554 listenaddress=0.0.0.0 connectport=55554 connectaddress=127.0.0.1
  ```

---

## The one real blocker: `mobilerun`

`mobilerun` is **not on PyPI**. Unit tests pass without it (they mock the agent
boundary), so M1 development proceeds immediately. For **live** device runs (M3),
get the mobilerun source onto the VPS and:
```bash
MOBILERUN_SRC=/root/src/mobilerun ./scripts/vps_bootstrap.sh
```

---

## Phone prep (once, over USB, before tailscale adb)

Each phone must expose adb over TCP:
```bash
adb -s <usb-serial> tcpip 5555
```
After that it's reachable at `<tailnet-ip>:5555`, which is exactly the serial
format the agent runner treats as TCP (`use_tcp=True` when the serial contains `:`).

---

## Verify success

```bash
cd /root/code/fffbt && source .venv/bin/activate
python -m pytest tests/registration tests/worker/agent_runner -q   # -> 52 passed
adb devices                                                        # phones listed as <ip>:5555 device
python -c "import asyncio; from src.registration.five_sim import FiveSimClient; print(asyncio.run(FiveSimClient().balance()))"
```

---

## Hermes runtime bundle (from the Mac, optional but fastest)

On the Mac (already documented in the transfer guide), `~/.hermes` was tarred:
`config.yaml .env auth.json SOUL.md memories/ skills/`. Copy it into WSL's
`~/.hermes` to clone the exact agent config + secrets instead of re-entering keys.
If you skip `auth.json`, just run `hermes setup` to re-auth Anthropic on the VPS.
