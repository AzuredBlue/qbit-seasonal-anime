from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from qbit_seasonal_anime.server import api as api_module
from qbit_seasonal_anime.server.app import create_app
from qbit_seasonal_anime.db.models import MatchHistory, Monitored, Feed, MonitoredStatus, Settings
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
        "ix_monitored_status_current_feed",
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
        for table_name in ("monitored", "rule_history")
        for index in inspect(db_engine).get_indexes(table_name)
    }
    assert expected_indexes <= actual_indexes


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
    # An explicit feed choice pins it, so auto-detect leaves it alone.
    assert updated.feed_pinned is True


def test_editing_other_fields_leaves_the_feed_and_pin_alone(client, session, mock_qbit):
    feed = Feed(id=1, qbit_feed_name="Feed 1", qbit_feed_url="https://feed1.org/rss", priority=1)
    show = Monitored(
        anilist_id=4010,
        display_name="Blue Box Season 2",
        aliases_json='["Blue Box Season 2"]',
        status=MonitoredStatus.UNCONFIRMED,
        current_feed_id=feed.id,
        feed_pinned=True,
    )
    session.add(feed)
    session.add(show)
    session.commit()
    session.refresh(show)

    res = client.post(f"/api/shows/{show.id}/edit", json={"ratio_limit": 2.5})
    assert res.status_code == 200

    session.expire_all()
    updated = session.get(Monitored, show.id)
    assert updated.current_feed_id == feed.id
    assert updated.feed_pinned is True
    # The edit still applied — to the rule, which is where ratio lives.
    written = mock_qbit.set_rss_rule.call_args.kwargs["rule_def"]
    assert written["ratioLimit"] == 2.5


def test_picking_auto_discover_feed_unpins_the_show(client, session, mock_qbit):
    feed = Feed(id=1, qbit_feed_name="Feed 1", qbit_feed_url="https://feed1.org/rss", priority=1)
    show = Monitored(
        anilist_id=4002,
        display_name="Re:Zero",
        aliases_json='["Re:Zero"]',
        status=MonitoredStatus.UNCONFIRMED,
        current_feed_id=feed.id,
        feed_pinned=True,
    )
    session.add(feed)
    session.add(show)
    session.commit()
    session.refresh(show)

    res = client.post(f"/api/shows/{show.id}/edit", json={"current_feed_id": 0})
    assert res.status_code == 200

    session.expire_all()
    updated = session.get(Monitored, show.id)
    assert updated.current_feed_id is None
    assert updated.feed_pinned is False


def test_rule_details_flags_unlearned_pattern(client, session, mock_qbit):
    feed = Feed(id=1, qbit_feed_name="Feed 1", qbit_feed_url="https://feed1.org/rss", priority=1)
    show = Monitored(
        anilist_id=4003,
        display_name="Upcoming Anime",
        aliases_json='["Upcoming Anime", "Upcoming Anime 2nd Season"]',
        status=MonitoredStatus.UNCONFIRMED,
        current_feed_id=feed.id,
        next_airing_episode=1,
    )
    session.add(feed)
    session.add(show)
    session.commit()
    session.refresh(show)

    mock_qbit.get_rss_items.return_value = {
        "Feed 1": {"url": "https://feed1.org/rss", "articles": []}
    }

    res = client.get(f"/api/shows/{show.id}/rule")
    assert res.status_code == 200
    data = res.json()
    assert data["has_learned_pattern"] is False
    assert data["is_upcoming"] is True
    assert data["feed_pinned"] is False
    assert data["candidate_feed_id"] == 0


def test_rule_details_reports_testing_show_not_upcoming(client, session, mock_qbit):
    from datetime import timedelta

    from qbit_seasonal_anime.db.models import utc_now

    feed = Feed(id=1, qbit_feed_name="Feed 1", qbit_feed_url="https://feed1.org/rss", priority=1)
    show = Monitored(
        anilist_id=4005,
        display_name="Aired But Unmatched Anime",
        aliases_json='["Aired But Unmatched Anime"]',
        status=MonitoredStatus.UNCONFIRMED,
        current_feed_id=feed.id,
        next_airing_episode=3,
        next_airing_at=utc_now() - timedelta(hours=6),
    )
    session.add(feed)
    session.add(show)
    session.commit()
    session.refresh(show)

    mock_qbit.get_rss_items.return_value = {
        "Feed 1": {"url": "https://feed1.org/rss", "articles": []}
    }

    data = client.get(f"/api/shows/{show.id}/rule").json()
    assert data["is_upcoming"] is False
    assert data["has_learned_pattern"] is False


def test_rule_details_reports_watched_candidate_feed(client, session, mock_qbit):
    from datetime import timedelta

    from qbit_seasonal_anime.db.models import utc_now

    feed = Feed(id=1, qbit_feed_name="Feed 1", qbit_feed_url="https://feed1.org/rss", priority=1)
    other = Feed(id=2, qbit_feed_name="Feed 2", qbit_feed_url="https://feed2.org/rss", priority=2)
    show = Monitored(
        anilist_id=4004,
        display_name="Split Cour Anime",
        aliases_json='["Split Cour Anime"]',
        status=MonitoredStatus.UNCONFIRMED,
        current_feed_id=feed.id,
        candidate_feed_id=other.id,
        candidate_feed_since=utc_now() - timedelta(minutes=1),
    )
    session.add(feed)
    session.add(other)
    session.add(show)
    session.commit()
    session.refresh(show)

    mock_qbit.get_rss_items.return_value = {
        "Feed 1": {"url": "https://feed1.org/rss", "articles": []}
    }

    data = client.get(f"/api/shows/{show.id}/rule").json()
    assert data["candidate_feed_id"] == other.id
    assert data["candidate_feed_name"] == "Feed 2"
    assert data["candidate_feed_since"] is not None


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


def test_get_show_rule_is_read_only_when_a_article_matches(client, session, mock_qbit):
    """Opening the rule modal must not confirm shows, write rules, or record history."""
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
    # The article is still reported as currently matching...
    assert len(data["matched_articles"]) == 1
    assert data["history_articles"] == []

    # ...but nothing was mutated and no match was invented from the cache.
    session.expire_all()
    updated_show = session.get(Monitored, show.id)
    assert updated_show.status == MonitoredStatus.UNCONFIRMED
    assert updated_show.last_confirmed_episode is None
    assert updated_show.matched_title is None
    mock_qbit.set_rss_rule.assert_not_called()
    assert client.get("/api/history").json() == []


def test_rule_details_separates_live_matches_from_recorded_history(client, session, mock_qbit):
    feed = Feed(id=3, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    show = Monitored(
        anilist_id=6002,
        display_name="Yomi no Tsugai",
        aliases_json='["Yomi no Tsugai"]',
        status=MonitoredStatus.FIXED,
        current_feed_id=feed.id,
        qbit_rule_name="[Seasonal] Yomi no Tsugai",
        matched_title="Yomi no Tsugai",
    )
    session.add(feed)
    session.add(show)
    session.commit()
    session.refresh(show)
    session.add(MatchHistory(
        monitored_id=show.id,
        show_name=show.display_name,
        rule_name=show.qbit_rule_name,
        release_title="[SubsPlease] Yomi no Tsugai - 21 (1080p) [OLD].mkv",
        episode=21,
    ))
    session.commit()

    mock_qbit.get_rss_rules.return_value = {"[Seasonal] Yomi no Tsugai": {"enabled": True, "mustContain": "Yomi no Tsugai"}}
    mock_qbit.get_matching_articles.return_value = {
        "https://subsplease.org/rss": ["[SubsPlease] Yomi no Tsugai - 22 (1080p) [NEW].mkv"]
    }

    data = client.get(f"/api/shows/{show.id}/rule").json()
    assert data["matched_articles"] == ["[SubsPlease] Yomi no Tsugai - 22 (1080p) [NEW].mkv"]
    assert data["history_articles"] == ["[SubsPlease] Yomi no Tsugai - 21 (1080p) [OLD].mkv"]

    # A release that is both currently matching and already recorded shows up in both.
    session.add(MatchHistory(
        monitored_id=show.id,
        show_name=show.display_name,
        rule_name=show.qbit_rule_name,
        release_title="[SubsPlease] Yomi no Tsugai - 22 (1080p) [NEW].mkv",
        episode=22,
    ))
    session.commit()

    data = client.get(f"/api/shows/{show.id}/rule").json()
    assert data["matched_articles"] == ["[SubsPlease] Yomi no Tsugai - 22 (1080p) [NEW].mkv"]
    assert data["history_articles"] == [
        "[SubsPlease] Yomi no Tsugai - 22 (1080p) [NEW].mkv",
        "[SubsPlease] Yomi no Tsugai - 21 (1080p) [OLD].mkv",
    ]


def test_history_endpoint_and_delete_still_work(client, session, mock_qbit):
    show = Monitored(anilist_id=6003, display_name="History Show", aliases_json='["History Show"]')
    session.add(show)
    session.commit()
    session.refresh(show)
    session.add(MatchHistory(
        monitored_id=show.id,
        show_name=show.display_name,
        rule_name="[Seasonal] History Show",
        release_title="[SubsPlease] History Show - 01 (1080p).mkv",
        episode=1,
    ))
    session.commit()

    hist_data = client.get("/api/history").json()
    assert len(hist_data) == 1
    assert hist_data[0]["episode"] == 1

    del_res = client.delete("/api/history")
    assert del_res.status_code == 200
    assert client.get("/api/history").json() == []




