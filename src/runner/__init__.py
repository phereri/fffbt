"""Standalone, DB-free Trial Reel poster.

This package is a thin wiring layer over the worker steps that lets a fresh
clone of the repo publish one Instagram Trial Reel to an already-prepared phone
without any Supabase / GenFarmer / launcher involvement. It is purely additive:
nothing under ``src/scheduler`` or the production pipeline imports it, and it
imports only the worker steps it needs.

See ``docs/setup/standalone-runner.md`` for the quickstart.
"""
