import asyncio

import pytest

from conftest import FakeClock
from datagvat_mcp.throttle import HostThrottles, Throttle, ThrottleTimeout


async def _acquire_times(throttle: Throttle, clock: FakeClock, n: int) -> list[float]:
    times = []
    for _ in range(n):
        async with throttle.slot():
            times.append(round(clock.t, 6))
    return times


async def test_burst_then_sustained_rate():
    clock = FakeClock()
    t = Throttle(rate=1.0, burst=3, clock=clock, sleep=clock.sleep)
    assert await _acquire_times(t, clock, 6) == [0, 0, 0, 1, 2, 3]


async def test_tokens_refill_over_time():
    clock = FakeClock()
    t = Throttle(rate=2.0, burst=2, clock=clock, sleep=clock.sleep)
    await _acquire_times(t, clock, 2)
    clock.t += 10  # idle: bucket refills but never above capacity
    assert await _acquire_times(t, clock, 3) == [10, 10, 10.5]


async def test_penalize_pauses_everything():
    clock = FakeClock()
    t = Throttle(rate=5.0, burst=5, clock=clock, sleep=clock.sleep)
    t.penalize(30)
    assert await _acquire_times(t, clock, 1) == [30]


async def test_queue_timeout_when_rate_too_slow():
    clock = FakeClock()
    t = Throttle(rate=0.001, burst=1, queue_timeout=60, clock=clock, sleep=clock.sleep)
    await _acquire_times(t, clock, 1)
    with pytest.raises(ThrottleTimeout):
        async with t.slot():
            pass


async def test_concurrency_limit_serialises_requests():
    t = Throttle(rate=1000, burst=1000, concurrency=1, queue_timeout=5)
    active = peak = 0

    async def worker():
        nonlocal active, peak
        async with t.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(5)))
    assert peak == 1


async def test_concurrency_queue_timeout():
    t = Throttle(rate=1000, burst=1000, concurrency=1, queue_timeout=0.05)
    release = asyncio.Event()

    async def holder():
        async with t.slot():
            await release.wait()

    task = asyncio.create_task(holder())
    await asyncio.sleep(0)
    with pytest.raises(ThrottleTimeout):
        async with t.slot():
            pass
    release.set()
    await task


def test_invalid_arguments():
    with pytest.raises(ValueError):
        Throttle(rate=0, burst=1)


def test_host_throttles_are_per_host_and_bounded():
    h = HostThrottles(1, 1, 5, max_hosts=2)
    a = h.for_host("A.example")
    assert h.for_host("a.example") is a
    h.for_host("b.example")
    h.for_host("c.example")
    assert h.for_host("a.example") is not a  # evicted
