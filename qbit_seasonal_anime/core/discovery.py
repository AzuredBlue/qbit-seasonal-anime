from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple
from sqlmodel import Session, select
from qbit_seasonal_anime.clients.qbit import QBitClient, QbitClientError
from qbit_seasonal_anime.core.matching import match_release_to_show
from qbit_seasonal_anime.db.models import Feed, Monitored

logger = logging.getLogger("qbit_seasonal_anime.core.discovery")


def flatten_rss_articles(rss_tree: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Parse qBittorrent RSS item tree into a mapping:
    { feed_url: [ { "title": ..., "torrentURL": ..., ... }, ... ] }
    """
    feed_articles: Dict[str, List[Dict[str, Any]]] = {}

    def traverse(node: Dict[str, Any]):
        for key, val in node.items():
            if isinstance(val, dict):
                if "url" in val:
                    feed_url = val["url"]
                    articles = val.get("articles") or []
                    feed_articles[feed_url] = articles
                else:
                    traverse(val)

    traverse(rss_tree)
    return feed_articles


def discover_feed_for_show(
    monitored: Monitored,
    feeds: List[Feed],
    qbit_client: QBitClient,
    excluded_feed_ids: Optional[List[int]] = None,
    cached_articles_by_url: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    preferred_feed_grace_seconds: int = 300,  # 5 minutes / 1-2 refresh cycles
) -> Optional[Tuple[Feed, Optional[str], Optional[str]]]:
    """
    Discover the best feed for a monitored show.
    Returns (selected_feed, observed_release_group, matched_title) or None if no feeds available.

    Selection strategy:
    1. Filter out excluded_feed_ids (e.g. previously stalled feeds).
    2. Sort candidate feeds by priority (1 is highest).
    3. Check candidate feeds in priority order. If the #1 Priority Feed contains matching articles,
       pick #1 immediately with zero delay.
    4. If a lower priority feed (e.g. #2) contains matching articles but #1 does not:
       - If the show aired recently (< 5 minutes ago), give #1 feed a 4-5 minute grace window
         (waiting 1-2 refresh cycles) to allow #1 to release its episode.
       - After the grace window, triggers a manual RSS refresh in qBittorrent and checks #1 again.
       - If #1 still has not released, falls back to #2.
    """
    excluded = set(excluded_feed_ids or [])
    available_feeds = [f for f in feeds if f.id not in excluded]

    if not available_feeds:
        logger.info(f"No available feeds left for '{monitored.display_name}' (all excluded/empty).")
        return None

    # Sort by priority ascending (1 = highest)
    sorted_feeds = sorted(available_feeds, key=lambda f: f.priority)
    top_feed = sorted_feeds[0]

    # Use passed cached articles or fetch once
    if cached_articles_by_url is not None:
        articles_by_url = cached_articles_by_url
    else:
        try:
            rss_tree = qbit_client.get_rss_items(with_data=True)
            articles_by_url = flatten_rss_articles(rss_tree)
        except QbitClientError as e:
            logger.warning(f"Could not fetch RSS cache for discovery: {e}.")
            articles_by_url = {}

    # Check top priority feed (#1) first
    top_articles = articles_by_url.get(top_feed.qbit_feed_url) or []
    for art in top_articles:
        title = art.get("title", "")
        is_match, score, parsed = match_release_to_show(title, monitored.aliases)
        if is_match:
            logger.info(
                f"Discovered #1 priority match for '{monitored.display_name}' in feed '{top_feed.qbit_feed_name}': "
                f"'{title}' (score: {score:.1f})"
            )
            return top_feed, parsed.get("release_group"), parsed.get("title")

    # Check if a lower-priority feed (#2, #3...) has releases
    lower_match = None
    for feed in sorted_feeds[1:]:
        articles = articles_by_url.get(feed.qbit_feed_url) or []
        for art in articles:
            title = art.get("title", "")
            is_match, score, parsed = match_release_to_show(title, monitored.aliases)
            if is_match:
                lower_match = (feed, parsed.get("release_group"), parsed.get("title"))
                break
        if lower_match:
            break

    if lower_match:
        matched_feed, rel_group, m_title = lower_match
        now = datetime.now(timezone.utc)
        air_at = monitored.next_airing_at
        if air_at and air_at.tzinfo is None:
            air_at = air_at.replace(tzinfo=timezone.utc)

        # If the show aired very recently (< 5 minutes ago), give #1 feed grace time to release
        if air_at and (now - air_at).total_seconds() < preferred_feed_grace_seconds:
            elapsed_m = (now - air_at).total_seconds() / 60
            logger.info(
                f"Observed release on Priority #{matched_feed.priority} '{matched_feed.qbit_feed_name}' for '{monitored.display_name}', "
                f"but waiting 4-5m buffer for Priority #1 '{top_feed.qbit_feed_name}' (elapsed: {elapsed_m:.1f}m)."
            )
            # Trigger background refresh of feeds in qBittorrent
            qbit_client.refresh_rss_feeds()
            return None

        # If grace window has elapsed, trigger one fresh manual refresh of #1 feed before locking lower feed
        try:
            qbit_client.refresh_rss_feeds()
            fresh_tree = qbit_client.get_rss_items(with_data=True)
            fresh_articles = flatten_rss_articles(fresh_tree)
            for art in (fresh_articles.get(top_feed.qbit_feed_url) or []):
                t = art.get("title", "")
                m, sc, pr = match_release_to_show(t, monitored.aliases)
                if m:
                    logger.info(f"Priority #1 feed '{top_feed.qbit_feed_name}' caught up after refresh for '{monitored.display_name}'!")
                    return top_feed, pr.get("release_group"), pr.get("title")
        except Exception as e:
            logger.debug(f"Fresh check of #1 feed skipped: {e}")

        logger.info(
            f"Fallback to Priority #{matched_feed.priority} feed '{matched_feed.qbit_feed_name}' for '{monitored.display_name}': "
            f"'{m_title}'"
        )
        return matched_feed, rel_group, m_title

    # No feed currently carries a confirmed release for this show
    logger.debug(f"No cached releases found for '{monitored.display_name}'. Remaining pending on #1 feed.")
    return None
