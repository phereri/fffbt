"""Create a 'golden' backup of a clean (logged-out, no-account) Instagram install.

Run on the GenFarmer Windows host, against a phone whose Instagram is freshly
installed / cleared and sitting on the login screen (NOT logged in):

    python backup.py <serial>           # e.g. python backup.py 100.89.126.111:5555
    python backup.py <serial> --clear   # pm clear first, then back up the fresh state

The resulting dir (data.tgz + manifest.json) is what you pass to
``register --clean-backup <dir>`` to restore a fresh Instagram fast (no APK
download). Test phone: Pixel 6 Pro.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# backup.py lives at the repo root; make ``src`` importable when run from anywhere.
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

from src.genfarmer.app_backup import default_backup_client  # noqa: E402

PACKAGE = "com.instagram.android"
CLEAN_BACKUP_ROOT = Path("clean_backups")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a clean Instagram golden backup.")
    parser.add_argument("serial", help="ADB serial (ip:port for TCP), e.g. 100.89.126.111:5555")
    parser.add_argument("--package", default=PACKAGE, help=f"Package (default: {PACKAGE}).")
    parser.add_argument("--clear", action="store_true",
                        help="Run 'pm clear' to reset to a fresh state before backing up.")
    parser.add_argument("--out", default=None,
                        help="Output dir (default: clean_backups/<package>/clean_install).")
    args = parser.parse_args()

    out_dir = Path(args.out) if args.out else CLEAN_BACKUP_ROOT / args.package / "clean_install"
    client = default_backup_client(backup_root=CLEAN_BACKUP_ROOT)

    print(f"--- Creating clean backup for {args.package} from {args.serial} ---")

    if args.clear:
        from src.genfarmer.app_backup import _default_shell  # local: real adb shell

        print(f"1. Clearing app data (pm clear {args.package})...")
        _default_shell(args.serial, f"pm clear {args.package}", 30.0)
        # Launch once so Instagram re-initialises its data dirs, then settle.
        _default_shell(
            args.serial,
            f"monkey -p {args.package} -c android.intent.category.LAUNCHER 1",
            30.0,
        )
        time.sleep(8)
    else:
        print("1. Skipping clear (assuming Instagram is already in a clean, logged-out state).")

    print("2. Creating backup...")
    result = client.backup(args.serial, args.package, out_dir=out_dir, label="clean_install")

    if not result.ok:
        print(f"\n--- BACKUP FAILED ---\nError: {result.error}")
        return 1

    print("\n--- Clean backup created successfully! ---")
    print(f"  Location: {result.backup_dir}")
    print(f"  Size:     {result.archive_size_bytes / 1024:.1f} KB")
    print(f"\nUse it with:\n  register --device-serial {args.serial} --clean-backup {result.backup_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
