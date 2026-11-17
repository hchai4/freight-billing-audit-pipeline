"""In-process quota for the public demo's LLM calls.

Counts live in memory, so they reset when the app restarts. They keep casual
overuse in check; the hard ceiling on spend is the budget limit set on the
OpenAI project the demo key belongs to.
"""

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Optional


@dataclass(frozen=True)
class UsageLimits:
    session_daily: int = 5
    ip_daily: int = 20
    global_daily: int = 150
    cooldown_seconds: float = 5.0
    max_input_chars: int = 6000


class UsageGuard:
    """Thread-safe daily counters per browser session, per IP, and overall."""

    def __init__(self, limits: UsageLimits, clock: Callable[[], float] = time.time):
        self.limits = limits
        self._clock = clock
        self._lock = threading.Lock()
        self._day = ""
        self._total = 0
        self._by_session: Dict[str, int] = {}
        self._by_ip: Dict[str, int] = {}
        self._last_call: Dict[str, float] = {}

    def _roll_day(self, now: float) -> None:
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
            self._total = 0
            self._by_session.clear()
            self._by_ip.clear()
            self._last_call.clear()

    def remaining(self, session_id: str, ip_key: Optional[str] = None) -> int:
        """AI calls this visitor can still make today."""
        with self._lock:
            self._roll_day(self._clock())
            left = [
                self.limits.session_daily - self._by_session.get(session_id, 0),
                self.limits.global_daily - self._total,
            ]
            if ip_key:
                left.append(self.limits.ip_daily - self._by_ip.get(ip_key, 0))
            return max(0, min(left))

    def try_acquire(self, session_id: str, ip_key: Optional[str] = None) -> str:
        """Reserves one AI call. Returns "" on success, else the refusal reason.

        Attempts are counted, not successes: a failed call can still cost tokens.
        """
        with self._lock:
            now = self._clock()
            self._roll_day(now)

            if self._total >= self.limits.global_daily:
                return (
                    "The demo has reached its shared daily AI budget. "
                    "Please come back tomorrow (resets at 00:00 UTC)."
                )
            if self._by_session.get(session_id, 0) >= self.limits.session_daily or (
                ip_key and self._by_ip.get(ip_key, 0) >= self.limits.ip_daily
            ):
                return (
                    "You have used all of your demo AI calls for today. "
                    "Quota resets at 00:00 UTC."
                )
            wait = self.limits.cooldown_seconds - (
                now - self._last_call.get(session_id, float("-inf"))
            )
            if wait > 0:
                return f"Please wait {int(wait) + 1}s before the next AI call."

            self._total += 1
            self._by_session[session_id] = self._by_session.get(session_id, 0) + 1
            if ip_key:
                self._by_ip[ip_key] = self._by_ip.get(ip_key, 0) + 1
            self._last_call[session_id] = now
            return ""
