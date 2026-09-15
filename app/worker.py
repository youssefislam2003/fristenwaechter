"""Background worker — the ONLY process that performs notification I/O and
scheduled compliance sweeps. Runs the APScheduler; the web process never does.

Jobs registered here:
  * outbox relay        — every 15 s, drains OutboxMessage (app/jobs/relay.py)
  * sweep_escalations   — daily 06:00 Berlin (nag + breach) (app/jobs/sweeps.py)
  * send_reminders      — deadline notch sweep 90/30/7/0 (app/jobs/sweeps.py)
  * purge_expired       — nightly 03:00 DSGVO retention (app/jobs/dsgvo.py)

All connect as app_jobs (BYPASSRLS) via jobs_session_factory — the relay and
sweeps are legitimately cross-tenant. Graceful shutdown drains in-flight jobs
and disposes the DB pool so a container stop doesn't sever a live transaction.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.db import dispose_engines, get_jobs_session_factory
from app.ops import CompositeHeartbeat, FileHeartbeat, HttpHeartbeat
from app.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5.5s [%(name)s] %(message)s",
)
logger = logging.getLogger("app.worker")


def build_scheduler() -> AsyncIOScheduler:
    """Construct and populate the scheduler. Notifier and sweep modules are
    imported here (not at module top) so this file stays importable during
    incremental builds and so a missing optional job logs instead of crashing
    the whole worker."""
    settings = get_settings()
    session_factory = get_jobs_session_factory()
    # External dead-man switch (silence pages the operator) + local liveness
    # file (a stale file fails the container healthcheck → auto-restart).
    heartbeat = CompositeHeartbeat([
        HttpHeartbeat(settings.HEARTBEAT_URL),
        FileHeartbeat(settings.WORKER_HEARTBEAT_FILE),
    ])

    scheduler = AsyncIOScheduler(timezone="Europe/Berlin")

    # ── outbox relay (always present) ──────────────────────────────────
    from app.jobs import relay
    from app.notifiers import build_notifiers

    notifiers = build_notifiers(settings, session_factory)
    relay.register(scheduler, session_factory, notifiers, heartbeat)
    logger.info("registered: outbox_relay (every %ds)", relay.RELAY_INTERVAL_SECONDS)

    # ── scheduled sweeps (added in T6) ─────────────────────────────────
    try:
        from app.jobs import sweeps

        # Sweeps only ENQUEUE outbox rows; the relay owns delivery, so they
        # need no notifiers — just the session factory and the heartbeat.
        sweeps.register(scheduler, session_factory, heartbeat)
        logger.info("registered: escalation + reminder sweeps")
    except ModuleNotFoundError:
        logger.warning("app.jobs.sweeps not present yet — sweeps NOT scheduled")

    # ── DSGVO purge (added in T8) ──────────────────────────────────────
    try:
        from app.jobs import dsgvo

        dsgvo.register(scheduler, session_factory)
        logger.info("registered: purge_expired (nightly 03:00)")
    except ModuleNotFoundError:
        logger.warning("app.jobs.dsgvo not present yet — purge NOT scheduled")

    return scheduler


async def run() -> None:
    """Start the scheduler and block until a termination signal arrives,
    then shut down gracefully (finish in-flight jobs, dispose the pool)."""
    scheduler = build_scheduler()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows dev
            # Windows event loop lacks add_signal_handler; Ctrl-C still raises
            # KeyboardInterrupt which unwinds through the finally below.
            pass

    scheduler.start()
    logger.info("worker started; jobs running. Waiting for shutdown signal.")
    try:
        await stop.wait()
    finally:
        logger.info("shutdown signal received — draining scheduler")
        scheduler.shutdown(wait=True)  # let in-flight jobs finish
        await dispose_engines()
        logger.info("worker stopped cleanly")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("interrupted")


if __name__ == "__main__":
    main()
