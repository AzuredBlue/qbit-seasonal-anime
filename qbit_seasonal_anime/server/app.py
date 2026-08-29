import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import logging
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from qbit_seasonal_anime.db.session import get_engine, get_settings, init_db
from qbit_seasonal_anime.clients.qbit import QBitClient
from qbit_seasonal_anime.clients.anilist import AniListClient
from qbit_seasonal_anime.core.supervisor import Supervisor
from qbit_seasonal_anime.workers.scheduler import calculate_next_poll_interval
from qbit_seasonal_anime.server.api import router
from qbit_seasonal_anime.server.state import state
from qbit_seasonal_anime.server.web_ui import get_web_ui_html

logger = logging.getLogger("qbit_seasonal_anime.server")


async def background_supervisor_task():
    """Supervisor loop running concurrently with the WebUI server."""
    engine = get_engine()
    anilist = AniListClient()
    state.add_log("Background supervisor service initialized.", "INFO")

    # Initial cycle on startup
    await asyncio.sleep(2)
    while True:
        try:
            with Session(engine) as session:
                settings = get_settings(session)
                qbit = QBitClient(host=settings.qbit_host, username=settings.qbit_username, password=settings.qbit_password, timeout=10)
                supervisor = Supervisor(session=session, qbit=qbit, anilist=anilist, settings=settings)

                state.is_running_cycle = True
                state.add_log("Executing background supervision check...", "INFO")
                try:
                    logs = await supervisor.run_full_cycle()
                    state.last_cycle_logs = logs
                    state.last_cycle_time = datetime.now(timezone.utc)
                    for l in logs:
                        state.add_log(f"Supervisor: {l}", "INFO")
                    if not logs:
                        state.add_log("Supervisor: All shows and rules up to date.", "INFO")
                except Exception as e:
                    state.add_log(f"Supervisor cycle error: {e}", "ERROR")
                    logger.error(f"Supervisor error: {e}", exc_info=True)
                finally:
                    state.is_running_cycle = False

                default_interval = max(60, settings.refresh_interval_minutes * 60)
                sleep_seconds, reason = calculate_next_poll_interval(
                    session,
                    default_interval_seconds=default_interval,
                    qbit_client=qbit,
                )
                now_utc = datetime.now(timezone.utc)
                state.next_check_seconds = sleep_seconds
                state.next_check_reason = reason
                state.next_check_time = now_utc
                state.target_next_check_time = now_utc + timedelta(seconds=sleep_seconds)
                state.add_log(f"Next check: {reason} (Sleeping {sleep_seconds // 60}m)...", "INFO")

            # Sleep or wait for wake_event (manual cycle trigger from WebUI)
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
    # Startup: Ensure DB schema & tables are initialized
    engine = get_engine()
    init_db(engine)

    # Launch background supervisor task
    bg_task = asyncio.create_task(background_supervisor_task())
    yield
    # Shutdown
    bg_task.cancel()
    try:
        await bg_task
    except asyncio.CancelledError:
        pass


def create_app() -> FastAPI:
    app = FastAPI(title="qbit-seasonal-anime", lifespan=lifespan)
    app.include_router(router)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return get_web_ui_html()

    return app


app = create_app()
