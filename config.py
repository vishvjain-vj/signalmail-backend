import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes")


def _list(name: str) -> list:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


ALLOWED_ORIGINS = _list("ALLOWED_ORIGINS") or ["http://localhost:3000"]
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
AUTO_SYNC_ON_BOOT = _bool("AUTO_SYNC_ON_BOOT", False)   # off: restarts shouldn't burn LLM quota
USER_TZ = os.getenv("USER_TZ", "Asia/Kolkata")           # used to read deadlines like "by 6 PM"
# Senders that must never be dropped, even if Gmail files them under Promotions/Social
ALLOW_SENDERS = _list("ALLOW_SENDERS")