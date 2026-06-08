"""Tests for the registration goal builder (``goal.py``)."""

from __future__ import annotations

from src.registration.goal import build_registration_goal


class TestBuildRegistrationGoal:
    def test_contains_device_and_core_instructions(self):
        goal = build_registration_goal(device_serial="100.64.0.5:5555")
        assert "100.64.0.5:5555" in goal
        assert "Instagram" in goal
        # Agent invents its own identity.
        assert "username" in goal.lower()
        assert "password" in goal.lower()
        assert "birthday" in goal.lower() or "date of birth" in goal.lower()

    def test_mentions_custom_tools(self):
        goal = build_registration_goal(device_serial="s")
        assert "buy_phone_number" in goal
        assert "get_sms_code" in goal
        assert "ask_operator" in goal

    def test_country_rendered(self):
        goal = build_registration_goal(device_serial="s", country="england")
        assert "england" in goal

    def test_age_policy_18_plus(self):
        goal = build_registration_goal(device_serial="s")
        assert "18" in goal

    def test_hard_stops_present(self):
        goal = build_registration_goal(device_serial="s")
        low = goal.lower()
        assert "ask_operator" in low
        # Unexpected screens should route to the operator, not guess.
        assert "unexpected" in low

    def test_do_not_reuse_existing_account(self):
        goal = build_registration_goal(device_serial="s")
        low = goal.lower()
        assert "new account" in low or "register" in low
