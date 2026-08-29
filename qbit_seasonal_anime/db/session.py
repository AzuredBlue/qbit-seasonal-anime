import os
from pathlib import Path
from typing import Optional, List
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select
from qbit_seasonal_anime.config import DB_PATH, CONFIG_DIR
from qbit_seasonal_anime.db.models import Settings, Feed, Monitored, RuleHistory

_engine = None


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Ensure SQLite enforces foreign key constraints."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_db_path() -> Path:
    return DB_PATH


def get_engine(db_path: Optional[Path] = None):
    global _engine
    if db_path is not None:
        target_path = db_path
        engine = create_engine(f"sqlite:///{target_path}", connect_args={"check_same_thread": False})
        return engine

    if _engine is None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Ensure config directory has safe permissions
        try:
            os.chmod(CONFIG_DIR, 0o700)
        except OSError:
            pass

        _engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})

        # Ensure sqlite DB file has 0600 permissions
        if DB_PATH.exists():
            try:
                os.chmod(DB_PATH, 0o600)
            except OSError:
                pass

    return _engine


def init_db(engine=None):
    if engine is None:
        engine = get_engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for col_name, col_type in [
            ("matched_title", "VARCHAR"),
            ("matched_release_group", "VARCHAR"),
            ("cover_image", "VARCHAR"),
            ("season_name", "VARCHAR"),
            ("season_year", "INTEGER"),
            ("title_romaji", "VARCHAR"),
            ("title_english", "VARCHAR"),
            ("status_before_pause", "VARCHAR"),
            ("custom_regex", "VARCHAR"),
            ("custom_must_not", "VARCHAR"),
        ]:
            try:
                session.exec(text(f"ALTER TABLE monitored ADD COLUMN {col_name} {col_type}"))
                session.commit()
            except Exception:
                session.rollback()

        try:
            session.exec(text("ALTER TABLE settings ADD COLUMN title_language VARCHAR DEFAULT 'english'"))
            session.commit()
        except Exception:
            session.rollback()

        # Seed default settings if empty
        stmt = select(Settings)
        settings = session.exec(stmt).first()
        if not settings:
            settings = Settings()
            session.add(settings)
            session.commit()


def get_settings(session: Session) -> Settings:
    stmt = select(Settings)
    settings = session.exec(stmt).first()
    if not settings:
        settings = Settings()
        session.add(settings)
        session.commit()
        session.refresh(settings)
    return settings
