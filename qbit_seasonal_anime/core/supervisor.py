import logging
from datetime import timedelta
from typing import Any, Dict, List
from sqlmodel import Session, select
from qbit_seasonal_anime.clients.anilist import AniListClient, AniListError
from qbit_seasonal_anime.clients.qbit import QBitClient, QbitClientError
from qbit_seasonal_anime.core.confirmation import verify_and_confirm_torrents
from qbit_seasonal_anime.core.discovery import discover_feed_for_show, flatten_rss_articles
from qbit_seasonal_anime.core.rules import build_rule_definition, build_rule_name, create_or_update_rule
from qbit_seasonal_anime.core.stall import check_and_handle_stalls
from qbit_seasonal_anime.db.models import Feed, Monitored, MonitoredStatus, RuleHistory, RuleOutcome, Settings, utc_now

logger = logging.getLogger("qbit_seasonal_anime.core.supervisor")


def _rules_are_equivalent(current: Dict[str, Any], desired: Dict[str, Any]) -> bool:
    """Return True if an existing qBittorrent rule matches the desired definition."""
    try:
        return (
            current.get("mustContain") == desired.get("mustContain")
            and current.get("mustNotContain") == desired.get("mustNotContain")
            and current.get("affectedFeeds") == desired.get("affectedFeeds")
            and current.get("savePath") == desired.get("savePath")
            and (current.get("assignedCategory") or "") == (desired.get("assignedCategory") or "")
            and current.get("useRegex") == desired.get("useRegex")
            and current.get("enabled") == desired.get("enabled")
        )
    except Exception:
        return False


class Supervisor:
    def __init__(self, session: Session, qbit: QBitClient, anilist: AniListClient, settings: Settings):
        self.session = session
        self.qbit = qbit
        self.anilist = anilist
        self.settings = settings

    def sync_feeds(self) -> List[str]:
        """Fetch RSS feeds from qBittorrent and ensure they are registered in the database."""
        logs = []
        try:
            qbit_feeds = self.qbit.get_rss_feeds_flat()
        except QbitClientError as e:
            logger.warning(f"Could not sync RSS feeds from qBittorrent: {e}")
            return [f"Feed sync error: {e}"]

        existing_feeds = {f.qbit_feed_url: f for f in self.session.exec(select(Feed)).all()}
        next_priority = len(existing_feeds) + 1
        added = 0

        for qf in qbit_feeds:
            url = qf["url"]
            name = qf["name"]
            if url not in existing_feeds:
                new_feed = Feed(qbit_feed_name=name, qbit_feed_url=url, priority=next_priority)
                self.session.add(new_feed)
                existing_feeds[url] = new_feed
                next_priority += 1
                added += 1
                logs.append(f"Discovered new qBit RSS feed: '{name}' (Priority {new_feed.priority})")

        self.session.commit()
        if added > 0:
            logs.append(f"Registered {added} new RSS feeds (Total: {len(existing_feeds)}).")
        else:
            logs.append(f"Verified {len(existing_feeds)} RSS feeds configured in qBittorrent.")
        return logs

    def bootstrap_unassigned_shows(self) -> List[str]:
        """Discover feeds and create initial rules for unassigned or newly added shows using the proactive approach."""
        logs = []
        stmt = select(Monitored).where(
            (Monitored.current_feed_id.is_(None)) | (Monitored.qbit_rule_name.is_(None)),
            Monitored.status.in_([MonitoredStatus.UNCONFIRMED, MonitoredStatus.STALLED]),
        )
        unassigned_shows = self.session.exec(stmt).all()
        all_feeds = self.session.exec(select(Feed).order_by(Feed.priority)).all()

        if not unassigned_shows or not all_feeds:
            return logs

        top_feed = all_feeds[0]

        # Query cached articles once for the entire batch of unassigned shows
        try:
            rss_tree = self.qbit.get_rss_items(with_data=True)
            cached_articles = flatten_rss_articles(rss_tree)
        except QbitClientError as e:
            logger.warning(f"Could not fetch RSS cache for bootstrap: {e}")
            cached_articles = {}

        for show in unassigned_shows:
            # Check previously failed feeds for exclusion
            failed_stmt = select(RuleHistory.feed_id).where(
                RuleHistory.monitored_id == show.id,
                RuleHistory.outcome.in_([RuleOutcome.STALLED, RuleOutcome.FALSE_POSITIVE, RuleOutcome.REPLACED]),
            )
            excluded = [fid for fid in self.session.exec(failed_stmt).all() if fid is not None]

            # 1. First, check if any feed already has matching articles in cache
            res = discover_feed_for_show(
                monitored=show,
                feeds=all_feeds,
                qbit_client=self.qbit,
                excluded_feed_ids=excluded,
                cached_articles_by_url=cached_articles,
            )
            if res:
                chosen_feed, obs_group, matched_title = res
                show.matched_title = matched_title
                show.matched_release_group = obs_group
                try:
                    rule_name = create_or_update_rule(
                        qbit_client=self.qbit,
                        monitored=show,
                        feed=chosen_feed,
                        base_dir=self.settings.base_dir,
                        category=self.settings.default_category,
                        ratio_limit=self.settings.default_seed_ratio,
                        release_group=obs_group,
                        title_language=getattr(self.settings, "title_language", "english"),
                    )
                    show.current_feed_id = chosen_feed.id
                    show.qbit_rule_name = rule_name
                    show.status = MonitoredStatus.FIXED
                    self.session.add(show)

                    hist = RuleHistory(
                        monitored_id=show.id,
                        feed_id=chosen_feed.id,
                        created_at=utc_now(),
                        outcome=RuleOutcome.CONFIRMED,
                        note=f"Verified rule created on '{chosen_feed.qbit_feed_name}'",
                    )
                    self.session.add(hist)
                    self.session.commit()

                    msg = f"Created verified rule for '{show.display_name}' on feed '{chosen_feed.qbit_feed_name}' (Works)"
                    logger.info(msg)
                    logs.append(msg)
                except QbitClientError as e:
                    err_msg = f"Failed creating rule for '{show.display_name}': {e}"
                    logger.error(err_msg)
            else:
                # 2. PROACTIVE SPECULATIVE RULE ON PRIORITY #1 FEED
                # If no feed has cached releases yet (e.g. upcoming show or unreleased premiere),
                # proactively arm a rule with broad multi-alias regex on the user's #1 Priority Feed
                target_feed = top_feed
                if top_feed.id in excluded:
                    avail = [f for f in all_feeds if f.id not in excluded]
                    target_feed = avail[0] if avail else None

                if target_feed:
                    try:
                        rule_name = create_or_update_rule(
                            qbit_client=self.qbit,
                            monitored=show,
                            feed=target_feed,
                            base_dir=self.settings.base_dir,
                            category=self.settings.default_category,
                            ratio_limit=self.settings.default_seed_ratio,
                            release_group=None,
                            title_language=getattr(self.settings, "title_language", "english"),
                        )
                        show.current_feed_id = target_feed.id
                        show.qbit_rule_name = rule_name
                        show.status = MonitoredStatus.UNCONFIRMED
                        self.session.add(show)

                        hist = RuleHistory(
                            monitored_id=show.id,
                            feed_id=target_feed.id,
                            created_at=utc_now(),
                            outcome=RuleOutcome.PENDING,
                            note=f"Proactive rule armed on #{target_feed.priority} feed '{target_feed.qbit_feed_name}'",
                        )
                        self.session.add(hist)
                        self.session.commit()

                        msg = f"Armed proactive rule for '{show.display_name}' on Priority #{target_feed.priority} feed '{target_feed.qbit_feed_name}' (Testing)"
                        logger.info(msg)
                        logs.append(msg)
                    except QbitClientError as e:
                        err_msg = f"Failed arming proactive rule for '{show.display_name}': {e}"
                        logger.error(err_msg)
                        logs.append(f"Warning: {err_msg}")

        return logs

    async def sync_anilist_schedule(self) -> List[str]:
        """Fetch updated episode numbers, air dates, and status from AniList. Also imports newly added shows."""
        logs = []
        if not self.settings.anilist_username.strip():
            return logs

        try:
            seasonal_list = await self.anilist.fetch_user_seasonal_anime(self.settings.anilist_username)
        except AniListError as e:
            logger.warning(f"Could not refresh AniList schedule: {e}")
            return [f"AniList schedule sync error: {e}"]

        existing_shows = {s.anilist_id: s for s in self.session.exec(select(Monitored)).all()}
        new_shows_count = 0
        pref_lang = getattr(self.settings, "title_language", "english")

        for data in seasonal_list:
            aid = data.get("anilist_id") or data.get("id")
            if not aid:
                continue
            
            en_t = data.get("title_english")
            ro_t = data.get("title_romaji")
            chosen_name = en_t if (pref_lang == "english" and en_t) else (ro_t or data["display_name"])

            if aid in existing_shows:
                show = existing_shows[aid]
                updated = False

                if data.get("title_romaji") and show.title_romaji != data.get("title_romaji"):
                    show.title_romaji = data.get("title_romaji")
                    updated = True
                if data.get("title_english") and show.title_english != data.get("title_english"):
                    show.title_english = data.get("title_english")
                    updated = True
                if show.display_name != chosen_name:
                    show.display_name = chosen_name
                    updated = True
                if data.get("next_airing_episode") != show.next_airing_episode:
                    show.next_airing_episode = data.get("next_airing_episode")
                    updated = True
                if data.get("next_airing_at") != show.next_airing_at:
                    show.next_airing_at = data.get("next_airing_at")
                    updated = True
                if data.get("total_episodes") != show.total_episodes:
                    show.total_episodes = data.get("total_episodes")
                    updated = True
                if data.get("cover_image") and show.cover_image != data.get("cover_image"):
                    show.cover_image = data.get("cover_image")
                    updated = True
                if data.get("season") and show.season_name != data.get("season"):
                    show.season_name = data.get("season")
                    updated = True
                if data.get("season_year") and show.season_year != data.get("season_year"):
                    show.season_year = data.get("season_year")
                    updated = True

                # Merge any new aliases
                current_aliases = set(show.aliases)
                for a in data.get("aliases", []):
                    if a and a.strip():
                        current_aliases.add(a.strip())
                show.aliases = list(current_aliases)

                if updated:
                    self.session.add(show)
            else:
                # Automatically import newly added seasonal anime
                from qbit_seasonal_anime.core.rules import sanitize_folder_name
                new_show = Monitored(
                    anilist_id=aid,
                    display_name=chosen_name,
                    title_romaji=data.get("title_romaji"),
                    title_english=data.get("title_english"),
                    aliases_json="[]",
                    status=MonitoredStatus.UNCONFIRMED,
                    total_episodes=data.get("total_episodes"),
                    next_airing_episode=data.get("next_airing_episode"),
                    next_airing_at=data.get("next_airing_at"),
                    save_folder=sanitize_folder_name(chosen_name),
                    cover_image=data.get("cover_image"),
                    season_name=data.get("season"),
                    season_year=data.get("season_year"),
                )
                new_show.aliases = data.get("aliases", [])
                self.session.add(new_show)
                existing_shows[aid] = new_show
                new_shows_count += 1

        self.session.commit()
        msg = f"Synced AniList schedule for {len(seasonal_list)} seasonal shows."
        if new_shows_count > 0:
            msg += f" (Discovered and added {new_shows_count} new seasonal shows to Monitored)"
        logs.append(msg)
        return logs

    def sync_active_rules(self) -> List[str]:
        """Ensure active rules in qBittorrent exist and have up-to-date definitions without redundant API calls."""
        logs = []
        active_shows = self.session.exec(
            select(Monitored).where(
                Monitored.current_feed_id.is_not(None),
                Monitored.status.in_([MonitoredStatus.UNCONFIRMED, MonitoredStatus.FIXED]),
            )
        ).all()
        feeds_map = {f.id: f for f in self.session.exec(select(Feed)).all()}

        try:
            client = self.qbit.get_client()
            existing_rules = client.rss_rules()
        except Exception as e:
            logger.debug(f"Could not fetch existing RSS rules from qBittorrent: {e}")
            existing_rules = {}

        refreshed = 0
        for show in active_shows:
            feed = feeds_map.get(show.current_feed_id)
            if not feed:
                continue

            rule_name = show.qbit_rule_name or build_rule_name(show.id or 0, show.display_name)
            desired_def = build_rule_definition(
                monitored=show,
                feed_url=feed.qbit_feed_url,
                base_dir=self.settings.base_dir,
                category=self.settings.default_category,
                ratio_limit=self.settings.default_seed_ratio,
                release_group=show.matched_release_group,
                title_language=getattr(self.settings, "title_language", "english"),
            )

            current_def = existing_rules.get(rule_name)
            # Skip updating if rule already exists with identical configuration
            if current_def and _rules_are_equivalent(current_def, desired_def):
                continue

            try:
                self.qbit.set_rss_rule(rule_name=rule_name, rule_def=desired_def)
                show.qbit_rule_name = rule_name
                self.session.add(show)
                refreshed += 1
            except QbitClientError as e:
                logger.warning(f"Could not refresh rule for '{show.display_name}': {e}")
                logs.append(f"Warning: Failed updating rule for '{show.display_name}': {e}")

        self.session.commit()
        if refreshed > 0:
            logs.append(f"Synchronized {refreshed} updated rules in qBittorrent.")
        return logs

    async def run_full_cycle(self) -> List[str]:
        """Execute one complete supervision iteration."""
        all_logs: List[str] = []

        # 1. Sync RSS feeds from qBittorrent
        all_logs.extend(self.sync_feeds())

        # 2. Sync schedule and discover newly added seasonal anime from AniList FIRST
        all_logs.extend(await self.sync_anilist_schedule())

        # 3. Bootstrap any new/unassigned shows
        all_logs.extend(self.bootstrap_unassigned_shows())

        # 4. Confirmation loop for active downloads and RSS releases
        all_logs.extend(verify_and_confirm_torrents(self.session, self.qbit, self.settings))

        # 5. Refresh & synchronize active rules in qBittorrent
        all_logs.extend(self.sync_active_rules())

        # 6. Check for stalled releases & trigger fallback
        all_logs.extend(check_and_handle_stalls(self.session, self.qbit, self.settings))

        # 7. Summary breakdown
        total_shows = self.session.exec(select(Monitored)).all()
        now = utc_now()
        works_cnt = sum(1 for s in total_shows if s.status == MonitoredStatus.FIXED)
        stalled_cnt = sum(1 for s in total_shows if s.status == MonitoredStatus.STALLED)
        paused_cnt = sum(1 for s in total_shows if s.status == MonitoredStatus.PAUSED)
        completed_cnt = sum(1 for s in total_shows if s.status == MonitoredStatus.COMPLETED)
        upcoming_cnt = 0
        testing_cnt = 0
        for s in total_shows:
            if s.status == MonitoredStatus.UNCONFIRMED:
                air_at = s.next_airing_at
                if air_at and air_at.tzinfo is None:
                    from datetime import timezone
                    air_at = air_at.replace(tzinfo=timezone.utc)
                is_unreleased = (
                    (s.next_airing_episode == 1 or s.next_airing_episode is None)
                    and (s.last_confirmed_episode or 0) == 0
                    and (air_at is None or air_at > now)
                )
                if is_unreleased:
                    upcoming_cnt += 1
                else:
                    testing_cnt += 1

        summary = f"Summary: {works_cnt} Works | {upcoming_cnt} Upcoming"
        if testing_cnt > 0:
            summary += f" | {testing_cnt} Testing"
        if stalled_cnt > 0:
            summary += f" | {stalled_cnt} Stalled"
        if completed_cnt > 0:
            summary += f" | {completed_cnt} Completed"
        if paused_cnt > 0:
            summary += f" | {paused_cnt} Paused"
        all_logs.append(summary)

        return all_logs
