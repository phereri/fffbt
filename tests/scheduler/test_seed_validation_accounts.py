"""Unit tests for scripts/seed_validation_accounts.py SQL generation."""

from __future__ import annotations

import importlib.util
import pathlib
import sys


def _load_module():
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "seed_validation_accounts",
        repo_root / "scripts" / "seed_validation_accounts.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


seed = _load_module()


class TestSeedSql:
    def test_insert_sets_is_validation_true(self):
        sql = seed._seed_sql("happy", "DEVICE001", updated_offset_seconds=10)
        assert "is_validation" in sql
        # The INSERT branch must explicitly mark new rows as validation seeds.
        assert "INSERT INTO automation.accounts" in sql
        assert "true,\n        now() - interval" in sql

    def test_update_branch_promotes_existing_to_validation(self):
        """Re-seeding must lift any pre-existing non-validation row."""
        sql = seed._seed_sql("error", "DEVICE002", updated_offset_seconds=10)
        assert "is_validation = true" in sql
        assert "COALESCE(is_validation, false) = false" in sql

    def test_seed_sql_is_idempotent_by_username(self):
        sql = seed._seed_sql("happy", "DEVICE001", updated_offset_seconds=10)
        assert "WHERE username = 'validation_happy_path'" in sql
        assert "WHERE NOT EXISTS (SELECT 1 FROM existing_account)" in sql
