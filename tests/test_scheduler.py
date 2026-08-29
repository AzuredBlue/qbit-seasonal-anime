import unittest
from datetime import timedelta
from sqlmodel import Session, SQLModel, create_engine
from qbit_seasonal_anime.db.models import Feed, Monitored, MonitoredStatus, utc_now
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
        # Even if a working show has an episode airing soon, qBit handles it, so we sleep default interval
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

    def test_no_hunting_for_show_without_release_date(self):
        # Shows like Aoashi 2nd Season without an air date should NOT trigger hunting mode
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
        self.assertIn("Checking every 5m 15s", reason)


if __name__ == "__main__":
    unittest.main()
