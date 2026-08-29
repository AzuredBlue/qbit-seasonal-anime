import unittest
from qbit_seasonal_anime.core.matching import (
    calculate_match_score,
    match_release_to_show,
    normalize_title,
    parse_release_title,
)
from tests.fixtures import SAMPLE_TORRENT_RELEASES


class TestMatching(unittest.TestCase):
    def test_normalize_title(self):
        self.assertEqual(normalize_title("Frieren: Beyond Journey's End"), "frieren beyond journey s end")
        self.assertEqual(normalize_title("[SubsPlease] Sousou no Frieren - 08"), "sousou no frieren 08")
        self.assertEqual(normalize_title("Ore dake Level Up na Ken (TV)"), "ore dake level up na ken")

    def test_parse_release_title(self):
        item = "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv"
        parsed = parse_release_title(item)
        self.assertEqual(parsed["release_group"], "SubsPlease")
        self.assertEqual(parsed["episode"], 8)
        self.assertIn("sousou no frieren", parsed["title"].lower())

    def test_match_release_to_show_positive(self):
        aliases = [
            "Sousou no Frieren",
            "Frieren: Beyond Journey's End",
            "Frieren",
            "葬送のフリーレン",
        ]
        release_1 = "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv"
        is_match, score, parsed = match_release_to_show(release_1, aliases)
        self.assertTrue(is_match)
        self.assertGreaterEqual(score, 85.0)
        self.assertEqual(parsed["episode"], 8)

        release_2 = "[Erai-raws] Frieren - Beyond Journey's End - 08 [1080p][Multiple Subtitle].mkv"
        is_match, score, parsed = match_release_to_show(release_2, aliases)
        self.assertTrue(is_match)
        self.assertGreaterEqual(score, 85.0)

    def test_match_release_to_show_negative(self):
        aliases = ["Sousou no Frieren", "Frieren"]
        unrelated = "[SubsPlease] Some Completely Different Anime - 08 (1080p) [12345678].mkv"
        is_match, score, _ = match_release_to_show(unrelated, aliases)
        self.assertFalse(is_match)
        self.assertLess(score, 70.0)

    def test_multi_season_matching(self):
        aliases = ["Solo Leveling Season 2", "Ore dake Level Up na Ken Season 2", "Solo Leveling"]
        release = "[SubsPlease] Ore dake Level Up na Ken Season 2 - 04 (1080p) [2B3C4D5E].mkv"
        is_match, score, parsed = match_release_to_show(release, aliases)
        self.assertTrue(is_match)
        self.assertEqual(parsed["episode"], 4)

    def test_parse_release_title_multiword_quality_stripping(self):
        # Multi-word titles where quality tag is at the end
        item = "Ore dake Level Up na Ken 1080p.mkv"
        parsed = parse_release_title(item)
        self.assertEqual(parsed["title"], "Ore dake Level Up na Ken")

        item2 = "[SubsPlease] Shangri-La Frontier S02E14 1080p WEB-DL.mkv"
        parsed2 = parse_release_title(item2)
        self.assertEqual(parsed2["season"], 2)
        self.assertEqual(parsed2["episode"], 14)
        self.assertEqual(parsed2["title"], "Shangri-La Frontier")

    def test_extract_release_group_avoids_digits(self):
        from qbit_seasonal_anime.core.matching import extract_release_group_tag
        self.assertIsNone(extract_release_group_tag("Show_Name-01.mkv"))
        self.assertEqual(extract_release_group_tag("[SubsPlease] Show - 01.mkv"), "SubsPlease")
        self.assertEqual(extract_release_group_tag("Show S01E05 1080p-VARYG.mkv"), "VARYG")

    def test_parse_release_title_audio_channels_and_fps(self):
        # Audio channels like 2.0 or 5.1 must not be mistaken for episode numbers
        p1 = parse_release_title("[Hentai] Ushiro no Shoumen Kamui-san - 04 [WEB 1080p DDP 2.0. H 264] (Uncensored)")
        self.assertEqual(p1["episode"], 4)
        self.assertEqual(p1["release_group"], "Hentai")
        self.assertEqual(p1["title"], "Ushiro no Shoumen Kamui-san")

        # High framerate specs must not be mistaken for episode numbers
        p2 = parse_release_title("[Raze] Youjo Senki S2 - 08 x265 10bit 1080p 143.8561fps.mkv")
        self.assertEqual(p2["season"], 2)
        self.assertEqual(p2["episode"], 8)
        self.assertEqual(p2["release_group"], "Raze")
        self.assertEqual(p2["title"], "Youjo Senki")

    def test_parse_release_title_years_and_dual_notation(self):
        # 4-digit years in movie/batch titles must not be mistaken for episode numbers
        p1 = parse_release_title("Gintama Yoshiwara in Flames 2026 1080p NF WEB-DL DUAL DDP5.1 H.264-VARYG (Shin Gintama Movie: Yoshiwara Daienjou, Dual-Audio)")
        self.assertIsNone(p1["episode"])
        self.assertEqual(p1["release_group"], "VARYG")
        self.assertEqual(p1["title"], "Gintama Yoshiwara in Flames")

        # Dual episode notations like "Title - 38 (S01E38)"
        p2 = parse_release_title("[Lazyleido-Mini] DIGIMON BEATBREAK - 38 (S01E38) - (WEB 1080p AV1 10-bit AAC 2.0) [F949F3F5]")
        self.assertEqual(p2["season"], 1)
        self.assertEqual(p2["episode"], 38)
        self.assertEqual(p2["release_group"], "Lazyleido-Mini")
        self.assertEqual(p2["title"], "DIGIMON BEATBREAK")

        # Long titles with numbers in the middle
        p3 = parse_release_title("[Erai-raws] Koko wa Ore ni Makasete Saki ni Ike to Itte kara 10-nen ga Tattara Densetsu ni Natteita - 09 [1080p CR WEBRip HEVC AAC][MultiSub][1435F93B]")
        self.assertEqual(p3["episode"], 9)
        self.assertEqual(p3["release_group"], "Erai-raws")
        self.assertEqual(p3["title"], "Koko wa Ore ni Makasete Saki ni Ike to Itte kara 10-nen ga Tattara Densetsu ni Natteita")


if __name__ == "__main__":
    unittest.main()
