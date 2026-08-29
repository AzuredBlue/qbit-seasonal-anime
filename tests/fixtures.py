from datetime import datetime, timedelta, timezone

def test_utc_now():
    return datetime.now(timezone.utc)

SAMPLE_TORRENT_RELEASES = [
    "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv",
    "[Erai-raws] Frieren - Beyond Journey's End - 08 [1080p][Multiple Subtitle].mkv",
    "[SubsPlease] Dandadan - 01 (1080p) [4A1C8F2E].mkv",
    "[Erai-raws] Dandadan - 01 [720p][Multiple Subtitle].mkv",
    "[SubsPlease] Ore dake Level Up na Ken Season 2 - 04 (1080p) [2B3C4D5E].mkv",
    "[SubsPlease] Solo Leveling Season 2 - 04 (1080p) [2B3C4D5E].mkv",
    "[SubsPlease] Shingeki no Kyojin The Final Season Part 3 - 01 (1080p) [8F9A1B2C].mkv",
    "[SubsPlease] Bleach - Sennen Kessen-hen - Soukoku-tan - 27 (1080p) [A1B2C3D4].mkv",
    # Unrelated / potential false positive triggers:
    "[SubsPlease] Some Completely Different Anime - 08 (1080p) [12345678].mkv",
    "[SubsPlease] Frieren Special Mini Anime - 01 (1080p) [AABBCCDD].mkv",
]

MOCK_ANILIST_SEASONAL_RESPONSE = {
    "data": {
        "MediaListCollection": {
            "lists": [
                {
                    "name": "Watching",
                    "status": "CURRENT",
                    "entries": [
                        {
                            "id": 1001,
                            "status": "CURRENT",
                            "progress": 7,
                            "media": {
                                "id": 154587,
                                "title": {
                                    "romaji": "Sousou no Frieren",
                                    "english": "Frieren: Beyond Journey's End",
                                    "native": "葬送のフリーレン",
                                    "userPreferred": "Sousou no Frieren",
                                },
                                "synonyms": [
                                    "Frieren at the Funeral",
                                    "Frieren",
                                ],
                                "format": "TV",
                                "status": "RELEASING",
                                "episodes": 28,
                                "nextAiringEpisode": {
                                    "airingAt": int((test_utc_now() + timedelta(days=2)).timestamp()),
                                    "timeUntilAiring": 172800,
                                    "episode": 8,
                                },
                                "season": "FALL",
                                "seasonYear": 2023,
                                "coverImage": {
                                    "large": "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx154587.jpg"
                                },
                            },
                        },
                        {
                            "id": 1002,
                            "status": "CURRENT",
                            "progress": 0,
                            "media": {
                                "id": 171018,
                                "title": {
                                    "romaji": "Dandadan",
                                    "english": "DAN DA DAN",
                                    "native": "ダンダダン",
                                    "userPreferred": "Dandadan",
                                },
                                "synonyms": [],
                                "format": "TV",
                                "status": "RELEASING",
                                "episodes": 12,
                                "nextAiringEpisode": {
                                    "airingAt": int((test_utc_now() - timedelta(hours=48)).timestamp()),
                                    "timeUntilAiring": -172800,
                                    "episode": 1,
                                },
                                "season": "FALL",
                                "seasonYear": 2024,
                                "coverImage": {
                                    "large": "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx171018.jpg"
                                },
                            },
                        },
                    ],
                }
            ]
        }
    }
}

MOCK_QBIT_RSS_ITEMS = {
    "SubsPlease": {
        "url": "https://subsplease.org/rss/?r=1080",
        "articles": [
            {
                "id": "1",
                "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv",
                "torrentURL": "https://subsplease.org/download/1",
            },
            {
                "id": "2",
                "title": "[SubsPlease] Dandadan - 01 (1080p) [4A1C8F2E].mkv",
                "torrentURL": "https://subsplease.org/download/2",
            },
        ],
    },
    "Erai-raws": {
        "url": "https://www.erai-raws.info/rss-1080p/",
        "articles": [
            {
                "id": "3",
                "title": "[Erai-raws] Frieren - Beyond Journey's End - 08 [1080p][Multiple Subtitle].mkv",
                "torrentURL": "https://erai-raws.info/download/3",
            }
        ],
    },
}
