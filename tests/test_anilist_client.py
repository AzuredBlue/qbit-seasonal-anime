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
                    ]
                }
            ]
        }
    }

    with patch.object(client, "_post_query", new_callable=AsyncMock) as mock_post, \
         patch("qbit_seasonal_anime.clients.anilist.get_current_and_next_season", return_value=(("WINTER", 2026), ("SPRING", 2026))):
        mock_post.return_value = payload

        shows = await client.fetch_user_seasonal_anime("TestUser")
        # Should only include show 1 and show 2, filtering out show 3!
        assert len(shows) == 2
        assert {s["anilist_id"] for s in shows} == {1, 2}
