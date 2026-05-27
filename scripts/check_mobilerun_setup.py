#!/usr/bin/env python3
"""Safe Mobilerun setup readiness checks.

This script validates repo-local Mobilerun configuration and environment
presence only. It does not connect to phones, tap, type, create jobs, run the
launcher, publish, or print secret values.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]


def _repo_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return ROOT / path


def _host_only(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    return parsed.netloc or parsed.path or None


def _yaml_status(path: Path) -> dict[str, Any]:
    if importlib.util.find_spec("yaml") is None:
        return {
            "available": False,
            "parsed": None,
            "error": "PyYAML is not installed",
        }
    try:
        import yaml  # type: ignore[import-untyped]

        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return {
            "available": True,
            "parsed": isinstance(data, dict),
            "error": None,
        }
    except Exception as exc:
        return {
            "available": True,
            "parsed": False,
            "error": str(exc),
        }


def run_checks(*, create_dirs: bool) -> tuple[dict[str, Any], int]:
    config_value = os.environ.get("MOBILERUN_CONFIG", "config/mobilerun/config.yaml")
    trajectories_value = os.environ.get("MOBILERUN_TRAJECTORIES_DIR", "trajectories")

    config_path = _repo_path(config_value)
    trajectories_dir = _repo_path(trajectories_value)
    app_card_path = ROOT / "config" / "mobilerun" / "app_cards" / "instagram.md"

    trajectories_error = None
    if trajectories_dir.exists():
        trajectories_ready = trajectories_dir.is_dir()
    elif create_dirs:
        try:
            trajectories_dir.mkdir(parents=True, exist_ok=True)
            trajectories_ready = True
        except OSError as exc:
            trajectories_ready = False
            trajectories_error = str(exc)
    else:
        parent = trajectories_dir.parent
        trajectories_ready = parent.exists() and os.access(parent, os.W_OK)

    mobilerun_import = importlib.util.find_spec("mobilerun") is not None

    result = {
        "paths": {
            "mobilerun_config": {
                "value": config_value,
                "resolved": str(config_path),
                "exists": config_path.is_file(),
            },
            "mobilerun_trajectories_dir": {
                "value": trajectories_value,
                "resolved": str(trajectories_dir),
                "exists": trajectories_dir.is_dir(),
                "ready": trajectories_ready,
                "error": trajectories_error,
            },
            "instagram_app_card": {
                "resolved": str(app_card_path),
                "exists": app_card_path.is_file(),
            },
        },
        "env": {
            "GOOGLE_API_KEY_present": bool(os.environ.get("GOOGLE_API_KEY")),
            "ANTHROPIC_API_KEY_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "ANTHROPIC_BASE_URL_host": _host_only(os.environ.get("ANTHROPIC_BASE_URL")),
        },
        "python": {
            "mobilerun_import": mobilerun_import,
        },
        "yaml": _yaml_status(config_path) if config_path.is_file() else {
            "available": importlib.util.find_spec("yaml") is not None,
            "parsed": None,
            "error": "config file missing",
        },
    }

    required_ok = [
        result["paths"]["mobilerun_config"]["exists"],
        result["paths"]["mobilerun_trajectories_dir"]["ready"],
        result["paths"]["instagram_app_card"]["exists"],
        result["env"]["GOOGLE_API_KEY_present"] or result["env"]["ANTHROPIC_API_KEY_present"],
        result["env"]["ANTHROPIC_BASE_URL_host"] is not None,
        result["python"]["mobilerun_import"],
        result["yaml"]["parsed"] is not False,
    ]
    result["ok"] = all(required_ok)
    return result, 0 if result["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Check safe Mobilerun setup prerequisites.")
    parser.add_argument(
        "--create-dirs",
        action="store_true",
        help="Create MOBILERUN_TRAJECTORIES_DIR when missing.",
    )
    args = parser.parse_args()

    result, code = run_checks(create_dirs=args.create_dirs)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
