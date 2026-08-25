"""Injectable clock. Every timing-dependent test uses FakeClock so the suite never waits."""

from motorooter.clock import Clock, FakeClock, SystemClock


def test_system_clock_satisfies_the_protocol():
    assert isinstance(SystemClock(), Clock)


def test_fake_clock_satisfies_the_protocol():
    assert isinstance(FakeClock(), Clock)


def test_fake_clock_starts_at_configured_time():
    assert FakeClock(start=100.0).now() == 100.0


def test_advance_moves_time_forward():
    clock = FakeClock()
    clock.advance(5.0)
    assert clock.now() == 5.0


async def test_sleep_advances_time_without_waiting():
    clock = FakeClock()
    await clock.sleep(30.0)
    assert clock.now() == 30.0


async def test_sleep_durations_are_recorded():
    """Backoff tests assert on the schedule, not on elapsed wall-clock."""
    clock = FakeClock()
    await clock.sleep(1.0)
    await clock.sleep(2.0)
    assert clock.slept == [1.0, 2.0]
