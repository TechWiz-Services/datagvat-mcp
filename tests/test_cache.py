import logging
import sqlite3

import pytest

from datagvat_mcp.cache import DB_FILE, MEMORY_LIMIT, DiskCache, end_call, note_served, start_call

MB = 1024 * 1024
LIMITS = {"search": MB, "dataset": MB, "catalogue": MB, "vocabulary": MB, "preview": MB}


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(directory=None, limits=LIMITS, max_stale=0.0, clock=None) -> tuple[DiskCache, Clock]:
    clock = clock or Clock()
    return DiskCache(str(directory) if directory else None, limits, max_stale, clock), clock


def test_get_set_and_expiry():
    c, clock = make()
    assert c.get("dataset", "a") is None
    c.set("dataset", "a", {"x": ["ä", 1, None]}, ttl=5)
    hit = c.get("dataset", "a")
    assert (hit.value, hit.fresh, hit.stored_at) == ({"x": ["ä", 1, None]}, True, clock.t)
    clock.t += 5
    assert c.get("dataset", "a") is None  # no stale period configured: gone
    assert c.stats()["dataset"]["entries"] == 0


def test_categories_are_separate():
    c, _ = make()
    c.set("dataset", "k", 1, 60)
    c.set("search", "k", 2, 60)
    assert (c.get("dataset", "k").value, c.get("search", "k").value) == (1, 2)
    assert c.get("preview", "k") is None


def test_expired_entry_stays_available_as_stale_copy():
    c, clock = make(max_stale=100)
    c.set("search", "q", [1], ttl=10)
    clock.t += 10
    hit = c.get("search", "q")
    assert hit.value == [1] and hit.fresh is False and hit.stored_at == clock.t - 10
    clock.t += 99
    assert c.get("search", "q").fresh is False
    clock.t += 1
    assert c.get("search", "q") is None


def test_zero_ttl_or_zero_size_switches_a_category_off():
    c, _ = make(limits={**LIMITS, "preview": 0})
    c.set("dataset", "a", 1, ttl=0)
    c.set("preview", "a", 1, ttl=60)
    c.set("unknown-category", "a", 1, ttl=60)
    assert c.get("dataset", "a") is None and c.get("preview", "a") is None and c.get("unknown-category", "a") is None


def test_falsy_values_are_cached():
    c, _ = make()
    c.set("search", "empty", {}, 60)
    assert c.get("search", "empty").value == {}
    c.clear()
    assert c.get("search", "empty") is None


def test_size_limit_evicts_least_recently_used_within_the_category_only():
    import os

    blob = lambda: os.urandom(600).hex()  # random, so every entry compresses to about the same size  # noqa: E731
    probe, _ = make()
    probe.set("search", "x", blob(), 60)
    one = probe.stats()["search"]["bytes"]
    limit = int(3.5 * one)  # room for three
    c, clock = make(limits={**LIMITS, "search": limit})
    c.set("dataset", "keep", blob(), 60)
    for name in ("a", "b", "c"):
        clock.t += 1
        c.set("search", name, blob(), 60)
    clock.t += 1
    assert c.get("search", "a")  # a is now the most recently used
    clock.t += 1
    c.set("search", "d", blob(), 60)
    c.set("search", "e", blob(), 60)
    assert c.get("search", "b") is None and c.get("search", "c") is None
    assert c.get("search", "a") and c.get("search", "d") and c.get("search", "e")
    assert c.get("dataset", "keep")
    stats = c.stats()
    assert stats["search"]["bytes"] <= limit == stats["search"]["limit_bytes"] and stats["search"]["entries"] == 3


def test_value_larger_than_the_limit_is_not_stored():
    import os

    c, _ = make(limits={**LIMITS, "preview": 500})
    c.set("preview", "big", os.urandom(2000).hex(), 60)
    assert c.get("preview", "big") is None


def test_survives_a_restart(tmp_path):
    c, clock = make(tmp_path, max_stale=50)
    assert c.persistent
    c.set("dataset", "d", {"id": "x"}, ttl=100)
    c.set("search", "old", 1, ttl=10)
    c.close()
    clock.t += 20
    again, _ = make(tmp_path, max_stale=50, clock=clock)
    assert again.get("dataset", "d").value == {"id": "x"} and again.get("dataset", "d").fresh
    assert again.get("search", "old").fresh is False
    again.close()
    clock.t += 1000
    later, _ = make(tmp_path, max_stale=50, clock=clock)
    assert later.stats()["dataset"]["entries"] == 0  # purged at start
    later.close()


def test_smaller_limit_after_a_restart_is_enforced(tmp_path):
    import os

    c, clock = make(tmp_path)
    for i in range(5):
        clock.t += 1
        c.set("search", str(i), os.urandom(600).hex(), 600)
    c.close()
    small, _ = make(tmp_path, limits={**LIMITS, "search": 3000}, clock=clock)
    assert small.stats()["search"]["bytes"] <= 3000 and small.get("search", "4") and small.get("search", "0") is None
    small.close()


def test_damaged_cache_file_is_replaced(tmp_path, caplog):
    (tmp_path / DB_FILE).write_bytes(b"this is not a database" * 100)
    with caplog.at_level(logging.WARNING, logger="datagvat_mcp.cache"):
        c, _ = make(tmp_path)
    assert c.persistent and "starting a new one" in caplog.text
    c.set("dataset", "a", 1, 60)
    assert c.get("dataset", "a").value == 1
    c.close()


def test_unusable_directory_falls_back_to_memory(tmp_path, caplog):
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    with caplog.at_level(logging.WARNING, logger="datagvat_mcp.cache"):
        c, _ = make(not_a_dir / "sub", limits={**LIMITS, "dataset": 500 * MB})
    assert not c.persistent and "caching in memory only" in caplog.text
    assert c.limits["dataset"] == MEMORY_LIMIT  # no 500 MB of RAM
    c.set("dataset", "a", 1, 60)
    assert c.get("dataset", "a").value == 1


def test_memory_cache_is_capped_but_disk_cache_is_not(tmp_path):
    big = {**LIMITS, "preview": 500 * MB}
    assert make(None, limits=big)[0].limits["preview"] == MEMORY_LIMIT
    disk, _ = make(tmp_path, limits=big)
    assert disk.limits["preview"] == 500 * MB
    disk.close()


def test_storage_errors_never_break_a_request(tmp_path, caplog):
    c, _ = make(tmp_path)
    c.set("dataset", "a", 1, 60)
    c._db.close()  # e.g. disk gone
    with caplog.at_level(logging.WARNING, logger="datagvat_mcp.cache"):
        assert c.get("dataset", "a") is None
        c.set("dataset", "b", 2, 60)
    assert "Cache read failed" in caplog.text and "Cache write failed" in caplog.text


def test_undecodable_entry_is_a_miss(tmp_path):
    c, clock = make(tmp_path)
    c._db.execute("INSERT INTO entries VALUES ('dataset', 'bad', ?, 3, ?, ?, ?)", (b"xyz", clock.t, clock.t + 60, clock.t))
    assert c.get("dataset", "bad") is None
    c.close()


def test_old_schema_is_discarded(tmp_path):
    db = sqlite3.connect(tmp_path / DB_FILE)
    db.execute("CREATE TABLE entries (something_else TEXT)")
    db.execute("PRAGMA user_version = 0")
    db.commit()
    db.close()
    c, _ = make(tmp_path)
    c.set("dataset", "a", 1, 60)
    assert c.persistent and c.get("dataset", "a").value == 1
    c.close()


# ------------------------------------------------------ per-call bookkeeping


def test_call_reports_nothing_when_all_was_fetched_now():
    token = start_call()
    assert end_call(token) is None
    note_served(1.0, stale=False)  # outside a call: ignored


def test_call_reports_the_oldest_copy_and_staleness():
    token = start_call()
    note_served(1_800_000_600.0, stale=False)
    note_served(1_800_000_000.0, stale=False)
    assert end_call(token) == {"saved_copy_from": "2027-01-15T08:00:00Z", "stale": False}
    token = start_call()
    note_served(1_800_000_000.0, stale=True)
    info = end_call(token)
    assert info["stale"] is True and "could not be reached" in info["warning"]


@pytest.mark.parametrize("category", ["search", "dataset", "catalogue", "vocabulary", "preview"])
def test_every_category_has_a_limit(category):
    assert make()[0].limits[category] == MB
