import unittest
from unittest.mock import MagicMock
from qbit_seasonal_anime.clients.qbit import QbitRSSRefreshError
from qbit_seasonal_anime.core.discovery import RssSnapshot, discover_feed_for_show, flatten_rss_articles
from qbit_seasonal_anime.db.models import Feed, Monitored
from tests.fixtures import MOCK_QBIT_RSS_ITEMS


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.feed_top = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss/?r=1080", priority=1)
        self.feed_second = Feed(id=2, qbit_feed_name="Erai-raws", qbit_feed_url="https://www.erai-raws.info/rss-1080p/", priority=2)
        self.feeds = [self.feed_second, self.feed_top]  # Unordered to test priority sorting

    def test_rss_snapshot_reuses_data_and_reloads_after_refresh(self):
        mock_qbit = MagicMock()
        mock_qbit.get_rss_items.return_value = MOCK_QBIT_RSS_ITEMS
        snapshot = RssSnapshot(mock_qbit)

        first = snapshot.get()
        second = snapshot.get()
        self.assertIs(first, second)
        mock_qbit.get_rss_items.assert_called_once()

        mock_qbit.refresh_rss_feeds.return_value = True
        snapshot.refresh(poll_interval_seconds=0, timeout_seconds=1)
        self.assertEqual(mock_qbit.get_rss_items.call_count, 3)
        mock_qbit.refresh_rss_feeds.assert_called_once()
        snapshot.get()
        self.assertEqual(mock_qbit.get_rss_items.call_count, 3)

    def test_rss_snapshot_caches_failed_load_for_cycle(self):
        from qbit_seasonal_anime.clients.qbit import QbitClientError

        mock_qbit = MagicMock()
        mock_qbit.get_rss_items.side_effect = QbitClientError("RSS unavailable")
        snapshot = RssSnapshot(mock_qbit)

        with self.assertRaises(QbitClientError):
            snapshot.get()
        with self.assertRaises(QbitClientError):
            snapshot.get()
        mock_qbit.get_rss_items.assert_called_once()

        snapshot.invalidate()
        with self.assertRaises(QbitClientError):
            snapshot.get()
        self.assertEqual(mock_qbit.get_rss_items.call_count, 2)

    def test_rss_snapshot_caches_refresh_failure(self):
        mock_qbit = MagicMock()
        mock_qbit.get_rss_items.return_value = MOCK_QBIT_RSS_ITEMS
        mock_qbit.refresh_rss_feeds.return_value = False
        snapshot = RssSnapshot(mock_qbit)
        snapshot.get()

        with self.assertRaises(QbitRSSRefreshError):
            snapshot.refresh(max_attempts=1, poll_interval_seconds=0, timeout_seconds=1)
        with self.assertRaises(QbitRSSRefreshError):
            snapshot.get()

        self.assertEqual(mock_qbit.get_rss_items.call_count, 2)

    def test_rss_snapshot_refresh_waits_for_existing_refresh_and_caches_result(self):
        mock_qbit = MagicMock()
        feed_url = self.feed_top.qbit_feed_url
        mock_qbit.get_rss_items.side_effect = [
            {
                "Feeds": {
                    "SubsPlease": {
                        "url": feed_url,
                        "isLoading": True,
                        "hasError": False,
                        "articles": [],
                    }
                }
            },
            {
                "Feeds": {
                    "SubsPlease": {
                        "url": feed_url,
                        "isLoading": False,
                        "hasError": False,
                        "articles": [],
                    }
                }
            },
            {
                "Feeds": {
                    "SubsPlease": {
                        "url": feed_url,
                        "isLoading": False,
                        "hasError": False,
                        "articles": [{"id": "ep8", "title": "Link Click S3 - 08"}],
                    }
                }
            },
        ]
        mock_qbit.refresh_rss_feeds.return_value = True
        snapshot = RssSnapshot(mock_qbit)

        articles = snapshot.refresh(poll_interval_seconds=0, timeout_seconds=1)

        self.assertEqual(articles[feed_url][0]["id"], "ep8")
        self.assertEqual(mock_qbit.get_rss_items.call_count, 3)
        self.assertIs(snapshot.get(), articles)
        self.assertEqual(mock_qbit.get_rss_items.call_count, 3)

    def test_rss_snapshot_refresh_retries_rejected_request(self):
        mock_qbit = MagicMock()
        feed_url = self.feed_top.qbit_feed_url
        settled = {
            "SubsPlease": {
                "url": feed_url,
                "isLoading": False,
                "hasError": False,
                "articles": [],
            }
        }
        fresh = {
            "SubsPlease": {
                "url": feed_url,
                "isLoading": False,
                "hasError": False,
                "articles": [{"id": "ep8", "title": "Link Click S3 - 08"}],
            }
        }
        mock_qbit.get_rss_items.side_effect = [settled, settled, fresh]
        mock_qbit.refresh_rss_feeds.side_effect = [False, True]
        snapshot = RssSnapshot(mock_qbit)

        articles = snapshot.refresh(max_attempts=2, poll_interval_seconds=0, timeout_seconds=1)

        self.assertEqual(articles[feed_url][0]["id"], "ep8")
        self.assertEqual(mock_qbit.refresh_rss_feeds.call_count, 2)

    def test_rss_snapshot_refresh_rejects_feed_errors(self):
        mock_qbit = MagicMock()
        feed_url = self.feed_top.qbit_feed_url
        mock_qbit.get_rss_items.side_effect = [
            {
                "SubsPlease": {
                    "url": feed_url,
                    "isLoading": False,
                    "hasError": False,
                    "articles": [],
                }
            },
            {
                "SubsPlease": {
                    "url": feed_url,
                    "isLoading": False,
                    "hasError": True,
                    "articles": [],
                }
            },
        ]
        mock_qbit.refresh_rss_feeds.return_value = True
        snapshot = RssSnapshot(mock_qbit)

        with self.assertRaises(QbitRSSRefreshError):
            snapshot.refresh(max_attempts=1, poll_interval_seconds=0, timeout_seconds=1)
        with self.assertRaises(QbitRSSRefreshError):
            snapshot.get()

        self.assertEqual(mock_qbit.get_rss_items.call_count, 2)

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

    def test_discover_feed_lower_priority_waits_for_grace_period_and_triggers_refresh(self):
        from datetime import datetime, timezone, timedelta
        mock_qbit = MagicMock()
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

        show = Monitored(
            id=3,
            anilist_id=55555,
            display_name="Fast Anime",
            aliases_json='["Fast Anime"]',
            next_airing_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )

        res = discover_feed_for_show(show, self.feeds, mock_qbit, preferred_feed_grace_seconds=300)
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

        show = Monitored(
            id=4,
            anilist_id=66666,
            display_name="Slower Show",
            aliases_json='["Slower Show"]',
            next_airing_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )

        res = discover_feed_for_show(show, self.feeds, mock_qbit, preferred_feed_grace_seconds=300)
        self.assertIsNotNone(res)
        feed, group, matched_title = res
        self.assertEqual(feed.id, 2)
        self.assertEqual(feed.qbit_feed_name, "Erai-raws")


if __name__ == "__main__":
    unittest.main()
