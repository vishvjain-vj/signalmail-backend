
from sqlalchemy import inspect
from database import engine
from models import Account

# 1. Print the Account model columns
print("\n========== ACCOUNT MODEL COLUMNS ==========")

for column in Account.__table__.columns:
    print(
        f"Column: {column.name} | "
        f"Type: {column.type} | "
        f"Nullable: {column.nullable}"
    )

# 2. Print actual database table columns
print("\n========== DATABASE TABLE COLUMNS ==========")

inspector = inspect(engine)

if inspector.has_table("accounts"):
    columns = inspector.get_columns("accounts")

    for column in columns:
        print(
            f"Column: {column['name']} | "
            f"Type: {column['type']} | "
            f"Nullable: {column['nullable']}"
        )
else:
    print("accounts table does not exist!")

# 3. Identify missing columns
model_columns = {
    column.name for column in Account.__table__.columns
}

database_columns = {
    column["name"] for column in inspector.get_columns("accounts")
}

missing_columns = model_columns - database_columns

print("\n========== MISSING COLUMNS ==========")

if missing_columns:
    for column in sorted(missing_columns):
        print(column)
else:
    print("No missing columns found!")
