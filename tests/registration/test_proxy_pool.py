"""Tests for the proxy pool quota logic (deterministic clock, tmp SQLite)."""

from __future__ import annotations

from src.registration.proxy_pool import Proxy, ProxyPool


class _Clock:
    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


def _pool(tmp_path, **kw):
    clock = _Clock()
    pool = ProxyPool(tmp_path / "proxies.db", now_fn=clock, **kw)
    return pool, clock


def test_add_is_idempotent(tmp_path):
    pool, _ = _pool(tmp_path)
    a = pool.add_from_line("1.2.3.4:1080:u:p", country="vietnam")
    b = pool.add_from_line("1.2.3.4:1080:u:p", country="vietnam")
    assert a == b
    assert len(pool.available()) == 1


def test_proxy_url_and_line():
    p = Proxy(id=1, protocol="socks5", server="1.2.3.4", port=1080, username="u", password="p")
    assert p.as_url() == "socks5://u:p@1.2.3.4:1080"
    assert p.as_line() == "1.2.3.4:1080:u:p"


def test_quota_caps_uses_in_window(tmp_path):
    pool, clock = _pool(tmp_path, max_uses=5, window_seconds=12 * 3600)
    pid = pool.add_from_line("1.2.3.4:1080:u:p")
    # 5 acquires succeed (same single proxy), 6th is blocked (all at quota).
    for _ in range(5):
        assert pool.acquire() is not None
    assert pool.usage_count(pid) == 5
    assert pool.acquire() is None


def test_window_rolls_off(tmp_path):
    pool, clock = _pool(tmp_path, max_uses=5, window_seconds=12 * 3600)
    pool.add_from_line("1.2.3.4:1080:u:p")
    for _ in range(5):
        pool.acquire()
    assert pool.acquire() is None
    clock.advance(12 * 3600 + 1)  # all 5 uses fall outside the window
    assert pool.acquire() is not None


def test_load_balances_least_used_first(tmp_path):
    pool, _ = _pool(tmp_path, max_uses=5)
    pool.add_from_line("1.1.1.1:1080:u:p")  # id 1
    pool.add_from_line("2.2.2.2:1080:u:p")  # id 2
    # First two acquires should hit each proxy once (least-used-first balancing).
    first = pool.acquire()
    second = pool.acquire()
    assert {first.server, second.server} == {"1.1.1.1", "2.2.2.2"}
    assert pool.usage_count(1) == 1 and pool.usage_count(2) == 1


def test_country_filter(tmp_path):
    pool, _ = _pool(tmp_path, max_uses=5)
    pool.add_from_line("1.1.1.1:1080:u:p", country="vietnam")
    pool.add_from_line("9.9.9.9:1080:u:p", country="usa")
    got = pool.acquire(country="vietnam")
    assert got.server == "1.1.1.1"
    assert pool.acquire(country="germany") is None  # none for that country


def test_disabled_proxy_not_acquired(tmp_path):
    pool, _ = _pool(tmp_path, max_uses=5)
    pid = pool.add_from_line("1.1.1.1:1080:u:p")
    pool.set_enabled(pid, False)
    assert pool.acquire() is None
    pool.set_enabled(pid, True)
    assert pool.acquire() is not None
