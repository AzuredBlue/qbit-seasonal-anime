import pytest
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from qbit_seasonal_anime.core.supervisor import Supervisor
from qbit_seasonal_anime.db.models import Feed, Monitored, MonitoredStatus, RuleHistory, RuleOutcome, Settings
from tests.fixtures import MOCK_QBIT_RSS_ITEMS


@pytest.mark.asyncio
async def test_supervisor_full_cycle():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    settings = Settings(id=1, default_category="Anime", anilist_username="TestUser", base_dir="/tmp/Anime")
    session.add(settings)

    show = Monitored(
        id=1,
        anilist_id=154587,
        display_name="Sousou no Frieren",
        aliases_json='["Sousou no Frieren", "Frieren"]',
        status=MonitoredStatus.UNCONFIRMED,
    )
    session.add(show)
    session.commit()

    mock_qbit = MagicMock()
    mock_qbit.get_rss_feeds_flat.return_value = [
        {"name": "SubsPlease", "url": "https://subsplease.org/rss/?r=1080"},
        {"name": "Erai-raws", "url": "https://www.erai-raws.info/rss-1080p/"},
    ]
    mock_qbit.get_rss_items.return_value = MOCK_QBIT_RSS_ITEMS

    mock_anilist = MagicMock()
    mock_anilist.fetch_user_seasonal_anime = AsyncMock(return_value=[])

    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=settings)

    logs = await supervisor.run_full_cycle()
    full_rss_calls = [
        call for call in mock_qbit.get_rss_items.call_args_list
        if call.kwargs.get("with_data") is True
    ]
    assert len(full_rss_calls) == 1
    session.refresh(show)

    feeds = session.exec(select(Feed)).all()
    assert len(feeds) == 2

    assert show.status == MonitoredStatus.FIXED
    assert show.current_feed_id is not None
    assert show.last_confirmed_episode is None

    hist = session.exec(select(RuleHistory)).all()
    assert len(hist) >= 1
    assert any(h.outcome == RuleOutcome.CONFIRMED for h in hist)


@pytest.mark.asyncio
async def test_anilist_reopens_stale_completed_show_when_finale_is_not_confirmed():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    settings = Settings(id=1, anilist_username="TestUser", base_dir="/tmp/Anime")
    show = Monitored(
        id=1,
        anilist_id=154587,
        display_name="Sousou no Frieren",
        aliases_json='["Sousou no Frieren"]',
        status=MonitoredStatus.COMPLETED,
        total_episodes=13,
        next_airing_episode=12,
        last_confirmed_episode=23,
    )
    session.add(settings)
    session.add(show)
    session.commit()

    mock_anilist = MagicMock()
    mock_anilist.fetch_user_seasonal_anime = AsyncMock(return_value=[{
        "anilist_id": 154587,
        "display_name": "Sousou no Frieren",
        "status": "RELEASING",
        "total_episodes": 13,
        "next_airing_episode": 12,
        "next_airing_at": None,
    }])
    supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=mock_anilist, settings=settings)

    await supervisor.sync_anilist_schedule()
    session.refresh(show)

    assert show.status == MonitoredStatus.FIXED
    assert show.next_airing_episode == 12
