from src.demo.usage_guard import UsageGuard, UsageLimits

DAY = 86400.0


class FakeClock:
    def __init__(self, now: float = 10 * DAY):
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_guard(**overrides):
    clock = FakeClock()
    limits = UsageLimits(**{"cooldown_seconds": 0.0, **overrides})
    return UsageGuard(limits, clock=clock), clock


def test_session_limit_blocks_after_quota():
    guard, _ = make_guard(session_daily=2)
    assert guard.try_acquire("s1") == ""
    assert guard.try_acquire("s1") == ""
    assert "used all" in guard.try_acquire("s1")
    assert guard.remaining("s1") == 0
    # Another visitor is unaffected.
    assert guard.try_acquire("s2") == ""


def test_ip_limit_survives_a_new_session():
    guard, _ = make_guard(session_daily=2, ip_daily=3)
    assert guard.try_acquire("s1", "ip") == ""
    assert guard.try_acquire("s1", "ip") == ""
    # A page refresh hands out a fresh session id, but the IP count carries over.
    assert guard.try_acquire("s2", "ip") == ""
    assert "used all" in guard.try_acquire("s3", "ip")
    assert guard.remaining("s3", "ip") == 0


def test_global_limit_blocks_everyone():
    guard, _ = make_guard(global_daily=2)
    assert guard.try_acquire("s1") == ""
    assert guard.try_acquire("s2") == ""
    assert "shared daily" in guard.try_acquire("s3")


def test_cooldown_between_calls():
    guard, clock = make_guard(cooldown_seconds=5.0)
    assert guard.try_acquire("s1") == ""
    clock.now += 1
    assert "wait" in guard.try_acquire("s1")
    # A refused call is not charged against the quota.
    assert guard.remaining("s1") == guard.limits.session_daily - 1
    clock.now += 5
    assert guard.try_acquire("s1") == ""


def test_counters_reset_on_new_utc_day():
    guard, clock = make_guard(session_daily=1, global_daily=1)
    assert guard.try_acquire("s1") == ""
    assert guard.try_acquire("s1") != ""
    clock.now += DAY
    assert guard.remaining("s1") == 1
    assert guard.try_acquire("s1") == ""
