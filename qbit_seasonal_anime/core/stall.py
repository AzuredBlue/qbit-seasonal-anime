from datetime import timedelta
import logging
from typing import List
from sqlmodel import Session, select
from qbit_seasonal_anime.clients.qbit import QBitClient, QbitClientError
from qbit_seasonal_anime.core.discovery import discover_feed_for_show
from qbit_seasonal_anime.core.rules import create_or_update_rule, delete_rule
from qbit_seasonal_anime.db.models import Feed, Monitored, MonitoredStatus, RuleHistory, RuleOutcome, Settings, utc_now

logger = logging.getLogger("qbit_seasonal_anime.core.stall")


def check_and_handle_stalls(
    session: Session,
    qbit_client: QBitClient,
    settings: Settings,
) -> List[str]:
    """
    Check for finished shows and air-date stalled releases.
    Triggers automatic fallback to the next priority feed when stalled.
    """
    logs: List[str] = []
    now = utc_now()
    stall_delta = timedelta(hours=settings.stall_wait_hours)

    # 1. Check for finished shows
    active_stmt = select(Monitored).where(
        Monitored.status.in_([MonitoredStatus.UNCONFIRMED, MonitoredStatus.FIXED])
    )
    shows = session.exec(active_stmt).all()
    all_feeds = session.exec(select(Feed).order_by(Feed.priority)).all()

    for show in shows:
        # Finished show check
        if show.next_airing_at is None and show.next_airing_episode is None:
            if show.total_episodes and (show.last_confirmed_episode or 0) >= show.total_episodes:
                if show.qbit_rule_name:
                    delete_rule(qbit_client, show.qbit_rule_name)
                    show.qbit_rule_name = None
                show.status = MonitoredStatus.COMPLETED
                session.add(show)
                session.commit()
                msg = f"Show '{show.display_name}' completed all {show.total_episodes} episodes. Status -> COMPLETED"
                logger.info(msg)
                logs.append(msg)
                continue
            else:
                # Finished broadcast on AniList, awaiting final episode in RSS
                continue

        # Stall checks ONLY apply to UNCONFIRMED (testing) shows
        if show.status != MonitoredStatus.UNCONFIRMED:
            continue

        # Active show stall check for unconfirmed shows
        if show.next_airing_at:
            # Handle naive or timezone-aware comparisons cleanly
            air_at = show.next_airing_at
            if air_at.tzinfo is None:
                from datetime import timezone
                air_at = air_at.replace(tzinfo=timezone.utc)

            # Query most recent rule history row for this show
            hist_stmt = (
                select(RuleHistory)
                .where(RuleHistory.monitored_id == show.id)
                .order_by(RuleHistory.created_at.desc())
            )
            latest_hist = session.exec(hist_stmt).first()

            # Rule grace period: give newly created/fallback rules time to catch up
            rule_created_at = latest_hist.created_at if latest_hist else air_at
            if rule_created_at and rule_created_at.tzinfo is None:
                from datetime import timezone
                rule_created_at = rule_created_at.replace(tzinfo=timezone.utc)

            cutoff_time = max(air_at, rule_created_at) + stall_delta

            if now > cutoff_time:
                expected_ep = show.next_airing_episode or 1
                last_ep = show.last_confirmed_episode or 0

                if last_ep < expected_ep:
                    msg = (
                        f"STALL DETECTED: '{show.display_name}' Ep {expected_ep} overdue by "
                        f"{(now - air_at).total_seconds() / 3600:.1f} hours (grace window: {settings.stall_wait_hours}h)."
                    )
                    logger.warning(msg)
                    logs.append(msg)

                    # Record stalled history
                    stalled_feed_id = show.current_feed_id
                    if latest_hist and latest_hist.outcome == RuleOutcome.PENDING:
                        latest_hist.outcome = RuleOutcome.STALLED
                        latest_hist.note = f"Stalled for Ep {expected_ep}"
                        session.add(latest_hist)
                    else:
                        stalled_hist = RuleHistory(
                            monitored_id=show.id,
                            feed_id=stalled_feed_id,
                            created_at=now,
                            outcome=RuleOutcome.STALLED,
                            note=f"Stalled for Ep {expected_ep}",
                        )
                        session.add(stalled_hist)
                    session.flush()

                    # Delete current qBit rule
                    if show.qbit_rule_name:
                        delete_rule(qbit_client, show.qbit_rule_name)

                    # Gather excluded feed IDs (feeds that failed or were replaced for this show)
                    failed_hist_stmt = select(RuleHistory.feed_id).where(
                        RuleHistory.monitored_id == show.id,
                        RuleHistory.outcome.in_([RuleOutcome.STALLED, RuleOutcome.FALSE_POSITIVE, RuleOutcome.REPLACED]),
                        RuleHistory.feed_id.isnot(None),
                    )
                    failed_feed_ids = [fid for fid in session.exec(failed_hist_stmt).all() if fid is not None]
                    if stalled_feed_id and stalled_feed_id not in failed_feed_ids:
                        failed_feed_ids.append(stalled_feed_id)

                    # Attempt fallback discovery
                    discovery_res = discover_feed_for_show(
                        monitored=show,
                        feeds=all_feeds,
                        qbit_client=qbit_client,
                        excluded_feed_ids=failed_feed_ids,
                    )

                    if discovery_res:
                        fallback_feed, obs_group, matched_title = discovery_res
                        show.matched_title = matched_title
                        show.matched_release_group = obs_group

                        # Create rule on fallback feed
                        try:
                            rule_name = create_or_update_rule(
                                qbit_client=qbit_client,
                                monitored=show,
                                feed=fallback_feed,
                                base_dir=settings.base_dir,
                                category=settings.default_category,
                                ratio_limit=settings.default_seed_ratio,
                                release_group=obs_group,
                            )
                            show.current_feed_id = fallback_feed.id
                            show.qbit_rule_name = rule_name
                            show.status = MonitoredStatus.UNCONFIRMED
                            session.add(show)

                            new_hist = RuleHistory(
                                monitored_id=show.id,
                                feed_id=fallback_feed.id,
                                created_at=now,
                                outcome=RuleOutcome.PENDING,
                                note=f"Fallback rule created on '{fallback_feed.qbit_feed_name}'",
                            )
                            session.add(new_hist)
                            session.commit()

                            fallback_msg = f"Fallback successful for '{show.display_name}': Moved to feed '{fallback_feed.qbit_feed_name}'"
                            logger.info(fallback_msg)
                            logs.append(fallback_msg)
                        except QbitClientError as e:
                            logger.error(f"Failed creating fallback rule for '{show.display_name}': {e}")
                            logs.append(f"Error creating fallback rule: {e}")
                    else:
                        # If no feed had matching cached articles, check if another candidate feed exists (e.g. Feed #2)
                        avail = [f for f in all_feeds if f.id not in failed_feed_ids]
                        if avail:
                            fallback_feed = avail[0]
                            try:
                                rule_name = create_or_update_rule(
                                    qbit_client=qbit_client,
                                    monitored=show,
                                    feed=fallback_feed,
                                    base_dir=settings.base_dir,
                                    category=settings.default_category,
                                    ratio_limit=settings.default_seed_ratio,
                                    release_group=None,
                                )
                                show.current_feed_id = fallback_feed.id
                                show.qbit_rule_name = rule_name
                                show.status = MonitoredStatus.UNCONFIRMED
                                session.add(show)

                                new_hist = RuleHistory(
                                    monitored_id=show.id,
                                    feed_id=fallback_feed.id,
                                    created_at=now,
                                    outcome=RuleOutcome.PENDING,
                                    note=f"Fallback proactive rule armed on #{fallback_feed.priority} feed '{fallback_feed.qbit_feed_name}'",
                                )
                                session.add(new_hist)
                                session.commit()

                                fallback_msg = f"Fallback armed for '{show.display_name}': Moved to Priority #{fallback_feed.priority} feed '{fallback_feed.qbit_feed_name}'"
                                logger.info(fallback_msg)
                                logs.append(fallback_msg)
                            except QbitClientError as e:
                                logger.error(f"Failed arming fallback rule: {e}")
                        else:
                            # All feeds exhausted -> STALLED
                            show.status = MonitoredStatus.STALLED
                            show.current_feed_id = None
                            session.add(show)
                            session.commit()
                            stall_msg = f"All feeds exhausted for '{show.display_name}'. Status -> STALLED (User attention needed)."
                            logger.error(stall_msg)
                            logs.append(stall_msg)

    return logs
