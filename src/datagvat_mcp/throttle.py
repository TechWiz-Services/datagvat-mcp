"""Outbound request throttling: one request at a time, with a pause after each.

The pause starts when a request has *completed* (response fully read), not
when it was sent, so a slow response never shortens the gap. Requests queue
in FIFO order. One Throttle is shared by every MCP session in the process, so
the limit applies to the whole server, not per connected agent.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager


class ThrottleTimeout(Exception):
    """Raised when a request could not get a slot within the queue timeout."""


class Throttle:
    def __init__(
        self,
        interval: float,
        queue_timeout: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if interval < 0:
            raise ValueError("interval must be >= 0")
        self.interval = interval
        self.queue_timeout = queue_timeout
        self._clock = clock
        self._sleep = sleep
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()  # FIFO: one request in flight at a time

    def penalize(self, seconds: float) -> None:
        """Hold back all requests for at least `seconds` (e.g. after 429 + Retry-After)."""
        self._next_allowed = max(self._next_allowed, self._clock() + max(0.0, seconds))

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        deadline = self._clock() + self.queue_timeout
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=self.queue_timeout)
        except TimeoutError as e:
            raise ThrottleTimeout("too many queued requests") from e
        started = False
        try:
            while True:
                now = self._clock()
                wait = self._next_allowed - now
                if wait <= 0:
                    break
                if now + wait > deadline:
                    raise ThrottleTimeout("rate limit queue timeout")
                await self._sleep(wait)
            started = True
            yield
        finally:
            if started:  # the pause starts at completion, also when the request failed
                self._next_allowed = max(self._next_allowed, self._clock() + self.interval)
            self._lock.release()


class HostThrottles:
    """One Throttle per host (used for preview downloads).

    Hosts can be pre-registered so that, for example, a preview URL that points
    at data.gv.at itself shares the API throttle instead of getting its own.
    """

    def __init__(self, interval: float, queue_timeout: float, max_hosts: int = 256):
        self._args = (interval, queue_timeout)
        self._max_hosts = max_hosts
        self._by_host: dict[str, Throttle] = {}
        self._pinned: dict[str, Throttle] = {}

    def register(self, host: str, throttle: Throttle) -> None:
        self._pinned[host.lower()] = throttle

    def for_host(self, host: str) -> Throttle:
        host = host.lower()
        if host in self._pinned:
            return self._pinned[host]
        t = self._by_host.get(host)
        if t is None:
            if len(self._by_host) >= self._max_hosts:
                self._by_host.pop(next(iter(self._by_host)))
            t = self._by_host[host] = Throttle(*self._args)
        return t
