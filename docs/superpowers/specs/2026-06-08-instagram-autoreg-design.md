# Instagram Account Auto-Registration (авторег) — Design Spec

Status: **approved (brainstorm) → implementing M1/M2**
Date: 2026-06-08
Owner: fffbt
Related: `.hermes.md` (session handoff), `docs/research/genfarmer-*.md`,
`docs/research/mobilerun-real-repo-task-map.md`

## 1. Goal

Build a standalone, lightweight tool that picks a free Android device and
registers a fresh Instagram account on it, fully agent-driven, with human-like
delays, scalable to hundreds of devices in parallel. Phone verification via
**5sim.net**. The agent invents its own username/password each time. Output is a
**CSV** containing everything needed to use the account (credentials, phone, and
the full device fingerprint + serials).

Current phase: **local testing on an Android Studio emulator** — no GenFarmer,
proxies, or real devices yet. The flow is developed interactively: the agent
decides what to do on screen and, on an unexpected screen, pauses and asks the
operator (`ask_operator`), saving all trajectories.

## 2. Module layout (`src/registration/`)

Standalone — does NOT touch Supabase / the state machine yet. Reuses only
`src/worker/agent_runner` + `src/worker/tools/device.py` + `_adb.py`.

```
src/registration/
  __init__.py
  cli.py         # register --device-serial <S> [--country ...] [--csv accounts.csv]
  five_sim.py    # 5sim.net client: buy_number, get_code(poll), finish, ban, cancel, balance
  tools.py       # MobileRun custom tools: buy_phone_number(), get_sms_code(), ask_operator()
  goal.py        # build_registration_goal() — IG signup goal (analog of build_trial_reel_goal)
  identity.py    # fallback identity generation (name/birthday) if the agent needs one
  fingerprint.py # device fingerprint+serial snapshot (wraps device_summary + raw getprop dump)
  rotator.py     # DeviceIdentityRotator interface: NoopRotator (local), GenFarmerAutoRotator (TODO)
  output.py      # atomic append row to CSV
  result.py      # RegistrationResult (dependency-free dataclass; lazy pydantic model for the agent)
```

### Conventions inherited from the repo

- **No hard deps at import time.** `pydantic` and `mobilerun` are imported lazily
  (see `agent_runner/result.py` / `_post_result_pydantic_model`). Modules import
  cleanly with only the stdlib + pytest present.
- **HTTP via stdlib `urllib`** (the repo already does this in `scheduler/cli.py`),
  wrapped in `asyncio.to_thread` to stay async (mirrors `tools/_adb.py`).
- Tools return `ToolResult` (`src/worker/tools/_types.py`).
- Tests mock at the HTTP / ADB boundary (`unittest.mock`), no network.

## 3. 5sim ownership — agent owns it via custom tools

The Python orchestrator does NOT pre-own the 5sim lifecycle. Two blocking custom
tools wrap the client; the agent calls them when it decides:

- `buy_phone_number(country="any")` → buys an `instagram` activation number,
  stores the live order on a per-run `RegistrationSession`, returns the phone.
- `get_sms_code()` → polls the active order for the SMS code (bounded timeout).

Money guardrails live in the tools/session, not the agent:

- A buy starts a timeout window; on `get_sms_code` timeout the order is auto-`cancel`led.
- On success the agent (or CLI epilogue) calls `finish`; on a bad/used number `ban`.
- `FIVESIM_API_KEY` from env. Base URL `https://5sim.net/v1`.

### 5sim REST surface used

| Action | Endpoint |
|---|---|
| balance/profile | `GET /user/profile` |
| buy activation | `GET /user/buy/activation/{country}/{operator}/{product}` |
| check order (poll SMS) | `GET /user/check/{id}` |
| finish (consumed OK) | `GET /user/finish/{id}` |
| ban (bad number) | `GET /user/ban/{id}` |
| cancel (no SMS / abort) | `GET /user/cancel/{id}` |

Auth: `Authorization: Bearer <FIVESIM_API_KEY>`, `Accept: application/json`.
Defaults: `operator=any`, `product=instagram`.

## 4. Pause/resume — blocking `ask_operator`

`ask_operator(question)` is a blocking custom tool. On call it captures a
screenshot + UI dump to artifacts, prints the question to the terminal, blocks on
stdin, and returns the operator's typed answer to the agent. No MobileRun run-loop
changes — same mechanism as `tap_share_and_confirm` (which already blocks ~22 s).
The blocking read is configurable / injectable so tests don't touch real stdin.

## 5. Wiring custom tools into MobileRun

**Single surgical change outside `registration/`:** add an optional `tools`
field to `AgentFactoryRequest` in `mobilerun_agent_runner.py` and pass it into
`MobileAgent(... tools=...)` in `_default_agent_factory`. Default empty → the
posting path is unchanged. The registration runner supplies its custom tools.
`mobilerun` is NOT installed locally (lazy import; tests mock it).

## 6. Agent-invented identity + result

The agent invents username, password, full name, birthday (18+). `identity.py`
provides a fallback generator only if needed.

`RegistrationResult` (dependency-free dataclass; mirrored by a lazy pydantic model
used as the agent's `output_model`) returns: `success, username, password,
full_name, birthday, phone_number, phone_country, fivesim_order_id,
failure_reason, notes`.

## 7. Device identity — rotate-then-capture behind an interface

```
DeviceIdentityRotator: async rotate(serial) -> RotationResult
  NoopRotator           # local/emulator: no-op (capture-only) — used now
  GenFarmerAutoRotator  # fleet: trigger ChangeDevice Automation Run via REST,
                        #        THEN verify ADB reachable (hazard) with timeout — TODO
```

Flow per registration: `rotate(serial)` → verify reachable → **snapshot
fingerprint (always)** → register. We never SET fingerprint fields; we READ them
with ADB at registration time and also dump raw `getprop` to an artifact so
nothing is lost if GenFarmer touches an unlisted prop. Local degrades to
capture-only via `NoopRotator`.

## 8. CSV schema (`accounts.csv`)

See `output.py::CSV_COLUMNS` — credentials, phone, timestamps, device serials,
the full fingerprint field set (model/brand/manufacturer/product/device,
build fingerprint/id, android version/sdk, serialno/android_id/gaid/imei/imsi/
boot_id, wifi_mac/ip, screen w/h/density, locale/timezone/carrier), and the
artifact paths (`raw_getprop_path`, `trajectory_path`, `status`).

## 9. Milestones

- **M0** — prereqs: install `mobilerun` locally (blocker), emulator in
  `adb devices` with Instagram installed, `FIVESIM_API_KEY` set + `five_sim.py`
  smoke-tested standalone.
- **M1** — scaffold: `five_sim.py`, `output.py`, `fingerprint.py`, `identity.py`,
  `rotator.py`, `result.py` + unit tests (mock HTTP/ADB).
- **M2** — wire `tools=` into the factory + `ask_operator` + `goal.py` v1 + `cli.py`.
- **M3** — first interactive registration run on the emulator; iterate goal /
  AppCard via `ask_operator`; accumulate trajectories.
- **M4** — harden: human-like delays, retries, free-device picker (sets up later
  integration into the parallel launcher).

## 10. Risks

- Instagram aggressively flags emulators / datacenter IPs → registration may hit
  challenge/ban on the emulator. Acceptable for FLOW DEVELOPMENT (map trajectories
  + `ask_operator` points); real success needs real devices + proxies.
- Portal IME (`com.mobilerun.portal`) absent on a bare emulator → text input falls
  back to the ADB keyboard (fine for username/password/code).
