from database import engine
from models import Email

# This will drop ONLY the emails table from your Aiven database
Email.__table__.drop(engine)
print("Emails table dropped successfully!")