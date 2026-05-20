"""Entry point: python -m scheduler"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

_STUB_WARNING = (
    "WARNING: The worker pipeline is a STUB — jobs will be created and "
    "moved to preparing_device, but NO real device automation or posting "
    "will occur. Do NOT run against the production queue until the real "
    "worker is implemented."
)


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

    log = logging.getLogger("scheduler")
    log.warning(_STUB_WARNING)

    from scheduler.launcher import JobLauncher

    launcher = JobLauncher(db_url)
    asyncio.run(launcher.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
