import unittest
from unittest.mock import MagicMock
from sqlmodel import Session, create_engine, SQLModel, select
from qbit_seasonal_anime.core.confirmation import verify_and_confirm_torrents
from qbit_seasonal_anime.db.models import Feed, MatchHistory, Monitored, MonitoredStatus, RuleHistory, RuleOutcome, Settings, utc_now


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
        call_order = []
        mock_qbit.set_rss_rule.side_effect = lambda **kwargs: call_order.append("rule")
        mock_received_time = utc_now()
        rule_name = "[Seasonal] Sousou no Frieren"
        release_title = "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv"
        mock_qbit.get_rule_match_times.side_effect = lambda pairs: (
            call_order.append("lookup") or {(rule_name, release_title): mock_received_time}
        )
        mock_qbit.get_rss_items.return_value = {
            "SubsPlease": {
                "url": "https://subsplease.org/rss/?r=1080",
                "articles": [
                    {
                        "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv",
                        "torrentURL": "https://subs/8.torrent",
                        "date": "03 Sep 2026 12:00:00 +0000",
                    }
                ]
            }
        }

        logs = verify_and_confirm_torrents(self.session, mock_qbit, self.settings)
        self.session.close()
        self.session = Session(self.engine)
        self.show = self.session.get(Monitored, 1)
        self.hist = self.session.get(RuleHistory, 1)

        self.assertEqual(self.show.status, MonitoredStatus.FIXED)
        self.assertEqual(self.show.last_confirmed_episode, 8)
        self.assertEqual(self.hist.outcome, RuleOutcome.CONFIRMED)
        self.assertTrue(any("Confirmed rule" in log for log in logs))
        mock_qbit.get_rule_match_times.assert_called_once()
        mock_qbit.get_rule_match_time.assert_not_called()
        self.assertEqual(call_order, ["rule", "lookup"])

        m_hist = self.session.exec(select(MatchHistory)).all()
        self.assertEqual(len(m_hist), 1)
        self.assertEqual(m_hist[0].release_title, "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv")
        diff_sec = abs((utc_now().replace(tzinfo=None) - m_hist[0].created_at).total_seconds())
        self.assertLess(diff_sec, 10)

    def test_confirmation_falls_back_when_batch_api_is_unavailable(self):
        mock_qbit = MagicMock()
        rule_name = "[Seasonal] Sousou no Frieren"
        release_title = "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv"
        mock_qbit.get_rule_match_times = None
        mock_qbit.get_rule_match_time.return_value = utc_now()
        mock_qbit.get_rss_items.return_value = {
            "SubsPlease": {
                "url": "https://subsplease.org/rss/?r=1080",
                "articles": [
                    {
                        "title": release_title,
                        "torrentURL": "https://subs/8.torrent",
                        "date": "03 Sep 2026 12:00:00 +0000",
                    }
                ],
            }
        }

        verify_and_confirm_torrents(self.session, mock_qbit, self.settings)

        self.assertIsNone(mock_qbit.get_rule_match_times)
        mock_qbit.get_rule_match_time.assert_called_once_with(
            rule_name=rule_name,
            release_title=release_title,
        )

    def test_confirmation_avoids_legacy_retry_storm_after_total_batch_failure(self):
        mock_qbit = MagicMock()
        release_title = "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv"
        mock_qbit.get_rule_match_times.return_value = None
        mock_qbit.get_rss_items.return_value = {
            "SubsPlease": {
                "url": "https://subsplease.org/rss/?r=1080",
                "articles": [
                    {
                        "title": release_title,
                        "torrentURL": "https://subs/8.torrent",
                        "date": "03 Sep 2026 12:00:00 +0000",
                    }
                ],
            }
        }

        verify_and_confirm_torrents(self.session, mock_qbit, self.settings)

        mock_qbit.get_rule_match_times.assert_called_once()
        mock_qbit.get_rule_match_time.assert_not_called()

    def test_qbit_match_time_lookup_batches_log_and_rules_requests(self):
        from qbit_seasonal_anime.clients.qbit import QBitClient

        client = QBitClient(host="http://localhost:8080")
        underlying = MagicMock()
        underlying.log_main.return_value = [
            MagicMock(spec=["timestamp"], timestamp=1700000001),
            MagicMock(message="RSS article release-a is accepted by rule rule-a", timestamp="invalid"),
            MagicMock(message="RSS article release-a is accepted by rule rule-a", timestamp=1699999999),
            MagicMock(message="RSS article release-b is accepted by rule rule-b", timestamp=1700000000),
        ]
        underlying.rss_rules.return_value = {
            "rule-c": {"lastMatch": "03 Sep 2026 12:00:00 +0000"},
        }
        client.get_client = MagicMock(return_value=underlying)

        result = client.get_rule_match_times([
            ("rule-a", "release-a"),
            ("rule-b", "release-b"),
            ("rule-c", "release-c"),
        ])

        assert result is not None
        self.assertIsNotNone(result[("rule-a", "release-a")])
        self.assertIsNotNone(result[("rule-b", "release-b")])
        self.assertIsNotNone(result[("rule-c", "release-c")])
        underlying.log_main.assert_called_once()
        underlying.rss_rules.assert_called_once()

    def test_qbit_match_time_batch_reports_total_source_failure(self):
        from qbit_seasonal_anime.clients.qbit import QBitClient

        client = QBitClient(host="http://localhost:8080")
        underlying = MagicMock()
        underlying.log_main.side_effect = RuntimeError("logs unavailable")
        underlying.rss_rules.side_effect = RuntimeError("rules unavailable")
        client.get_client = MagicMock(return_value=underlying)

        result = client.get_rule_match_times([("rule-a", "release-a")])

        self.assertIsNone(result)
        underlying.log_main.assert_called_once()
        underlying.rss_rules.assert_called_once()

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

        self.assertEqual(self.show.status, MonitoredStatus.UNCONFIRMED)


if __name__ == "__main__":
    unittest.main()
