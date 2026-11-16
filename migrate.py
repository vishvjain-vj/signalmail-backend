"""One-off, safe to re-run.  Usage (from signalmail-backend):  python migrate.py"""
from sqlalchemy import inspect, text

import models  # noqa: F401  (registers all tables)
from database import Base, engine

NEW_COLUMNS = {
    "emails": {
        "deadline_at": "DATETIME NULL",
        "cls_status": "VARCHAR(12) NOT NULL DEFAULT 'done'",
        "cls_attempts": "SMALLINT NOT NULL DEFAULT 0",
        "older_loaded": "BOOLEAN NOT NULL DEFAULT 0",
    },
    "accounts": {
        "sync_checkpoint": "DATETIME NULL",
        "sync_error": "VARCHAR(255) NULL",
    },
}


def main():
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in NEW_COLUMNS.items():
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    print(f"added {table}.{name}")
        # the old last_synced_at was really the checkpoint
        conn.execute(text(
            "UPDATE accounts SET sync_checkpoint = last_synced_at "
            "WHERE sync_checkpoint IS NULL AND last_synced_at IS NOT NULL"
        ))
        # failed/uncategorized rows get retried by the next sync
        conn.execute(text(
            "UPDATE emails SET cls_status = 'provisional' "
            "WHERE (category IS NULL OR category = 'UNCATEGORIZED') AND cls_status = 'done'"
        ))
    Base.metadata.create_all(bind=engine)   # creates feedback_events and sender_weights
    print("migration complete")


if __name__ == "__main__":
    main()