import base64
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional

from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


def build_gmail_client(account):
    """Rebuilds the Gmail client using the stored refresh token."""
    from main import decrypt_token, SCOPES

    with open("client_secret.json", "r") as f:
        secret_data = json.load(f)
        key_type = "web" if "web" in secret_data else "installed"
        client_id = secret_data[key_type]["client_id"]
        client_secret = secret_data[key_type]["client_secret"]

    creds = Credentials(
        token=None,
        refresh_token=decrypt_token(account.encrypted_refresh_token),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    return build("gmail", "v1", credentials=creds)


BASE_EXCLUDE = "-in:chats -in:drafts -in:spam -in:trash"
TAB_EXCLUDE = "-category:promotions -category:social"


def _list_ids(service, query: str, limit: int):
    """Follows pagination until `limit` ids are collected. Returns (ids, truncated)."""
    ids, token = [], None
    while len(ids) < limit:
        params = {"userId": "me", "q": query, "maxResults": min(100, limit - len(ids))}
        if token:
            params["pageToken"] = token
        resp = service.users().messages().list(**params).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids[:limit], bool(token) and len(ids) >= limit


def list_message_ids(service, *, after_ts: Optional[int] = None, before_ts: Optional[int] = None,
                     limit: int = 30, extra_senders: Optional[List[str]] = None):
    """
    Newest-first message ids. Promotions/Social tabs are excluded, except for senders in
    `extra_senders` (the allow-list). Returns (ids, truncated).
    """
    terms = []
    if after_ts is not None:
        terms.append(f"after:{int(after_ts)}")
    if before_ts is not None:
        terms.append(f"before:{int(before_ts)}")
    time_q = " ".join(terms)

    main_q = " ".join(t for t in (time_q, BASE_EXCLUDE, TAB_EXCLUDE) if t)
    ids, truncated = _list_ids(service, main_q, limit)

    if extra_senders:
        senders = " OR ".join(extra_senders)
        extra_q = " ".join(t for t in (
            time_q, f"from:({senders})", "(category:promotions OR category:social)", BASE_EXCLUDE) if t)
        extra_ids, _ = _list_ids(service, extra_q, limit)
        seen = set(ids)
        ids += [i for i in extra_ids if i not in seen]
    return ids, truncated


def _received_at(raw_msg: dict, date_header: Optional[str]) -> datetime:
    """Gmail's internalDate is when Gmail received the mail; the Date header can be wrong or spoofed."""
    ms = raw_msg.get("internalDate")
    if ms:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).replace(tzinfo=None)
    try:
        d = parsedate_to_datetime(date_header)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return datetime.now(timezone.utc).replace(tzinfo=None)


def get_and_parse_email(gmail_client, message_id: str) -> dict:
    """Fetches a specific email and strips the noise (HTML/CSS/Reply chains)."""
    raw_msg = gmail_client.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in raw_msg["payload"].get("headers", [])}

    subject = headers.get("subject", "(No Subject)")
    sender = headers.get("from", "Unknown")
    received_at_header = headers.get("date")
    thread_id = raw_msg.get("threadId")

    plain_chunks, html_chunks, ics_data = [], [], None

    def walk_parts(part):
        nonlocal ics_data
        mime_type = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data")

        if mime_type == "text/plain" and body_data:
            padding = 4 - (len(body_data) % 4)
            padded = body_data + ("=" * padding) if padding != 4 else body_data
            plain_chunks.append(base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore"))
        elif mime_type == "text/html" and body_data:
            padding = 4 - (len(body_data) % 4)
            padded = body_data + ("=" * padding) if padding != 4 else body_data
            html_chunks.append(base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore"))
        elif mime_type in ["text/calendar", "application/ics"] and body_data:
            padding = 4 - (len(body_data) % 4)
            padded = body_data + ("=" * padding) if padding != 4 else body_data
            ics_data = base64.urlsafe_b64decode(padded)

        for sub in part.get("parts", []):
            walk_parts(sub)

    walk_parts(raw_msg.get("payload", {}))

    raw_text = ""
    if html_chunks:
        soup = BeautifulSoup("\n".join(html_chunks), "html.parser")
        for tag in soup(["style", "script", "head", "meta", "blockquote"]):
            tag.decompose()
        for quote in soup.find_all(class_=re.compile("gmail_quote|yahoo_quoted")):
            quote.decompose()
        raw_text = soup.get_text(separator=" ", strip=True)
    elif plain_chunks:
        raw_text = "\n".join(plain_chunks)

    clean_text = re.sub(r"\s+", " ", raw_text)[:4000]

    return {
        "id": message_id,
        "thread_id": thread_id,
        "subject": subject,
        "sender": sender,
        "clean_text": clean_text,
        "received_at_raw": received_at_header,
        "received_at": _received_at(raw_msg, received_at_header),   # naive UTC
        "has_ics": bool(ics_data),
    }