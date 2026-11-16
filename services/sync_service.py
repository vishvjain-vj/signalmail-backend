from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from sqlalchemy import and_, asc, desc, func, or_
from sqlalchemy.orm import Session

import config
from database import SessionLocal
from models import Account, Email
from services.ai_service import CHUNK_SIZE, EmailTriageItem, categorize_email_batch
from services.feedback import dismissed_ids, log_event
from services.gmail_service import build_gmail_client, get_and_parse_email, list_message_ids
from services.ranking import parse_deadline, utcnow

BATCH_SIZE = CHUNK_SIZE          # one Gmail batch = one LLM call
COLD_START_COUNT = 30            # first sync of an account
OLDER_PAGE = 15                  # mails per "Load older" click
MAX_NEW_PER_SYNC = 150           # safety cap on mails listed per sync
RETRY_PER_SYNC = 12              # provisional rows re-sent to the AI per sync
MAX_AI_RETRIES = 3
CHECKPOINT_OVERLAP_S = 180       # re-list a little before the checkpoint; known mails are skipped for free

# Sliding window policy
ROUTINE_RETENTION_DAYS = 3
ROUTINE_MAX_COUNT = 15
IMPORTANT_RETENTION_DAYS = 14
ACCOUNT_HARD_CAP = 30

_active: set = set()
_active_lock = threading.Lock()

_RULES = [(cat, re.compile(rx)) for cat, rx in [
    ("PROMOTIONS",          r"discount|cashback|coupon|% off|unsubscribe|promo|special offer|deal of|sale ends"),
    ("SCHOLARSHIPS",        r"scholarship|fellowship|stipend|\bnsp\b|\bongc\b"),
    ("PLACEMENTS_PROJECTS", r"placement|internship|\bp&t\b|recruit|hiring|live project"),
    ("CLASSES_EXAMS",       r"\b(class|classes|exam|exams|assignment|professor|lecture|labs?)\b"),
    ("FINANCE_MARKETS",     r"\b(broking|nse|bse|dividend|mtf|portfolio|etfs?)\b|funds/securities"),
    ("COMPETITIVE_CODING",  r"codeforces|leetcode|hackerrank|\bcontests?\b"),
    ("SECURITY_ALERTS",     r"security alert|sign-in|\botp\b|verification code|password reset"),
]]


def fallback_categorize(p: dict) -> EmailTriageItem:
    """Used only when the AI is unavailable. The result is stored as 'provisional' and retried later."""
    blob = f"{p['sender']} {p['subject']} {p['clean_text'][:300]}".lower()
    cat = next((c for c, rx in _RULES if rx.search(blob)), "UNCATEGORIZED")
    return EmailTriageItem(
        email_id=p["id"],
        category=cat,
        headline=(p["subject"] or "New Email")[:80],
        tldr_summary=p["clean_text"][:220] or p["subject"] or "",
        signal_score=2 if cat == "PROMOTIONS" else 7,
        urgency_score=5,
        is_important=(cat != "PROMOTIONS"),
        is_meeting=p["has_ics"],
        action_required=None,
        deadline_at=None,
    )


def chunk_list(data: list, size: int):
    for i in range(0, len(data), size):
        yield data[i:i + size]


def _ts(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _checkpoint(db: Session, account: Account) -> Optional[datetime]:
    if account.sync_checkpoint:
        return account.sync_checkpoint
    return db.query(func.max(Email.received_at)).filter(Email.account_id == account.id).scalar()


def _older_cursor(db: Session, account_id: str) -> Optional[datetime]:
    """'Load older' continues below the oldest unpinned mail on screen (pinned old mails must not count)."""
    return (
        db.query(func.min(Email.received_at))
        .filter(Email.account_id == account_id, Email.user_marked_important == False)  # noqa: E712
        .scalar()
    )


def _retry_ids(db: Session, account_id: str, exclude: set) -> list:
    rows = (
        db.query(Email.id)
        .filter(
            Email.account_id == account_id,
            Email.cls_status == "provisional",
            Email.cls_attempts < MAX_AI_RETRIES,
        )
        .order_by(desc(Email.received_at))
        .limit(RETRY_PER_SYNC)
        .all()
    )
    return [r[0] for r in rows if r[0] not in exclude]


def _get_account_checkpoint(db: Session, account_id: str):
    """
    Uses the newest stored email timestamp as the Gmail `after:` checkpoint.
    If the DB has 0 emails for this account, returns None so a full 30-email fetch runs.
    """
    latest_email_dt = (
        db.query(func.max(Email.received_at))
        .filter(Email.account_id == account_id)
        .scalar()
    )
    return latest_email_dt


def cleanup_account_db(
    db: Session,
    account_id: str,
    max_keep: int = ACCOUNT_HARD_CAP,
    purge_older_loaded: bool = False,
):
    """
    Cleans expired rows, optionally purges temporary 'Load Older' emails on normal sync,
    and enforces the per-account `max_keep` cap while protecting starred/pinned emails.
    """
    from sqlalchemy import or_, desc, asc

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    routine_cutoff = now - timedelta(days=ROUTINE_RETENTION_DAYS)
    important_cutoff = now - timedelta(days=IMPORTANT_RETENTION_DAYS)

    has_star_col = hasattr(Email, "user_marked_important")
    not_starred = (
        or_(Email.user_marked_important.is_(None), Email.user_marked_important == False)
        if has_star_col
        else True
    )

    # 1. If running a normal sync, clear unstarred emails that were pulled via "Load Older"
    if purge_older_loaded and hasattr(Email, "from_older"):
        db.query(Email).filter(
            Email.account_id == account_id,
            not_starred,
            Email.from_older == True,
        ).delete(synchronize_session=False)

    # 2. Purge any leftover PROMOTIONS
    db.query(Email).filter(
        Email.account_id == account_id,
        Email.category == "PROMOTIONS",
    ).delete(synchronize_session=False)

    # 3. Purge unstarred routine emails (is_important=False, signal_score<7) older than 3 days
    db.query(Email).filter(
        Email.account_id == account_id,
        not_starred,
        Email.is_important == False,
        Email.signal_score < 7,
        Email.received_at < routine_cutoff,
    ).delete(synchronize_session=False)

    # 4. Cap remaining unstarred routine emails at ROUTINE_MAX_COUNT (15)
    routine_ids = [
        r[0]
        for r in db.query(Email.id)
        .filter(
            Email.account_id == account_id,
            not_starred,
            Email.is_important == False,
            Email.signal_score < 7,
        )
        .order_by(desc(Email.received_at))
        .all()
    ]
    if len(routine_ids) > ROUTINE_MAX_COUNT:
        db.query(Email).filter(Email.id.in_(routine_ids[ROUTINE_MAX_COUNT:])).delete(
            synchronize_session=False
        )

    # 5. Purge unstarred high-signal emails older than 14 days
    db.query(Email).filter(
        Email.account_id == account_id,
        not_starred,
        Email.received_at < important_cutoff,
    ).delete(synchronize_session=False)

    # 6. Enforce overall account hard cap (30 default), evicting oldest low-signal rows first
    total_count = db.query(Email).filter(Email.account_id == account_id).count()
    if total_count > max_keep:
        excess = total_count - max_keep
        evict_ids = [
            r[0]
            for r in db.query(Email.id)
            .filter(Email.account_id == account_id, not_starred)
            .order_by(
                asc(Email.is_important),
                asc(Email.signal_score),
                asc(Email.received_at),
            )
            .limit(excess)
            .all()
        ]
        if evict_ids:
            db.query(Email).filter(Email.id.in_(evict_ids)).delete(
                synchronize_session=False
            )

    db.commit()


def _upsert(db: Session, account: Account, p: dict, item: EmailTriageItem, ai_ok: bool, from_older: bool):
    sender_raw = p["sender"]
    sender_name = sender_raw.split("<")[0].strip().strip('"') if "<" in sender_raw else sender_raw
    row = db.query(Email).filter(Email.id == p["id"]).first()
    if row is None:
        row = Email(
            id=p["id"], thread_id=p["thread_id"], account_id=account.id,
            sender_name=sender_name, sender_email=sender_raw, subject=p["subject"],
            received_at=p["received_at"], cls_attempts=0, older_loaded=from_older,
        )
        db.add(row)

    row.headline = (item.headline or p["subject"] or "New Email")[:255]
    row.tldr_summary = (item.tldr_summary or "")[:2000]
    row.category = item.category[:100]
    row.signal_score = item.signal_score
    row.urgency_score = item.urgency_score
    row.is_important = item.is_important
    row.is_meeting = item.is_meeting
    row.action_required = item.action_required[:255] if item.action_required else None
    if ai_ok:
        row.deadline_at = parse_deadline(item.deadline_at)
        row.cls_status = "done"
    else:
        row.cls_status = "provisional"
        row.cls_attempts = (row.cls_attempts or 0) + 1


def sync_recent_emails_for_account(account_id: str, load_older: bool = False):
    """Public entry point. Skips if a sync for this account is already running."""
    with _active_lock:
        if account_id in _active:
            print(f"Sync already running for {account_id}, skipping.")
            return
        _active.add(account_id)
    try:
        _sync_impl(account_id, load_older)
    finally:
        with _active_lock:
            _active.discard(account_id)


def _sync_impl(account_id: str, load_older: bool):
    db = SessionLocal()
    try:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return
        checkpoint = _checkpoint(db, account)
        senders = config.ALLOW_SENDERS

        # ---- 1. List candidate mail ids (cheap: ids only) ----
        try:
            gmail = build_gmail_client(account)
            truncated = False
            if load_older:
                cursor = _older_cursor(db, account.id)
                before = _ts(cursor) + 1 if cursor else None
                ids, _ = list_message_ids(gmail, before_ts=before, limit=OLDER_PAGE * 4, extra_senders=senders)
                print(f"Load older for {account.email_address} (before {cursor})...")
            elif checkpoint:
                ids, truncated = list_message_ids(
                    gmail, after_ts=_ts(checkpoint) - CHECKPOINT_OVERLAP_S,
                    limit=MAX_NEW_PER_SYNC, extra_senders=senders)
                print(f"Incremental sync for {account.email_address} (after {checkpoint})...")
            else:
                ids, truncated = list_message_ids(gmail, limit=COLD_START_COUNT, extra_senders=senders)
                print(f"Cold start for {account.email_address}...")
        except RefreshError:
            account.sync_error = "Google sign-in expired. Reconnect this account."
            db.commit()
            print(f"⚠️ [AUTH] Token expired or revoked for {account.email_address}. Reconnect via /api/auth/login.")
            return
        except Exception as e:
            print(f"Gmail listing failed for {account.email_address}: {e}")
            return

        if truncated:
            print(f"⚠️ More than {MAX_NEW_PER_SYNC} new mails; the oldest were skipped (use Load older).")

        # ---- 2. Decide what needs work ----
        existing = {r.id: r for r in db.query(Email).filter(Email.id.in_(ids)).all()} if ids else {}
        dismissed = dismissed_ids(db, account.id, ids)
        new_ids = [i for i in ids if i not in existing and i not in dismissed]
        if load_older:
            new_ids = new_ids[:OLDER_PAGE]
            retry_ids = []
        else:
            retry_ids = _retry_ids(db, account.id, exclude=set(new_ids))
        target_ids = new_ids + retry_ids
        print(f"{len(new_ids)} new, {len(retry_ids)} to retry, in batches of {BATCH_SIZE}.")

        existing_categories = [
            r[0] for r in db.query(Email.category)
            .filter(Email.account_id == account.id, Email.cls_status == "done",
                    Email.category.isnot(None), Email.category != "PROMOTIONS",
                    Email.category != "UNCATEGORIZED")
            .distinct().all() if r[0]
        ]

        # ---- 3. Fetch, classify, store ----
        seen_dates = [r.received_at for r in existing.values() if r.received_at]
        fetch_failed = False

        for id_batch in chunk_list(target_ids, BATCH_SIZE):
            parsed = []
            for mid in id_batch:
                try:
                    parsed.append(get_and_parse_email(gmail, mid))
                except RefreshError:
                    account.sync_error = "Google sign-in expired. Reconnect this account."
                    db.commit()
                    return
                except (HttpError, OSError) as e:      # transient: keep the checkpoint where it is
                    fetch_failed = True
                    print(f"Gmail fetch failed for {mid}: {e}")
                except Exception as e:                 # parse bug: skip this mail, don't block forever
                    print(f"Skipping unparseable mail {mid}: {e}")
            if not parsed:
                continue

            ai_map = categorize_email_batch(parsed, existing_categories)
            existing_categories = sorted(
                set(existing_categories) | {i.category for i in ai_map.values() if i.category != "PROMOTIONS"})

            for p in parsed:
                item = ai_map.get(p["id"])
                ai_ok = item is not None
                if not ai_ok:
                    item = fallback_categorize(p)
                seen_dates.append(p["received_at"])

                if ai_ok and item.category == "PROMOTIONS":
                    # Only AI-confirmed promotions are skipped. A tombstone stops them being re-sent to the AI.
                    db.query(Email).filter(Email.id == p["id"]).delete(synchronize_session=False)
                    log_event(db, account_id=account.id, email_id=p["id"], sender_raw=p["sender"],
                              category="PROMOTIONS", action="promo")
                    print(f"  ✗ [PROMOTION] {p['subject'][:55]}")
                    continue

                _upsert(db, account, p, item, ai_ok, from_older=load_older)
                tag = "" if ai_ok else " (rule-based)"
                print(f"  ✓ [{item.category}]{tag} {item.headline}")
            db.commit()

        # ---- 4. Finish: move the checkpoint only after a clean run ----
        if not load_older:
            max_seen = max(seen_dates) if seen_dates else None
            if max_seen and not fetch_failed:
                account.sync_checkpoint = max(max_seen, checkpoint) if checkpoint else max_seen
            cleanup_account_db(db, account.id, max_keep=ACCOUNT_HARD_CAP, purge_older_loaded=True)
            account.last_synced_at = utcnow()
        account.sync_error = None
        db.commit()
        print(f"Sync complete for {account.email_address}.")
    finally:
        db.close()