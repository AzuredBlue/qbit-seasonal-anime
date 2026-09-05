from datetime import datetime, timezone, timedelta
import asyncio
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from qbit_seasonal_anime.db.session import get_engine, get_settings
from qbit_seasonal_anime.db.models import Monitored, Feed, RuleHistory, Settings, MonitoredStatus, RuleOutcome, utc_now
from qbit_seasonal_anime.clients.qbit import QBitClient, QbitClientError
from qbit_seasonal_anime.clients.anilist import AniListClient
from qbit_seasonal_anime.core.supervisor import Supervisor
from qbit_seasonal_anime.core.rules import delete_rule
from qbit_seasonal_anime.workers.scheduler import calculate_next_poll_interval
from qbit_seasonal_anime.server.state import state

router = APIRouter(prefix="/api")
engine = get_engine()
anilist_client = AniListClient()


def get_db():
    with Session(engine) as session:
        yield session


def get_qbit(session: Session = Depends(get_db)) -> QBitClient:
    s = get_settings(session)
    return QBitClient(host=s.qbit_host, username=s.qbit_username, password=s.qbit_password, timeout=10)


# -------------------------------------------------------------
# Shows Endpoints
# -------------------------------------------------------------
@router.get("/shows")
def get_shows(session: Session = Depends(get_db)):
    settings = get_settings(session)
    now = datetime.now(timezone.utc)
    shows = session.exec(select(Monitored).order_by(Monitored.id)).all()
    feeds = {f.id: f.qbit_feed_name for f in session.exec(select(Feed)).all()}
    prefer_english = (getattr(settings, "title_language", "english") == "english")

    result = []
    for s in shows:
        airing_at = s.next_airing_at
        if airing_at and airing_at.tzinfo is None:
            airing_at = airing_at.replace(tzinfo=timezone.utc)

        # Classification: Released / Currently Airing vs Planned Next Season / Upcoming
        is_released = (
            s.status in (MonitoredStatus.FIXED, MonitoredStatus.COMPLETED)
            or (s.next_airing_episode is not None and s.next_airing_episode > 1)
            or (s.last_confirmed_episode is not None and s.last_confirmed_episode >= 1)
            or (airing_at is not None and airing_at <= now)
        )

        english_title = s.title_english
        romaji_title = s.title_romaji or s.display_name
        effective_display_name = english_title if (prefer_english and english_title) else (romaji_title or s.display_name)

        from qbit_seasonal_anime.core.rules import sanitize_folder_name, compress_home_path
        base_template = settings.base_dir or "~/Anime/{name}"
        # If user explicitly customized save_folder to a custom path, use it; otherwise use effective_display_name
        is_custom_folder = bool(s.save_folder and s.save_folder != sanitize_folder_name(s.display_name) and s.save_folder != sanitize_folder_name(s.title_romaji or "") and s.save_folder != sanitize_folder_name(s.title_english or ""))
        if is_custom_folder and (s.save_folder.startswith("/") or s.save_folder.startswith("~")):
            resolved_save_path = s.save_folder
        else:
            folder_name = sanitize_folder_name(s.save_folder if is_custom_folder else effective_display_name)
            if "{name}" in base_template:
                resolved_save_path = base_template.replace("{name}", folder_name)
            else:
                resolved_save_path = f"{base_template.rstrip('/')}/{folder_name}"

        resolved_save_path = compress_home_path(resolved_save_path)

        result.append({
            "id": s.id,
            "anilist_id": s.anilist_id,
            "display_name": effective_display_name,
            "title_romaji": romaji_title,
            "title_english": s.title_english,
            "cover_image": s.cover_image,
            "season_name": s.season_name,
            "season_year": s.season_year,
            "status": s.status.value,
            "current_feed_id": s.current_feed_id,
            "current_feed_name": feeds.get(s.current_feed_id) if s.current_feed_id else None,
            "qbit_rule_name": s.qbit_rule_name,
            "total_episodes": s.total_episodes,
            "next_airing_episode": s.next_airing_episode,
            "next_airing_at": airing_at.isoformat() if airing_at else None,
            "next_airing_formatted": airing_at.strftime("%d/%m %H:%M") if airing_at else None,
            "last_confirmed_episode": s.last_confirmed_episode,
            "matched_title": s.matched_title,
            "matched_release_group": s.matched_release_group,
            "save_folder": s.save_folder,
            "save_path": resolved_save_path,
            "is_released": is_released,
        })
    return result


@router.post("/shows/{show_id}/pause")
def toggle_pause_show(show_id: int, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    if show.status == MonitoredStatus.PAUSED:
        # Restore the saved pre-pause status, falling back to a sensible guess
        if show.status_before_pause:
            try:
                show.status = MonitoredStatus(show.status_before_pause)
            except ValueError:
                show.status = MonitoredStatus.UNCONFIRMED
        elif show.current_feed_id and show.matched_release_group:
            show.status = MonitoredStatus.FIXED
        else:
            show.status = MonitoredStatus.UNCONFIRMED
        show.status_before_pause = None

        if show.qbit_rule_name:
            try:
                rules = qbit.get_rss_rules()
                if show.qbit_rule_name in rules:
                    rdef = rules[show.qbit_rule_name]
                    rdef["enabled"] = True
                    qbit.set_rss_rule(show.qbit_rule_name, rdef)
            except Exception as e:
                state.add_log(f"Warning enabling qBit rule for '{show.display_name}': {e}", "WARNING")

        session.add(show)
        session.commit()
        state.add_log(f"Resumed show '{show.display_name}'.", "INFO")
        return {"status": "success", "new_status": show.status.value, "message": f"Resumed '{show.display_name}'"}
    else:
        show.status_before_pause = show.status.value
        show.status = MonitoredStatus.PAUSED

        if show.qbit_rule_name:
            try:
                rules = qbit.get_rss_rules()
                if show.qbit_rule_name in rules:
                    rdef = rules[show.qbit_rule_name]
                    rdef["enabled"] = False
                    qbit.set_rss_rule(show.qbit_rule_name, rdef)
            except Exception as e:
                state.add_log(f"Warning disabling qBit rule for '{show.display_name}': {e}", "WARNING")

        session.add(show)
        session.commit()
        state.add_log(f"Paused show '{show.display_name}'.", "INFO")
        return {"status": "success", "new_status": show.status.value, "message": f"Paused '{show.display_name}'"}


@router.post("/shows/{show_id}/rediscover")
def rediscover_show(show_id: int, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    old_feed_id = show.current_feed_id
    if show.qbit_rule_name:
        try:
            delete_rule(qbit, show.qbit_rule_name)
        except Exception as e:
            state.add_log(f"Warning: Could not delete rule '{show.qbit_rule_name}': {e}", "WARNING")

    if old_feed_id:
        hist = RuleHistory(
            monitored_id=show.id,
            feed_id=old_feed_id,
            outcome=RuleOutcome.FALSE_POSITIVE,
            note="Manually reset/rediscovered from WebUI.",
        )
        session.add(hist)

    show.current_feed_id = None
    show.qbit_rule_name = None
    show.matched_title = None
    show.matched_release_group = None
    show.status = MonitoredStatus.UNCONFIRMED
    session.add(show)
    session.commit()

    state.add_log(f"Reset rule for '{show.display_name}'. Will rediscover on next supervision cycle.", "INFO")
    return {"status": "success", "message": f"Reset rule for '{show.display_name}'. Will rediscover on next cycle."}


@router.get("/shows/{show_id}/rule")
def get_show_rule_details(show_id: int, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    settings = get_settings(session)
    feed = session.get(Feed, show.current_feed_id) if show.current_feed_id else None

    # Fetch live rule from qBittorrent if rule_name exists
    qbit_rule_data = {}
    matched_articles = []
    if show.qbit_rule_name:
        try:
            qrules = qbit.get_rss_rules()
            qbit_rule_data = qrules.get(show.qbit_rule_name, {})
            articles_resp = qbit.get_matching_articles(show.qbit_rule_name)
            if isinstance(articles_resp, dict):
                for k, v in articles_resp.items():
                    if isinstance(v, list) and v:
                        matched_articles.extend(v)
            elif isinstance(articles_resp, list):
                matched_articles = articles_resp
        except Exception as e:
            state.add_log(f"Warning fetching rule details from qBit: {e}", "WARNING")

    # If qBittorrent in-memory matchingArticles API returned empty, evaluate regex against cached feed articles
    if not matched_articles and feed:
        try:
            from qbit_seasonal_anime.core.rules import build_regex_pattern
            import re
            from qbit_seasonal_anime.core.discovery import flatten_rss_articles

            must_pattern = qbit_rule_data.get("mustContain") or build_regex_pattern(show)
            must_not_pattern = qbit_rule_data.get("mustNotContain")

            if must_pattern:
                rss_tree = qbit.get_rss_items(with_data=True)
                all_cached = flatten_rss_articles(rss_tree)
                feed_items = all_cached.get(feed.qbit_feed_url, [])
                
                must_re = re.compile(must_pattern)
                must_not_re = re.compile(must_not_pattern) if must_not_pattern else None

                for item in feed_items:
                    title = item.get("title", "") if isinstance(item, dict) else str(item)
                    if must_re.search(title):
                        if must_not_re and must_not_re.search(title):
                            continue
                        matched_articles.append(title)
        except Exception as e:
            state.add_log(f"Warning matching against cached articles: {e}", "DEBUG")

    # If show was UNCONFIRMED but matching articles exist, auto-confirm to Working immediately
    if show.status == MonitoredStatus.UNCONFIRMED and matched_articles and feed:
        from qbit_seasonal_anime.core.matching import match_release_to_show
        best_ep = None
        best_title = None
        best_parsed = None
        for title in matched_articles:
            is_match, score, parsed = match_release_to_show(title, show.aliases)
            if is_match:
                ep = parsed.get("episode")
                if ep is not None:
                    if best_ep is None or ep > best_ep:
                        best_ep = ep
                        best_title = title
                        best_parsed = parsed

        if best_ep is not None and best_parsed:
            show.last_confirmed_episode = max(show.last_confirmed_episode or 0, best_ep)
            show.matched_title = best_parsed.get("title")
            show.matched_release_group = best_parsed.get("release_group")
            show.status = MonitoredStatus.FIXED

            hist_stmt = (
                select(RuleHistory)
                .where(RuleHistory.monitored_id == show.id)
                .order_by(RuleHistory.created_at.desc())
            )
            latest_hist = session.exec(hist_stmt).first()
            if latest_hist and latest_hist.outcome == RuleOutcome.PENDING:
                latest_hist.outcome = RuleOutcome.CONFIRMED
                latest_hist.note = f"Verified with RSS release: {best_title}"
                session.add(latest_hist)

            from qbit_seasonal_anime.core.rules import create_or_update_rule
            try:
                create_or_update_rule(
                    qbit_client=qbit,
                    monitored=show,
                    feed=feed,
                    base_dir=settings.base_dir,
                    category=settings.default_category,
                    ratio_limit=settings.default_seed_ratio,
                    release_group=show.matched_release_group,
                )
            except Exception as e:
                state.add_log(f"Warning tightening rule in qBittorrent: {e}", "WARNING")

            session.add(show)
            session.commit()
            state.add_log(f"Auto-confirmed rule for '{show.display_name}' (Ep {best_ep}) via '{best_title}' -> Works", "INFO")

    prefer_english = (getattr(settings, "title_language", "english") == "english")
    effective_display_name = show.title_english if (prefer_english and show.title_english) else (show.title_romaji or show.display_name)

    from qbit_seasonal_anime.core.rules import build_regex_pattern, build_rule_name, is_show_rule_enabled, compress_home_path, resolve_save_path, sanitize_folder_name
    expected_rule_name = build_rule_name(show.id or 0, effective_display_name)
    rule_is_enabled = qbit_rule_data.get("enabled") if "enabled" in qbit_rule_data else is_show_rule_enabled(show)
    
    # If user explicitly customized save_folder to a custom path, use it; otherwise use effective_display_name
    is_custom_folder = bool(show.save_folder and show.save_folder != sanitize_folder_name(show.display_name) and show.save_folder != sanitize_folder_name(show.title_romaji or "") and show.save_folder != sanitize_folder_name(show.title_english or ""))
    default_save_path = resolve_save_path(settings.base_dir, effective_display_name, show.save_folder if is_custom_folder else None)
    raw_save_path = qbit_rule_data.get("savePath") or default_save_path
    default_must_not = r"(720p|480p|540p|360p|576p|batch|complete|\(\d+[-~]\d+\)|\[\d+[-~]\d+\])"

    return {
        "show_id": show.id,
        "display_name": effective_display_name,
        "cover_image": show.cover_image,
        "has_rule": bool(show.current_feed_id or show.qbit_rule_name),
        "rule_name": show.qbit_rule_name or expected_rule_name,
        "enabled": rule_is_enabled,
        "feed_name": feed.qbit_feed_name if feed else None,
        "feed_url": feed.qbit_feed_url if feed else None,
        "must_contain": qbit_rule_data.get("mustContain") or show.custom_regex or (build_regex_pattern(show.aliases) if feed else None),
        "must_not_contain": qbit_rule_data.get("mustNotContain") or show.custom_must_not or default_must_not,
        "save_path": compress_home_path(raw_save_path),
        "save_folder": (show.save_folder if is_custom_folder else "") or "",
        "current_feed_id": show.current_feed_id or 0,
        "category": qbit_rule_data.get("assignedCategory", settings.default_category),
        "ratio_limit": (qbit_rule_data.get("torrentParams") or {}).get("ratio_limit", settings.default_seed_ratio),
        "status": show.status.value,
        "matched_title": show.matched_title,
        "matched_release_group": show.matched_release_group,
        "matched_articles": matched_articles[:15],
    }


class EditShowRequest(BaseModel):
    current_feed_id: Optional[int] = None
    save_folder: Optional[str] = None
    category: Optional[str] = None
    ratio_limit: Optional[float] = None
    must_contain: Optional[str] = None
    must_not_contain: Optional[str] = None


@router.post("/shows/{show_id}/edit")
def edit_show(show_id: int, req: EditShowRequest, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    settings = get_settings(session)
    old_feed_id = show.current_feed_id
    new_feed_id = None if req.current_feed_id is not None and req.current_feed_id <= 0 else (req.current_feed_id if req.current_feed_id is not None else show.current_feed_id)

    # Update save folder if provided
    if req.save_folder is not None:
        val = req.save_folder.strip()
        if not val or val == "{name}":
            show.save_folder = ""
        else:
            show.save_folder = val

    if req.must_contain is not None:
        show.custom_regex = req.must_contain.strip() if req.must_contain.strip() else None
    if req.must_not_contain is not None:
        show.custom_must_not = req.must_not_contain.strip() if req.must_not_contain.strip() else None

    category = req.category.strip() if req.category is not None and req.category.strip() else settings.default_category
    ratio_limit = req.ratio_limit if req.ratio_limit is not None and req.ratio_limit >= 0 else settings.default_seed_ratio

    # If old rule exists in qBittorrent, clean it up
    if show.qbit_rule_name:
        try:
            delete_rule(qbit, show.qbit_rule_name)
        except Exception as e:
            state.add_log(f"Warning deleting old rule '{show.qbit_rule_name}': {e}", "WARNING")
        show.qbit_rule_name = None

    if new_feed_id is None:
        # Reset to Auto-Discover
        show.current_feed_id = None
        show.matched_title = None
        show.matched_release_group = None
        show.status = MonitoredStatus.UNCONFIRMED
        session.add(show)
        session.commit()
        state.add_log(f"Reset '{show.display_name}' to Auto-Discover mode.", "INFO")
        return {"status": "success", "message": f"'{show.display_name}' set to Auto-Discover."}

    # User assigned a specific feed
    feed = session.get(Feed, new_feed_id)
    if not feed:
        raise HTTPException(status_code=400, detail="Selected feed not found")

    show.current_feed_id = feed.id

    # Check cached RSS articles in that feed to see if we can match immediately
    matched_article = None
    try:
        rss_data = qbit.get_rss_items(with_data=True)
        from qbit_seasonal_anime.core.discovery import flatten_rss_articles
        articles_by_feed = flatten_rss_articles(rss_data)
        feed_articles = articles_by_feed.get(feed.qbit_feed_url, [])

        from qbit_seasonal_anime.core.matching import match_release_to_show
        best_art = None
        best_parsed = None
        best_score = 0
        for art in feed_articles:
            is_match, score, parsed_info = match_release_to_show(art.get("title", ""), show.aliases)
            if is_match and score > best_score:
                best_score = score
                best_art = art
                best_parsed = parsed_info
        matched_article = best_art
    except Exception as e:
        state.add_log(f"Warning searching feed cache: {e}", "WARNING")

    from qbit_seasonal_anime.core.rules import create_or_update_rule
    if matched_article and best_parsed:
        show.matched_title = best_parsed.get("title")
        show.matched_release_group = best_parsed.get("release_group")
        show.status = MonitoredStatus.FIXED
        if req.must_contain is None:
            show.custom_regex = None
        rname = create_or_update_rule(
            qbit_client=qbit,
            monitored=show,
            feed=feed,
            base_dir=settings.base_dir,
            category=category,
            ratio_limit=ratio_limit,
            must_contain=show.custom_regex,
            must_not_contain=show.custom_must_not,
        )
        show.qbit_rule_name = rname
        msg = f"Assigned to '{feed.qbit_feed_name}' and matched cached release: {matched_article.get('title')} (Status: Working)"
    else:
        # Not in current cache -> set to Testing and create broad rule waiting for next episode drop
        show.matched_title = None
        show.matched_release_group = None
        show.status = MonitoredStatus.UNCONFIRMED
        rname = create_or_update_rule(
            qbit_client=qbit,
            monitored=show,
            feed=feed,
            base_dir=settings.base_dir,
            category=category,
            ratio_limit=ratio_limit,
            must_contain=show.custom_regex,
            must_not_contain=show.custom_must_not,
        )
        show.qbit_rule_name = rname
        msg = f"Assigned to '{feed.qbit_feed_name}'. Rule created in Testing mode, waiting for next episode drop."

    session.add(show)
    session.commit()
    state.add_log(f"Show '{show.display_name}': {msg}", "INFO")
    return {"status": "success", "message": msg}


@router.delete("/shows/{show_id}")
def delete_show(show_id: int, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    name = show.display_name
    if show.qbit_rule_name:
        try:
            delete_rule(qbit, show.qbit_rule_name)
        except Exception as e:
            state.add_log(f"Warning deleting rule for '{name}': {e}", "WARNING")

    # Delete history rows
    for h in session.exec(select(RuleHistory).where(RuleHistory.monitored_id == show.id)).all():
        session.delete(h)
    session.flush()
    session.delete(show)
    session.commit()

    state.add_log(f"Deleted show '{name}' from monitoring.", "INFO")
    return {"status": "success", "message": f"Deleted '{name}' from monitoring."}


# -------------------------------------------------------------
# Feeds Endpoints
# -------------------------------------------------------------
@router.get("/feeds")
def get_feeds(session: Session = Depends(get_db)):
    feeds = session.exec(select(Feed).order_by(Feed.priority)).all()
    return [{"id": f.id, "qbit_feed_name": f.qbit_feed_name, "qbit_feed_url": f.qbit_feed_url, "priority": f.priority} for f in feeds]


class ReorderItem(BaseModel):
    id: int
    priority: int


class ReorderRequest(BaseModel):
    feeds: List[ReorderItem]


@router.post("/feeds/reorder")
def reorder_feeds(req: ReorderRequest, session: Session = Depends(get_db)):
    for item in req.feeds:
        feed = session.get(Feed, item.id)
        if feed:
            feed.priority = item.priority
            session.add(feed)
    session.commit()
    state.add_log("RSS Feed priorities reordered.", "INFO")
    return {"status": "success", "message": "RSS Feed priorities updated."}


@router.post("/feeds/sync")
def sync_feeds(session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    settings = get_settings(session)
    sup = Supervisor(session=session, qbit=qbit, anilist=anilist_client, settings=settings)
    try:
        logs = sup.sync_feeds()
        for l in logs:
            state.add_log(l, "INFO")
        return {"status": "success", "logs": logs, "message": " | ".join(logs) if logs else "Feeds synced."}
    except Exception as e:
        state.add_log(f"Error syncing feeds: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------
# Settings Endpoints
# -------------------------------------------------------------
@router.get("/settings")
def get_current_settings(session: Session = Depends(get_db)):
    from qbit_seasonal_anime.core.rules import compress_home_path
    s = get_settings(session)
    base_dir = s.base_dir or "~/Anime/{name}"
    if base_dir and "{name}" not in base_dir:
        base_dir = f"{base_dir.rstrip('/')}/{{name}}"
    return {
        "qbit_host": s.qbit_host,
        "qbit_username": s.qbit_username,
        "qbit_password_set": bool(s.qbit_password),
        "base_dir": compress_home_path(base_dir),
        "default_category": s.default_category,
        "default_seed_ratio": s.default_seed_ratio,
        "anilist_username": s.anilist_username,
        "refresh_interval_minutes": s.refresh_interval_minutes,
        "stall_wait_hours": s.stall_wait_hours,
        "title_language": getattr(s, "title_language", "english") or "english",
    }


class UpdateSettingsRequest(BaseModel):
    qbit_host: Optional[str] = None
    qbit_username: Optional[str] = None
    qbit_password: Optional[str] = None
    base_dir: Optional[str] = None
    default_category: Optional[str] = None
    default_seed_ratio: Optional[float] = None
    anilist_username: Optional[str] = None
    refresh_interval_minutes: Optional[int] = None
    stall_wait_hours: Optional[int] = None
    title_language: Optional[str] = None


@router.post("/settings")
def update_settings(req: UpdateSettingsRequest, session: Session = Depends(get_db)):
    s = get_settings(session)
    if req.qbit_host is not None:
        s.qbit_host = req.qbit_host.strip()
    if req.qbit_username is not None:
        s.qbit_username = req.qbit_username.strip()
    if req.qbit_password is not None and req.qbit_password != "":
        s.qbit_password = req.qbit_password
    if req.base_dir is not None:
        raw_base = req.base_dir.strip()
        if raw_base and "{name}" not in raw_base:
            raw_base = f"{raw_base.rstrip('/\\')}/{{name}}"
        s.base_dir = raw_base
    if req.default_category is not None:
        s.default_category = req.default_category.strip()
    if req.default_seed_ratio is not None:
        s.default_seed_ratio = req.default_seed_ratio
    if req.anilist_username is not None:
        s.anilist_username = req.anilist_username.strip()
    if req.refresh_interval_minutes is not None:
        s.refresh_interval_minutes = req.refresh_interval_minutes
    if req.stall_wait_hours is not None:
        s.stall_wait_hours = req.stall_wait_hours
    if req.title_language is not None:
        new_lang = req.title_language.strip().lower()
        if new_lang != s.title_language:
            s.title_language = new_lang
            for show in session.exec(select(Monitored)).all():
                en_t = show.title_english
                ro_t = show.title_romaji
                new_display = en_t if (new_lang == "english" and en_t) else (ro_t or show.display_name)
                show.display_name = new_display
                session.add(show)

    session.add(s)
    session.commit()
    state.add_log("Settings updated successfully.", "INFO")
    return {"status": "success", "message": "Settings updated."}


@router.post("/settings/test-qbit")
def test_qbit_connection(session: Session = Depends(get_db)):
    s = get_settings(session)
    client = QBitClient(host=s.qbit_host, username=s.qbit_username, password=s.qbit_password, timeout=6)
    try:
        res = client.test_connection()
        return {"status": "success", "app_version": res.get("app_version"), "api_version": res.get("api_version")}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Connection failed: {e}")


@router.post("/settings/sync-anilist")
async def sync_anilist_now(session: Session = Depends(get_db)):
    s = get_settings(session)
    if not s.anilist_username:
        raise HTTPException(status_code=400, detail="AniList username is not set in Settings.")

    qbit = QBitClient(host=s.qbit_host, username=s.qbit_username, password=s.qbit_password, timeout=10)
    sup = Supervisor(session=session, qbit=qbit, anilist=anilist_client, settings=s)
    try:
        logs = []
        # 1. Sync RSS feeds from qBittorrent
        logs.extend(sup.sync_feeds())
        # 2. Sync schedule and import newly added shows from AniList
        sync_logs = await sup.sync_anilist_schedule()
        logs.extend(sync_logs)
        # 3. Automatically rediscover and bootstrap rules for new/unassigned shows
        bootstrap_logs = sup.bootstrap_unassigned_shows()
        logs.extend(bootstrap_logs)
        # 4. Verify and confirm rules against new articles
        from qbit_seasonal_anime.core.confirmation import verify_and_confirm_torrents
        confirm_logs = verify_and_confirm_torrents(session, qbit, s)
        logs.extend(confirm_logs)

        for l in logs:
            state.add_log(l, "INFO")

        msg = sync_logs[0] if sync_logs else "AniList synced."
        if bootstrap_logs:
            msg += f" Discovered feeds and created rules for {len(bootstrap_logs)} shows."
        return {"status": "success", "logs": logs, "message": msg}
    except Exception as e:
        state.add_log(f"AniList sync error: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/settings/clear-all")
def clear_all_monitored_debug(session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    shows = session.exec(select(Monitored)).all()
    count = len(shows)
    try:
        qrules = qbit.get_rss_rules()
        for rname in qrules:
            if (
                rname.startswith("[Seasonal]")
                or rname.startswith("[qbit-seasonal-anime]")
                or any(show.qbit_rule_name == rname for show in shows)
            ):
                delete_rule(qbit, rname)
    except Exception as e:
        state.add_log(f"Warning clearing qbit rules: {e}", "WARNING")

    for h in session.exec(select(RuleHistory)).all():
        session.delete(h)
    session.flush()

    for s in shows:
        session.delete(s)
    session.commit()

    state.add_log(f"Cleared all {count} monitored shows and deleted qBittorrent rules.", "WARNING")
    return {"status": "success", "message": f"Cleared all {count} shows."}


# -------------------------------------------------------------
# Supervision Cycle & Status Endpoints
# -------------------------------------------------------------
@router.post("/cycle/run")
async def run_cycle_now(session: Session = Depends(get_db)):
    if state.is_running_cycle:
        return {"status": "busy", "message": "A supervision cycle is already in progress."}

    s = get_settings(session)
    qbit = QBitClient(host=s.qbit_host, username=s.qbit_username, password=s.qbit_password, timeout=10)
    sup = Supervisor(session=session, qbit=qbit, anilist=anilist_client, settings=s)

    state.is_running_cycle = True
    state.add_log("Manual supervision cycle initiated from WebUI.", "INFO")
    try:
        logs = await sup.run_full_cycle()
        state.last_cycle_logs = logs
        state.last_cycle_time = datetime.now(timezone.utc)
        for l in logs:
            state.add_log(f"Supervisor: {l}", "INFO")

        # Recalculate next timer
        default_interval = max(60, s.refresh_interval_minutes * 60)
        sleep_sec, reason = calculate_next_poll_interval(
            session,
            default_interval_seconds=default_interval,
            qbit_client=qbit,
        )
        now_utc = datetime.now(timezone.utc)
        state.next_check_seconds = sleep_sec
        state.next_check_reason = reason
        state.next_check_time = now_utc
        state.target_next_check_time = now_utc + timedelta(seconds=sleep_sec)

        return {
            "status": "success",
            "logs": logs,
            "next_check_reason": reason,
            "next_check_seconds": sleep_sec,
            "target_next_check_time": state.target_next_check_time.isoformat(),
            "message": "Supervision cycle completed successfully."
        }
    except Exception as e:
        state.add_log(f"Error during supervision cycle: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        state.is_running_cycle = False


@router.get("/status")
def get_system_status(session: Session = Depends(get_db)):
    shows = session.exec(select(Monitored)).all()
    feeds_count = len(session.exec(select(Feed)).all())
    now = utc_now()

    works = sum(1 for s in shows if s.status == MonitoredStatus.FIXED)
    stalled = sum(1 for s in shows if s.status == MonitoredStatus.STALLED)
    paused = sum(1 for s in shows if s.status == MonitoredStatus.PAUSED)
    completed = sum(1 for s in shows if s.status == MonitoredStatus.COMPLETED)

    upcoming = 0
    testing = 0
    for s in shows:
        if s.status == MonitoredStatus.UNCONFIRMED:
            airing_at = s.next_airing_at
            if airing_at and airing_at.tzinfo is None:
                airing_at = airing_at.replace(tzinfo=timezone.utc)
            is_unreleased = (
                (s.next_airing_episode == 1 or s.next_airing_episode is None)
                and (s.last_confirmed_episode or 0) == 0
                and (airing_at is None or airing_at > now)
            )
            if is_unreleased:
                upcoming += 1
            else:
                testing += 1

    remaining_seconds = 0
    if state.target_next_check_time:
        rem = (state.target_next_check_time - now).total_seconds()
        remaining_seconds = max(0, int(rem))
    elif state.next_check_seconds:
        remaining_seconds = state.next_check_seconds

    return {
        "daemon_active": True,
        "is_running_cycle": state.is_running_cycle,
        "last_cycle_time": state.last_cycle_time.isoformat() if state.last_cycle_time else None,
        "next_check_reason": state.next_check_reason,
        "next_check_seconds": remaining_seconds,
        "target_next_check_time": state.target_next_check_time.isoformat() if state.target_next_check_time else None,
        "total_shows": len(shows),
        "counts": {
            "works": works,
            "upcoming": upcoming,
            "testing": testing,
            "stalled": stalled,
            "paused": paused,
            "completed": completed,
            "feeds": feeds_count,
        }
    }


@router.get("/logs")
def get_recent_logs(limit: int = 100):
    all_logs = list(state.logs)
    return all_logs[-limit:]
