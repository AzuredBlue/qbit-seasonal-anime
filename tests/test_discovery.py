import unittest
from unittest.mock import MagicMock
from qbit_seasonal_anime.core.discovery import discover_feed_for_show, flatten_rss_articles
from qbit_seasonal_anime.db.models import Feed, Monitored
from tests.fixtures import MOCK_QBIT_RSS_ITEMS


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.feed_top = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss/?r=1080", priority=1)
        self.feed_second = Feed(id=2, qbit_feed_name="Erai-raws", qbit_feed_url="https://www.erai-raws.info/rss-1080p/", priority=2)
        self.feeds = [self.feed_second, self.feed_top]  # Unordered to test priority sorting

    def test_flatten_rss_articles(self):
        articles_by_url = flatten_rss_articles(MOCK_QBIT_RSS_ITEMS)
        self.assertIn("https://subsplease.org/rss/?r=1080", articles_by_url)
        self.assertEqual(len(articles_by_url["https://subsplease.org/rss/?r=1080"]), 2)

    def test_discover_feed_with_matching_cache(self):
        mock_qbit = MagicMock()
        mock_qbit.get_rss_items.return_value = MOCK_QBIT_RSS_ITEMS

        show = Monitored(
            id=1,
            anilist_id=154587,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren", "Frieren"]',
        )

        res = discover_feed_for_show(show, self.feeds, mock_qbit)
        self.assertIsNotNone(res)
        feed, group, matched_title = res
        # Should pick SubsPlease (priority 1) since both have it or SubsPlease is top
        self.assertEqual(feed.id, 1)
        self.assertEqual(group, "SubsPlease")
        self.assertEqual(matched_title, "Sousou no Frieren")

    def test_discover_feed_with_empty_cache_returns_none(self):
        mock_qbit = MagicMock()
        mock_qbit.get_rss_items.return_value = {}

        show = Monitored(
            id=2,
            anilist_id=999999,
            display_name="Unreleased Brand New Anime",
            aliases_json='["Unreleased Brand New Anime"]',
        )

        res = discover_feed_for_show(show, self.feeds, mock_qbit)
        self.assertIsNone(res)

    def test_discover_feed_with_exclusion(self):
        mock_qbit = MagicMock()
        mock_qbit.get_rss_items.return_value = MOCK_QBIT_RSS_ITEMS

        show = Monitored(
            id=1,
            anilist_id=154587,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren", "Frieren: Beyond Journey\'s End"]',
        )

    def test_discover_feed_lower_priority_waits_for_grace_period_and_triggers_refresh(self):
        from datetime import datetime, timezone, timedelta
        mock_qbit = MagicMock()
        # Feed 2 has release, Feed 1 does not
        mock_qbit.get_rss_items.return_value = {
            "Erai-raws": {
                "url": "https://www.erai-raws.info/rss-1080p/",
                "articles": [
                    {"title": "[Erai-raws] Fast Anime - 01 [1080p].mkv", "torrentURL": "https://erai/1.torrent"}
                ]
            },
            "SubsPlease": {
                "url": "https://subsplease.org/rss/?r=1080",
                "articles": []
            }
        }

        # Show aired only 1 minute ago (< 5 minute buffer)
        show = Monitored(
            id=3,
            anilist_id=55555,
            display_name="Fast Anime",
            aliases_json='["Fast Anime"]',
            next_airing_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )

        res = discover_feed_for_show(show, self.feeds, mock_qbit, preferred_feed_grace_seconds=300)
        # Should return None (waiting on Feed 1 grace window) and trigger manual refresh
        self.assertIsNone(res)
        mock_qbit.refresh_rss_feeds.assert_called()

    def test_discover_feed_lower_priority_fallback_after_grace_period(self):
        from datetime import datetime, timezone, timedelta
        mock_qbit = MagicMock()
        mock_qbit.get_rss_items.return_value = {
            "Erai-raws": {
                "url": "https://www.erai-raws.info/rss-1080p/",
                "articles": [
                    {"title": "[Erai-raws] Slower Show - 01 [1080p].mkv", "torrentURL": "https://erai/1.torrent"}
                ]
            },
            "SubsPlease": {
                "url": "https://subsplease.org/rss/?r=1080",
                "articles": []
            }
        }

        # Show aired 10 minutes ago (> 5 minute buffer)
        show = Monitored(
            id=4,
            anilist_id=66666,
            display_name="Slower Show",
            aliases_json='["Slower Show"]',
            next_airing_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )

        res = discover_feed_for_show(show, self.feeds, mock_qbit, preferred_feed_grace_seconds=300)
        # Should fallback to Feed 2 (Erai-Raws)
        self.assertIsNotNone(res)
        feed, group, matched_title = res
        self.assertEqual(feed.id, 2)
        self.assertEqual(feed.qbit_feed_name, "Erai-raws")


if __name__ == "__main__":
    unittest.main()
