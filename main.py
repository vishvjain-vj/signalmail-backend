import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

import config
from database import Base, SessionLocal, engine, get_db
from models import Account, Email, SenderWeight
from services.feedback import log_event
from services.ranking import attention_reason, priority_score, sender_key, utcnow
from services.sync_service import sync_recent_emails_for_account, log_event

load_dotenv()

# Creates missing tables. New columns on existing tables come from `python migrate.py`.
Base.metadata.create_all(bind=engine)

app = FastAPI(title="SignalMail Triage Engine")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://signalmail.vercel.app",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FERNET_KEY = os.getenv("FERNET_KEY")
cipher = Fernet(FERNET_KEY.encode())


def encrypt_token(token: str) -> str:
    return cipher.encrypt(token.encode()).decode()


def decrypt_token(encrypted_token: str) -> str:
    return cipher.decrypt(encrypted_token.encode()).decode()


SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
]

oauth_store = {}


@app.get("/")
def read_root():
    return {"status": "SignalMail Backend Running"}


# ---------------------------------------------------------------- auth
@app.get("/api/auth/login")
def login():
    flow = Flow.from_client_secrets_file(
        "client_secret.json", scopes=SCOPES, redirect_uri=os.getenv("OAUTH_REDIRECT_URI")
    )
    auth_url, state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )
    oauth_store[state] = getattr(flow, "code_verifier", None)
    return RedirectResponse(auth_url)

@app.get("/api/emails/{account_id}")
def get_inbox(account_id: str, keep: int = 30, db: Session = Depends(get_db)):
    """
    Read-only inbox fetch matching _sync_impl.
    Promotions and dismissed emails are already removed via _sync_impl and log_event.
    """
    keep = max(1, min(keep, 200))
    query = db.query(Email).filter(Email.account_id == account_id)

    # Sort pinned emails first if user_marked_important exists on Email model, then newest first
    if hasattr(Email, "user_marked_important"):
        query = query.order_by(desc(Email.user_marked_important), desc(Email.received_at))
    else:
        query = query.order_by(desc(Email.received_at))

    return query.limit(keep).all()

@app.get("/api/auth/callback")
def oauth_callback(state: str, code: str, db: Session = Depends(get_db)):
    flow = Flow.from_client_secrets_file(
        "client_secret.json", scopes=SCOPES, redirect_uri=os.getenv("OAUTH_REDIRECT_URI")
    )
    code_verifier = oauth_store.get(state)
    if code_verifier:
        flow.code_verifier = code_verifier

    flow.fetch_token(code=code)
    creds = flow.credentials

    oauth2_service = build("oauth2", "v2", credentials=creds)
    user_info = oauth2_service.userinfo().get().execute()
    email_address = user_info.get("email")
    display_name = user_info.get("name", email_address.split("@")[0])
    avatar_url = user_info.get("picture", "")

    gmail_service = build("gmail", "v1", credentials=creds)
    watch_response = gmail_service.users().watch(
        userId="me", body={"topicName": os.getenv("PUBSUB_TOPIC_NAME")}
    ).execute()

    history_id = int(watch_response.get("historyId"))
    watch_expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    encrypted_refresh_token = encrypt_token(creds.refresh_token)

    existing_account = db.query(Account).filter(Account.email_address == email_address).first()
    if existing_account:
        existing_account.encrypted_refresh_token = encrypted_refresh_token
        existing_account.last_history_id = history_id
        existing_account.watch_expires_at = watch_expires_at
        existing_account.avatar_url = avatar_url
        existing_account.sync_error = None
    else:
        db.add(Account(
            id=str(uuid.uuid4()),
            email_address=email_address,
            display_name=display_name,
            avatar_url=avatar_url,
            encrypted_refresh_token=encrypted_refresh_token,
            last_history_id=history_id,
            watch_expires_at=watch_expires_at,
        ))

    db.commit()
    oauth_store.pop(state, None)
    return RedirectResponse(config.FRONTEND_URL)


# ---------------------------------------------------------------- accounts and sync
@app.get("/api/accounts")
def list_accounts(db: Session = Depends(get_db)):
    """Returns linked accounts along with last_synced_at and sync_error from _sync_impl."""
    accounts = db.query(Account).all()
    return [
        {
            "id": acc.id,
            "email": acc.email_address,
            "last_synced_at": (
                acc.last_synced_at.isoformat()
                if getattr(acc, "last_synced_at", None)
                else None
            ),
            "sync_error": getattr(acc, "sync_error", None),
        }
        for acc in accounts
    ]


@app.post("/api/sync/{account_id}")
def manual_sync(account_id: str, background_tasks: BackgroundTasks,
                load_older: bool = False, db: Session = Depends(get_db)):
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        return {"error": "Account not found"}
    background_tasks.add_task(sync_recent_emails_for_account, account.id, load_older)
    return {"message": f"Sync started for {account.email_address}", "load_older": load_older}


@app.post("/api/sync-all")
def sync_all_accounts(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    accounts = db.query(Account).all()
    for acc in accounts:
        background_tasks.add_task(sync_recent_emails_for_account, acc.id, False)
    return {"message": f"Sync scheduled for {len(accounts)} accounts"}


# ---------------------------------------------------------------- reading mail (no side effects)
_NOT_PROMO = or_(Email.category.is_(None), Email.category != "PROMOTIONS")


def _row_dict(e: Email) -> dict:
    return {c.name: getattr(e, c.name) for c in Email.__table__.columns}


@app.get("/api/attention")
def needs_attention(limit: int = 40, db: Session = Depends(get_db)):
    """Across all accounts: pinned, due soon, urgent, or high priority, best first."""
    limit = max(1, min(limit, 100))
    now = utcnow()
    rows = db.query(Email).filter(_NOT_PROMO).order_by(desc(Email.received_at)).limit(1000).all()
    keys = list({sender_key(r.sender_email) for r in rows})
    weights = (
        {w.sender_key: w.weight for w in db.query(SenderWeight).filter(SenderWeight.sender_key.in_(keys)).all()}
        if keys else {}
    )

    out = []
    for r in rows:
        score = priority_score(r, weights.get(sender_key(r.sender_email), 0), now)
        reason = attention_reason(r, score, now)
        if reason:
            d = _row_dict(r)
            d["priority_score"] = round(score, 1)
            d["attention_reason"] = reason
            out.append(d)
    out.sort(key=lambda d: (d["priority_score"], d["received_at"]), reverse=True)
    return out[:limit]


# ---------------------------------------------------------------- user actions
@app.patch("/api/emails/{email_id}/star")
def toggle_star_email(email_id: str, db: Session = Depends(get_db)):
    """Pins/unpins an email."""
    email = db.query(Email).filter(Email.id == email_id).first()
    if not email:
        return {"error": "Email not found"}
    if hasattr(email, "user_marked_important"):
        email.user_marked_important = not bool(email.user_marked_important)
        db.commit()
        return {"id": email.id, "user_marked_important": email.user_marked_important}
    return {"id": email.id, "user_marked_important": False}


@app.delete("/api/emails/{email_id}")
def dismiss_email(email_id: str, db: Session = Depends(get_db)):
    """
    Records a tombstone via log_event so dismissed_ids() in _sync_impl
    never re-fetches this email, then deletes the row from MySQL.
    """
    email = db.query(Email).filter(Email.id == email_id).first()
    if not email:
        return {"status": "already_removed"}

    log_event(
        db,
        account_id=email.account_id,
        email_id=email.id,
        sender_raw=email.sender_email or email.sender_name or "",
        category=email.category or "UNCATEGORIZED",
        action="dismiss",
    )
    db.delete(email)
    db.commit()
    return {"status": "deleted", "id": email_id}


# ---------------------------------------------------------------- optional sync at boot
def auto_sync_all_accounts_on_boot():
    """Opt-in (AUTO_SYNC_ON_BOOT=true): sync every linked account once at startup."""
    def run_initial_sync():
        db = SessionLocal()
        try:
            account_ids = [acc.id for acc in db.query(Account).all()]
        finally:
            db.close()
        for acc_id in account_ids:
            sync_recent_emails_for_account(acc_id)

    threading.Thread(target=run_initial_sync, daemon=True).start()


if config.AUTO_SYNC_ON_BOOT:
    app.add_event_handler("startup", auto_sync_all_accounts_on_boot)