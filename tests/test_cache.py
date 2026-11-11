from datagvat_mcp.cache import MISSING, TTLCache


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_get_set_and_expiry():
    clock = Clock()
    c = TTLCache(10, clock=clock)
    assert c.get("a") is MISSING
    c.set("a", 1, ttl=5)
    assert c.get("a") == 1
    clock.t = 5
    assert c.get("a") is MISSING
    assert len(c) == 0
    assert (c.hits, c.misses) == (1, 2)


def test_lru_eviction():
    c = TTLCache(2)
    c.set("a", 1, 60)
    c.set("b", 2, 60)
    c.get("a")  # a is now most recently used
    c.set("c", 3, 60)
    assert c.get("b") is MISSING
    assert c.get("a") == 1 and c.get("c") == 3


def test_zero_ttl_or_size_disables_caching():
    c = TTLCache(10)
    c.set("a", 1, ttl=0)
    assert c.get("a") is MISSING
    c0 = TTLCache(0)
    c0.set("a", 1, ttl=60)
    assert c0.get("a") is MISSING


def test_falsy_values_are_cached():
    c = TTLCache(10)
    c.set("empty", {}, 60)
    assert c.get("empty") == {}
    c.clear()
    assert c.get("empty") is MISSING
