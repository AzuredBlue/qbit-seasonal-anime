import unittest
from unittest.mock import MagicMock
from sqlmodel import Session, create_engine, SQLModel
from qbit_seasonal_anime.core.confirmation import verify_and_confirm_torrents
from qbit_seasonal_anime.db.models import Feed, Monitored, MonitoredStatus, RuleHistory, RuleOutcome, Settings


class TestConfirmation(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

        self.settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime")
        self.session.add(self.settings)

        self.feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss/?r=1080", priority=1)
        self.session.add(self.feed)

        self.show = Monitored(
            id=1,
            anilist_id=154587,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren", "Frieren"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=1,
            save_folder="Sousou no Frieren",
        )
        self.session.add(self.show)

        self.hist = RuleHistory(
            id=1,
            monitored_id=1,
            feed_id=1,
            outcome=RuleOutcome.PENDING,
            note="Initial rule",
        )
        self.session.add(self.hist)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_confirmation_true_positive(self):
        mock_qbit = MagicMock()
        mock_qbit.get_rss_items.return_value = {
            "SubsPlease": {
                "url": "https://subsplease.org/rss/?r=1080",
                "articles": [
                    {"title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv", "torrentURL": "https://subs/8.torrent"}
                ]
            }
        }

        logs = verify_and_confirm_torrents(self.session, mock_qbit, self.settings)
        self.session.refresh(self.show)
        self.session.refresh(self.hist)

        # Status should transition to FIXED
        self.assertEqual(self.show.status, MonitoredStatus.FIXED)
        self.assertEqual(self.show.last_confirmed_episode, 8)
        self.assertEqual(self.hist.outcome, RuleOutcome.CONFIRMED)
        self.assertTrue(any("Confirmed rule" in log for log in logs))

    def test_confirmation_no_matching_article_remains_unconfirmed(self):
        mock_qbit = MagicMock()
        mock_qbit.get_rss_items.return_value = {
            "SubsPlease": {
                "url": "https://subsplease.org/rss/?r=1080",
                "articles": [
                    {"title": "[SubsPlease] Completely Unrelated Anime - 01 (1080p).mkv", "torrentURL": "https://subs/1.torrent"}
                ]
            }
        }

        logs = verify_and_confirm_torrents(self.session, mock_qbit, self.settings)
        self.session.refresh(self.show)

        # Should remain UNCONFIRMED
        self.assertEqual(self.show.status, MonitoredStatus.UNCONFIRMED)


if __name__ == "__main__":
    unittest.main()
