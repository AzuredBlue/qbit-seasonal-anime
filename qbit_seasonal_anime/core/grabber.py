import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from sqlmodel import Session, select

from qbit_seasonal_anime.clients.qbit import QBitClient, QbitClientError
from qbit_seasonal_anime.core.confirmation import record_match_event
from qbit_seasonal_anime.core.discovery import RssSnapshot, flatten_rss_articles
from qbit_seasonal_anime.core.matching import match_release_to_show, parse_release_title
from qbit_seasonal_anime.core.rules import resolve_save_path
from qbit_seasonal_anime.db.models import (
    Episode,
    EpisodeStatus,
    Feed,
    GrabDecision,
    GrabDecisionType,
    Monitored,
    MonitoredStatus,
    SeenFeedItem,
    Settings,
    TorrentOperation,
    TorrentOperationStatus,
    utc_now,
)

logger = logging.getLogger("qbit_seasonal_anime.core.grabber")

PREFERRED_FEED_GRACE_SECONDS = 300


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _operation_tag() -> str:
    return f"qsa-op-{uuid4().hex}"


def _episode_tags(show: Monitored, episode: int, version: int, operation_tag: str) -> List[str]:
    return [
        "qsa-managed",
        f"qsa-show-{show.id}",
        f"qsa-ep-{episode}",
        f"qsa-v{version}",
        operation_tag,
    ]


def _torrent_hash(torrent: Any) -> Optional[str]:
    value = getattr(torrent, "hash", None)
    return str(value) if value else None


def _torrent_progress(torrent: Any) -> float:
    try:
        return float(getattr(torrent, "progress", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _torrent_state(torrent: Any) -> str:
    return str(getattr(torrent, "state", "") or "").lower()


def _is_seeding_torrent(torrent: Any) -> bool:
    return _torrent_progress(torrent) >= 1.0 or _torrent_state(torrent) in {
        "uploading",
        "pausedup",
        "stoppedup",
        "queuedup",
        "forcedup",
        "stalledup",
    }


def _managed_torrents(qbit: QBitClient, settings: Settings) -> List[Any]:
    by_hash: Dict[str, Any] = {}
    try:
        for torrent in qbit.get_torrents(tag="qsa-managed"):
            torrent_hash = _torrent_hash(torrent)
            if torrent_hash:
                by_hash[torrent_hash] = torrent
    except Exception as e:
        logger.debug(f"Managed torrent lookup failed: {e}")
    if settings.default_category:
        try:
            for torrent in qbit.get_torrents(category=settings.default_category):
                torrent_hash = _torrent_hash(torrent)
                if torrent_hash:
                    by_hash.setdefault(torrent_hash, torrent)
        except Exception as e:
            logger.debug(f"Category torrent lookup failed: {e}")
    return list(by_hash.values())


def _find_tagged(qbit: QBitClient, tag: str) -> Optional[Any]:
    try:
        torrents = list(qbit.get_torrents(tag=tag))
    except Exception as e:
        logger.debug(f"Could not find tagged torrent {tag}: {e}")
        return None
    return torrents[0] if torrents else None


def _operation_for_episode(session: Session, episode_id: int) -> Optional[TorrentOperation]:
    stmt = select(TorrentOperation).where(
        TorrentOperation.episode_id == episode_id,
        TorrentOperation.status.notin_([TorrentOperationStatus.COMPLETED, TorrentOperationStatus.FAILED]),
    )
    return session.exec(stmt.order_by(TorrentOperation.created_at.desc())).first()


def _set_episode_release(session: Session, episode: Episode, operation: TorrentOperation, torrent_hash: Optional[str], status: EpisodeStatus) -> None:
    episode.status = status
    episode.version = operation.version
    episode.release_title = operation.release_title
    episode.release_group = operation.release_group
    episode.torrent_url = operation.new_torrent_url
    episode.operation_tag = operation.operation_tag
    episode.torrent_hash = torrent_hash
    episode.last_error = None
    episode.retry_after = None
    session.add(episode)


def _complete_episode(session: Session, episode: Episode, reason: Optional[str] = None) -> None:
    episode.status = EpisodeStatus.COMPLETED
    episode.downloaded_at = episode.downloaded_at or utc_now()
    episode.last_error = reason
    episode.retry_after = None
    session.add(episode)
    show = session.get(Monitored, episode.monitored_id)
    if show:
        show.last_confirmed_episode = max(show.last_confirmed_episode or 0, episode.episode_number)
        session.add(show)


def _fail_operation(session: Session, operation: TorrentOperation, episode: Episode, error: str) -> None:
    operation.status = TorrentOperationStatus.FAILED
    operation.last_error = error
    operation.updated_at = utc_now()
    episode.status = EpisodeStatus.FAILED
    episode.last_error = error
    episode.retry_after = None
    session.add(operation)
    session.add(episode)
    session.commit()


def _record_decision(
    session: Session,
    show: Monitored,
    episode: Optional[Episode],
    feed: Feed,
    article: Dict[str, Any],
    title: str,
    episode_number: int,
    version: int,
    decision: GrabDecisionType,
    reason: str,
) -> None:
    item_id = str(article.get("id") or article.get("torrentURL") or article.get("link") or title)
    existing = session.exec(
        select(GrabDecision).where(
            GrabDecision.monitored_id == show.id,
            GrabDecision.feed_url == feed.qbit_feed_url,
            GrabDecision.feed_item_id == item_id,
            GrabDecision.episode == episode_number,
            GrabDecision.version == version,
            GrabDecision.decision == decision,
        )
    ).first()
    if existing:
        return
    session.add(GrabDecision(
        monitored_id=show.id,
        episode_id=episode.id if episode else None,
        feed_url=feed.qbit_feed_url,
        feed_item_id=item_id,
        release_title=title,
        episode=episode_number,
        version=version,
        decision=decision,
        reason=reason,
    ))
    session.commit()


def _sync_seen_items(
    session: Session,
    feeds: List[Feed],
    articles_by_url: Dict[str, List[Dict[str, Any]]],
) -> Dict[Tuple[str, str], datetime]:
    first_seen: Dict[Tuple[str, str], datetime] = {}
    now = utc_now()
    for feed in feeds:
        articles = articles_by_url.get(feed.qbit_feed_url, [])
        item_ids = [
            str(article.get("id") or article.get("torrentURL") or article.get("link") or article.get("title", ""))
            for article in articles
            if article.get("title")
        ]
        if not item_ids:
            continue
        rows = session.exec(
            select(SeenFeedItem).where(
                SeenFeedItem.feed_url == feed.qbit_feed_url,
                SeenFeedItem.item_id.in_(item_ids),
            )
        ).all()
        known = {row.item_id: row.created_at for row in rows}
        for item_id in item_ids:
            first_seen[(feed.qbit_feed_url, item_id)] = known.get(item_id, now)
            if item_id not in known:
                article = next(
                    article for article in articles
                    if str(article.get("id") or article.get("torrentURL") or article.get("link") or article.get("title", "")) == item_id
                )
                session.add(SeenFeedItem(
                    feed_url=feed.qbit_feed_url,
                    item_id=item_id,
                    title=article.get("title", ""),
                ))
        if len(known) < len(item_ids):
            session.commit()
    return first_seen


def sync_show_episodes(session: Session, show: Monitored) -> List[Episode]:
    if not show.id:
        return []
    episodes = session.exec(select(Episode).where(Episode.monitored_id == show.id)).all()
    by_number = {episode.episode_number: episode for episode in episodes}
    target_count = show.total_episodes or max(show.next_airing_episode or 0, show.last_confirmed_episode or 0, 1)
    if show.total_episodes:
        target_count = min(target_count, show.total_episodes)
    changed = False
    for number in range(1, target_count + 1):
        if number in by_number:
            continue
        completed = bool(
            show.last_confirmed_episode
            and show.last_confirmed_episode <= target_count
            and number <= show.last_confirmed_episode
        )
        episode = Episode(
            monitored_id=show.id,
            episode_number=number,
            status=EpisodeStatus.COMPLETED if completed else EpisodeStatus.WANTED,
        )
        session.add(episode)
        by_number[number] = episode
        changed = True
    if changed:
        session.commit()
    return list(by_number.values())


def _find_old_hash(qbit: QBitClient, settings: Settings, show: Monitored, episode: Episode) -> Optional[str]:
    if episode.torrent_hash:
        return episode.torrent_hash
    for torrent in _managed_torrents(qbit, settings):
        name = str(getattr(torrent, "name", "") or "").lower()
        if episode.release_title and episode.release_title.lower() == name:
            return _torrent_hash(torrent)
        if show.display_name.lower() in name and f"{episode.episode_number:02d}" in name:
            return _torrent_hash(torrent)
    return None


def _finish_operation(session: Session, qbit: QBitClient, operation: TorrentOperation, episode: Episode) -> Optional[str]:
    if operation.status == TorrentOperationStatus.COMPLETED:
        return None
    if not operation.new_torrent_hash:
        return None
    if operation.kind == "replace" and operation.old_torrent_hash and operation.old_torrent_hash != operation.new_torrent_hash:
        if operation.status == TorrentOperationStatus.PREPARING:
            return None
        if operation.status == TorrentOperationStatus.ADDED:
            try:
                old_torrents = list(qbit.get_torrents(hashes=[operation.old_torrent_hash]))
                if old_torrents:
                    qbit.pause_torrents([operation.old_torrent_hash])
                    qbit.delete_torrents([operation.old_torrent_hash], delete_files=True)
            except Exception as e:
                _fail_operation(session, operation, episode, str(e))
                return None
            operation.status = TorrentOperationStatus.OLD_REMOVED
            operation.updated_at = utc_now()
            session.add(operation)
            session.commit()
        if operation.status == TorrentOperationStatus.OLD_REMOVED:
            try:
                qbit.recheck_torrents([operation.new_torrent_hash])
                qbit.resume_torrents([operation.new_torrent_hash])
            except Exception as e:
                _fail_operation(session, operation, episode, str(e))
                return None
    _set_episode_release(session, episode, operation, operation.new_torrent_hash, EpisodeStatus.DOWNLOADING)
    operation.status = TorrentOperationStatus.COMPLETED
    operation.last_error = None
    operation.updated_at = utc_now()
    session.add(operation)
    session.add(episode)
    session.commit()
    show = session.get(Monitored, episode.monitored_id)
    feed = session.get(Feed, episode.feed_id) if episode.feed_id else None
    if show:
        record_match_event(
            session=session,
            monitored_id=show.id,
            show_name=show.display_name,
            rule_name=f"Direct: {show.display_name}",
            release_title=operation.release_title,
            feed_name=feed.qbit_feed_name if feed else None,
            episode=episode.episode_number,
            matched_regex=show.custom_regex,
        )
        session.commit()
    return f"Added '{show_name(episode, session)}' Ep {episode.episode_number} v{operation.version}"


def show_name(episode: Episode, session: Session) -> str:
    show = session.get(Monitored, episode.monitored_id)
    return show.display_name if show else str(episode.monitored_id)


def _recover_operations(session: Session, qbit: QBitClient, settings: Settings) -> List[str]:
    logs: List[str] = []
    operations = session.exec(
        select(TorrentOperation).where(
            TorrentOperation.status.notin_([TorrentOperationStatus.COMPLETED, TorrentOperationStatus.FAILED])
        )
    ).all()
    for operation in operations:
        episode = session.get(Episode, operation.episode_id)
        if not episode:
            continue
        if not operation.new_torrent_hash:
            if _aware(operation.updated_at) and _aware(operation.updated_at) < utc_now() - timedelta(minutes=15):
                operation.status = TorrentOperationStatus.FAILED
                operation.last_error = "Timed out waiting for qBittorrent to expose the new torrent."
                operation.updated_at = utc_now()
                if operation.kind == "replace" and episode.torrent_hash:
                    _complete_episode(session, episode, operation.last_error)
                else:
                    episode.status = EpisodeStatus.FAILED
                    episode.last_error = operation.last_error
                    episode.retry_after = None
                    session.add(episode)
                session.add(operation)
                session.commit()
                continue
            torrent = _find_tagged(qbit, operation.operation_tag)
            if torrent:
                operation.new_torrent_hash = _torrent_hash(torrent)
                operation.status = TorrentOperationStatus.ADDED
                operation.updated_at = utc_now()
                session.add(operation)
                session.commit()
        message = _finish_operation(session, qbit, operation, episode)
        if message:
            logs.append(message)
    return logs


def update_episode_status(session: Session, qbit: QBitClient, settings: Settings) -> List[str]:
    logs = _recover_operations(session, qbit, settings)
    torrents = _managed_torrents(qbit, settings)
    by_hash = {_torrent_hash(torrent): torrent for torrent in torrents if _torrent_hash(torrent)}
    untracked = session.exec(select(Episode).where(
        Episode.torrent_hash.is_(None),
        Episode.status == EpisodeStatus.COMPLETED,
    )).all()
    for episode in untracked:
        show = session.get(Monitored, episode.monitored_id)
        if not show:
            continue
        for torrent in torrents:
            name = str(getattr(torrent, "name", "") or "")
            if not name:
                continue
            if episode.release_title and not (
                name.startswith(episode.release_title) or episode.release_title.startswith(name)
            ):
                continue
            if not episode.release_title and not (
                show.display_name.lower() in name.lower()
                and f"{episode.episode_number:02d}" in name
            ):
                continue
            episode.torrent_hash = _torrent_hash(torrent)
            episode.release_title = name
            episode.feed_id = show.current_feed_id
            episode.status = EpisodeStatus.COMPLETED if _is_seeding_torrent(torrent) else EpisodeStatus.DOWNLOADING
            session.add(episode)
            break
    episodes = session.exec(select(Episode).where(Episode.status.in_([
        EpisodeStatus.QUEUED,
        EpisodeStatus.DOWNLOADING,
        EpisodeStatus.REPLACING,
    ]))).all()
    for episode in episodes:
        torrent = by_hash.get(episode.torrent_hash)
        if not torrent:
            if episode.status == EpisodeStatus.REPLACING:
                continue
            if episode.torrent_hash:
                reason = "Torrent is no longer present in qBittorrent; treating accepted download as completed."
                _complete_episode(session, episode, reason)
                logs.append(f"Assumed completed {show_name(episode, session)} Ep {episode.episode_number} after qBittorrent cleanup")
            continue
        episode.torrent_hash = _torrent_hash(torrent)
        if episode.status == EpisodeStatus.REPLACING:
            continue
        if _is_seeding_torrent(torrent):
            if episode.status != EpisodeStatus.COMPLETED:
                _complete_episode(session, episode)
                logs.append(f"Completed {show_name(episode, session)} Ep {episode.episode_number}")
        elif _torrent_state(torrent) in {"error", "missingfiles", "unknown"}:
            episode.status = EpisodeStatus.FAILED
            episode.last_error = f"qBittorrent torrent state: {_torrent_state(torrent)}"
            session.add(episode)
    session.commit()
    return logs


def _preferred_feed(show: Monitored, feeds: List[Feed]) -> Optional[Feed]:
    if not feeds:
        return None
    pinned = next((feed for feed in feeds if feed.id == show.pinned_feed_id), None)
    if pinned:
        return pinned
    current = next((feed for feed in feeds if feed.id == show.current_feed_id), None)
    return current or min(feeds, key=lambda feed: feed.priority)


def _mapped_episode(show: Monitored, raw_episode: int, latest_aired: Optional[int], target_count: int) -> Optional[int]:
    if raw_episode <= target_count:
        return raw_episode
    return None


def _start_operation(
    session: Session,
    qbit: QBitClient,
    settings: Settings,
    show: Monitored,
    feed: Feed,
    episode: Episode,
    article: Dict[str, Any],
    parsed: Dict[str, Any],
    version: int,
) -> Optional[TorrentOperation]:
    if _operation_for_episode(session, episode.id):
        return None
    torrent_url = str(article.get("torrentURL") or article.get("link") or article.get("id") or "")
    if not torrent_url:
        return None
    previous_status = episode.status
    previous_version = episode.version
    previous_feed_id = episode.feed_id
    previous_release_title = episode.release_title
    previous_release_group = episode.release_group
    previous_torrent_url = episode.torrent_url
    previous_operation_tag = episode.operation_tag
    episode.feed_id = feed.id
    episode.torrent_url = torrent_url
    episode.release_title = article.get("title", "")
    episode.release_group = parsed.get("release_group")
    operation_tag = _operation_tag()
    episode.operation_tag = operation_tag
    if version > previous_version:
        episode.status = EpisodeStatus.REPLACING
    else:
        episode.status = EpisodeStatus.QUEUED
    operation = TorrentOperation(
        episode_id=episode.id,
        kind="replace" if version > previous_version else "grab",
        operation_tag=operation_tag,
        release_title=article.get("title", ""),
        release_group=parsed.get("release_group"),
        version=version,
        new_torrent_url=torrent_url,
        old_torrent_hash=_find_old_hash(qbit, settings, show, episode) if version > previous_version else None,
    )
    session.add(episode)
    session.add(operation)
    session.commit()
    if settings.default_category and not qbit.ensure_category_exists(settings.default_category):
        _fail_operation(session, operation, episode, "Could not create the configured qBittorrent category.")
        return None
    save_path = resolve_save_path(
        settings.base_dir,
        show.title_english if getattr(settings, "title_language", "english") == "english" and show.title_english else (show.title_romaji or show.display_name),
        show.save_folder,
    )
    try:
        qbit.add_torrent(
            urls=operation.new_torrent_url,
            save_path=save_path,
            category=settings.default_category,
            tags=_episode_tags(show, episode.episode_number, version, operation.operation_tag),
            is_paused=operation.kind == "replace",
            ratio_limit=settings.default_seed_ratio,
        )
    except Exception as e:
        operation.status = TorrentOperationStatus.FAILED
        operation.last_error = str(e)
        operation.updated_at = utc_now()
        episode.status = previous_status
        episode.version = previous_version
        episode.feed_id = previous_feed_id
        episode.release_title = previous_release_title
        episode.release_group = previous_release_group
        episode.torrent_url = previous_torrent_url
        episode.operation_tag = previous_operation_tag
        episode.last_error = str(e)
        episode.retry_after = None
        if previous_status not in {
            EpisodeStatus.COMPLETED,
            EpisodeStatus.DOWNLOADING,
            EpisodeStatus.REPLACING,
        }:
            episode.status = EpisodeStatus.FAILED
        session.add(operation)
        session.add(episode)
        session.commit()
        return None
    show.current_feed_id = feed.id
    show.matched_title = parsed.get("title") or article.get("title", "")
    show.matched_release_group = parsed.get("release_group")
    show.status = MonitoredStatus.FIXED
    session.add(show)
    session.commit()
    torrent = _find_tagged(qbit, operation.operation_tag)
    if torrent:
        operation.new_torrent_hash = _torrent_hash(torrent)
        operation.status = TorrentOperationStatus.ADDED
        operation.updated_at = utc_now()
        session.add(operation)
        session.commit()
        _finish_operation(session, qbit, operation, episode)
    else:
        episode.status = EpisodeStatus.QUEUED
        episode.torrent_url = operation.new_torrent_url
        episode.operation_tag = operation.operation_tag
        episode.release_title = operation.release_title
        episode.release_group = operation.release_group
        session.add(episode)
        session.commit()
    return operation


def _decide(show: Monitored, title: str) -> Tuple[bool, Dict[str, Any]]:
    custom = (show.custom_regex or "").strip()
    if custom:
        try:
            if re.search(custom, title, re.IGNORECASE):
                excluded = show.custom_must_not or ""
                if excluded and re.search(excluded, title, re.IGNORECASE):
                    return False, {}
                return True, parse_release_title(title)
        except re.error:
            pass
    matched, _, parsed = match_release_to_show(title, show.aliases)
    if matched and show.custom_must_not:
        try:
            if re.search(show.custom_must_not, title, re.IGNORECASE):
                return False, {}
        except re.error:
            pass
    return matched, parsed


def evaluate_and_grab_releases(
    session: Session,
    qbit: QBitClient,
    settings: Settings,
    feeds: Optional[List[Feed]] = None,
    mode: str = "direct",
    rss_snapshot: Optional[RssSnapshot] = None,
) -> List[str]:
    logs: List[str] = []
    if mode == "direct":
        logs.extend(update_episode_status(session, qbit, settings))
    if feeds is None:
        feeds = session.exec(select(Feed).order_by(Feed.priority)).all()
    if not feeds:
        return logs
    try:
        articles_by_url = rss_snapshot.get() if rss_snapshot else flatten_rss_articles(qbit.get_rss_items(with_data=True))
    except QbitClientError as e:
        logger.warning(f"Could not fetch RSS items for direct grab: {e}")
        return logs
    first_seen = _sync_seen_items(session, feeds, articles_by_url)
    shows = session.exec(select(Monitored).where(Monitored.status.in_([
        MonitoredStatus.UNCONFIRMED,
        MonitoredStatus.FIXED,
        MonitoredStatus.STALLED,
    ]))).all()
    now = utc_now()
    for show in shows:
        episodes = sync_show_episodes(session, show)
        episodes_by_number = {episode.episode_number: episode for episode in episodes}
        failed_versions: Dict[int, set] = {}
        episode_ids = [episode.id for episode in episodes if episode.id]
        if episode_ids:
            failed_operations = session.exec(
                select(TorrentOperation).where(
                    TorrentOperation.episode_id.in_(episode_ids),
                    TorrentOperation.status == TorrentOperationStatus.FAILED,
                )
            ).all()
            for failed_operation in failed_operations:
                failed_versions.setdefault(failed_operation.episode_id, set()).add(failed_operation.version)
        target_count = show.total_episodes or max(show.next_airing_episode or 0, show.last_confirmed_episode or 0, 1)
        latest_aired = None
        airing_at = _aware(show.next_airing_at)
        if show.next_airing_episode:
            latest_aired = show.next_airing_episode if airing_at and airing_at <= now else max(0, show.next_airing_episode - 1)
        elif show.status == MonitoredStatus.COMPLETED:
            latest_aired = target_count
        if show.next_airing_episode == 1 and airing_at and airing_at > now:
            continue
        preferred = _preferred_feed(show, feeds)
        ordered = [preferred] if preferred else []
        if not show.pinned_feed_id:
            ordered.extend(feed for feed in feeds if not preferred or feed.id != preferred.id)
        for feed in ordered:
            for article in articles_by_url.get(feed.qbit_feed_url, []):
                title = article.get("title", "")
                if not title:
                    continue
                matched, parsed = _decide(show, title)
                if not matched:
                    continue
                raw_episode = parsed.get("episode")
                if raw_episode is None or raw_episode <= 0:
                    continue
                episode_number = _mapped_episode(show, int(raw_episode), latest_aired, target_count)
                if episode_number is None:
                    continue
                if latest_aired is not None and episode_number > latest_aired:
                    continue
                episode = episodes_by_number.get(episode_number)
                if not episode:
                    continue
                version = int(parsed.get("version") or 1)
                if version in failed_versions.get(episode.id, set()):
                    continue
                if episode.status == EpisodeStatus.FAILED and version <= episode.version:
                    continue
                if episode.status in {EpisodeStatus.COMPLETED, EpisodeStatus.DOWNLOADING, EpisodeStatus.REPLACING} and version <= episode.version:
                    continue
                if episode.status == EpisodeStatus.QUEUED and version <= episode.version:
                    continue
                item_id = str(article.get("id") or article.get("torrentURL") or article.get("link") or title)
                seen_at = first_seen.get((feed.qbit_feed_url, item_id))
                is_fresh = False
                if seen_at:
                    is_fresh = (now - _aware(seen_at)).total_seconds() < PREFERRED_FEED_GRACE_SECONDS
                if preferred and feed.id != preferred.id and (episode.status == EpisodeStatus.WANTED or version > episode.version) and is_fresh:
                    decision = GrabDecisionType.WOULD_WAIT
                    reason = f"Waiting for preferred feed {preferred.qbit_feed_name}."
                    _record_decision(session, show, episode, feed, article, title, episode_number, version, decision, reason)
                    logs.append(f"{reason} {show.display_name} Ep {episode_number}")
                    continue
                if mode == "observe":
                    decision = GrabDecisionType.WOULD_REPLACE if version > episode.version else GrabDecisionType.WOULD_GRAB
                    reason = "Higher release version detected." if decision == GrabDecisionType.WOULD_REPLACE else "Wanted episode detected."
                    _record_decision(session, show, episode, feed, article, title, episode_number, version, decision, reason)
                    logs.append(f"Would {'replace' if decision == GrabDecisionType.WOULD_REPLACE else 'grab'} {show.display_name} Ep {episode_number} v{version}: {title}")
                    continue
                if mode != "direct":
                    continue
                operation = _start_operation(session, qbit, settings, show, feed, episode, article, parsed, version)
                if operation:
                    if operation.kind == "replace":
                        logs.append(f"Queued replacement for {show.display_name} Ep {episode_number} v{version}: {title}")
                    else:
                        logs.append(f"Queued {show.display_name} Ep {episode_number} v{version}: {title}")
    return logs
