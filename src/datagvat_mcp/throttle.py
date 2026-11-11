"""Outbound request throttling: token bucket + concurrency cap.

One Throttle instance is shared by every MCP session in the process, so the
limit applies to the whole server, not per connected agent.
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
        rate: float,
        burst: int,
        concurrency: int = 1,
        queue_timeout: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if rate <= 0 or burst < 1 or concurrency < 1:
            raise ValueError("rate must be > 0, burst and concurrency >= 1")
        self.rate = rate
        self.capacity = float(burst)
        self.queue_timeout = queue_timeout
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._blocked_until = 0.0
        self._sem = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()

    def penalize(self, seconds: float) -> None:
        """Pause all requests for `seconds` (e.g. after a 429 with Retry-After)."""
        self._blocked_until = max(self._blocked_until, self._clock() + max(0.0, seconds))

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        deadline = self._clock() + self.queue_timeout
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.queue_timeout)
        except TimeoutError as e:
            raise ThrottleTimeout("too many queued requests") from e
        try:
            await self._take_token(deadline)
            yield
        finally:
            self._sem.release()

    async def _take_token(self, deadline: float) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                if now < self._blocked_until:
                    wait = self._blocked_until - now
                else:
                    self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                    self._updated = now
                    if self._tokens >= 1:
                        self._tokens -= 1
                        return
                    wait = (1 - self._tokens) / self.rate
                if now + wait > deadline:
                    raise ThrottleTimeout("rate limit queue timeout")
                await self._sleep(wait)


class HostThrottles:
    """Lazily created Throttle per host (used for preview downloads)."""

    def __init__(self, rate: float, burst: int, queue_timeout: float, max_hosts: int = 256):
        self._args = (rate, burst, 1, queue_timeout)
        self._max_hosts = max_hosts
        self._by_host: dict[str, Throttle] = {}

    def for_host(self, host: str) -> Throttle:
        host = host.lower()
        t = self._by_host.get(host)
        if t is None:
            if len(self._by_host) >= self._max_hosts:
                self._by_host.pop(next(iter(self._by_host)))
            t = self._by_host[host] = Throttle(*self._args)
        return t
