from sqlalchemy import text
from database import SessionLocal

db = SessionLocal()
try:
    before = db.execute(text("SELECT COUNT(*) FROM emails")).scalar()
    db.execute(text("DELETE FROM emails"))
    db.commit()
    after = db.execute(text("SELECT COUNT(*) FROM emails")).scalar()
    print(f"emails: {before} -> {after}")
finally:
    db.close()