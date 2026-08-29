from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from qbit_seasonal_anime.server.app import create_app
from qbit_seasonal_anime.db.models import Monitored, Feed, MonitoredStatus, Settings
from qbit_seasonal_anime.server.api import get_db, get_qbit


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        # Seed default settings
        settings = Settings(id=1, qbit_host="http://localhost:8080", default_category="Anime")
        s.add(settings)
        s.commit()
    return engine


@pytest.fixture
def session(db_engine):
    with Session(db_engine) as s:
        yield s


@pytest.fixture
def mock_qbit():
    q = MagicMock()
    q.get_rss_rules.return_value = {}
    q.delete_rss_rule.return_value = True
    return q


@pytest.fixture
def client(db_engine, mock_qbit):
    app = create_app()

    def override_get_db():
        with Session(db_engine) as s:
            yield s

    def override_get_qbit():
        return mock_qbit

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_qbit] = override_get_qbit

    with TestClient(app) as test_client:
        yield test_client


def test_index_returns_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "qbit-seasonal-anime" in response.text
    assert "Releasing" in response.text
    assert "Planned" in response.text
    assert "Calendar" in response.text
    assert "tab-calendar" in response.text
    assert "calendar-weekly-grid" in response.text


def test_get_shows(client, session):
    show1 = Monitored(
        anilist_id=1001,
        display_name="Bleach S1",
        status=MonitoredStatus.FIXED,
        cover_image="https://example.com/bleach.jpg",
    )
    show2 = Monitored(
        anilist_id=1002,
        display_name="Ao Ashi S2",
        status=MonitoredStatus.UNCONFIRMED,
        next_airing_episode=1,
    )
    session.add(show1)
    session.add(show2)
    session.commit()

    response = client.get("/api/shows")
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 2
    
    b_show = next(s for s in data if s["display_name"] == "Bleach S1")
    assert b_show["is_released"] is True
    assert b_show["cover_image"] == "https://example.com/bleach.jpg"

    a_show = next(s for s in data if s["display_name"] == "Ao Ashi S2")
    assert a_show["is_released"] is False


def test_toggle_pause_show(client, session):
    show = Monitored(anilist_id=2001, display_name="Test Pause Show", status=MonitoredStatus.UNCONFIRMED)
    session.add(show)
    session.commit()
    session.refresh(show)

    res1 = client.post(f"/api/shows/{show.id}/pause")
    assert res1.status_code == 200
    assert res1.json()["new_status"] == MonitoredStatus.PAUSED.value

    res2 = client.post(f"/api/shows/{show.id}/pause")
    assert res2.status_code == 200
    assert res2.json()["new_status"] == MonitoredStatus.UNCONFIRMED.value


def test_feeds_and_reorder(client, session):
    f1 = Feed(qbit_feed_name="Feed A", qbit_feed_url="https://feed.a/rss", priority=1)
    f2 = Feed(qbit_feed_name="Feed B", qbit_feed_url="https://feed.b/rss", priority=2)
    session.add(f1)
    session.add(f2)
    session.commit()
    session.refresh(f1)
    session.refresh(f2)

    res = client.get("/api/feeds")
    assert res.status_code == 200
    feeds = res.json()
    assert len(feeds) >= 2

    # Swap priorities
    reorder_res = client.post("/api/feeds/reorder", json={
        "feeds": [
            {"id": f1.id, "priority": 2},
            {"id": f2.id, "priority": 1},
        ]
    })
    assert reorder_res.status_code == 200
    assert reorder_res.json()["status"] == "success"

    session.expire_all()
    updated_f1 = session.get(Feed, f1.id)
    updated_f2 = session.get(Feed, f2.id)
    assert updated_f1.priority == 2
    assert updated_f2.priority == 1


def test_settings_endpoints(client, session):
    res = client.get("/api/settings")
    assert res.status_code == 200
    s_data = res.json()
    assert "qbit_host" in s_data

    update_res = client.post("/api/settings", json={
        "qbit_host": "http://192.168.1.50:8080",
        "default_category": "anime-seasonal",
        "default_seed_ratio": 1.5,
    })
    assert update_res.status_code == 200

    # Verify updated in DB
    session.expire_all()
    s = session.exec(select(Settings)).first()
    assert s.qbit_host == "http://192.168.1.50:8080"
    assert s.default_category == "anime-seasonal"
    assert s.default_seed_ratio == 1.5


def test_system_status(client, session):
    res = client.get("/api/status")
    assert res.status_code == 200
    st = res.json()
    assert st["daemon_active"] is True
    assert "counts" in st


def test_delete_show(client, session):
    show = Monitored(anilist_id=3001, display_name="Show To Delete", status=MonitoredStatus.UNCONFIRMED)
    session.add(show)
    session.commit()
    session.refresh(show)
    show_id = show.id

    res = client.delete(f"/api/shows/{show_id}")
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    session.expire_all()
    deleted = session.get(Monitored, show_id)
    assert deleted is None


def test_edit_show_endpoint(client, session, mock_qbit):
    mock_qbit.get_rss_items.return_value = {
        "Feed 1": {
            "uid": "1",
            "url": "https://feed1.org/rss",
            "articles": [
                {"id": "1", "title": "[SubsPlease] Bleach - 01 (1080p) [ABCD].mkv"}
            ]
        }
    }
    feed = Feed(id=1, qbit_feed_name="Feed 1", qbit_feed_url="https://feed1.org/rss", priority=1)
    show = Monitored(anilist_id=4001, display_name="Bleach", aliases_json='["Bleach"]', status=MonitoredStatus.UNCONFIRMED)
    session.add(feed)
    session.add(show)
    session.commit()
    session.refresh(show)

    # Edit show: assign feed 1 and custom save folder
    res = client.post(f"/api/shows/{show.id}/edit", json={
        "current_feed_id": feed.id,
        "save_folder": "Bleach Custom",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"

    session.expire_all()
    updated = session.get(Monitored, show.id)
    assert updated.save_folder == "Bleach Custom"
    assert updated.current_feed_id == feed.id


def test_title_language_setting_switch(client, session):
    show = Monitored(
        anilist_id=5001,
        display_name="Kusuriya no Hitorigoto",
        title_romaji="Kusuriya no Hitorigoto",
        title_english="The Apothecary Diaries",
        status=MonitoredStatus.FIXED,
    )
    session.add(show)
    session.commit()

    # 1. Default (English)
    res = client.get("/api/shows")
    assert res.status_code == 200
    s_default = next(s for s in res.json() if s["anilist_id"] == 5001)
    assert s_default["display_name"] == "The Apothecary Diaries"

    # 2. Update setting to Romaji / JA
    up_res = client.post("/api/settings", json={"title_language": "romaji"})
    assert up_res.status_code == 200

    # 3. Check shows in Romaji
    res_ro = client.get("/api/shows")
    assert res_ro.status_code == 200
    s_ro = next(s for s in res_ro.json() if s["anilist_id"] == 5001)
    assert s_ro["display_name"] == "Kusuriya no Hitorigoto"

    # 4. Switch back to English
    client.post("/api/settings", json={"title_language": "english"})
    res_en = client.get("/api/shows")
    s_en = next(s for s in res_en.json() if s["anilist_id"] == 5001)
    assert s_en["display_name"] == "The Apothecary Diaries"


