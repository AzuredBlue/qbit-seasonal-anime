from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import pytest
from sqlmodel import Session, SQLModel, create_engine

from qbit_seasonal_anime.core.supervisor import Supervisor
from qbit_seasonal_anime.db.models import Monitored, MonitoredStatus, Settings, utc_now


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_continuous_numbering_never_falsely_completed(session):
    """Shows using continuous numbering (e.g. Hyakkano S3 Ep 10 released as Ep 34) must not be marked completed."""
    now = utc_now()
    show = Monitored(
        id=1,
        anilist_id=1001,
        display_name="Hyakkano Season 3",
        aliases_json='["Hyakkano"]',
        status=MonitoredStatus.FIXED,
        total_episodes=12,
        next_airing_episode=11,
        next_airing_at=now + timedelta(days=3),
        last_confirmed_episode=34,  # Continuous franchise number
        qbit_rule_name="[Seasonal] Hyakkano S3",
    )
    session.add(show)
    session.commit()

    mock_qbit = MagicMock()
    mock_anilist = MagicMock()
    settings = Settings(id=1, base_dir="/tmp")
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=settings)

    logs = supervisor.reconcile_schedule_rollover()
    session.refresh(show)

    assert show.status == MonitoredStatus.FIXED
    assert show.next_airing_episode == 11
    assert show.qbit_rule_name == "[Seasonal] Hyakkano S3"
    mock_qbit.delete_rss_rule.assert_not_called()


def test_continuous_numbering_rolls_to_next_seasonal_episode(session):
    """When an episode airs, it rolls to current_ep + 1 (+7d), never corrupted by continuous episode count."""
    now = utc_now()
    show = Monitored(
        id=2,
        anilist_id=1002,
        display_name="Re:ZERO Season 4",
        aliases_json='["Re:ZERO"]',
        status=MonitoredStatus.FIXED,
        total_episodes=19,
        next_airing_episode=8,
        next_airing_at=now - timedelta(hours=3),
        last_confirmed_episode=82,  # Continuous franchise number
        qbit_rule_name="[Seasonal] Re:ZERO",
    )
    session.add(show)
    session.commit()

    mock_qbit = MagicMock()
    mock_anilist = MagicMock()
    settings = Settings(id=1, base_dir="/tmp")
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=settings)

    logs = supervisor.reconcile_schedule_rollover()
    session.refresh(show)

    assert any("Rolled schedule to Ep 9" in log for log in logs)
    assert show.status == MonitoredStatus.FIXED
    assert show.next_airing_episode == 9  # Incremented from 8 -> 9, NOT 83!
    assert show.next_airing_at is not None
    air_at = show.next_airing_at if show.next_airing_at.tzinfo else show.next_airing_at.replace(tzinfo=timezone.utc)
    assert air_at > now


def test_finale_rolls_over_by_default_without_local_completion(session):
    """Season finale rolls over by default (+7d) without falsely marking completed locally."""
    now = utc_now()
    show = Monitored(
        id=3,
        anilist_id=1003,
        display_name="THE GHOST IN THE SHELL",
        aliases_json='["GITS"]',
        status=MonitoredStatus.FIXED,
        total_episodes=10,
        next_airing_episode=10,
        next_airing_at=now - timedelta(hours=5),
        last_confirmed_episode=10,
        qbit_rule_name="[Seasonal] THE GHOST IN THE SHELL",
    )
    session.add(show)
    session.commit()

    mock_qbit = MagicMock()
    mock_anilist = MagicMock()
    settings = Settings(id=1, base_dir="/tmp")
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=settings)

    logs = supervisor.reconcile_schedule_rollover()
    session.refresh(show)

    # Must remain FIXED with rule intact (never completed locally), but roll forward to next week
    assert show.status == MonitoredStatus.FIXED
    assert show.next_airing_episode == 11
    assert show.qbit_rule_name == "[Seasonal] THE GHOST IN THE SHELL"
    mock_qbit.delete_rss_rule.assert_not_called()
    assert show.next_airing_at is not None
    air_at = show.next_airing_at if show.next_airing_at.tzinfo else show.next_airing_at.replace(tzinfo=timezone.utc)
    assert air_at > now


def test_stale_fixed_show_overdue_advance(session):
    now = utc_now()
    show = Monitored(
        id=4,
        anilist_id=1004,
        display_name="Stale Show",
        aliases_json='["Stale Show"]',
        status=MonitoredStatus.FIXED,
        total_episodes=12,
        next_airing_episode=3,
        next_airing_at=now - timedelta(hours=30),  # > 24 hours ago
        last_confirmed_episode=2,
    )
    session.add(show)
    session.commit()

    mock_qbit = MagicMock()
    mock_anilist = MagicMock()
    settings = Settings(id=1, base_dir="/tmp")
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=settings)

    logs = supervisor.reconcile_schedule_rollover()
    session.refresh(show)

    assert any("Rolled schedule to Ep 4" in log for log in logs)
    assert show.next_airing_episode == 4
    assert show.next_airing_at is not None
    air_at = show.next_airing_at if show.next_airing_at.tzinfo else show.next_airing_at.replace(tzinfo=timezone.utc)
    assert air_at > now
