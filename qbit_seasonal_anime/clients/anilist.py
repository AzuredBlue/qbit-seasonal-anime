import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
import httpx2 as httpx

logger = logging.getLogger("qbit_seasonal_anime.clients.anilist")

ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"

USER_SEASONAL_QUERY = """
query ($userName: String) {
  MediaListCollection(userName: $userName, type: ANIME, status_in: [CURRENT, PLANNING]) {
    lists {
      name
      status
      entries {
        id
        status
        progress
        media {
          id
          title {
            romaji
            english
            native
            userPreferred
          }
          synonyms
          format
          status
          episodes
          nextAiringEpisode {
            airingAt
            timeUntilAiring
            episode
          }
          season
          seasonYear
          coverImage {
            large
            medium
          }
        }
      }
    }
  }
}
"""

MEDIA_DETAILS_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title {
      romaji
      english
      native
      userPreferred
    }
    synonyms
    format
    status
    episodes
    nextAiringEpisode {
      airingAt
      timeUntilAiring
      episode
    }
    season
    seasonYear
  }
}
"""


class AniListError(Exception):
    """Base exception for AniList API errors."""
    pass


def get_current_and_next_season(dt: Optional[datetime] = None):
    """
    Calculate current and next anime season and year.
    Seasons: WINTER (Jan-Mar), SPRING (Apr-Jun), SUMMER (Jul-Sep), FALL (Oct-Dec)
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    month = dt.month
    year = dt.year

    if 1 <= month <= 3:
        cur_season = "WINTER"
        next_season = "SPRING"
        cur_year = year
        next_year = year
    elif 4 <= month <= 6:
        cur_season = "SPRING"
        next_season = "SUMMER"
        cur_year = year
        next_year = year
    elif 7 <= month <= 9:
        cur_season = "SUMMER"
        next_season = "FALL"
        cur_year = year
        next_year = year
    else:
        cur_season = "FALL"
        next_season = "WINTER"
        cur_year = year
        next_year = year + 1

    return (cur_season, cur_year), (next_season, next_year)


class AniListClient:
    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    async def _post_query(self, query: str, variables: Dict[str, Any], max_retries: int = 3) -> Dict[str, Any]:
        """Execute GraphQL query with exponential backoff on rate limits (HTTP 429)."""
        backoff = 2.0
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(max_retries):
                try:
                    resp = await client.post(
                        ANILIST_GRAPHQL_URL,
                        json={"query": query, "variables": variables},
                        headers={"Content-Type": "application/json", "Accept": "application/json"},
                    )
                    if resp.status_code == 429:
                        raw_retry = resp.headers.get("Retry-After")
                        try:
                            retry_after = float(raw_retry) if raw_retry else backoff
                        except (ValueError, TypeError):
                            retry_after = backoff
                        logger.warning(f"AniList rate limited (429). Waiting {retry_after} seconds...")
                        await asyncio.sleep(retry_after)
                        backoff *= 2
                        continue

                    resp.raise_for_status()
                    data = resp.json()
                    if "errors" in data:
                        raise AniListError(f"AniList GraphQL error: {data['errors']}")
                    return data.get("data") or {}
                except httpx.HTTPStatusError as e:
                    if attempt == max_retries - 1:
                        raise AniListError(f"AniList HTTP error: {e.response.status_code} - {e.response.text}") from e
                    await asyncio.sleep(backoff)
                    backoff *= 2
                except httpx.RequestError as e:
                    if attempt == max_retries - 1:
                        raise AniListError(f"AniList network connection failed: {e}") from e
                    await asyncio.sleep(backoff)
                    backoff *= 2

        raise AniListError("Max retries exceeded querying AniList API")

    async def fetch_user_seasonal_anime(self, username: str) -> List[Dict[str, Any]]:
        """
        Fetch user's anime filtered strictly to:
        1. Currently releasing anime
        2. Upcoming anime planned for next season (or current season if not yet released)
        """
        if not username.strip():
            return []

        data = await self._post_query(USER_SEASONAL_QUERY, {"userName": username.strip()})
        collection = data.get("MediaListCollection") or {}
        lists = collection.get("lists", [])

        (cur_season, cur_year), (next_season, next_year) = get_current_and_next_season()

        anime_dict: Dict[int, Dict[str, Any]] = {}
        for l in lists:
            for entry in l.get("entries", []):
                media = entry.get("media")
                if not media:
                    continue

                media_id = media["id"]
                if media_id in anime_dict:
                    continue

                status = media.get("status")
                season = media.get("season")
                season_year = media.get("seasonYear")
                next_airing = media.get("nextAiringEpisode")

                # Strictly filter:
                # 1. Currently releasing (status == RELEASING or has nextAiringEpisode)
                # 2. Upcoming show for next season (or upcoming in current season)
                is_currently_releasing = (status == "RELEASING") or (next_airing is not None)
                is_next_season_planned = (
                    (season == next_season and season_year == next_year) or
                    (season == cur_season and season_year == cur_year and status == "NOT_YET_RELEASED")
                )

                if not (is_currently_releasing or is_next_season_planned):
                    continue

                # Build alias list
                titles = media.get("title", {})
                aliases = set()
                for key in ["romaji", "english", "native", "userPreferred"]:
                    val = titles.get(key)
                    if val and isinstance(val, str) and val.strip():
                        aliases.add(val.strip())
                for syn in media.get("synonyms", []):
                    if syn and isinstance(syn, str) and syn.strip():
                        aliases.add(syn.strip())

                # Next airing
                next_airing_episode = next_airing.get("episode") if next_airing else None
                next_airing_at = None
                if next_airing and next_airing.get("airingAt"):
                    next_airing_at = datetime.fromtimestamp(next_airing["airingAt"], tz=timezone.utc)

                preferred_title = (
                    titles.get("userPreferred")
                    or titles.get("english")
                    or titles.get("romaji")
                    or f"Anime_{media_id}"
                )

                anime_dict[media_id] = {
                    "anilist_id": media_id,
                    "display_name": preferred_title,
                    "title_romaji": titles.get("romaji") or "",
                    "title_english": titles.get("english") or "",
                    "aliases": list(aliases),
                    "status": status or "UNKNOWN",
                    "total_episodes": media.get("episodes"),
                    "next_airing_episode": next_airing_episode,
                    "next_airing_at": next_airing_at,
                    "season": season,
                    "season_year": season_year,
                    "cover_image": (media.get("coverImage") or {}).get("large") or "",
                }

        return list(anime_dict.values())

    async def fetch_media_details(self, media_id: int) -> Optional[Dict[str, Any]]:
        """Fetch updated episode and airing details for a specific media ID."""
        data = await self._post_query(MEDIA_DETAILS_QUERY, {"id": media_id})
        media = data.get("Media")
        if not media:
            return None

        titles = media.get("title", {})
        aliases = set()
        for key in ["romaji", "english", "native", "userPreferred"]:
            val = titles.get(key)
            if val and isinstance(val, str) and val.strip():
                aliases.add(val.strip())
        for syn in media.get("synonyms", []):
            if syn and isinstance(syn, str) and syn.strip():
                aliases.add(syn.strip())

        next_airing = media.get("nextAiringEpisode")
        next_airing_episode = next_airing["episode"] if next_airing else None
        next_airing_at = None
        if next_airing and next_airing.get("airingAt"):
            next_airing_at = datetime.fromtimestamp(next_airing["airingAt"], tz=timezone.utc)

        return {
            "anilist_id": media_id,
            "display_name": titles.get("userPreferred") or titles.get("english") or titles.get("romaji"),
            "aliases": list(aliases),
            "status": media.get("status"),
            "total_episodes": media.get("episodes"),
            "next_airing_episode": next_airing_episode,
            "next_airing_at": next_airing_at,
        }
