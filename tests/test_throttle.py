import asyncio

import pytest

from conftest import FakeClock
from datagvat_mcp.throttle import HostThrottles, Throttle, ThrottleTimeout


def make(interval: float = 2.0, queue_timeout: float = 60.0) -> tuple[Throttle, FakeClock]:
    clock = FakeClock()
    return Throttle(interval, queue_timeout, clock=clock, sleep=clock.sleep), clock


async def _start_times(throttle: Throttle, clock: FakeClock, n: int, duration: float = 0.0) -> list[float]:
    """Run n requests one after another; each takes `duration` on the fake clock."""
    times = []
    for _ in range(n):
        async with throttle.slot():
            times.append(round(clock.t, 6))
            clock.t += duration
    return times


async def test_first_request_is_immediate_then_one_pause_between_requests():
    t, clock = make(interval=2)
    assert await _start_times(t, clock, 4) == [0, 2, 4, 6]


async def test_pause_counts_from_completion_not_from_start():
    t, clock = make(interval=2)
    # Each request takes 3 s: the next one starts 2 s after the previous one *finished*.
    assert await _start_times(t, clock, 3, duration=3) == [0, 5, 10]


async def test_no_wait_after_an_idle_period():
    t, clock = make(interval=2)
    await _start_times(t, clock, 1)
    clock.t += 30
    assert await _start_times(t, clock, 2) == [30, 32]  # idle time is not saved up as a burst


async def test_zero_interval_only_serialises():
    t, clock = make(interval=0)
    assert await _start_times(t, clock, 3) == [0, 0, 0]


async def test_failed_request_is_followed_by_the_pause_too():
    t, clock = make(interval=2)
    with pytest.raises(RuntimeError):
        async with t.slot():
            raise RuntimeError("upstream broke")
    assert await _start_times(t, clock, 1) == [2]


async def test_penalize_holds_back_the_next_request():
    t, clock = make(interval=2)
    t.penalize(30)
    assert await _start_times(t, clock, 2) == [30, 32]


async def test_penalize_never_shortens_a_longer_wait():
    t, clock = make(interval=2)
    t.penalize(30)
    t.penalize(5)
    t.penalize(-1)
    assert await _start_times(t, clock, 1) == [30]


async def test_queue_timeout_when_the_pause_is_longer_than_the_deadline():
    t, clock = make(interval=2, queue_timeout=10)
    t.penalize(11)
    with pytest.raises(ThrottleTimeout):
        async with t.slot():
            pytest.fail("must not get a slot")
    # The request that gave up did not run, so it must not push the next slot further out.
    assert t._next_allowed == 11
    assert not t._lock.locked()


async def test_only_one_request_in_flight():
    t = Throttle(0, queue_timeout=5)
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


async def test_requests_are_served_in_arrival_order():
    t = Throttle(0, queue_timeout=5)
    order = []

    async def worker(i):
        async with t.slot():
            order.append(i)
            await asyncio.sleep(0)

    await asyncio.gather(*(worker(i) for i in range(8)))
    assert order == list(range(8))


async def test_real_clock_spacing():
    t = Throttle(0.05, queue_timeout=5)
    loop = asyncio.get_running_loop()
    finished, started = [], []

    async def worker():
        async with t.slot():
            started.append(loop.time())
            await asyncio.sleep(0.02)
            finished.append(loop.time())

    await asyncio.gather(*(worker() for _ in range(3)))
    gaps = [s - f for f, s in zip(finished, started[1:])]
    assert all(g >= 0.045 for g in gaps), gaps


async def test_queue_timeout_while_another_request_holds_the_slot():
    t = Throttle(0, queue_timeout=0.05)
    release = asyncio.Event()

    async def holder():
        async with t.slot():
            await release.wait()

    task = asyncio.create_task(holder())
    await asyncio.sleep(0)
    with pytest.raises(ThrottleTimeout):
        async with t.slot():
            pytest.fail("must not get a slot")
    release.set()
    await task
    async with t.slot():  # usable again afterwards
        pass


async def test_cancelled_waiter_releases_the_slot():
    t, clock = make(interval=2)
    await _start_times(t, clock, 1)

    async def never_sleep(_seconds):
        await asyncio.Event().wait()

    t._sleep = never_sleep
    waiter = asyncio.create_task(t.slot().__aenter__())
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not t._lock.locked()
    assert t._next_allowed == 2  # a request that never ran adds no pause


def test_invalid_arguments():
    with pytest.raises(ValueError):
        Throttle(-1)


def test_host_throttles_are_per_host_and_bounded():
    h = HostThrottles(1, 5, max_hosts=2)
    a = h.for_host("A.example")
    assert h.for_host("a.example") is a
    assert (a.interval, a.queue_timeout) == (1, 5)
    h.for_host("b.example")
    h.for_host("c.example")
    assert h.for_host("a.example") is not a  # evicted


def test_registered_host_shares_a_throttle_and_is_never_evicted():
    api = Throttle(2)
    h = HostThrottles(1, 5, max_hosts=1)
    h.register("WWW.Data.gv.at", api)
    assert h.for_host("www.data.gv.at") is api
    h.for_host("b.example")
    h.for_host("c.example")
    assert h.for_host("www.DATA.gv.at") is api
    assert h.for_host("files.example") is not api
