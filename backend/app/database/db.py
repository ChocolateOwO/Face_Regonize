from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine

from app.config import DATABASE_PATH

DATABASE_URL = f"sqlite:///{DATABASE_PATH}"
engine = create_engine(DATABASE_URL, echo=False, connect_args={"check_same_thread": False})


def init_db() -> None:
    from app.models import models  # noqa: F401  registers tables on SQLModel.metadata

    SQLModel.metadata.create_all(engine)

    # THE single place migrations are ever executed. create_all() above adds
    # missing tables but never adds a column to a table that already exists,
    # so released schema changes arrive through here. Deliberately allowed to
    # raise: a failed migration must stop startup so an in-flight update sees
    # an unhealthy backend and rolls back, rather than the app running on a
    # half-migrated database.
    from app.migrations.runner import run_pending_migrations

    run_pending_migrations()


def get_session():
    with Session(engine) as session:
        yield session
