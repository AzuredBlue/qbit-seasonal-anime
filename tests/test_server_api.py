from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from qbit_seasonal_anime.server import api as api_module
from qbit_seasonal_anime.server.app import create_app
from qbit_seasonal_anime.db.models import Monitored, Feed, MonitoredStatus, Settings
from qbit_seasonal_anime.server.api import get_db, get_qbit
from qbit_seasonal_anime.db.session import init_db


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
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
    q.remove_rss_rule.return_value = True
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


def test_init_db_adds_query_indexes_to_legacy_schema(db_engine):
    expected_indexes = {
        "ix_monitored_current_feed_id",
        "ix_monitored_pinned_feed_id",
        "ix_monitored_status_current_feed",
        "ix_episode_status_version",
        "ix_rule_history_feed_id",
        "ix_rule_history_created_at",
        "ix_rule_history_monitored_created",
        "ix_rule_history_monitored_outcome_feed",
    }
    with db_engine.begin() as connection:
        for index_name in expected_indexes:
            connection.exec_driver_sql(f"DROP INDEX IF EXISTS {index_name}")

    init_db(db_engine)

    actual_indexes = {
        index["name"]
        for table_name in ("monitored", "episodes", "rule_history")
        for index in inspect(db_engine).get_indexes(table_name)
    }
    assert expected_indexes <= actual_indexes


def test_init_db_adds_rules_default_to_legacy_settings():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TABLE settings (
                id INTEGER PRIMARY KEY,
                qbit_host VARCHAR,
                qbit_username VARCHAR,
                qbit_password VARCHAR,
                base_dir VARCHAR,
                default_category VARCHAR,
                default_seed_ratio FLOAT,
                anilist_username VARCHAR,
                refresh_interval_minutes INTEGER,
                stall_wait_hours INTEGER,
                title_language VARCHAR
            )
        """)
        connection.exec_driver_sql("INSERT INTO settings (id, title_language) VALUES (1, 'english')")

    init_db(engine)

    with Session(engine) as session:
        assert session.exec(select(Settings)).first().download_mode == "rules"
    engine.dispose()


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


def test_web_ui_guards_polling_requests(client):
    response = client.get("/")
    html = response.text
    assert "if (!force && document.hidden) return;" in html
    assert "if (force) statusUpdateQueued = true;" in html
    assert "if (historyLoadInFlight) {" in html
    assert "historyLoadQueued = true;" in html
    assert "loadHistory(queuedManual);" in html
    assert "document.addEventListener('visibilitychange'" in html
    assert "setInterval(updateStatus, 15000)" in html
    assert "setDownloadMode('direct')" in html
    assert "set-download-mode" in html


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

    session.expire_all()
    s = session.exec(select(Settings)).first()
    assert s.qbit_host == "http://192.168.1.50:8080"
    assert s.default_category == "anime-seasonal"
    assert s.default_seed_ratio == 1.5


def test_settings_switch_to_direct_verifies_rule_ownership(client, session, monkeypatch):
    supervisor = MagicMock()
    supervisor.prepare_download_mode.return_value = ["Disabled managed RSS rules."]
    monkeypatch.setattr(api_module, "Supervisor", MagicMock(return_value=supervisor))
    monkeypatch.setattr(api_module, "QBitClient", MagicMock())

    response = client.post("/api/settings", json={"download_mode": "direct"})

    assert response.status_code == 200
    assert response.json()["download_mode"] == "direct"
    supervisor.prepare_download_mode.assert_called_once_with("direct")
    session.expire_all()
    assert session.exec(select(Settings)).first().download_mode == "direct"


def test_settings_switch_failure_keeps_rules_mode(client, session, monkeypatch):
    supervisor = MagicMock()
    supervisor.prepare_download_mode.side_effect = RuntimeError("rule still active")
    monkeypatch.setattr(api_module, "Supervisor", MagicMock(return_value=supervisor))
    monkeypatch.setattr(api_module, "QBitClient", MagicMock())

    response = client.post("/api/settings", json={"download_mode": "direct"})

    assert response.status_code == 409
    session.expire_all()
    assert session.exec(select(Settings)).first().download_mode == "rules"


def test_system_status(client, session):
    session.add(Feed(qbit_feed_name="Feed 1", qbit_feed_url="https://feed1.example/rss", priority=1))
    session.add(Feed(qbit_feed_name="Feed 2", qbit_feed_url="https://feed2.example/rss", priority=2))
    session.add(Monitored(anilist_id=7001, display_name="Working", status=MonitoredStatus.FIXED))
    session.add(Monitored(anilist_id=7002, display_name="Upcoming", status=MonitoredStatus.UNCONFIRMED))
    session.add(Monitored(anilist_id=7003, display_name="Stalled", status=MonitoredStatus.STALLED))
    session.add(Monitored(anilist_id=7004, display_name="Paused", status=MonitoredStatus.PAUSED))
    session.add(Monitored(anilist_id=7005, display_name="Completed", status=MonitoredStatus.COMPLETED))
    session.commit()

    res = client.get("/api/status")
    assert res.status_code == 200
    st = res.json()
    assert st["daemon_active"] is True
    assert st["total_shows"] == 5
    assert st["counts"] == {
        "works": 1,
        "upcoming": 1,
        "testing": 0,
        "stalled": 1,
        "paused": 1,
        "completed": 1,
        "feeds": 2,
    }


def test_manual_cycle_offloads_scheduler_calculation(client, monkeypatch):
    supervisor = MagicMock()
    supervisor.run_full_cycle = AsyncMock(return_value=["Cycle complete"])
    monkeypatch.setattr(api_module, "Supervisor", MagicMock(return_value=supervisor))
    for name, value in {
        "is_running_cycle": False,
        "last_cycle_time": None,
        "next_check_reason": api_module.state.next_check_reason,
        "next_check_seconds": api_module.state.next_check_seconds,
        "target_next_check_time": api_module.state.target_next_check_time,
    }.items():
        monkeypatch.setattr(api_module.state, name, value)

    scheduler = MagicMock(return_value=(321, "Next cycle"))
    monkeypatch.setattr(api_module, "calculate_next_poll_interval", scheduler)
    to_thread = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
    fake_asyncio = MagicMock()
    fake_asyncio.to_thread = to_thread
    monkeypatch.setattr(api_module, "asyncio", fake_asyncio)

    response = client.post("/api/cycle/run")

    assert response.status_code == 200
    assert response.json()["next_check_seconds"] == 321
    assert response.json()["next_check_reason"] == "Next cycle"
    supervisor.run_full_cycle.assert_awaited_once_with()
    to_thread.assert_awaited_once()
    assert to_thread.await_args.args[0] is scheduler
    scheduler.assert_called_once()
    assert api_module.state.is_running_cycle is False


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


def test_direct_rule_details_do_not_create_qbit_rules(client, session, mock_qbit):
    settings = session.exec(select(Settings)).first()
    settings.download_mode = "direct"
    feed = Feed(id=3, qbit_feed_name="Direct Feed", qbit_feed_url="https://direct.example/rss", priority=1)
    show = Monitored(
        anilist_id=4100,
        display_name="Direct Show",
        aliases_json='["Direct Show"]',
        status=MonitoredStatus.UNCONFIRMED,
        current_feed_id=feed.id,
    )
    session.add(feed)
    session.add(show)
    session.commit()
    mock_qbit.get_rss_items.return_value = {
        "Direct Feed": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "1", "title": "[Group] Direct Show - 01 [1080p].mkv"}],
        }
    }

    response = client.get(f"/api/shows/{show.id}/rule")

    assert response.status_code == 200
    mock_qbit.set_rss_rule.assert_not_called()


def test_get_direct_episode_records(client, session):
    show = Monitored(
        anilist_id=4200,
        display_name="Episode Show",
        aliases_json='["Episode Show"]',
        status=MonitoredStatus.FIXED,
        total_episodes=2,
    )
    session.add(show)
    session.commit()

    response = client.get(f"/api/shows/{show.id}/episodes")

    assert response.status_code == 200
    assert [episode["episode_number"] for episode in response.json()] == [1, 2]


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

    res = client.get("/api/shows")
    assert res.status_code == 200
    s_default = next(s for s in res.json() if s["anilist_id"] == 5001)
    assert s_default["display_name"] == "The Apothecary Diaries"

    up_res = client.post("/api/settings", json={"title_language": "romaji"})
    assert up_res.status_code == 200

    res_ro = client.get("/api/shows")
    assert res_ro.status_code == 200
    s_ro = next(s for s in res_ro.json() if s["anilist_id"] == 5001)
    assert s_ro["display_name"] == "Kusuriya no Hitorigoto"

    client.post("/api/settings", json={"title_language": "english"})
    res_en = client.get("/api/shows")
    s_en = next(s for s in res_en.json() if s["anilist_id"] == 5001)
    assert s_en["display_name"] == "The Apothecary Diaries"


def test_get_show_rule_auto_confirms_when_matching_article_present(client, session, mock_qbit):
    feed = Feed(id=2, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    show = Monitored(
        anilist_id=6001,
        display_name="Yomi no Tsugai",
        aliases_json='["Yomi no Tsugai"]',
        status=MonitoredStatus.UNCONFIRMED,
        current_feed_id=feed.id,
        qbit_rule_name="[Seasonal] Yomi no Tsugai",
    )
    session.add(feed)
    session.add(show)
    session.commit()
    session.refresh(show)

    mock_qbit.get_rss_rules.return_value = {
        "[Seasonal] Yomi no Tsugai": {"enabled": True, "mustContain": "(Yomi\\s+no\\s+Tsugai)"}
    }
    mock_qbit.get_matching_articles.return_value = {
        "https://subsplease.org/rss": ["[SubsPlease] Yomi no Tsugai - 22 (1080p) [3B57467D].mkv"]
    }
    mock_qbit.get_rss_items.return_value = {
        "https://subsplease.org/rss": {
            "articles": [
                {
                    "title": "[SubsPlease] Yomi no Tsugai - 22 (1080p) [3B57467D].mkv",
                    "date": "01 Sep 2026 12:00:00 +0000",
                }
            ]
        }
    }

    res = client.get(f"/api/shows/{show.id}/rule")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "fixed"
    assert len(data["matched_articles"]) == 1

    session.expire_all()
    updated_show = session.get(Monitored, show.id)
    assert updated_show.status == MonitoredStatus.FIXED
    assert updated_show.last_confirmed_episode == 22
    assert updated_show.matched_release_group == "SubsPlease"

    hist_res = client.get("/api/history")
    assert hist_res.status_code == 200
    hist_data = hist_res.json()
    assert len(hist_data) == 1
    assert hist_data[0]["show_name"] == "Yomi no Tsugai"
    assert hist_data[0]["episode"] == 22
    assert "Yomi no Tsugai - 22" in hist_data[0]["release_title"]

    from datetime import datetime, timezone
    created_dt = datetime.fromisoformat(hist_data[0]["created_at"])
    now_dt = datetime.now(timezone.utc)
    assert abs((now_dt - created_dt).total_seconds()) < 10

    del_res = client.delete("/api/history")
    assert del_res.status_code == 200
    hist_after = client.get("/api/history").json()
    assert len(hist_after) == 0




