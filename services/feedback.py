from typing import Iterable, Set

from sqlalchemy.orm import Session

from models import FeedbackEvent, SenderWeight
from services.ranking import sender_key

WEIGHT_DELTA = {"star": 3, "unstar": -3, "not_relevant": -5}   # "done" is neutral on purpose
WEIGHT_LIMIT = 15
DISMISS_ACTIONS = ("done", "not_relevant", "promo")


def log_event(db: Session, *, account_id: str, email_id: str, sender_raw: str,
              category, action: str) -> None:
    """Record an action and nudge the sender's weight. Caller commits."""
    key = sender_key(sender_raw)
    db.add(FeedbackEvent(account_id=account_id, email_id=email_id, action=action,
                         sender_key=key, category=category))
    delta = WEIGHT_DELTA.get(action, 0)
    if delta and key:
        row = db.query(SenderWeight).filter(SenderWeight.sender_key == key).first()
        if row is None:
            row = SenderWeight(sender_key=key, weight=0)
            db.add(row)
        row.weight = max(-WEIGHT_LIMIT, min(WEIGHT_LIMIT, (row.weight or 0) + delta))


def dismissed_ids(db: Session, account_id: str, ids: Iterable[str]) -> Set[str]:
    """Mails the user dismissed (or that were auto-skipped as promotions): never re-fetch these."""
    ids = list(ids)
    if not ids:
        return set()
    rows = (
        db.query(FeedbackEvent.email_id)
        .filter(
            FeedbackEvent.account_id == account_id,
            FeedbackEvent.action.in_(DISMISS_ACTIONS),
            FeedbackEvent.email_id.in_(ids),
        )
        .all()
    )
    return {r[0] for r in rows}