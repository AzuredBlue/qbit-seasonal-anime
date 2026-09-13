from datetime import datetime, timezone
import pytest
from unittest.mock import AsyncMock, patch
from qbit_seasonal_anime.clients.anilist import AniListClient, get_current_and_next_season
from tests.fixtures import MOCK_ANILIST_SEASONAL_RESPONSE


def test_season_calculation():
    winter_dt = datetime(2026, 2, 15, tzinfo=timezone.utc)
    cur, nxt = get_current_and_next_season(winter_dt)
    assert cur == ("WINTER", 2026)
    assert nxt == ("SPRING", 2026)

    fall_dt = datetime(2026, 11, 10, tzinfo=timezone.utc)
    cur, nxt = get_current_and_next_season(fall_dt)
    assert cur == ("FALL", 2026)
    assert nxt == ("WINTER", 2027)


@pytest.mark.asyncio
async def test_fetch_user_seasonal_anime_filtering():
    client = AniListClient()

    # Response with 1 releasing, 1 planned next season, and 1 old finished show
    payload = {
        "MediaListCollection": {
            "lists": [
                {
                    "name": "Planning",
                    "entries": [
                        {
                            "media": {
                                "id": 1,
                                "title": {"romaji": "Currently Airing Show"},
                                "status": "RELEASING",
                                "nextAiringEpisode": {"episode": 3, "airingAt": 1700000000},
                            }
                        },
                        {
                            "media": {
                                "id": 2,
                                "title": {"romaji": "Next Season Upcoming Show"},
                                "status": "NOT_YET_RELEASED",
                                "season": "SPRING",
                                "seasonYear": 2026,
                            }
                        },
                        {
                            "media": {
                                "id": 3,
                                "title": {"romaji": "Old Finished Backlog Show"},
                                "status": "FINISHED",
                                "season": "FALL",
                                "seasonYear": 2015,
                                "nextAiringEpisode": None,
                            }
                        },
                        {
                            "media": {
                                "id": 4,
                                "title": {"romaji": "Far Future Planned Show (2+ Seasons Away)"},
                                "status": "NOT_YET_RELEASED",
                                "season": "WINTER",
                                "seasonYear": 2027,
                                "nextAiringEpisode": {"episode": 1, "airingAt": 1800000000},
                            }
                        },
                    ]
                }
            ]
        }
    }

    with patch.object(client, "_post_query", new_callable=AsyncMock) as mock_post, \
         patch("qbit_seasonal_anime.clients.anilist.get_current_and_next_season", return_value=(("WINTER", 2026), ("SPRING", 2026))):
        mock_post.return_value = payload

        shows = await client.fetch_user_seasonal_anime("TestUser")
        # Should only include show 1 (currently releasing) and show 2 (upcoming next season),
        # strictly filtering out show 3 (finished) and show 4 (future season beyond next season)!
        assert len(shows) == 2
        assert {s["anilist_id"] for s in shows} == {1, 2}


@pytest.mark.asyncio
async def test_fetch_user_seasonal_anime_includes_current_season_finished_and_monitored():
    client = AniListClient()

    payload = {
        "MediaListCollection": {
            "lists": [
                {
                    "name": "Watching",
                    "entries": [
                        {
                            "media": {
                                "id": 10,
                                "title": {"romaji": "Summer Finished Show"},
                                "status": "FINISHED",
                                "season": "SUMMER",
                                "seasonYear": 2026,
                                "episodes": 12,
                                "nextAiringEpisode": None,
                            }
                        },
                        {
                            "media": {
                                "id": 20,
                                "title": {"romaji": "Extending Cour from Spring now Finished"},
                                "status": "FINISHED",
                                "season": "SPRING",
                                "seasonYear": 2026,
                                "episodes": 24,
                                "nextAiringEpisode": None,
                            }
                        },
                        {
                            "media": {
                                "id": 30,
                                "title": {"romaji": "Unmonitored Old Spring Backlog"},
                                "status": "FINISHED",
                                "season": "SPRING",
                                "seasonYear": 2026,
                                "episodes": 12,
                                "nextAiringEpisode": None,
                            }
                        },
                    ]
                }
            ]
        }
    }

    with patch.object(client, "_post_query", new_callable=AsyncMock) as mock_post, \
         patch("qbit_seasonal_anime.clients.anilist.get_current_and_next_season", return_value=(("SUMMER", 2026), ("FALL", 2026))):
        mock_post.return_value = payload

        # Monitored IDs has 20 (extending cour), but not 30
        shows = await client.fetch_user_seasonal_anime("TestUser", monitored_anilist_ids={20})
        # Should include:
        # - Show 10: Current season finished (SUMMER 2026)
        # - Show 20: Tracked monitored extending cour finished (SPRING 2026)
        # Should exclude:
        # - Show 30: Unmonitored old spring backlog
        assert len(shows) == 2
        assert {s["anilist_id"] for s in shows} == {10, 20}

