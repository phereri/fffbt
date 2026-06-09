"""Tests for the registration CLI orchestration (``cli.py``).

No real device/network/agent. The agent factory, fingerprint snapshot, rotator,
and 5sim client are injected fakes. Verifies the end-to-end orchestration:
rotate -> snapshot -> run agent -> write CSV row, plus arg parsing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.registration.cli import RegistrationRunner, build_arg_parser
from src.registration.fingerprint import FingerprintSnapshot
from src.registration.output import CSV_COLUMNS
from src.registration.rotator import NoopRotator, RotationResult


class _FakeStructured:
    def __init__(self, **kw):
        self.success = kw.get("success", True)
        self.username = kw.get("username", "alice123")
        self.password = kw.get("password", "Str0ng!pass")
        self.full_name = kw.get("full_name", "Alice Carter")
        self.birthday = kw.get("birthday", "2000-01-01")
        self.phone_number = kw.get("phone_number", "+790111")
        self.phone_country = kw.get("phone_country", "any")
        self.fivesim_order_id = kw.get("fivesim_order_id", "555")
        self.failure_reason = kw.get("failure_reason", None)
        self.notes = kw.get("notes", None)


class _FakeResultEvent:
    def __init__(self, structured, status="complete"):
        self.structured_output = structured
        self.status = status


class _FakeAgent:
    def __init__(self, event):
        self._event = event
        self.runs = 0

    async def run(self):
        self.runs += 1
        return self._event


def _fake_factory(event):
    captured = {}

    def factory(request):
        captured["request"] = request
        return _FakeAgent(event)

    return factory, captured


async def _fake_snapshot(serial, **kw):
    return FingerprintSnapshot(
        serial=serial,
        fields={"fp_model": "Pixel 7", "fp_brand": "google"},
        raw_getprop="[ro.product.model]: [Pixel 7]",
        raw_getprop_path=str(kw.get("raw_getprop_path") or ""),
    )


def _runner(tmp_path, event=None, **kw):
    event = event or _FakeResultEvent(_FakeStructured())
    factory, captured = _fake_factory(event)
    rotator = kw.pop("rotator", None) or NoopRotator()
    runner = RegistrationRunner(
        device_serial="100.64.0.5:5555",
        csv_path=str(tmp_path / "accounts.csv"),
        country="any",
        agent_factory=factory,
        snapshot_fn=_fake_snapshot,
        rotator=rotator,
        artifacts_dir=str(tmp_path / "art"),
        **kw,
    )
    return runner, captured


class TestArgParser:
    def test_requires_device_serial(self):
        parser = build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["register"])

    def test_parses_options(self):
        parser = build_arg_parser()
        ns = parser.parse_args(
            ["register", "--device-serial", "dev1", "--country", "england", "--csv", "out.csv"]
        )
        assert ns.device_serial == "dev1"
        assert ns.country == "england"
        assert ns.csv == "out.csv"


class TestRunnerOrchestration:
    def test_success_writes_csv_row(self, tmp_path):
        runner, captured = _runner(tmp_path)
        result = asyncio.run(runner.run())
        assert result.success
        csv_path = tmp_path / "accounts.csv"
        assert csv_path.exists()
        import csv as _csv
        with open(csv_path, newline="", encoding="utf-8") as fh:
            rows = list(_csv.DictReader(fh))
        assert len(rows) == 1
        row = rows[0]
        assert row["username"] == "alice123"
        assert row["fp_model"] == "Pixel 7"
        assert row["device_adb_serial"] == "100.64.0.5:5555"
        assert row["status"] == "success"
        assert set(row.keys()) == set(CSV_COLUMNS)

    def test_goal_and_tools_passed_to_factory(self, tmp_path):
        runner, captured = _runner(tmp_path)
        asyncio.run(runner.run())
        request = captured["request"]
        assert "Instagram" in request.goal
        assert "100.64.0.5:5555" in request.goal
        # Custom tools wired in (MobileRun custom_tools dict format).
        assert len(request.tools) == 3
        assert set(request.tools) == {"buy_phone_number", "get_sms_code", "ask_operator"}
        # Each entry exposes a callable under "function".
        assert all(callable(v["function"]) for v in request.tools.values())

    def test_failure_records_failed_status(self, tmp_path):
        event = _FakeResultEvent(_FakeStructured(success=False, failure_reason="signup_blocked"))
        runner, _ = _runner(tmp_path, event=event)
        result = asyncio.run(runner.run())
        assert not result.success
        import csv as _csv
        with open(tmp_path / "accounts.csv", newline="", encoding="utf-8") as fh:
            rows = list(_csv.DictReader(fh))
        assert rows[0]["status"] == "failed"

    def test_unreachable_device_skips_agent(self, tmp_path):
        async def probe(serial):
            return False

        runner, captured = _runner(tmp_path, rotator=NoopRotator(reachable_probe=probe))
        result = asyncio.run(runner.run())
        assert not result.success
        assert "request" not in captured  # agent never built
