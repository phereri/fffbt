"""Standalone Instagram account auto-registration (авторег) package.

A lightweight, agent-driven tool that registers a fresh Instagram account on a
free Android device. Phone verification via 5sim.net; the agent invents its own
username/password each run. Output is a CSV row with credentials + full device
fingerprint.

This package does NOT touch Supabase / the scheduler state machine. It reuses
``src.worker.agent_runner`` and ``src.worker.tools`` only. See
``docs/superpowers/specs/2026-06-08-instagram-autoreg-design.md``.
"""
