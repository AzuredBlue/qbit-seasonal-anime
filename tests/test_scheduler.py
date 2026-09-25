import unittest
from datetime import timedelta
from sqlmodel import Session, SQLModel, create_engine
from qbit_seasonal_anime.db.models import Episode, EpisodeStatus, Feed, Monitored, MonitoredStatus, utc_now
from qbit_seasonal_anime.workers.scheduler import calculate_next_poll_interval


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss/?r=1080", priority=1)
        self.session.add(feed)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_hunting_mode_when_show_aired_recently_without_feed(self):
        now = utc_now()
        show = Monitored(
            id=1,
            anilist_id=101,
            display_name="Airing Anime",
            aliases_json='["Airing Anime"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=None,
            next_airing_episode=1,
            next_airing_at=now - timedelta(minutes=15),
        )
        self.session.add(show)
        self.session.commit()

        dur, reason = calculate_next_poll_interval(self.session, default_interval_seconds=21600, hunting_interval_seconds=300)
        self.assertEqual(dur, 300)
        self.assertIn("Hunting mode", reason)

    def test_wake_on_air_time_when_upcoming_show_airs_soon(self):
        now = utc_now()
        show = Monitored(
            id=1,
            anilist_id=101,
            display_name="Upcoming Anime",
            aliases_json='["Upcoming Anime"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=None,
            next_airing_episode=1,
            next_airing_at=now + timedelta(minutes=45),
        )
        self.session.add(show)
        self.session.commit()

        dur, reason = calculate_next_poll_interval(self.session, default_interval_seconds=21600, hunting_interval_seconds=300)
        self.assertAlmostEqual(dur, 2700, delta=10)
        self.assertIn("Upcoming premiere", reason)

    def test_default_interval_when_all_shows_working(self):
        now = utc_now()
        show = Monitored(
            id=1,
            anilist_id=101,
            display_name="Working Anime",
            aliases_json='["Working Anime"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            last_confirmed_episode=5,
            next_airing_episode=6,
            next_airing_at=now + timedelta(minutes=30),  # Airs in 30 mins, but rule is already working
        )
        self.session.add(show)
        self.session.commit()

        dur, reason = calculate_next_poll_interval(self.session, default_interval_seconds=21600, hunting_interval_seconds=300)
        self.assertEqual(dur, 21600)
        self.assertIn("working rules", reason)

    def test_direct_mode_uses_direct_ownership_message(self):
        show = Monitored(
            id=7,
            anilist_id=707,
            display_name="Direct Anime",
            aliases_json='["Direct Anime"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            next_airing_episode=6,
            next_airing_at=utc_now() + timedelta(minutes=30),
        )
        self.session.add(show)
        self.session.commit()

        duration, reason = calculate_next_poll_interval(
            self.session,
            default_interval_seconds=21600,
            download_mode="direct",
        )

        self.assertEqual(duration, 21600)
        self.assertIn("direct ownership", reason)
        self.assertNotIn("working rules", reason)

    def test_direct_fixed_show_hunts_for_post_rollover_wanted_episode(self):
        show = Monitored(
            id=71,
            anilist_id=710,
            display_name="Link Click Season 3",
            aliases_json='["Link Click Season 3"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            last_confirmed_episode=7,
            next_airing_episode=9,
            next_airing_at=utc_now() + timedelta(days=7),
        )
        self.session.add(show)
        self.session.flush()
        self.session.add(Episode(monitored_id=show.id, episode_number=8, status=EpisodeStatus.WANTED))
        self.session.commit()

        duration, reason = calculate_next_poll_interval(
            self.session,
            default_interval_seconds=21600,
            hunting_interval_seconds=300,
            download_mode="direct",
        )

        self.assertEqual(duration, 300)
        self.assertIn("Direct hunting", reason)
        self.assertIn("Link Click Season 3", reason)

    def test_direct_fixed_show_ignores_old_episode_gap(self):
        show = Monitored(
            id=74,
            anilist_id=740,
            display_name="Re:ZERO Season 4",
            aliases_json='["Re:ZERO Season 4"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            last_confirmed_episode=18,
            next_airing_episode=19,
            next_airing_at=utc_now() + timedelta(days=5),
        )
        self.session.add(show)
        self.session.flush()
        self.session.add(Episode(monitored_id=show.id, episode_number=12, status=EpisodeStatus.WANTED))
        self.session.add(Episode(monitored_id=show.id, episode_number=18, status=EpisodeStatus.COMPLETED))
        self.session.add(Episode(monitored_id=show.id, episode_number=19, status=EpisodeStatus.WANTED))
        self.session.commit()

        duration, reason = calculate_next_poll_interval(
            self.session,
            default_interval_seconds=21600,
            download_mode="direct",
        )

        self.assertEqual(duration, 21600)
        self.assertIn("direct ownership", reason)

    def test_direct_fixed_show_ignores_future_wanted_episode(self):
        from unittest.mock import MagicMock

        show = Monitored(
            id=72,
            anilist_id=720,
            display_name="Caught Up Anime",
            aliases_json='["Caught Up Anime"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            last_confirmed_episode=8,
            next_airing_episode=9,
            next_airing_at=utc_now() + timedelta(days=7),
        )
        self.session.add(show)
        self.session.flush()
        self.session.add(Episode(monitored_id=show.id, episode_number=8, status=EpisodeStatus.COMPLETED))
        self.session.add(Episode(monitored_id=show.id, episode_number=9, status=EpisodeStatus.WANTED))
        self.session.commit()
        mock_qbit = MagicMock()

        duration, reason = calculate_next_poll_interval(
            self.session,
            default_interval_seconds=21600,
            qbit_client=mock_qbit,
            download_mode="direct",
        )

        self.assertEqual(duration, 21600)
        self.assertIn("direct ownership", reason)
        mock_qbit.get_rss_refresh_interval_seconds.assert_not_called()

    def test_rules_fixed_show_ignores_direct_episode_backlog(self):
        from unittest.mock import MagicMock

        show = Monitored(
            id=73,
            anilist_id=730,
            display_name="Rules Anime",
            aliases_json='["Rules Anime"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            last_confirmed_episode=7,
            next_airing_episode=9,
            next_airing_at=utc_now() + timedelta(days=7),
        )
        self.session.add(show)
        self.session.flush()
        self.session.add(Episode(monitored_id=show.id, episode_number=8, status=EpisodeStatus.WANTED))
        self.session.commit()
        mock_qbit = MagicMock()

        duration, reason = calculate_next_poll_interval(
            self.session,
            default_interval_seconds=21600,
            qbit_client=mock_qbit,
            download_mode="rules",
        )

        self.assertEqual(duration, 21600)
        self.assertIn("working rules", reason)
        mock_qbit.get_rss_refresh_interval_seconds.assert_not_called()

    def test_observe_mode_uses_observation_message(self):
        show = Monitored(
            id=8,
            anilist_id=808,
            display_name="Observed Anime",
            aliases_json='["Observed Anime"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            next_airing_episode=6,
            next_airing_at=utc_now() + timedelta(minutes=30),
        )
        self.session.add(show)
        self.session.commit()

        duration, reason = calculate_next_poll_interval(
            self.session,
            default_interval_seconds=21600,
            download_mode="observe",
        )

        self.assertEqual(duration, 21600)
        self.assertIn("are observed", reason)
        self.assertNotIn("working rules", reason)

    def test_fixed_show_does_not_query_rss_refresh_interval(self):
        from unittest.mock import MagicMock

        show = Monitored(
            id=6,
            anilist_id=606,
            display_name="Already Working Anime",
            aliases_json='["Already Working Anime"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            next_airing_episode=1,
            next_airing_at=utc_now() - timedelta(days=1),
        )
        self.session.add(show)
        self.session.commit()

        mock_qbit = MagicMock()
        duration, reason = calculate_next_poll_interval(
            self.session,
            default_interval_seconds=21600,
            qbit_client=mock_qbit,
        )

        self.assertEqual(duration, 21600)
        self.assertIn("working rules", reason)
        mock_qbit.get_rss_refresh_interval_seconds.assert_not_called()

    def test_no_hunting_for_show_without_release_date(self):
        show = Monitored(
            id=2,
            anilist_id=202,
            display_name="Aoashi 2nd Season",
            aliases_json='["Aoashi 2nd Season", "Ao Ashi S2"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=None,
            next_airing_episode=None,
            next_airing_at=None,
        )
        self.session.add(show)
        self.session.commit()

        dur, reason = calculate_next_poll_interval(self.session, default_interval_seconds=21600)
        self.assertEqual(dur, 21600)
        self.assertNotIn("Hunting mode", reason)
        self.assertIn("waiting for air dates", reason)

    def test_dynamic_qbit_rss_refresh_interval(self):
        from unittest.mock import MagicMock
        now = utc_now()
        show = Monitored(
            id=3,
            anilist_id=303,
            display_name="Aired Show",
            aliases_json='["Aired Show"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=None,
            next_airing_episode=1,
            next_airing_at=now - timedelta(minutes=10),
        )
        self.session.add(show)
        self.session.commit()

        mock_qbit = MagicMock()
        mock_qbit.get_rss_refresh_interval_seconds.return_value = 315  # 5 min + 15 sec

        dur, reason = calculate_next_poll_interval(self.session, qbit_client=mock_qbit)
        self.assertEqual(dur, 315)
    def test_hunting_mode_when_previous_episode_aired_recently(self):
        now = utc_now()
        show = Monitored(
            id=4,
            anilist_id=404,
            display_name="Yomi no Tsugai",
            aliases_json='["Yomi no Tsugai"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=1,
            last_confirmed_episode=None,  # Ep 22 not confirmed yet
            next_airing_episode=23,
            next_airing_at=now + timedelta(days=7) - timedelta(hours=3),  # Previous episode aired 3 hours ago
        )
        self.session.add(show)
        self.session.commit()

        dur, reason = calculate_next_poll_interval(self.session, default_interval_seconds=21600, hunting_interval_seconds=300)
        self.assertEqual(dur, 300)
        self.assertIn("Hunting mode", reason)

    def test_no_hunting_when_unconfirmed_show_next_episode_is_days_away_and_no_recent_air(self):
        now = utc_now()
        show = Monitored(
            id=5,
            anilist_id=505,
            display_name="Future Premiere Anime",
            aliases_json='["Future Premiere Anime"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=1,
            last_confirmed_episode=None,
            next_airing_episode=1,
            next_airing_at=now + timedelta(days=5),
        )
        self.session.add(show)
        self.session.commit()

        dur, reason = calculate_next_poll_interval(self.session, default_interval_seconds=21600, hunting_interval_seconds=300)
        self.assertEqual(dur, 21600)
        self.assertNotIn("Hunting mode", reason)


if __name__ == "__main__":
    unittest.main()
