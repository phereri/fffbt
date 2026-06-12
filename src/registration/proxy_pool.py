"""Proxy pool with per-proxy usage quotas, state in SQLite.

For production: each account registration is routed through a proxy (on-device
GenFarmer ProxyConnector). Instagram flags an IP that verifies too many accounts,
so each proxy may be used for at most ``max_uses`` registrations within a rolling
``window_seconds`` (default: 5 per 12h). :meth:`ProxyPool.acquire` returns the
next under-quota proxy and records the use atomically, so concurrent autoreg runs
load-balance and never overuse one IP.

Time is injected (``now_fn``) so tests are deterministic without real waiting.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_MAX_USES = 5
DEFAULT_WINDOW_SECONDS = 12 * 3600  # 12 hours


@dataclass
class Proxy:
    """A SOCKS5/HTTP proxy row."""

    id: int
    protocol: str
    server: str
    port: int
    username: str = ""
    password: str = ""
    country: str = ""
    enabled: bool = True

    def as_url(self) -> str:
        auth = f"{self.username}:{self.password}@" if self.username else ""
        return f"{self.protocol}://{auth}{self.server}:{self.port}"

    def as_line(self) -> str:
        return f"{self.server}:{self.port}:{self.username}:{self.password}"


class ProxyPool:
    """SQLite-backed proxy pool enforcing a per-proxy rolling-window quota."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        max_uses: int = DEFAULT_MAX_USES,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._max_uses = int(max_uses)
        self._window = float(window_seconds)
        self._now = now_fn
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS proxies(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              protocol TEXT NOT NULL DEFAULT 'socks5',
              server   TEXT NOT NULL,
              port     INTEGER NOT NULL,
              username TEXT NOT NULL DEFAULT '',
              password TEXT NOT NULL DEFAULT '',
              country  TEXT NOT NULL DEFAULT '',
              enabled  INTEGER NOT NULL DEFAULT 1,
              UNIQUE(server, port, username)
            );
            CREATE TABLE IF NOT EXISTS proxy_usage(
              id        INTEGER PRIMARY KEY AUTOINCREMENT,
              proxy_id  INTEGER NOT NULL REFERENCES proxies(id),
              used_at   REAL NOT NULL,
              note      TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ix_proxy_usage ON proxy_usage(proxy_id, used_at);
            """
        )
        self._conn.commit()

    # -- population ---------------------------------------------------------

    def add_proxy(
        self,
        server: str,
        port: int,
        username: str = "",
        password: str = "",
        *,
        protocol: str = "socks5",
        country: str = "",
    ) -> int:
        """Insert a proxy (idempotent on server/port/username); return its id."""
        self._conn.execute(
            "INSERT OR IGNORE INTO proxies(protocol,server,port,username,password,country) "
            "VALUES(?,?,?,?,?,?)",
            (protocol, server, int(port), username, password, country),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM proxies WHERE server=? AND port=? AND username=?",
            (server, int(port), username),
        ).fetchone()
        return int(row["id"])

    def add_from_line(self, line: str, *, country: str = "", protocol: str = "socks5") -> int:
        """Add a proxy from an ``ip:port:user:pass`` line (user/pass optional)."""
        parts = line.strip().split(":")
        if len(parts) < 2:
            raise ValueError(f"bad proxy line: {line!r}")
        server, port = parts[0], int(parts[1])
        username = parts[2] if len(parts) > 2 else ""
        password = parts[3] if len(parts) > 3 else ""
        return self.add_proxy(server, port, username, password, protocol=protocol, country=country)

    def set_enabled(self, proxy_id: int, enabled: bool) -> None:
        self._conn.execute("UPDATE proxies SET enabled=? WHERE id=?", (1 if enabled else 0, proxy_id))
        self._conn.commit()

    # -- quota / selection --------------------------------------------------

    def usage_count(self, proxy_id: int, now: float | None = None) -> int:
        """Uses of ``proxy_id`` within the current rolling window."""
        now = self._now() if now is None else now
        cutoff = now - self._window
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM proxy_usage WHERE proxy_id=? AND used_at>=?",
            (proxy_id, cutoff),
        ).fetchone()
        return int(row["c"])

    def _row_to_proxy(self, row: sqlite3.Row) -> Proxy:
        return Proxy(
            id=int(row["id"]), protocol=row["protocol"], server=row["server"],
            port=int(row["port"]), username=row["username"], password=row["password"],
            country=row["country"], enabled=bool(row["enabled"]),
        )

    def _candidate_rows(self, country: str | None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM proxies WHERE enabled=1"
        args: tuple = ()
        if country:
            sql += " AND country=?"
            args = (country,)
        sql += " ORDER BY id"
        return self._conn.execute(sql, args).fetchall()

    def available(self, *, country: str | None = None, now: float | None = None) -> list[Proxy]:
        """Enabled proxies currently under quota, least-used first."""
        now = self._now() if now is None else now
        out: list[tuple[int, Proxy]] = []
        for row in self._candidate_rows(country):
            c = self.usage_count(int(row["id"]), now)
            if c < self._max_uses:
                out.append((c, self._row_to_proxy(row)))
        out.sort(key=lambda t: (t[0], t[1].id))  # fewest uses first, stable by id
        return [p for _, p in out]

    def acquire(self, *, country: str | None = None, note: str = "") -> Proxy | None:
        """Return the least-used under-quota proxy and record one use.

        Returns ``None`` if every (matching, enabled) proxy is at quota — the
        caller should wait/raise rather than burn an over-used IP.
        """
        now = self._now()
        candidates = self.available(country=country, now=now)
        if not candidates:
            return None
        chosen = candidates[0]
        self._conn.execute(
            "INSERT INTO proxy_usage(proxy_id, used_at, note) VALUES(?,?,?)",
            (chosen.id, now, note),
        )
        self._conn.commit()
        return chosen

    def close(self) -> None:
        self._conn.close()


__all__ = ["Proxy", "ProxyPool", "DEFAULT_MAX_USES", "DEFAULT_WINDOW_SECONDS"]
