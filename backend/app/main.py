from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlmodel import Session, select

from fastapi import Request
from fastapi.responses import JSONResponse

from app.api import activities, admin, network as network_api, nodes as nodes_api, attendees, auth, export, files, history, imports, pdpa, people, photo_batches, recognition, reports, settings_routes, system, update_routes, uploads
from app.auth.security import hash_password
from app.config import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    APP_VERSION,
    RETENTION_CHECK_INTERVAL_SECONDS,
    UPDATE_CHECK_INTERVAL_MINUTES,
)
from app.database.db import engine, init_db
from app.face_recognition.engine import get_face_app
from app.face_recognition.index import recognition_index
from app.models.models import Person, User
from app.services import maintenance, settings_cache
from app.services.photo_processing_service import run_retention_cleanup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Reconize — Event Management & Face Recognition")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Paths still allowed to accept writes while an update is in progress —
# without these the admin could not drive or recover the very update that put
# the app into maintenance mode.
_MAINTENANCE_EXEMPT_PREFIXES = ("/api/update", "/api/auth")


@app.middleware("http")
async def maintenance_guard(request: Request, call_next):
    """Refuse writes during the critical update window.

    One middleware rather than a dependency added to every existing router:
    the point is to cover check-ins, imports, PDPA changes, settings writes and
    photo-batch creation without editing any of those features. Maintenance
    state is read from the durable update state file, so it survives the
    backend restart that happens mid-update.
    """
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        path = request.url.path
        if not path.startswith(_MAINTENANCE_EXEMPT_PREFIXES) and maintenance.is_active():
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "Maintenance in progress — an application update is running. "
                    f"({maintenance.reason()}) Please try again in a moment."
                },
            )
    return await call_next(request)


def seed_demo_admin() -> None:
    with Session(engine) as session:
        existing = session.exec(select(User).where(User.username == ADMIN_USERNAME)).first()
        if existing:
            return
        admin = User(username=ADMIN_USERNAME, password_hash=hash_password(ADMIN_PASSWORD), role="admin")
        session.add(admin)
        session.commit()


async def _retention_loop() -> None:
    """Lightweight periodic check for expired photo batches — plain asyncio,
    no new scheduler dependency. Only runs while this backend process is up,
    same as every other background behavior in this app (the kiosk, live
    recognition, etc. are all likewise only active while the server runs)."""
    while True:
        await asyncio.sleep(RETENTION_CHECK_INTERVAL_SECONDS)
        try:
            cleaned = await asyncio.to_thread(run_retention_cleanup)
            if cleaned:
                logger.info("Retention cleanup: removed %d expired photo batch(es)", cleaned)
        except Exception:  # noqa: BLE001 — a cleanup failure must never kill the loop
            logger.exception("Retention cleanup check failed")


async def _update_check_loop() -> None:
    """Periodic background check, on the same plain-asyncio pattern as the
    retention loop. Detection only — an update is never installed without an
    admin explicitly asking for it."""
    from app.services import update_service

    while True:
        await asyncio.sleep(max(UPDATE_CHECK_INTERVAL_MINUTES, 5) * 60)
        try:
            if not update_service.is_configured():
                continue
            result = await asyncio.to_thread(update_service.check_for_update, True)
            if result.get("update_available"):
                logger.info("Update available: %s (installed %s)", result["latest_version"], APP_VERSION)
        except Exception:  # noqa: BLE001 — a failed check must never kill the loop
            logger.exception("Update check failed")


@app.on_event("startup")
async def on_startup() -> None:
    t0 = time.perf_counter()
    init_db()
    seed_demo_admin()

    # Load the face model and every registered embedding into memory now,
    # not on the first recognition request — the app should come up already
    # warm and ready for real-time recognition. get_face_app() also runs one
    # throwaway inference internally to pay ONNX Runtime's first-call cost
    # here, not on the user's first real scan (that cost is logged separately
    # inside engine.py as "Model warm-up inference took ...").
    t_model_start = time.perf_counter()
    get_face_app()
    model_ready_ms = (time.perf_counter() - t_model_start) * 1000

    with Session(engine) as session:
        recognition_index.rebuild(session.exec(select(Person)).all())
        settings_cache.load_from_db(session)

    logger.info(
        "Startup complete in %.0fms total (model load + warm-up inference: %.0fms, %d faces indexed)",
        (time.perf_counter() - t0) * 1000,
        model_ready_ms,
        recognition_index.size(),
    )

    # An update that crashed mid-flight would otherwise leave this backend
    # silently refusing writes; surface it instead.
    abandoned = maintenance.abandoned_update()
    if abandoned:
        logger.warning(
            "A previous update did not complete (stage: %s). Technical log: %s",
            abandoned.get("stage", "unknown"),
            abandoned.get("log_path", "n/a"),
        )

    asyncio.create_task(_retention_loop())
    asyncio.create_task(_update_check_loop())


app.include_router(auth.router)
app.include_router(people.router)
app.include_router(recognition.router)
app.include_router(uploads.router)
app.include_router(attendees.router)
app.include_router(history.router)
app.include_router(imports.router)
app.include_router(reports.router)
app.include_router(settings_routes.router)
app.include_router(export.router)
app.include_router(files.router)
app.include_router(system.router)
app.include_router(admin.router)
app.include_router(pdpa.router)
app.include_router(activities.router)
app.include_router(network_api.router)
app.include_router(nodes_api.router)
app.include_router(photo_batches.router)
app.include_router(photo_batches.cleanup_router)
app.include_router(update_routes.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Serve the built React frontend (frontend/dist) if present, so the whole app
# can be run as a single backend process without needing the Vite dev server.
# That single-origin mode is what makes the app reachable from other machines:
# one host, one port, no CORS, and no hardcoded API address to get wrong.
FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


class SPAStaticFiles(StaticFiles):
    """StaticFiles that falls back to index.html for unknown paths.

    The app is a single-page app: only "/" exists as a real file, so opening
    /activities or a station link like /recognition?activity=... directly - or
    just pressing refresh on one - would otherwise 404. Those station links are
    the whole point of running stations on other machines, so the fallback is
    not optional here.

    Anything under /api is never reached by this: those routes are registered
    before the mount and win.
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                # A missing ASSET should still 404 rather than silently return
                # HTML - that turns a broken build into a confusing blank page.
                if "." in Path(path).name:
                    raise
                return await super().get_response("index.html", scope)
            raise


if FRONTEND_DIST.exists():
    app.mount("/", SPAStaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
