import json
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional
from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel


from qbit_seasonal_anime.config import (
    DEFAULT_BASE_DIR,
    DEFAULT_CATEGORY,
    DEFAULT_QBIT_HOST,
    DEFAULT_QBIT_PASSWORD,
    DEFAULT_QBIT_USERNAME,
    DEFAULT_REFRESH_INTERVAL_MINUTES,
    DEFAULT_SEED_RATIO,
    DEFAULT_STALL_WAIT_HOURS,
    DEFAULT_DOWNLOAD_MODE,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MonitoredStatus(str, Enum):
    UNCONFIRMED = "unconfirmed"
    FIXED = "fixed"
    STALLED = "stalled"
    COMPLETED = "completed"
    PAUSED = "paused"


class EpisodeStatus(str, Enum):
    WANTED = "wanted"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    REPLACING = "replacing"
    FAILED = "failed"


class TorrentOperationStatus(str, Enum):
    PREPARING = "preparing"
    ADDED = "added"
    OLD_REMOVED = "old_removed"
    COMPLETED = "completed"
    FAILED = "failed"


class GrabDecisionType(str, Enum):
    WOULD_GRAB = "would_grab"
    WOULD_REPLACE = "would_replace"
    WOULD_WAIT = "would_wait"
    SKIPPED = "skipped"


class RuleOutcome(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"
    STALLED = "stalled"
    REPLACED = "replaced"


class Settings(SQLModel, table=True):
    __tablename__ = "settings"

    id: Optional[int] = Field(default=None, primary_key=True)
    qbit_host: str = Field(default=DEFAULT_QBIT_HOST)
    qbit_username: str = Field(default=DEFAULT_QBIT_USERNAME)
    qbit_password: str = Field(default=DEFAULT_QBIT_PASSWORD)
    base_dir: str = Field(default=DEFAULT_BASE_DIR)
    default_category: str = Field(default=DEFAULT_CATEGORY)
    default_seed_ratio: float = Field(default=DEFAULT_SEED_RATIO)
    anilist_username: str = Field(default="")
    refresh_interval_minutes: int = Field(default=DEFAULT_REFRESH_INTERVAL_MINUTES)
    stall_wait_hours: int = Field(default=DEFAULT_STALL_WAIT_HOURS)
    title_language: str = Field(default="english")  # "english" or "romaji"
    download_mode: str = Field(default=DEFAULT_DOWNLOAD_MODE)


class Feed(SQLModel, table=True):
    __tablename__ = "feeds"

    id: Optional[int] = Field(default=None, primary_key=True)
    qbit_feed_name: str = Field(index=True)
    qbit_feed_url: str = Field(unique=True, index=True)
    priority: int = Field(default=1, index=True)  # Lower number = higher priority (1 is top)


class Monitored(SQLModel, table=True):
    __tablename__ = "monitored"

    id: Optional[int] = Field(default=None, primary_key=True)
    anilist_id: int = Field(unique=True, index=True)
    display_name: str = Field(index=True)
    title_romaji: Optional[str] = Field(default=None, nullable=True)
    title_english: Optional[str] = Field(default=None, nullable=True)
    aliases_json: str = Field(default="[]")
    status: MonitoredStatus = Field(default=MonitoredStatus.UNCONFIRMED, index=True)
    current_feed_id: Optional[int] = Field(default=None, foreign_key="feeds.id", ondelete="SET NULL", nullable=True, index=True)
    pinned_feed_id: Optional[int] = Field(default=None, foreign_key="feeds.id", ondelete="SET NULL", nullable=True, index=True)
    qbit_rule_name: Optional[str] = Field(default=None, nullable=True)
    total_episodes: Optional[int] = Field(default=None, nullable=True)
    next_airing_episode: Optional[int] = Field(default=None, nullable=True)
    next_airing_at: Optional[datetime] = Field(default=None, nullable=True)
    last_confirmed_episode: Optional[int] = Field(default=None, nullable=True)
    save_folder: str = Field(default="")
    matched_title: Optional[str] = Field(default=None, nullable=True)
    matched_release_group: Optional[str] = Field(default=None, nullable=True)
    cover_image: Optional[str] = Field(default=None, nullable=True)
    season_name: Optional[str] = Field(default=None, nullable=True)
    season_year: Optional[int] = Field(default=None, nullable=True)
    status_before_pause: Optional[str] = Field(default=None, nullable=True)
    custom_regex: Optional[str] = Field(default=None, nullable=True)
    custom_must_not: Optional[str] = Field(default=None, nullable=True)

    @property
    def aliases(self) -> List[str]:
        try:
            data = json.loads(self.aliases_json)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    @aliases.setter
    def aliases(self, val: List[str]) -> None:
        self.aliases_json = json.dumps(list(dict.fromkeys(val)))  # unique while preserving order


class RuleHistory(SQLModel, table=True):
    __tablename__ = "rule_history"

    id: Optional[int] = Field(default=None, primary_key=True)
    monitored_id: int = Field(foreign_key="monitored.id", ondelete="CASCADE", index=True)
    feed_id: Optional[int] = Field(default=None, foreign_key="feeds.id", ondelete="SET NULL", nullable=True, index=True)
    created_at: datetime = Field(default_factory=utc_now, index=True)
    outcome: RuleOutcome = Field(default=RuleOutcome.PENDING, index=True)
    note: Optional[str] = Field(default=None, nullable=True)


class MatchHistory(SQLModel, table=True):
    __tablename__ = "match_history"

    id: Optional[int] = Field(default=None, primary_key=True)
    monitored_id: Optional[int] = Field(default=None, foreign_key="monitored.id", ondelete="SET NULL", nullable=True, index=True)
    show_name: str = Field(index=True)
    rule_name: str
    feed_name: Optional[str] = None
    release_title: str = Field(index=True)
    episode: Optional[int] = None
    created_at: datetime = Field(default_factory=utc_now, index=True)
    matched_regex: Optional[str] = Field(default=None, nullable=True)


class Episode(SQLModel, table=True):
    __tablename__ = "episodes"
    __table_args__ = (
        UniqueConstraint("monitored_id", "episode_number", name="uq_episode_monitored_number"),
        Index("ix_episode_status_version", "status", "version"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    monitored_id: int = Field(foreign_key="monitored.id", ondelete="CASCADE", index=True)
    episode_number: int = Field(index=True)
    status: EpisodeStatus = Field(default=EpisodeStatus.WANTED, index=True)
    version: int = Field(default=1)
    release_title: Optional[str] = Field(default=None, nullable=True)
    release_group: Optional[str] = Field(default=None, nullable=True)
    feed_id: Optional[int] = Field(default=None, foreign_key="feeds.id", ondelete="SET NULL", nullable=True, index=True)
    feed_item_id: Optional[str] = Field(default=None, nullable=True)
    torrent_url: Optional[str] = Field(default=None, nullable=True)
    torrent_hash: Optional[str] = Field(default=None, nullable=True, index=True)
    operation_tag: Optional[str] = Field(default=None, nullable=True, index=True)
    downloaded_at: Optional[datetime] = Field(default=None, nullable=True)
    last_error: Optional[str] = Field(default=None, nullable=True)
    retry_after: Optional[datetime] = Field(default=None, nullable=True, index=True)


class TorrentOperation(SQLModel, table=True):
    __tablename__ = "torrent_operations"
    __table_args__ = (
        Index("ix_torrent_operation_status_updated", "status", "updated_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    episode_id: int = Field(foreign_key="episodes.id", ondelete="CASCADE", index=True)
    kind: str = Field(index=True)
    status: TorrentOperationStatus = Field(default=TorrentOperationStatus.PREPARING, index=True)
    operation_tag: str = Field(unique=True, index=True)
    release_title: str
    release_group: Optional[str] = Field(default=None, nullable=True)
    version: int = Field(default=1)
    new_torrent_url: str
    new_torrent_hash: Optional[str] = Field(default=None, nullable=True, index=True)
    old_torrent_hash: Optional[str] = Field(default=None, nullable=True, index=True)
    last_error: Optional[str] = Field(default=None, nullable=True)
    created_at: datetime = Field(default_factory=utc_now, index=True)
    updated_at: datetime = Field(default_factory=utc_now, index=True)


class SeenFeedItem(SQLModel, table=True):
    __tablename__ = "seen_feed_items"
    __table_args__ = (
        UniqueConstraint("feed_url", "item_id", name="uq_seen_feed_item"),
        Index("ix_seen_feed_item_created", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    feed_url: str = Field(index=True)
    item_id: str = Field(index=True)
    title: str = Field(default="")
    created_at: datetime = Field(default_factory=utc_now, index=True)


class GrabDecision(SQLModel, table=True):
    __tablename__ = "grab_decisions"
    __table_args__ = (
        Index("ix_grab_decision_episode_created", "episode", "created_at"),
        Index("ix_grab_decision_item", "feed_url", "feed_item_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    monitored_id: int = Field(foreign_key="monitored.id", ondelete="CASCADE", index=True)
    episode_id: Optional[int] = Field(default=None, foreign_key="episodes.id", ondelete="SET NULL", nullable=True, index=True)
    feed_url: str = Field(index=True)
    feed_item_id: str = Field(index=True)
    release_title: str
    episode: int
    version: int = Field(default=1)
    decision: GrabDecisionType
    reason: str = Field(default="")
    created_at: datetime = Field(default_factory=utc_now, index=True)

