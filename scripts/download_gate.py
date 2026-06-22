#!/usr/bin/env python3
"""Serial video-download queue shared across the whole fleet.

Only ONE video downloads at a time — across all devices AND all fleet processes —
because the slow VN->RU S3 link collapses when several download at once. A caller
waiting for its turn is QUEUED (no timeout while queued); once it is the caller's
turn it runs the actual download under its OWN timeout.

    async with DownloadGate():                  # blocks (queued) until our turn
        await VideoPreparationStep().run(...)    # the real download (360s timeout)

Ordering is FIFO within a process (asyncio lock) and one-at-a-time across processes
(an exclusive lock file). A lock left by a crashed/killed holder is stolen after
``_STALE_SECS`` so the queue can never wedge permanently.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

_LOCKFILE = Path(__file__).resolve().parents[1] / "data" / "download.lock"
_STALE_SECS = 420                 # > max legit hold (360s download + transcode/push)
_proc_lock = asyncio.Lock()       # FIFO ordering within one process


class DownloadGate:
    """Async context manager whose ``__aenter__`` returns only when it is this
    caller's turn to download. Use ``time.monotonic()`` around it to measure how
    long the request sat in the queue."""

    async def __aenter__(self) -> "DownloadGate":
        await _proc_lock.acquire()                  # serialise + FIFO in-process
        _LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
        while True:                                 # then the cross-process lock
            try:
                self._fd = os.open(str(_LOCKFILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, f"{os.getpid()} {int(time.time())}".encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - _LOCKFILE.stat().st_mtime > _STALE_SECS:
                        _LOCKFILE.unlink()
                        continue                    # stale holder -> steal it
                except FileNotFoundError:
                    continue                        # vanished -> retry immediately
                except Exception:
                    pass
                await asyncio.sleep(1.0)
            except Exception:
                self._fd = None                     # never deadlock -> proceed ungated
                return self

    async def __aexit__(self, *exc) -> None:
        try:
            if getattr(self, "_fd", None) is not None:
                os.close(self._fd)
                _LOCKFILE.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
        finally:
            try:
                _proc_lock.release()
            except RuntimeError:
                pass
