import re
import unittest
from qbit_seasonal_anime.core.rules import (
    build_regex_pattern,
    build_rule_definition,
    build_rule_name,
    sanitize_folder_name,
)
from qbit_seasonal_anime.db.models import Monitored, MonitoredStatus


class TestRules(unittest.TestCase):
    def test_sanitize_folder_name(self):
        self.assertEqual(sanitize_folder_name("Ranma 1/2 (2024) 3rd Season"), "Ranma 1-2 (2024) 3rd Season")
        self.assertEqual(sanitize_folder_name("Fate/stay night [Heaven's Feel]"), "Fate-stay night [Heaven's Feel]")
        self.assertEqual(sanitize_folder_name("BLEACH: Sennen Kessen-hen - Kashin-tan"), "BLEACH - Sennen Kessen-hen - Kashin-tan")
        self.assertEqual(sanitize_folder_name('Anime With: "Quotes" & <Brackets>? *'), "Anime With - Quotes & Brackets")
        self.assertEqual(sanitize_folder_name("Re:Zero kara Hajimeru Isekai Seikatsu"), "Re - Zero kara Hajimeru Isekai Seikatsu")
        self.assertEqual(sanitize_folder_name(""), "Anime")

    def test_build_regex_pattern_broad(self):
        aliases = ["Sousou no Frieren", "Frieren: Beyond Journey's End"]
        pattern = build_regex_pattern(aliases)
        self.assertTrue(pattern.startswith("("))
        self.assertTrue(re.search(pattern, "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv", re.IGNORECASE))
        self.assertTrue(re.search(pattern, "[Erai-raws] Frieren: Beyond Journey's End - 08.mkv", re.IGNORECASE))
        self.assertFalse(re.search(pattern, "[SubsPlease] Dandadan - 01.mkv", re.IGNORECASE))

    def test_build_regex_pattern_simple_matched_title(self):
        pattern = build_regex_pattern(
            aliases=["Mushoku Tensei: Isekai Ittara Honki Dasu 3rd Season", "Mushoku Tensei S3"],
            matched_title="Mushoku Tensei S3",
        )
        self.assertEqual(pattern, r"Mushoku\s+Tensei\s+S3")
        self.assertTrue(re.search(pattern, "[SubsPlease] Mushoku Tensei S3 - 09 (1080p) [DDF202A0].mkv", re.IGNORECASE))
        self.assertTrue(re.search(pattern, "Mushoku Tensei S3 - 09 (1080p) [Varyg].mkv", re.IGNORECASE))
        self.assertTrue(re.search(pattern, "[Erai-raws] Mushoku Tensei S3 - 09 (1080p).mkv", re.IGNORECASE))
        self.assertFalse(re.search(pattern, "[SubsPlease] Bleach - 45.mkv", re.IGNORECASE))

    def test_build_rule_definition(self):
        show = Monitored(
            id=1,
            anilist_id=154587,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren", "Frieren"]',
            save_folder="Sousou no Frieren",
            status=MonitoredStatus.FIXED,
        )
        rule_def = build_rule_definition(
            monitored=show,
            feed_url="https://subsplease.org/rss/?r=1080",
            base_dir="~/Anime",
            category="",
            ratio_limit=1.0,
        )
        self.assertTrue(rule_def["enabled"])
        self.assertTrue(rule_def["useRegex"])
        self.assertFalse(rule_def["smartFilter"])
        self.assertIn("720p", rule_def["mustNotContain"])
        self.assertIn("batch", rule_def["mustNotContain"])
        self.assertEqual(rule_def["affectedFeeds"], ["https://subsplease.org/rss/?r=1080"])
        self.assertEqual(rule_def["assignedCategory"], "")
        self.assertEqual(rule_def["ratioLimit"], 1.0)
        self.assertEqual(rule_def["torrentParams"]["ratio_limit"], 1.0)
        self.assertEqual(rule_def["torrentParams"]["category"], "")
        self.assertTrue(rule_def["savePath"].endswith("Sousou no Frieren"))

        # Test unconfirmed upcoming show is disabled
        upcoming_show = Monitored(
            id=2,
            anilist_id=999,
            display_name="Upcoming Anime",
            aliases_json='["Upcoming Anime"]',
            status=MonitoredStatus.UNCONFIRMED,
        )
        upcoming_def = build_rule_definition(
            monitored=upcoming_show,
            feed_url="https://subsplease.org/rss/?r=1080",
            base_dir="~/Anime",
        )
        self.assertFalse(upcoming_def["enabled"])

    def test_build_rule_name(self):
        name = build_rule_name(42, "Frieren: Beyond Journey's End")
        self.assertEqual(name, "[Seasonal] Frieren - Beyond Journey's End")

    def test_generate_season_variants_does_not_corrupt_pronoun_i(self):
        from qbit_seasonal_anime.core.rules import generate_season_variants
        variants = generate_season_variants("I Was Reincarnated as a Slime")
        # Should not strip "I" or generate "Season 1" variants from pronoun "I"
        self.assertEqual(variants, ["I Was Reincarnated as a Slime"])

        # Roman numerals II and III should still expand
        v2 = generate_season_variants("Mushoku Tensei III")
        self.assertIn("Mushoku Tensei S3", v2)

    def test_monitored_aliases_property_safe_json(self):
        show = Monitored(anilist_id=999, display_name="Test", aliases_json="invalid json")
        self.assertEqual(show.aliases, [])

        show2 = Monitored(anilist_id=998, display_name="Test 2", aliases_json='"not a list"')
        self.assertEqual(show2.aliases, [])

    def test_create_or_update_rule_ensures_category(self):
        from unittest.mock import MagicMock
        from qbit_seasonal_anime.core.rules import create_or_update_rule
        from qbit_seasonal_anime.db.models import Feed

        mock_qbit = MagicMock()
        mock_qbit.get_matching_articles.return_value = {"feed_url": ["[SubsPlease] Frieren - 08.mkv"]}
        show = Monitored(id=1, anilist_id=101, display_name="Frieren", aliases_json='["Frieren"]')
        feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss")

        rule_name = create_or_update_rule(
            qbit_client=mock_qbit,
            monitored=show,
            feed=feed,
            base_dir="~/Anime",
            category="Anime",
            ratio_limit=1.0,
        )
        self.assertEqual(rule_name, "[Seasonal] Frieren")
        mock_qbit.ensure_category_exists.assert_called_with("Anime")
        mock_qbit.set_rss_rule.assert_called_once()
        mock_qbit.get_matching_articles.assert_called_with("[Seasonal] Frieren")

    def test_resolve_save_path_placeholders(self):
        from qbit_seasonal_anime.core.rules import resolve_save_path
        import os
        home = os.path.expanduser("~")

        # 1. Base template with {name}
        p1 = resolve_save_path("~/Anime/{name}", "BLEACH: Sennen Kessen-hen")
        self.assertEqual(p1, f"{home}/Anime/BLEACH - Sennen Kessen-hen")

        # 2. Base template without {name} -> automatically filled in as subfolder
        p2 = resolve_save_path("~/Anime", "Sousou no Frieren")
        self.assertEqual(p2, f"{home}/Anime/Sousou no Frieren")

        # 3. Custom show path with {name}
        p3 = resolve_save_path("~/Anime/{name}", "Dandadan", custom_save_folder="~/Downloads/{name}")
        self.assertEqual(p3, f"{home}/Downloads/Dandadan")

        # 4. Custom show relative folder name
        p4 = resolve_save_path("~/Anime/{name}", "Aoashi", custom_save_folder="Aoashi 2nd Season")
        self.assertEqual(p4, f"{home}/Anime/Aoashi 2nd Season")


if __name__ == "__main__":
    unittest.main()
