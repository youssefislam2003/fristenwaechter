"""Brevo transactional-email notifier (EU subprocessor).

Implements the relay's Notifier protocol: ``send(recipient, template, payload,
idempotency_key) -> provider_message_id``. All failures — non-2xx, timeout,
transport error — map to NotifierError so the relay's backoff/dead-letter
machinery treats them uniformly. Brevo accepts an Idempotency-Key header; we
pass the outbox dedup_key through so an at-least-once re-send collapses to one
delivery on the provider side.
"""
from __future__ import annotations

import logging

import httpx

from app.jobs.relay import NotifierError
from app.notifiers.templates import render_email
from app.settings import Settings

logger = logging.getLogger(__name__)

_BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"
_TIMEOUT = httpx.Timeout(10.0)


class BrevoNotifier:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        # transport injection is the seam MockTransport uses in unit tests.
        self._transport = transport

    async def send(
        self, *, recipient: str, template: str, payload: dict[str, object], idempotency_key: str
    ) -> str:
        content = render_email(
            template, payload, base_url=self._settings.BASE_URL
        )
        body = {
            "sender": {
                "name": self._settings.MAIL_FROM_NAME,
                "email": self._settings.MAIL_FROM_EMAIL,
            },
            "to": [{"email": recipient}],
            "subject": content.subject,
            "htmlContent": content.html,
            "textContent": content.text,
        }
        headers = {
            "api-key": self._settings.BREVO_API_KEY.get_secret_value(),
            "accept": "application/json",
            "content-type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, transport=self._transport
            ) as client:
                resp = await client.post(_BREVO_ENDPOINT, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise NotifierError(f"brevo transport error: {exc}") from exc

        if resp.status_code // 100 != 2:
            raise NotifierError(
                f"brevo HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            message_id = resp.json().get("messageId")
        except ValueError:
            message_id = None
        # A 2xx without a messageId still counts as delivered; synthesize an id
        # so the relay records something non-null.
        return str(message_id) if message_id else f"brevo:{idempotency_key}"
