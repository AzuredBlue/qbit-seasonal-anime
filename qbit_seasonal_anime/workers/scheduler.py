import asyncio
import logging
import signal
from datetime import timezone
from typing import Optional, Tuple
from sqlmodel import Session, select
from qbit_seasonal_anime.clients.anilist import AniListClient
from qbit_seasonal_anime.clients.qbit import QBitClient
from qbit_seasonal_anime.core.supervisor import Supervisor
from qbit_seasonal_anime.db.models import Monitored, MonitoredStatus, utc_now
from qbit_seasonal_anime.db.session import get_engine, get_settings

logger = logging.getLogger("qbit_seasonal_anime.workers.scheduler")


def calculate_next_poll_interval(
    session: Session,
    default_interval_seconds: int = 21600,
    hunting_interval_seconds: Optional[int] = None,
    qbit_client: Optional[QBitClient] = None,
) -> Tuple[int, str]:
    """
    Dynamically calculate the optimal sleep duration until the next check:
    - Shows whose RSS rules already work (FIXED) are ignored, as qBittorrent downloads them automatically.
    - Shows without release dates (next_airing_at is None) are ignored from hunting; they wait for AniList schedule.
    - If any upcoming/unconfirmed show has aired recently (next_airing_at <= now) -> Hunting mode (qBittorrent RSS refresh rate + 15s).
    - If the next upcoming unconfirmed show airs sooner than default interval -> Sleep until its TV air time to bind rule.
    - Otherwise (all active shows have working rules or are waiting for air dates) -> Sleep default interval (e.g. 6 hours).
    """
    now = utc_now()
    shows = session.exec(select(Monitored)).all()

    if not shows:
        return default_interval_seconds, "No monitored shows. Sleeping default interval."

    # Determine dynamic hunting interval based on qBittorrent RSS refresh rate (+15s)
    effective_hunting_interval = hunting_interval_seconds
    if effective_hunting_interval is None:
        if qbit_client:
            effective_hunting_interval = qbit_client.get_rss_refresh_interval_seconds()
        else:
            effective_hunting_interval = 315  # 5 minutes + 15s

    hunting_shows = []
    unresolved_upcoming = []

    for s in shows:
        # Ignore completed, paused, and already WORKING shows (qBittorrent handles working rules automatically)
        if s.status in (MonitoredStatus.PAUSED, MonitoredStatus.COMPLETED, MonitoredStatus.FIXED):
            continue

        airing_at = s.next_airing_at
        if airing_at and airing_at.tzinfo is None:
            airing_at = airing_at.replace(tzinfo=timezone.utc)

        # Only evaluate shows that have an established air date
        if airing_at is None:
            # Announced/upcoming show without an air date yet (e.g. Ao Ashi S2) -> do not hunt
            continue

        # Show does not have a confirmed working rule yet
        if s.current_feed_id is None or s.status == MonitoredStatus.STALLED:
            if airing_at <= now:
                hunting_shows.append(s)
            else:
                unresolved_upcoming.append((airing_at, s))
        elif s.status == MonitoredStatus.UNCONFIRMED:
            if airing_at <= now:
                hunting_shows.append(s)
            else:
                unresolved_upcoming.append((airing_at, s))

    # 1. If actively hunting for a release that just aired on TV to create/verify rule
    if hunting_shows:
        names = ", ".join(f"'{s.display_name}'" for s in hunting_shows[:3])
        if len(hunting_shows) > 3:
            names += f" and {len(hunting_shows) - 3} more"
        h_mins = effective_hunting_interval // 60
        h_secs = effective_hunting_interval % 60
        interval_str = f"{h_mins}m {h_secs}s" if h_secs else f"{h_mins}m"
        return effective_hunting_interval, f"Hunting mode: {len(hunting_shows)} show(s) waiting for rule/release ({names}). Checking every {interval_str}."

    # 2. If an unassigned/upcoming show premieres sooner than default interval -> wake on air time to bind rule
    if unresolved_upcoming:
        unresolved_upcoming.sort(key=lambda x: x[0])
        earliest_air, earliest_show = unresolved_upcoming[0]
        seconds_until_air = int((earliest_air - now).total_seconds())
        if seconds_until_air < default_interval_seconds:
            sleep_duration = max(60, seconds_until_air)
            air_str = earliest_air.strftime("%d/%m %H:%M UTC")
            return sleep_duration, f"Upcoming premiere: '{earliest_show.display_name}' airs on {air_str} (in {sleep_duration // 60}m). Sleeping until air time to create rule."

    return default_interval_seconds, f"All active shows have working rules or are waiting for air dates. Sleeping {default_interval_seconds // 60}m until next routine check."


async def run_daemon_loop(poll_interval_seconds: Optional[int] = None) -> None:
    """Run the supervisor indefinitely in smart-adaptive background daemon mode."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting qbit-seasonal-anime supervisor in daemon mode...")

    engine = get_engine()
    anilist = AniListClient()
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    while not stop_event.is_set():
        with Session(engine) as session:
            settings = get_settings(session)
            default_interval = poll_interval_seconds or (settings.refresh_interval_minutes * 60)
            default_interval = max(60, default_interval)

            qbit = QBitClient(
                host=settings.qbit_host,
                username=settings.qbit_username,
                password=settings.qbit_password,
            )
            supervisor = Supervisor(session=session, qbit=qbit, anilist=anilist, settings=settings)

            try:
                logger.info("Executing supervisor cycle...")
                logs = await supervisor.run_full_cycle()
                for l in logs:
                    logger.info(f"Supervisor: {l}")
            except Exception as e:
                logger.error(f"Error in supervisor cycle: {e}", exc_info=True)

            sleep_duration, reason = calculate_next_poll_interval(
                session,
                default_interval_seconds=default_interval,
                qbit_client=qbit,
            )
            logger.info(f"{reason} (Next check in {sleep_duration}s / {sleep_duration // 60}m)")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_duration)
        except asyncio.TimeoutError:
            pass

    logger.info("qbit-seasonal-anime supervisor daemon stopped gracefully.")
