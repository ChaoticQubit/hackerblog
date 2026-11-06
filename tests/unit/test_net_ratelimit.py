import anyio
import pytest

from hackerblog.core.net.ratelimit import HostLimiter


class FakeTime:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now_s = 1000.0
        self.slept_s: list[float] = []

    def clock(self) -> float:
        return self.now_s

    async def sleep(self, seconds: float) -> None:
        self.slept_s.append(seconds)
        self.now_s += seconds


@pytest.mark.anyio
async def test_second_request_to_host_waits_min_delay() -> None:
    time = FakeTime()
    limiter = HostLimiter(2.0, 4, clock=time.clock, sleep=time.sleep)
    async with limiter.slot("a.example"):
        time.now_s += 0.5
    async with limiter.slot("a.example"):
        pass
    assert time.slept_s == [2.0]


@pytest.mark.anyio
async def test_other_host_does_not_wait() -> None:
    time = FakeTime()
    limiter = HostLimiter(2.0, 4, clock=time.clock, sleep=time.sleep)
    async with limiter.slot("a.example"):
        pass
    async with limiter.slot("b.example"):
        pass
    assert time.slept_s == []


@pytest.mark.anyio
async def test_raise_delay_only_raises() -> None:
    time = FakeTime()
    limiter = HostLimiter(2.0, 4, clock=time.clock, sleep=time.sleep)
    limiter.raise_delay("a.example", 5.0)
    limiter.raise_delay("a.example", 1.0)
    assert limiter.delay_s("a.example") == 5.0
    limiter.raise_delay("a.example", 10_000.0)
    assert limiter.delay_s("a.example") == HostLimiter.DELAY_S_MAX
    async with limiter.slot("a.example"):
        pass
    async with limiter.slot("a.example"):
        pass
    assert time.slept_s == [HostLimiter.DELAY_S_MAX]


@pytest.mark.anyio
async def test_hosts_max_bounds_parallel_hosts() -> None:
    limiter = HostLimiter(0.5, 2)
    active: list[str] = []
    peak = 0

    async def hit(host: str) -> None:
        nonlocal peak
        async with limiter.slot(host):
            active.append(host)
            peak = max(peak, len(active))
            await anyio.sleep(0.02)
            active.remove(host)

    async with anyio.create_task_group() as group:
        for host in ("a", "b", "c", "d"):
            group.start_soon(hit, host)
    assert peak == 2
