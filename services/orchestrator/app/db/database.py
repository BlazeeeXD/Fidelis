"""Database configuration for the Orchestrator service."""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://user:pass@localhost:5432/pr_reviewer",
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""



engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


def get_db():
    """Provide a database session for application code."""

    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()