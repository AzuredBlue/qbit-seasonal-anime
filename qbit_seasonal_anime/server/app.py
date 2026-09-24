import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import logging
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from qbit_seasonal_anime.db.session import get_engine, get_settings, init_db
from qbit_seasonal_anime.clients.qbit import QBitClient, QbitAuthenticationError, QbitClientError
from qbit_seasonal_anime.clients.anilist import AniListClient
from qbit_seasonal_anime.core.supervisor import Supervisor
from qbit_seasonal_anime.workers.scheduler import calculate_next_poll_interval
from qbit_seasonal_anime.server.api import router
from qbit_seasonal_anime.server.state import state
from qbit_seasonal_anime.server.web_ui import get_web_ui_html

logger = logging.getLogger("qbit_seasonal_anime.server")


QBIT_RETRY_DELAYS = (1, 2, 4, 8, 16, 30, 60)
QBIT_AUTH_RETRY_SECONDS = 300


def _format_sleep(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m"


def _set_next_check(seconds: int, reason: str) -> None:
    now_utc = datetime.now(timezone.utc)
    state.next_check_seconds = seconds
    state.next_check_reason = reason
    state.target_next_check_time = now_utc + timedelta(seconds=seconds)
    state.add_log(f"Next check: {reason} (Sleeping {_format_sleep(seconds)}).", "INFO")


def _next_qbit_retry(retry_index: int) -> int:
    return QBIT_RETRY_DELAYS[min(retry_index, len(QBIT_RETRY_DELAYS) - 1)]


async def background_supervisor_task():
    """Supervisor loop running concurrently with the WebUI server."""
    engine = get_engine()
    anilist = AniListClient()
    state.add_log("Background supervisor service initialized.", "INFO")
    retry_index = 0

    while True:
        connection_ready = False
        sleep_seconds = 60
        try:
            with Session(engine) as session:
                settings = get_settings(session)
                qbit = QBitClient(host=settings.qbit_host, username=settings.qbit_username, password=settings.qbit_password, timeout=10)
                try:
                    await asyncio.to_thread(qbit.test_connection)
                    connection_ready = True
                    if retry_index:
                        state.add_log("qBittorrent connection restored.", "INFO")
                    retry_index = 0
                except QbitAuthenticationError as e:
                    sleep_seconds = QBIT_AUTH_RETRY_SECONDS
                    _set_next_check(sleep_seconds, f"qBittorrent authentication failed: {e}")
                    state.add_log("qBittorrent authentication failed; check the configured username and password.", "ERROR")
                except QbitClientError as e:
                    sleep_seconds = _next_qbit_retry(retry_index)
                    retry_index = min(retry_index + 1, len(QBIT_RETRY_DELAYS) - 1)
                    _set_next_check(sleep_seconds, f"qBittorrent is unavailable: {e}")

                if connection_ready:
                    supervisor = Supervisor(session=session, qbit=qbit, anilist=anilist, settings=settings)

                    state.is_running_cycle = True
                    state.add_log("Executing background supervision check...", "INFO")
                    try:
                        logs = await supervisor.run_full_cycle()
                        await asyncio.to_thread(qbit.test_connection)
                        state.last_cycle_time = datetime.now(timezone.utc)
                        for l in logs:
                            state.add_log(f"Supervisor: {l}", "INFO")
                        if not logs:
                            state.add_log("Supervisor: All shows and rules up to date.", "INFO")
                    except QbitAuthenticationError as e:
                        connection_ready = False
                        sleep_seconds = QBIT_AUTH_RETRY_SECONDS
                        _set_next_check(sleep_seconds, f"qBittorrent authentication failed: {e}")
                    except QbitClientError as e:
                        connection_ready = False
                        sleep_seconds = _next_qbit_retry(retry_index)
                        retry_index = min(retry_index + 1, len(QBIT_RETRY_DELAYS) - 1)
                        _set_next_check(sleep_seconds, f"qBittorrent became unavailable during the cycle: {e}")
                    except Exception as e:
                        state.add_log(f"Supervisor cycle error: {e}", "ERROR")
                        logger.error(f"Supervisor error: {e}", exc_info=True)
                    finally:
                        state.is_running_cycle = False

                    if connection_ready:
                        default_interval = max(60, settings.refresh_interval_minutes * 60)
                        try:
                            sleep_seconds, reason = await asyncio.to_thread(
                                calculate_next_poll_interval,
                                session,
                                default_interval_seconds=default_interval,
                                qbit_client=qbit,
                                download_mode=settings.download_mode,
                            )
                        except QbitAuthenticationError as e:
                            connection_ready = False
                            sleep_seconds = QBIT_AUTH_RETRY_SECONDS
                            _set_next_check(sleep_seconds, f"qBittorrent authentication failed: {e}")
                        except QbitClientError as e:
                            connection_ready = False
                            sleep_seconds = _next_qbit_retry(retry_index)
                            retry_index = min(retry_index + 1, len(QBIT_RETRY_DELAYS) - 1)
                            _set_next_check(sleep_seconds, f"qBittorrent became unavailable while scheduling: {e}")
                        else:
                            _set_next_check(sleep_seconds, reason)

            if not connection_ready:
                await asyncio.sleep(sleep_seconds)
                continue

            state.wake_event.clear()
            try:
                await asyncio.wait_for(state.wake_event.wait(), timeout=sleep_seconds)
                state.add_log("Supervisor woke up early from manual WebUI trigger.", "INFO")
            except asyncio.TimeoutError:
                pass

        except asyncio.CancelledError:
            state.add_log("Background supervisor stopped.", "INFO")
            break
        except Exception as e:
            state.add_log(f"Unexpected error in background task: {e}", "ERROR")
            logger.error(f"Scheduler loop error: {e}", exc_info=True)
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = get_engine()
    init_db(engine)

    bg_task = asyncio.create_task(background_supervisor_task())
    yield
    bg_task.cancel()
    try:
        await bg_task
    except asyncio.CancelledError:
        pass


def create_app() -> FastAPI:
    app = FastAPI(title="qbit-seasonal-anime", lifespan=lifespan)
    app.include_router(router)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return get_web_ui_html()

    return app


app = create_app()
