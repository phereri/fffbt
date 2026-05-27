#!/usr/bin/env python3
"""Seed validation-only account/environment rows for controlled MVP checks.

This script does not log in to Instagram, apply a proxy, touch devices, create
jobs, or run posting automation. It only creates the minimum active DB rows
required for create-job eligibility when phones are already logged in.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _management_api_query(project_ref: str, pat: str, sql: str) -> list[dict]:
    url = f"https://api.supabase.com/v1/projects/{project_ref}/database/query"
    body = json.dumps({"query": sql}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {pat}",
            "Content-Type": "application/json",
            "User-Agent": "fffbt-validation-seed/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Management API query failed ({e.code}): {detail}") from None
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected Management API response: {data!r}")
    return data


def _seed_sql(role: str, serial: str, *, updated_offset_seconds: int) -> str:
    username = f"validation_{role}_path"
    label = f"validation_{role}_path"
    model = "validation-happy" if role == "happy" else "validation-error"
    return f"""
WITH existing_account AS (
    SELECT id FROM automation.accounts
    WHERE username = {_sql_literal(username)} AND platform = 'instagram'
),
created_account AS (
    INSERT INTO automation.accounts (username, password, platform, status, created_at, updated_at)
    SELECT
        {_sql_literal(username)},
        'VALIDATION_ALREADY_LOGGED_IN_NO_REAL_PASSWORD',
        'instagram',
        'active',
        now() - interval '{int(updated_offset_seconds)} seconds',
        now() - interval '{int(updated_offset_seconds)} seconds'
    WHERE NOT EXISTS (SELECT 1 FROM existing_account)
    RETURNING id
),
account_row AS (
    SELECT id FROM existing_account
    UNION ALL
    SELECT id FROM created_account
    LIMIT 1
),
status_update AS (
    UPDATE automation.accounts
       SET status = 'active',
           password = 'VALIDATION_ALREADY_LOGGED_IN_NO_REAL_PASSWORD'
     WHERE id = (SELECT id FROM account_row)
       AND status <> 'active'
    RETURNING id
),
existing_env AS (
    SELECT * FROM automation.account_environments
    WHERE account_id = (SELECT id FROM account_row)
),
new_proxy AS (
    INSERT INTO automation.proxies (host, port, protocol, country_code, status)
    SELECT 'validation-no-proxy.invalid', 8080, 'http', 'ZZ', 'active'
    WHERE NOT EXISTS (SELECT 1 FROM existing_env)
    RETURNING id
),
new_device_profile AS (
    INSERT INTO automation.device_profiles (
        brand, model, android_version, screen_width, screen_height,
        screen_density, locale, timezone, status
    )
    SELECT 'Validation', {_sql_literal(model)}, '12', 1080, 1920, 420,
           'en_US', 'America/Los_Angeles', 'active'
    WHERE NOT EXISTS (SELECT 1 FROM existing_env)
    RETURNING id
),
new_gps AS (
    INSERT INTO automation.gps_locations (label, latitude, longitude, accuracy_meters, status)
    SELECT {_sql_literal(label)}, 37.7749000, -122.4194000, 25.0, 'active'
    WHERE NOT EXISTS (SELECT 1 FROM existing_env)
    RETURNING id
),
new_app_state AS (
    INSERT INTO automation.app_states (session_data, status, last_synced_at)
    SELECT jsonb_build_object(
               'validation_only', true,
               'device_serial', {_sql_literal(serial)},
               'login_assumption', 'already_logged_in_on_phone'
           ),
           'active',
           now()
    WHERE NOT EXISTS (SELECT 1 FROM existing_env)
    RETURNING id
),
created_env AS (
    INSERT INTO automation.account_environments (
        account_id, proxy_id, device_profile_id, gps_location_id, app_state_id
    )
    SELECT
        (SELECT id FROM account_row),
        (SELECT id FROM new_proxy),
        (SELECT id FROM new_device_profile),
        (SELECT id FROM new_gps),
        (SELECT id FROM new_app_state)
    WHERE NOT EXISTS (SELECT 1 FROM existing_env)
    RETURNING id
),
env_row AS (
    SELECT id FROM existing_env
    UNION ALL
    SELECT id FROM created_env
    LIMIT 1
)
SELECT
    {_sql_literal(role)} AS role,
    {_sql_literal(serial)} AS serial,
    (SELECT id FROM account_row) AS account_id,
    (SELECT id FROM env_row) AS environment_id,
    EXISTS (SELECT 1 FROM created_account) AS account_created,
    EXISTS (SELECT 1 FROM created_env) AS environment_created;
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed validation-only account/environment rows."
    )
    parser.add_argument("--via-management-api", action="store_true", required=True)
    parser.add_argument("--project-ref", required=True)
    parser.add_argument("--happy-serial", required=True)
    parser.add_argument("--error-serial", required=True)
    args = parser.parse_args(argv)

    pat = os.environ.get("SUPABASE_PAT")
    if not pat:
        print("error: SUPABASE_PAT env var is required.", file=sys.stderr)
        return 2

    results = []
    results.extend(
        _management_api_query(
            args.project_ref,
            pat,
            _seed_sql("happy", args.happy_serial, updated_offset_seconds=20),
        )
    )
    results.extend(
        _management_api_query(
            args.project_ref,
            pat,
            _seed_sql("error", args.error_serial, updated_offset_seconds=10),
        )
    )
    print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
