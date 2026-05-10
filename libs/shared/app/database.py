"""Database session factories."""

import logging
import os
import time

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker, Session

from .config import DatabaseConfig

logger = logging.getLogger(__name__)


def create_session_factory(db_cfg: DatabaseConfig) -> sessionmaker:
    """Create a SQLAlchemy session factory."""
    engine = create_engine(db_cfg.sync_url, pool_pre_ping=True, pool_size=10, max_overflow=20)
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_tables(db_cfg: DatabaseConfig, base, max_attempts: int = None, backoff_seconds: float = None):
    """Create all tables for the given Base, retrying on transient connection errors.

    Cross-cluster Postgres connections (external DB via Scaleway LB) can drop
    randomly at gunicorn worker boot. Without retry, the worker exits with
    code 3, k8s marks it CrashLoopBackOff, and the rollout stalls. This
    happened 3+ times during the May 2026 deploys. The retry loop converts
    those transient failures into a slightly slower boot — still bounded.

    Defaults : 6 attempts, 5s backoff each (max ~25s before giving up).
    Tunable via env to keep the function pure.
    """
    if max_attempts is None:
        max_attempts = max(1, int(os.getenv("DB_INIT_MAX_ATTEMPTS", "6")))
    if backoff_seconds is None:
        backoff_seconds = max(0.0, float(os.getenv("DB_INIT_BACKOFF_SECONDS", "5")))

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        engine = create_engine(db_cfg.sync_url, pool_pre_ping=True)
        try:
            base.metadata.create_all(engine)
            if attempt > 1:
                logger.info("init_tables succeeded on attempt %d/%d", attempt, max_attempts)
            return
        except OperationalError as e:
            last_err = e
            engine.dispose()
            if attempt >= max_attempts:
                break
            logger.warning(
                "init_tables transient failure (attempt %d/%d) — retrying in %ss: %s",
                attempt, max_attempts, backoff_seconds, str(e)[:200],
            )
            time.sleep(backoff_seconds)
    # Exhausted retries — let the original exception propagate so the worker
    # exits and Kubernetes can decide what to do (CrashLoopBackOff after the
    # retries means a real outage, not a transient blip).
    raise last_err  # type: ignore[misc]
