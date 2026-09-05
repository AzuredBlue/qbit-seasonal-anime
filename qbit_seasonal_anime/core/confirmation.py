from datetime import datetime, timezone
import logging
from typing import List, Optional
from sqlmodel import Session, select
from qbit_seasonal_anime.clients.qbit import QBitClient, QbitClientError
from qbit_seasonal_anime.core.matching import match_release_to_show
from qbit_seasonal_anime.core.discovery import flatten_rss_articles, parse_article_date
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
        existing = session.exec(
            select(MatchHistory).where(MatchHistory.release_title == release_title)
        ).first()
        if existing:
            if matched_regex and not existing.matched_regex:
                existing.matched_regex = matched_regex
                session.add(existing)
                session.commit()
            return existing

        if match_time and match_time.tzinfo:
            stored_time = match_time.astimezone(timezone.utc).replace(tzinfo=None)
        elif match_time:
            stored_time = match_time
        else:
            stored_time = utc_now().replace(tzinfo=None)

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
) -> List[str]:
    """
    Verify and confirm rules against new and cached RSS feed articles.
    When a feed article matches an unconfirmed show's regex and aliases,
    it confirms the rule as working (Works) and updates the last confirmed episode.
    """
    logs: List[str] = []

    try:
        rss_tree = qbit_client.get_rss_items(with_data=True)
        articles_by_url = flatten_rss_articles(rss_tree)
    except QbitClientError as e:
        logger.warning(f"Could not fetch RSS articles for rule confirmation: {e}")
        return logs

    # Fetch shows that have a rule assigned
    stmt = select(Monitored).where(
        Monitored.current_feed_id.is_not(None),
        Monitored.status.in_([MonitoredStatus.UNCONFIRMED, MonitoredStatus.FIXED]),
    )
    monitored_shows = session.exec(stmt).all()

    feeds_map = {f.id: f for f in session.exec(select(Feed)).all()}

    for show in monitored_shows:
        feed = feeds_map.get(show.current_feed_id)
        if not feed:
            continue

        articles = articles_by_url.get(feed.qbit_feed_url) or []
        best_ep = None
        matched_title = None
        best_parsed = None
        best_art = None

        for a in articles:
            title = a.get("title", "")
            is_match, score, parsed = match_release_to_show(title, show.aliases)
            if is_match:
                ep = parsed.get("episode")
                if ep is not None:
                    if best_ep is None or ep > best_ep:
                        best_ep = ep
                        matched_title = title
                        best_parsed = parsed
                        best_art = a

        if best_ep is not None:
            current_last = show.last_confirmed_episode or 0
            updated = False

            if best_ep > current_last:
                show.last_confirmed_episode = best_ep
                updated = True

            if best_parsed:
                if show.matched_title != best_parsed.get("title"):
                    show.matched_title = best_parsed.get("title")
                    updated = True
                if show.matched_release_group != best_parsed.get("release_group"):
                    show.matched_release_group = best_parsed.get("release_group")
                    updated = True

            if show.status == MonitoredStatus.UNCONFIRMED:
                show.status = MonitoredStatus.FIXED
                updated = True

                hist_stmt = (
                    select(RuleHistory)
                    .where(RuleHistory.monitored_id == show.id)
                    .order_by(RuleHistory.created_at.desc())
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
                    )
                except Exception as e:
                    logger.warning(f"Could not update cleaned rule in qBittorrent for '{show.display_name}': {e}")

                msg = f"Confirmed rule for '{show.display_name}' (Ep {best_ep}) via RSS '{matched_title}'. Cleaned up rule -> Works"
                logger.info(msg)
                logs.append(msg)

            if matched_title:
                match_time = None
                if best_art:
                    art_dt = parse_article_date(best_art)
                    if art_dt and art_dt.year > 2000:
                        match_time = art_dt
                regex_pat = show.custom_regex or build_regex_pattern(
                    show.aliases,
                    matched_title=show.matched_title,
                    release_group=show.matched_release_group,
                )
                record_match_event(
                    session=session,
                    monitored_id=show.id,
                    show_name=show.display_name,
                    rule_name=show.qbit_rule_name or f"[Seasonal] {show.display_name}",
                    release_title=matched_title,
                    feed_name=feed.qbit_feed_name if feed else None,
                    episode=best_ep,
                    match_time=match_time,
                    matched_regex=regex_pat,
                )

            if updated:
                session.add(show)
                session.commit()

    return logs


# Backward compatibility alias
verify_and_confirm_torrents = verify_and_confirm_rules_from_feeds
