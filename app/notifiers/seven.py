"""seven.io SMS notifier (DE subprocessor).

Implements the relay's Notifier protocol. Two responsibilities beyond a plain
HTTP call:
  * SMS budget — non-critical templates consult a per-company monthly quota;
    when exhausted the SMS is skipped (a sentinel id is returned so the relay
    marks the outbox row done rather than retrying forever) and a WARNING
    alert is raised once per month. Critical §21 templates NEVER consult the
    quota. The company_id needed for the count is read from the payload, which
    every capped enqueuer includes.
  * Error mapping — non-2xx, a seven.io non-"100" status code, timeouts, and
    transport errors all become NotifierError for the relay to back off on.
"""
from __future__ import annotations

import logging
import uuid
from typing import Protocol

import httpx

from app.jobs.relay import NotifierError
from app.notifiers.templates import render_sms
from app.settings import Settings

logger = logging.getLogger(__name__)

_SEVEN_ENDPOINT = "https://gateway.seven.io/api/sms"
_TIMEOUT = httpx.Timeout(10.0)

# Critical, liability-bearing templates that bypass the SMS budget entirely.
CRITICAL_SMS_TEMPLATES = frozenset({"fsk_failed_critical", "escalation_breached"})

# Returned to the relay when a non-critical SMS is dropped for budget reasons.
# It is a successful "delivery" from the relay's perspective (no retry), while
# the WARNING alert records the suppression for the customer.
SKIPPED_SENTINEL_PREFIX = "skipped:sms_cap:"


class SmsQuota(Protocol):
    async def over_limit(self, company_id: uuid.UUID) -> bool: ...
    async def record_skip(self, company_id: uuid.UUID) -> None: ...


class SevenNotifier:
    def __init__(
        self,
        settings: Settings,
        *,
        quota: SmsQuota | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._quota = quota
        self._transport = transport

    async def send(
        self, *, recipient: str, template: str, payload: dict[str, object], idempotency_key: str
    ) -> str:
        # ── budget gate (non-critical only) ──
        if self._quota is not None and template not in CRITICAL_SMS_TEMPLATES:
            company_id = payload.get("company_id")
            if company_id is not None:
                cid = uuid.UUID(str(company_id))
                if await self._quota.over_limit(cid):
                    await self._quota.record_skip(cid)
                    logger.info(
                        "SMS suppressed (cap) template=%s company=%s", template, cid
                    )
                    return f"{SKIPPED_SENTINEL_PREFIX}{cid}"

        body = render_sms(template, payload)
        params = {
            "to": recipient,
            "text": body,
            "from": self._settings.SMS_SENDER,
            "foreign_id": idempotency_key,  # seven.io idempotency/tracking handle
            "json": 1,
        }
        headers = {
            "X-Api-Key": self._settings.SEVEN_IO_API_KEY.get_secret_value(),
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, transport=self._transport
            ) as client:
                resp = await client.post(
                    _SEVEN_ENDPOINT, data=params, headers=headers
                )
        except httpx.HTTPError as exc:
            raise NotifierError(f"seven.io transport error: {exc}") from exc

        if resp.status_code // 100 != 2:
            raise NotifierError(
                f"seven.io HTTP {resp.status_code}: {resp.text[:200]}"
            )

        data = _parse(resp)
        # seven.io success code is the string "100". Anything else is a failure
        # even under HTTP 200 (their convention).
        if str(data.get("success")) != "100":
            raise NotifierError(f"seven.io rejected: {resp.text[:200]}")

        messages = data.get("messages")
        msg_id = None
        if isinstance(messages, list) and messages:
            first = messages[0]
            if isinstance(first, dict):
                msg_id = first.get("id")
        return str(msg_id) if msg_id else f"seven:{idempotency_key}"


def _parse(resp: httpx.Response) -> dict[str, object]:
    try:
        parsed = resp.json()
    except ValueError as exc:
        raise NotifierError(f"seven.io non-JSON response: {resp.text[:200]}") from exc
    if not isinstance(parsed, dict):
        raise NotifierError(f"seven.io unexpected payload: {resp.text[:200]}")
    return parsed
