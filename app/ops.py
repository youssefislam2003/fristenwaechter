"""Operational primitives shared by the worker: the dead-man heartbeat.

The relay (app/jobs/relay.py) pings a ``DeadManSwitch`` after every fully
successful cycle. Its SILENCE — not any error it emits — is what pages the
operator: a worker that has crashed or wedged stops pinging, and the external
monitor (Healthchecks.io-style) fires. This inverts the usual "alert on
error" model, which cannot detect a process that died before it could report.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


class _Heartbeat(Protocol):
    async def ping(self) -> None: ...


class HttpHeartbeat:
    """Pings a Healthchecks.io-style URL. A failure to ping is logged but
    never raised: the heartbeat is a health SIGNAL, not part of the relay's
    correctness path, and must not turn a healthy cycle into a crash loop."""

    def __init__(self, url: str, *, timeout: float = 10.0) -> None:
        self._url = url
        self._timeout = timeout

    async def ping(self) -> None:
        if not self._url:  # unconfigured (e.g. local dev) → no-op
            return
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(self._url)
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — heartbeat must never crash caller
            logger.warning("heartbeat ping failed: %s", exc)


class FileHeartbeat:
    """Touches a file after every healthy cycle. A container healthcheck
    (scripts/worker_healthcheck.py) then treats a STALE file as unhealthy —
    the local dead-man switch that lets Docker/K8s restart a wedged worker
    even when the external monitor can't reach it."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    async def ping(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch()
        except OSError as exc:  # pragma: no cover - fs failure is non-fatal
            logger.warning("heartbeat file touch failed: %s", exc)


class CompositeHeartbeat:
    """Fan a single ping out to several heartbeats (e.g. external HTTP + local
    file). One failing sink never blocks the others."""

    def __init__(self, sinks: list[_Heartbeat]) -> None:
        self._sinks = sinks

    async def ping(self) -> None:
        for sink in self._sinks:
            await sink.ping()


class NullHeartbeat:
    """No-op heartbeat for tests and single-shot invocations."""

    async def ping(self) -> None:  # pragma: no cover - trivial
        return
