# app/db.py

from sqlmodel import SQLModel,Field, create_engine, Session

# SQLite database (file-based)
DATABASE_URL = "sqlite:///./barber.db"

# Engine = connection to the database
engine = create_engine(
    DATABASE_URL,
    echo=False,          # set to True later if you want to see SQL
    connect_args={"check_same_thread": False},  # required for SQLite + FastAPI
)

# Dependency: one session per request
def get_session():
    with Session(engine) as session:
        yield session
