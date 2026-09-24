from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from qbit_seasonal_anime.clients.qbit import QbitClientError
from qbit_seasonal_anime.core.grabber import evaluate_and_grab_releases, sync_show_episodes
from qbit_seasonal_anime.core.supervisor import Supervisor
from qbit_seasonal_anime.db.models import Episode, EpisodeStatus, Feed, GrabDecision, Monitored, MonitoredStatus, Settings


def _database():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine, Session(engine)


def _show(session, **kwargs):
    values = {
        "id": 1,
        "anilist_id": 154587,
        "display_name": "Sousou no Frieren",
        "aliases_json": '["Sousou no Frieren", "Frieren"]',
        "status": MonitoredStatus.UNCONFIRMED,
        "total_episodes": 8,
        "next_airing_episode": 8,
        "next_airing_at": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    values.update(kwargs)
    show = Monitored(**values)
    session.add(show)
    session.commit()
    return show


def _qbit(title="[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv", events=None):
    qbit = MagicMock()
    torrent = MagicMock(hash="hash-1", progress=0.1, state="downloading", name=title)
    qbit.ensure_category_exists.return_value = True
    qbit.get_torrents.side_effect = lambda **kwargs: [torrent] if kwargs.get("tag", "").startswith("qsa-op-") or kwargs.get("tag") == "qsa-managed" else []
    qbit.add_torrent.side_effect = lambda **kwargs: events.append("add") if events is not None else True
    qbit.pause_torrents.side_effect = lambda *args: events.append("pause") if events is not None else None
    qbit.delete_torrents.side_effect = lambda *args, **kwargs: events.append("delete") if events is not None else None
    qbit.recheck_torrents.side_effect = lambda *args: events.append("recheck") if events is not None else None
    qbit.resume_torrents.side_effect = lambda *args: events.append("resume") if events is not None else None
    return qbit, torrent


def test_direct_grab_is_idempotent_across_cycles():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{
                "id": "ep8",
                "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv",
                "torrentURL": "magnet:ep8",
            }],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)).first()
    assert episode.status == EpisodeStatus.DOWNLOADING
    assert episode.torrent_hash == "hash-1"
    assert qbit.add_torrent.call_count == 1
    assert show.status == MonitoredStatus.FIXED
    session.close()
    engine.dispose()


def test_direct_does_not_map_absolute_feed_number_with_invalid_history():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(
        session,
        display_name="Re:ZERO -Starting Life in Another World- Season 4",
        aliases_json='["Re:ZERO -Starting Life in Another World- Season 4", "Re Zero kara Hajimeru Isekai Seikatsu"]',
        total_episodes=19,
        next_airing_episode=19,
        next_airing_at=datetime.now(timezone.utc) + timedelta(days=1),
        last_confirmed_episode=84,
    )
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{
                "id": "absolute-78",
                "title": "[SubsPlease] Re Zero kara Hajimeru Isekai Seikatsu - 78 (1080p) [30D08902].mkv",
                "torrentURL": "magnet:absolute-78",
            }],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 12)).first()
    assert episode.status == EpisodeStatus.WANTED
    qbit.add_torrent.assert_not_called()
    session.close()
    engine.dispose()


def test_direct_grab_recovers_when_hash_appears_after_restart():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session)
    qbit, torrent = _qbit()
    visible = {"value": False}
    qbit.get_torrents.side_effect = lambda **kwargs: [torrent] if visible["value"] and (kwargs.get("tag", "").startswith("qsa-op-") or kwargs.get("tag") == "qsa-managed") else []
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "ep8", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv", "torrentURL": "magnet:ep8"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    visible["value"] = True
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)).first()
    assert episode.status == EpisodeStatus.DOWNLOADING
    assert episode.torrent_hash == "hash-1"
    assert qbit.add_torrent.call_count == 1
    session.close()
    engine.dispose()


def test_observe_mode_records_decisions_without_adding_torrents():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="observe")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{
                "id": "ep8",
                "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv",
                "torrentURL": "magnet:ep8",
            }],
        }
    }

    logs = evaluate_and_grab_releases(session, qbit, settings, [feed], mode="observe")

    assert any("Would grab" in log for log in logs)
    assert session.exec(select(GrabDecision)).first() is not None
    qbit.add_torrent.assert_not_called()
    session.close()
    engine.dispose()


def test_pinned_feed_is_strict_in_direct_mode():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    top = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    pinned = Feed(id=2, qbit_feed_name="Erai", qbit_feed_url="https://erai.example/rss", priority=2)
    session.add(settings)
    session.add(top)
    session.add(pinned)
    show = _show(session, current_feed_id=top.id, pinned_feed_id=pinned.id)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": top.qbit_feed_url, "articles": [{"id": "sub", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv", "torrentURL": "magnet:sub"}]},
        "Erai": {"url": pinned.qbit_feed_url, "articles": [{"id": "erai", "title": "[Erai-raws] Frieren - 08 [1080p].mkv", "torrentURL": "magnet:erai"}]},
    }

    evaluate_and_grab_releases(session, qbit, settings, [top, pinned], mode="direct")

    assert qbit.add_torrent.call_args.kwargs["urls"] == "magnet:erai"
    assert show.current_feed_id == pinned.id
    session.close()
    engine.dispose()


def test_v2_add_is_paused_and_old_files_are_deleted_only_after_new_hash_is_known():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=2, next_airing_episode=2, last_confirmed_episode=1)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.COMPLETED
    episode.version = 1
    episode.release_title = "[SubsPlease] Sousou no Frieren - 01 (1080p) [OLD].mkv"
    episode.torrent_hash = "old-hash"
    session.add(episode)
    session.commit()
    events = []
    title = "[SubsPlease] Sousou no Frieren - 01v2 (1080p) [NEW].mkv"
    qbit, new_torrent = _qbit(title=title, events=events)
    old_torrent = MagicMock(hash="old-hash", progress=1, state="stoppedUP", name=episode.release_title)
    new_torrent.hash = "new-hash"
    qbit.get_torrents.side_effect = lambda **kwargs: [new_torrent] if kwargs.get("tag", "").startswith("qsa-op-") else ([old_torrent] if kwargs.get("hashes") == ["old-hash"] else ([old_torrent, new_torrent] if kwargs.get("tag") == "qsa-managed" else []))
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "v2", "title": title, "torrentURL": "magnet:v2"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert events == ["add", "pause", "delete", "recheck", "resume"]
    assert episode.version == 2
    assert episode.torrent_hash == "new-hash"
    assert episode.status == EpisodeStatus.DOWNLOADING
    qbit.delete_torrents.assert_called_once_with(["old-hash"], delete_files=True)
    session.close()
    engine.dispose()


def test_stopped_seeding_torrent_is_completed_without_regrab():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session)
    qbit, torrent = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "ep8", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv", "torrentURL": "magnet:ep8"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    torrent.progress = 1
    torrent.state = "stoppedUP"
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)).first()
    assert episode.status == EpisodeStatus.COMPLETED
    assert qbit.add_torrent.call_count == 1
    session.close()
    engine.dispose()


def test_missing_accepted_torrent_is_completed_without_retry():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "ep8", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv", "torrentURL": "magnet:ep8"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    qbit.get_torrents.side_effect = lambda **kwargs: []
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)).first()
    assert episode.status == EpisodeStatus.COMPLETED
    assert qbit.add_torrent.call_count == 1
    session.close()
    engine.dispose()


def test_failed_direct_add_is_not_retried():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session)
    qbit, _ = _qbit()
    qbit.get_torrents.side_effect = lambda **kwargs: []
    qbit.add_torrent.side_effect = RuntimeError("add failed")
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "ep8", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv", "torrentURL": "magnet:ep8"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)).first()
    assert episode.status == EpisodeStatus.FAILED
    assert qbit.add_torrent.call_count == 1
    session.close()
    engine.dispose()


def test_v2_replacement_continues_when_v1_was_already_removed():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=2, next_airing_episode=2, last_confirmed_episode=1)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.COMPLETED
    episode.version = 1
    episode.release_title = "[SubsPlease] Sousou no Frieren - 01 (1080p) [OLD].mkv"
    episode.torrent_hash = "old-hash"
    session.add(episode)
    session.commit()
    events = []
    title = "[SubsPlease] Sousou no Frieren - 01v2 (1080p) [NEW].mkv"
    qbit, new_torrent = _qbit(title=title, events=events)
    new_torrent.hash = "new-hash"
    qbit.get_torrents.side_effect = lambda **kwargs: [new_torrent] if kwargs.get("tag", "").startswith("qsa-op-") else []
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "v2", "title": title, "torrentURL": "magnet:v2"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert events == ["add", "recheck", "resume"]
    assert episode.version == 2
    assert episode.torrent_hash == "new-hash"
    assert episode.status == EpisodeStatus.DOWNLOADING
    qbit.delete_torrents.assert_not_called()
    session.close()
    engine.dispose()


def test_failed_v2_add_keeps_old_episode():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=2, next_airing_episode=2, last_confirmed_episode=1)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.COMPLETED
    episode.torrent_hash = "old-hash"
    session.add(episode)
    session.commit()
    qbit, _ = _qbit(title="[SubsPlease] Sousou no Frieren - 01v2 (1080p) [NEW].mkv")
    qbit.add_torrent.side_effect = RuntimeError("add failed")
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "v2", "title": "[SubsPlease] Sousou no Frieren - 01v2 (1080p) [NEW].mkv", "torrentURL": "magnet:v2"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert episode.status == EpisodeStatus.COMPLETED
    assert episode.torrent_hash == "old-hash"
    assert qbit.add_torrent.call_count == 1
    qbit.delete_torrents.assert_not_called()
    session.close()
    engine.dispose()


def test_rules_transition_marks_owned_articles_read():
    engine, session = _database()
    settings = Settings(id=1, download_mode="rules")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, current_feed_id=feed.id, total_episodes=1, last_confirmed_episode=1)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.release_title = "[Group] Sousou no Frieren - 01 (1080p).mkv"
    session.add(episode)
    session.commit()
    qbit = MagicMock()
    qbit.get_rss_feed_paths.return_value = {feed.qbit_feed_url: "SubsPlease"}
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": feed.qbit_feed_url, "articles": [{"id": "ep1", "title": episode.release_title}]}
    }
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    logs = supervisor.shield_owned_articles()
    second_logs = supervisor.shield_owned_articles()

    assert any("previously downloaded RSS" in log for log in logs)
    assert second_logs == []
    qbit.mark_rss_article_read.assert_called_once_with("SubsPlease", "ep1")
    session.close()
    engine.dispose()


def test_shield_owned_articles_is_scoped_to_each_show():
    engine, session = _database()
    settings = Settings(id=1, download_mode="observe")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    first = _show(session, id=1, anilist_id=1001, display_name="First Show", aliases_json='["First Show"]', current_feed_id=feed.id)
    second = _show(session, id=2, anilist_id=1002, display_name="Second Show", aliases_json='["Second Show"]', current_feed_id=feed.id)
    sync_show_episodes(session, first)
    sync_show_episodes(session, second)
    first_episode = session.exec(select(Episode).where(Episode.monitored_id == first.id, Episode.episode_number == 1)).first()
    second_episode = session.exec(select(Episode).where(Episode.monitored_id == second.id, Episode.episode_number == 1)).first()
    first_episode.status = EpisodeStatus.COMPLETED
    first_episode.release_title = "First release"
    second_episode.status = EpisodeStatus.COMPLETED
    second_episode.release_title = "Second release"
    session.add(first_episode)
    session.add(second_episode)
    session.commit()
    qbit = MagicMock()
    qbit.get_rss_feed_paths.return_value = {feed.qbit_feed_url: "SubsPlease"}
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [
                {"id": "first-item", "title": "First release"},
                {"id": "second-item", "title": "Second release"},
            ],
        }
    }
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    supervisor.shield_owned_articles()

    marked_ids = {call.args[1] for call in qbit.mark_rss_article_read.call_args_list}
    assert marked_ids == {"first-item", "second-item"}
    session.close()
    engine.dispose()


def test_shield_does_not_treat_hashless_queued_release_as_owned():
    engine, session = _database()
    settings = Settings(id=1, download_mode="observe")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, current_feed_id=feed.id)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.QUEUED
    episode.release_title = "Queued but unconfirmed release"
    session.add(episode)
    session.commit()
    qbit = MagicMock()
    qbit.get_rss_feed_paths.return_value = {feed.qbit_feed_url: "SubsPlease"}
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "queued-item", "title": episode.release_title}],
        }
    }
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    supervisor.shield_owned_articles()

    qbit.mark_rss_article_read.assert_not_called()
    session.close()
    engine.dispose()


def test_direct_preflight_disables_and_verifies_managed_rules():
    engine, session = _database()
    settings = Settings(id=1, download_mode="direct")
    show = _show(session)
    show.qbit_rule_name = "[Seasonal] Sousou no Frieren"
    session.add(show)
    session.commit()
    qbit = MagicMock()
    rules = {"[Seasonal] Sousou no Frieren": {"enabled": True, "mustContain": "Frieren"}}
    qbit.get_rss_rules.return_value = rules
    qbit.set_rss_rule.side_effect = lambda name, rule_def: rules.__setitem__(name, rule_def)
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    supervisor.disable_managed_rules()

    assert rules["[Seasonal] Sousou no Frieren"]["enabled"] is False
    assert qbit.set_rss_rule.call_count == 1
    session.close()
    engine.dispose()


def test_direct_preflight_blocks_when_rule_cannot_be_disabled():
    engine, session = _database()
    settings = Settings(id=1, download_mode="direct")
    session.add(settings)
    qbit = MagicMock()
    qbit.get_rss_rules.return_value = {"[Seasonal] Show": {"enabled": True}}
    qbit.set_rss_rule.side_effect = RuntimeError("busy")
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    try:
        supervisor.disable_managed_rules()
    except QbitClientError:
        pass
    else:
        raise AssertionError("Expected direct ownership preflight to fail")
    session.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_supervisor_direct_cycle_keeps_rules_disabled():
    engine, session = _database()
    settings = Settings(
        id=1,
        default_category="Anime",
        base_dir="/tmp/Anime",
        download_mode="direct",
        anilist_username="",
    )
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    _show(session)
    qbit, _ = _qbit()
    qbit.get_rss_feeds_flat.return_value = [{"name": "SubsPlease", "url": feed.qbit_feed_url}]
    qbit.get_rss_rules.return_value = {}
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{
                "id": "ep8",
                "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv",
                "torrentURL": "magnet:ep8",
            }],
        }
    }
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    await supervisor.run_full_cycle()

    qbit.add_torrent.assert_called()
    qbit.set_rss_rule.assert_not_called()
    session.close()
    engine.dispose()
