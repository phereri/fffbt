"""Entry point: python -m scheduler

Thin wrapper that delegates to ``scheduler.cli run-launcher`` so there is a
single launcher code path with the same safe default (real proof_of_posting
steps) and the same flags (--max-jobs, --max-parallel, --stub).
"""

from __future__ import annotations

import sys


def main() -> int:
    from scheduler.cli import cmd_run_launcher

    return cmd_run_launcher(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
