"""Database session factories."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from .config import DatabaseConfig


def create_session_factory(db_cfg: DatabaseConfig) -> sessionmaker:
    """Create a SQLAlchemy session factory."""
    engine = create_engine(db_cfg.sync_url, pool_pre_ping=True, pool_size=10, max_overflow=20)
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_tables(db_cfg: DatabaseConfig, base):
    """Create all tables for the given Base."""
    engine = create_engine(db_cfg.sync_url, pool_pre_ping=True)
    base.metadata.create_all(engine)
