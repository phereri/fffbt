"""Entry point: python -m scheduler"""

from __future__ import annotations

import asyncio
import logging
import os
import sys


def main() -> int:
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("error: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    level = os.environ.get("LOG_LEVEL", "info").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(levelname)s: %(message)s",
    )

    from scheduler.launcher import JobLauncher

    launcher = JobLauncher(db_url)
    asyncio.run(launcher.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
