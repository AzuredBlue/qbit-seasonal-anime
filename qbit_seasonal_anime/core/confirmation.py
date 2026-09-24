from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Set
from sqlmodel import Session, select, or_
from qbit_seasonal_anime.clients.qbit import QBitClient, QbitClientError
from qbit_seasonal_anime.core.matching import match_release_to_show, prepare_aliases
from qbit_seasonal_anime.core.discovery import RssSnapshot, flatten_rss_articles, parse_article_date
from qbit_seasonal_anime.core.rules import create_or_update_rule, build_regex_pattern
from qbit_seasonal_anime.db.models import Monitored, MonitoredStatus, RuleHistory, RuleOutcome, Settings, Feed, MatchHistory, utc_now

logger = logging.getLogger("qbit_seasonal_anime.core.confirmation")


def record_match_event(
    session: Session,
    monitored_id: Optional[int],
    show_name: str,
    rule_name: str,
    release_title: str,
    feed_name: Optional[str] = None,
    episode: Optional[int] = None,
    match_time: Optional[datetime] = None,
    matched_regex: Optional[str] = None,
) -> Optional[MatchHistory]:
    """Record a matched release in match_history if not already recorded."""
    try:
        if isinstance(match_time, datetime):
            if match_time.tzinfo:
                stored_time = match_time.astimezone(timezone.utc).replace(tzinfo=None)
            else:
                stored_time = match_time
        else:
            stored_time = utc_now().replace(tzinfo=None)

        existing = session.exec(
            select(MatchHistory).where(MatchHistory.release_title == release_title)
        ).first()
        if existing:
            updated_existing = False
            if matched_regex and not existing.matched_regex:
                existing.matched_regex = matched_regex
                updated_existing = True
            if stored_time and existing.created_at != stored_time:
                existing.created_at = stored_time
                updated_existing = True
            if updated_existing:
                session.add(existing)
            return existing

        record = MatchHistory(
            monitored_id=monitored_id,
            show_name=show_name,
            rule_name=rule_name,
            feed_name=feed_name,
            release_title=release_title,
            episode=episode,
            created_at=stored_time,
            matched_regex=matched_regex,
        )
        session.add(record)

        # Keep the latest 200 items in match_history
        all_hist = session.exec(select(MatchHistory).order_by(MatchHistory.created_at.desc())).all()
        if len(all_hist) > 200:
            for extra in all_hist[200:]:
                session.delete(extra)

        return record
    except Exception as e:
        logger.debug(f"Could not record match event: {e}")
        return None



def verify_and_confirm_rules_from_feeds(
    session: Session,
    qbit_client: QBitClient,
    settings: Settings,
    rss_snapshot: Optional[RssSnapshot] = None,
    parsed_articles: Optional[Dict[str, Dict[str, Any]]] = None,
    known_categories: Optional[Set[str]] = None,
) -> List[str]:
    """
    Verify and confirm rules against new and cached RSS feed articles.
    When a feed article matches an unconfirmed show's regex and aliases,
    it confirms the rule as working (Works) and updates the last confirmed episode.
    """
    logs: List[str] = []

    stmt = select(Monitored).where(
        Monitored.current_feed_id.is_not(None),
        Monitored.status.in_([MonitoredStatus.UNCONFIRMED, MonitoredStatus.FIXED]),
    )
    monitored_shows = session.exec(stmt).all()
    if not monitored_shows:
        return logs

    try:
        if rss_snapshot is not None:
            articles_by_url = rss_snapshot.get()
        else:
            rss_tree = qbit_client.get_rss_items(with_data=True)
            articles_by_url = flatten_rss_articles(rss_tree)
    except QbitClientError as e:
        logger.warning(f"Could not fetch RSS articles for rule confirmation: {e}")
        return logs

    feeds_map = {f.id: f for f in session.exec(select(Feed)).all()}
    if parsed_articles is None:
        parsed_articles = {}

    candidates = []
    for show in monitored_shows:
        feed = feeds_map.get(show.current_feed_id)
        if not feed:
            continue

        articles = articles_by_url.get(feed.qbit_feed_url) or []
        aliases = show.aliases
        test_pattern = build_regex_pattern(aliases)
        prepared_aliases = prepare_aliases(aliases)
        best_ep = None
        matched_title = None
        best_parsed = None
        best_art = None

        for article in articles:
            title = article.get("title", "")
            is_match, _, parsed = match_release_to_show(
                title,
                aliases,
                test_pattern=test_pattern,
                prepared_aliases=prepared_aliases,
                parsed_cache=parsed_articles,
            )
            if is_match:
                episode = parsed.get("episode")
                if episode is not None and (best_ep is None or episode > best_ep):
                    best_ep = episode
                    matched_title = title
                    best_parsed = parsed
                    best_art = article

        if best_ep is not None:
            rule_name = show.qbit_rule_name or f"[Seasonal] {show.display_name}"
            candidates.append({
                "show": show,
                "feed": feed,
                "aliases": aliases,
                "rule_name": rule_name,
                "best_ep": best_ep,
                "matched_title": matched_title,
                "best_parsed": best_parsed,
                "best_art": best_art,
            })

    pending_events = []
    for candidate in candidates:
        show = candidate["show"]
        feed = candidate["feed"]
        aliases = candidate["aliases"]
        best_ep = candidate["best_ep"]
        matched_title = candidate["matched_title"]
        best_parsed = candidate["best_parsed"]
        rule_name = candidate["rule_name"]

        current_last = show.last_confirmed_episode or 0
        if best_ep > current_last:
            show.last_confirmed_episode = best_ep

        if best_parsed:
            if show.matched_title != best_parsed.get("title"):
                show.matched_title = best_parsed.get("title")
            if show.matched_release_group != best_parsed.get("release_group"):
                show.matched_release_group = best_parsed.get("release_group")

        if show.status == MonitoredStatus.UNCONFIRMED:
            show.status = MonitoredStatus.FIXED

            hist_stmt = (
                select(RuleHistory)
                .where(RuleHistory.monitored_id == show.id)
                .order_by(RuleHistory.created_at.desc())
                .limit(1)
            )
            latest_hist = session.exec(hist_stmt).first()
            if latest_hist and latest_hist.outcome == RuleOutcome.PENDING:
                latest_hist.outcome = RuleOutcome.CONFIRMED
                latest_hist.note = f"Verified with RSS release: {matched_title}"
                session.add(latest_hist)

            try:
                create_or_update_rule(
                    qbit_client=qbit_client,
                    monitored=show,
                    feed=feed,
                    base_dir=settings.base_dir,
                    category=settings.default_category,
                    ratio_limit=settings.default_seed_ratio,
                    release_group=show.matched_release_group,
                    known_categories=known_categories,
                )
            except Exception as e:
                logger.warning(f"Could not update cleaned rule in qBittorrent for '{show.display_name}': {e}")

            msg = f"Confirmed rule for '{show.display_name}' (Ep {best_ep}) via RSS '{matched_title}'. Cleaned up rule -> Works"
            logger.info(msg)
            logs.append(msg)

        regex_pat = show.custom_regex or build_regex_pattern(
            aliases,
            matched_title=show.matched_title,
            release_group=show.matched_release_group,
        )
        pending_events.append({
            "show_id": show.id,
            "show_name": show.display_name,
            "feed_name": feed.qbit_feed_name,
            "rule_name": rule_name,
            "matched_title": matched_title,
            "best_art": candidate["best_art"],
            "episode": best_ep,
            "matched_regex": regex_pat,
        })

        session.add(show)

    match_pairs = [
        (event["rule_name"], event["matched_title"])
        for event in pending_events
        if event["matched_title"]
    ]
    match_times = {}
    batch_lookup_available = False
    if match_pairs:
        try:
            batch_lookup = getattr(qbit_client, "get_rule_match_times", None)
            if callable(batch_lookup):
                batch_lookup_available = True
                batch_result = batch_lookup(match_pairs)
                if isinstance(batch_result, dict):
                    match_times = batch_result
        except Exception as e:
            logger.debug(f"Could not batch qBittorrent match-time lookup: {e}")

    if match_pairs and not batch_lookup_available:
        for rule_name, release_title in match_pairs:
            try:
                match_times[(rule_name, release_title)] = qbit_client.get_rule_match_time(
                    rule_name=rule_name,
                    release_title=release_title,
                )
            except Exception:
                match_times[(rule_name, release_title)] = None

    for event in pending_events:
        rule_name = event["rule_name"]
        matched_title = event["matched_title"]
        match_time = match_times.get((rule_name, matched_title))
        if not isinstance(match_time, datetime):
            match_time = None

        if not match_time and event["best_art"]:
            art_dt = parse_article_date(event["best_art"])
            if art_dt and art_dt.year > 2000:
                match_time = art_dt

        if not match_time:
            match_time = utc_now()

        record_match_event(
            session=session,
            monitored_id=event["show_id"],
            show_name=event["show_name"],
            rule_name=rule_name,
            release_title=matched_title,
            feed_name=event["feed_name"],
            episode=event["episode"],
            match_time=match_time,
            matched_regex=event["matched_regex"],
        )

    if pending_events:
        session.commit()
    return logs


# Backward compatibility alias
verify_and_confirm_torrents = verify_and_confirm_rules_from_feeds


def has_downloaded_final_episode(session: Session, show: Monitored) -> bool:
    """
    Return True if the final episode of a show has been matched/downloaded by qBittorrent.
    Checks:
    1. show.total_episodes is known and positive.
    2. show.last_confirmed_episode >= show.total_episodes, OR
       match_history has a recorded match for episode >= show.total_episodes.
    """
    if not show.total_episodes or show.total_episodes <= 0:
        return False

    if (show.last_confirmed_episode or 0) >= show.total_episodes:
        return True

    # Check match history by monitored_id or show_name
    match_conditions = []
    if show.id:
        match_conditions.append(MatchHistory.monitored_id == show.id)
    if show.display_name:
        match_conditions.append(MatchHistory.show_name == show.display_name)

    if match_conditions:
        mh = session.exec(
            select(MatchHistory).where(
                or_(*match_conditions),
                MatchHistory.episode >= show.total_episodes,
            )
        ).first()
        if mh:
            show.last_confirmed_episode = max(show.last_confirmed_episode or 0, mh.episode or show.total_episodes)
            session.add(show)
            return True

    return False
