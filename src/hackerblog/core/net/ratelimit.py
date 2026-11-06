"""Per-host politeness: one request in flight per host, a minimum gap, and a host cap."""

import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import anyio


@dataclass
class _HostState:
    delay_s: float
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    next_at_s: float = float("-inf")


class HostLimiter:
    """Serialise requests per host with a minimum gap and cap how many hosts run at once.

    clock is a monotonic seconds source and sleep an async sleep; both are injectable so
    tests can drive time. The gap is measured from the end of the previous request.
    """

    DELAY_S_MAX = 60.0
    HOSTS_MAX = 10_000

    def __init__(
        self,
        delay_s_min: float,
        hosts_parallel_max: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        assert 0 <= delay_s_min <= self.DELAY_S_MAX
        assert hosts_parallel_max >= 1
        self._delay_s_min = delay_s_min
        self._slots = anyio.Semaphore(hosts_parallel_max)
        self._hosts: dict[str, _HostState] = {}
        self._clock = clock
        self._sleep = sleep

    def _state(self, host: str) -> _HostState:
        assert host
        state = self._hosts.get(host)
        if state is None:
            assert len(self._hosts) < self.HOSTS_MAX, "too many hosts for one limiter"
            state = self._hosts[host] = _HostState(self._delay_s_min)
        return state

    def delay_s(self, host: str) -> float:
        """Return the current minimum gap for host."""
        delay_s = self._state(host).delay_s
        assert self._delay_s_min <= delay_s <= self.DELAY_S_MAX
        return delay_s

    def raise_delay(self, host: str, delay_s: float) -> None:
        """Raise host's gap to delay_s (capped at DELAY_S_MAX); never lowers it."""
        assert delay_s >= 0
        state = self._state(host)
        state.delay_s = max(state.delay_s, min(delay_s, self.DELAY_S_MAX))
        assert state.delay_s >= self._delay_s_min

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncGenerator[None]:
        """Hold host's single request slot, waiting out its gap and a free host slot."""
        state = self._state(host)
        async with state.lock:
            wait_s = state.next_at_s - self._clock()
            assert wait_s <= self.DELAY_S_MAX
            if wait_s > 0:
                await self._sleep(wait_s)
            async with self._slots:
                try:
                    yield
                finally:
                    state.next_at_s = self._clock() + state.delay_s
