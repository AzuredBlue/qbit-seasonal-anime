import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlmodel import Session, SQLModel, create_engine, select
from qbit_seasonal_anime.core.supervisor import Supervisor
from qbit_seasonal_anime.db.models import Feed, Monitored, MonitoredStatus, RuleHistory, RuleOutcome, Settings
from tests.fixtures import MOCK_QBIT_RSS_ITEMS


@pytest.mark.asyncio
async def test_supervisor_full_cycle():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    settings = Settings(id=1, default_category="Anime", anilist_username="TestUser", base_dir="/tmp/Anime")
    session.add(settings)

    # Initial show without feed assignment
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
    mock_qbit.get_torrents_by_category.return_value = [
        {
            "name": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv",
            "save_path": "/tmp/Anime/Sousou no Frieren",
        }
    ]

    mock_anilist = MagicMock()
    mock_anilist.fetch_user_seasonal_anime = AsyncMock(return_value=[])

    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=settings)

    logs = await supervisor.run_full_cycle()
    session.refresh(show)

    # Check feeds were synced
    feeds = session.exec(select(Feed)).all()
    assert len(feeds) == 2

    # Check show was bootstrapped, rule created and confirmed via torrent download!
    assert show.status == MonitoredStatus.FIXED
    assert show.current_feed_id is not None
    assert show.last_confirmed_episode == 8

    # Check rule history was inserted and confirmed
    hist = session.exec(select(RuleHistory)).all()
    assert len(hist) >= 1
    assert any(h.outcome == RuleOutcome.CONFIRMED for h in hist)
