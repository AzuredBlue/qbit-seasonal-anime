import os
from pathlib import Path
from typing import Optional
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select
from qbit_seasonal_anime.config import DB_PATH, CONFIG_DIR
from qbit_seasonal_anime.db.models import Settings, Feed, Monitored, RuleHistory, MatchHistory

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
        try:
            os.chmod(CONFIG_DIR, 0o700)
        except OSError:
            pass

        _engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})

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
    def get_table_columns(session: Session, table: str) -> set:
        return {r[1] for r in session.exec(text(f"PRAGMA table_info({table})")).all()}

    with Session(engine) as session:
        monitored_cols = get_table_columns(session, "monitored")
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
            ("pinned_feed_id", "INTEGER"),
        ]:
            if col_name not in monitored_cols:
                session.exec(text(f"ALTER TABLE monitored ADD COLUMN {col_name} {col_type}"))
                session.commit()

        settings_cols = get_table_columns(session, "settings")
        if "title_language" not in settings_cols:
            session.exec(text("ALTER TABLE settings ADD COLUMN title_language VARCHAR DEFAULT 'english'"))
            session.commit()
        if "download_mode" not in settings_cols:
            session.exec(text("ALTER TABLE settings ADD COLUMN download_mode VARCHAR DEFAULT 'rules'"))
            session.commit()

        history_cols = get_table_columns(session, "match_history")
        if "matched_regex" not in history_cols:
            session.exec(text("ALTER TABLE match_history ADD COLUMN matched_regex VARCHAR"))
            session.commit()

        episode_cols = get_table_columns(session, "episodes")
        for col_name, col_type, default in [
            ("status", "VARCHAR", "'wanted'"),
            ("version", "INTEGER", "1"),
            ("release_title", "VARCHAR", None),
            ("release_group", "VARCHAR", None),
            ("feed_id", "INTEGER", None),
            ("feed_item_id", "VARCHAR", None),
            ("torrent_url", "VARCHAR", None),
            ("torrent_hash", "VARCHAR", None),
            ("operation_tag", "VARCHAR", None),
            ("downloaded_at", "TIMESTAMP", None),
            ("last_error", "VARCHAR", None),
            ("retry_after", "TIMESTAMP", None),
        ]:
            if col_name not in episode_cols:
                suffix = f" DEFAULT {default}" if default else ""
                session.exec(text(f"ALTER TABLE episodes ADD COLUMN {col_name} {col_type}{suffix}"))
                session.commit()

        seen_cols = get_table_columns(session, "seen_feed_items")
        for col_name, col_type in [
            ("title", "VARCHAR DEFAULT ''"),
            ("created_at", "TIMESTAMP"),
            ("shielded_at", "TIMESTAMP"),
        ]:
            if col_name not in seen_cols:
                session.exec(text(f"ALTER TABLE seen_feed_items ADD COLUMN {col_name} {col_type}"))
                session.commit()

        session.exec(text("DELETE FROM episodes WHERE id NOT IN (SELECT MIN(id) FROM episodes GROUP BY monitored_id, episode_number)"))
        session.exec(text("DELETE FROM seen_feed_items WHERE id NOT IN (SELECT MIN(id) FROM seen_feed_items GROUP BY feed_url, item_id)"))
        session.exec(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_episode_monitored_number ON episodes (monitored_id, episode_number)"))
        session.exec(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_seen_feed_item ON seen_feed_items (feed_url, item_id)"))

        for index_name, table_name, columns in [
            ("ix_monitored_current_feed_id", "monitored", "current_feed_id"),
            ("ix_monitored_pinned_feed_id", "monitored", "pinned_feed_id"),
            ("ix_monitored_status_current_feed", "monitored", "status, current_feed_id"),
            ("ix_episode_status_version", "episodes", "status, version"),
            ("ix_seen_feed_item_shielded", "seen_feed_items", "shielded_at"),
            ("ix_torrent_operation_status_updated", "torrent_operations", "status, updated_at"),
            ("ix_grab_decision_episode_created", "grab_decisions", "episode, created_at"),
            ("ix_grab_decision_item", "grab_decisions", "feed_url, feed_item_id"),
            ("ix_rule_history_feed_id", "rule_history", "feed_id"),
            ("ix_rule_history_created_at", "rule_history", "created_at"),
            ("ix_rule_history_monitored_created", "rule_history", "monitored_id, created_at DESC"),
            ("ix_rule_history_monitored_outcome_feed", "rule_history", "monitored_id, outcome, feed_id"),
        ]:
            session.exec(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} ({columns})"))
        session.commit()

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
