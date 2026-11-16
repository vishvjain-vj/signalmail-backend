import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import config

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

ATTENTION_SCORE = 55        # score that alone puts a mail in "Needs attention"
DEADLINE_WINDOW_H = 48      # deadlines inside this window always surface
OVERDUE_GRACE_H = 24        # keep just-missed deadlines visible for a day


def user_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(config.USER_TZ)
        except Exception:
            pass
    return timezone.utc


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_str(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    return dt.replace(tzinfo=timezone.utc).astimezone(user_tz()).strftime("%a %Y-%m-%d %H:%M")


def now_local_str() -> str:
    return datetime.now(user_tz()).strftime("%A %Y-%m-%d %H:%M")


def sender_key(sender_raw: str) -> str:
    m = re.search(r"<([^>]+)>", sender_raw or "")
    return ((m.group(1) if m else (sender_raw or "")).strip().lower())[:255]


def parse_deadline(value) -> Optional[datetime]:
    """LLM gives 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM' in the user's timezone -> naive UTC, or None."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if len(s) == 10:                       # date only -> end of that day
        d = d.replace(hour=23, minute=59)
    if d.tzinfo is None:
        d = d.replace(tzinfo=user_tz())
    d = d.astimezone(timezone.utc).replace(tzinfo=None)
    now = utcnow()
    if d < now - timedelta(days=400) or d > now + timedelta(days=400):
        return None                        # almost certainly a hallucinated date
    return d


def hours_to_deadline(e, now: datetime) -> Optional[float]:
    if not e.deadline_at:
        return None
    return (e.deadline_at - now).total_seconds() / 3600


def _deadline_boost(h: float) -> int:
    if h < -OVERDUE_GRACE_H:
        return 0
    if h < 0:
        return 15
    if h <= 6:
        return 40
    if h <= 24:
        return 32
    if h <= 48:
        return 24
    if h <= 168:
        return 10
    return 0


def priority_score(e, weight: int = 0, now: Optional[datetime] = None) -> float:
    """Computed at read time, so deadline urgency is always current."""
    now = now or utcnow()
    score = (e.urgency_score or 1) * 4 + (e.signal_score or 1) * 2
    if e.is_important:
        score += 6
    if e.is_meeting:
        score += 4
    h = hours_to_deadline(e, now)
    if h is not None:
        score += _deadline_boost(h)
    if e.user_marked_important:
        score += 50
    return float(score + weight)


def attention_reason(e, score: float, now: datetime) -> Optional[str]:
    """Short reason this mail needs attention, or None if it doesn't."""
    h = hours_to_deadline(e, now)
    if h is not None and -OVERDUE_GRACE_H <= h <= DEADLINE_WINDOW_H:
        if h < 0:
            return f"Overdue {max(1, round(-h))}h"
        if h < 1:
            return "Due within 1h"
        return f"Due in {round(h)}h"
    if (e.urgency_score or 0) >= 8:
        return "Urgent"
    if e.user_marked_important:
        return "Pinned"
    if score >= ATTENTION_SCORE:
        return "High priority"
    return None