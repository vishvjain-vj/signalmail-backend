import threading
import time
from collections import deque
import os
from datetime import datetime, timezone

class DailyBudgetGuard:
    """Tracks cumulative daily tokens and halts LLM calls once the cap is hit."""

    def __init__(self, max_daily_tokens: int = 250_000):
        self.max_daily_tokens = max_daily_tokens
        self.current_day = datetime.now(timezone.utc).date()
        self.tokens_used_today = 0
        self._lock = threading.Lock()

    def record_usage(self, tokens: int):
        with self._lock:
            today = datetime.now(timezone.utc).date()
            if today != self.current_day:
                self.current_day = today
                self.tokens_used_today = 0
            self.tokens_used_today += tokens

    def is_exhausted(self) -> bool:
        with self._lock:
            today = datetime.now(timezone.utc).date()
            if today != self.current_day:
                self.current_day = today
                self.tokens_used_today = 0
            return self.tokens_used_today >= self.max_daily_tokens

class TokenLimiter:
    """Sliding 60s window over tokens and requests. One instance per provider."""

    def __init__(self, tpm: int, rpm: int, safety: float = 0.85):
        self.tpm = int(tpm * safety)
        self.rpm = max(1, int(rpm * safety))
        self._events = deque()          # [timestamp, tokens]
        self._lock = threading.Lock()

    def _trim(self, now: float):
        while self._events and now - self._events[0][0] >= 60:
            self._events.popleft()

    def try_acquire(self, est_tokens: int):
        """(ticket, 0) if the call may go now, else (None, seconds_to_wait)."""
        with self._lock:
            now = time.time()
            self._trim(now)
            used = sum(t for _, t in self._events)
            if not self._events or (used + est_tokens <= self.tpm and len(self._events) < self.rpm):
                ticket = [now, est_tokens]
                self._events.append(ticket)
                return ticket, 0.0
            return None, max(self._events[0][0] + 60 - now, 0.5)

    def reconcile(self, ticket, actual_tokens: int):
        with self._lock:
            ticket[1] = actual_tokens   # replace the estimate with real usage

    def penalize(self, seconds: float):
        with self._lock:                # block this provider after a 429
            self._events.append([time.time() + seconds - 60, self.tpm])