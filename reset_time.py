from database import SessionLocal
from models import Account

db = SessionLocal()

# Reset BOTH the UI label and the actual Gmail fetch checkpoint
db.query(Account).update({
    "last_synced_at": None,
    "sync_checkpoint": None
})
db.commit()
db.close()

print("✅ Both UI and hidden sync checkpoints reset to NULL! Cold Start unlocked.")