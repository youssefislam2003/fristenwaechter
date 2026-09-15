"""Notifier unit tests — HTTP behavior via httpx.MockTransport (no network),
template rendering, and the SMS-cap gate with a fake quota (no DB).

The provider request shapes and error mapping are asserted here; the
DB-backed cap counting is exercised in the integration suite.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from app.jobs.relay import NotifierError
from app.notifiers import templates as tpl
from app.notifiers.brevo import BrevoNotifier
from app.notifiers.seven import SKIPPED_SENTINEL_PREFIX, SevenNotifier
from app.settings import Settings

_SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    DATABASE_URL="postgresql+asyncpg://app_user:x@localhost/f",
    JOBS_DATABASE_URL="postgresql+asyncpg://app_jobs:x@localhost/f",
    BREVO_API_KEY="brevo-k",
    SEVEN_IO_API_KEY="seven-k",
    SECRET_KEY="s",
    BASE_URL="https://app.example.de",
)

_FSK_PAYLOAD = {
    "driver_name": "Max Mustermann",
    "deadline": "18.07.2026",
    "case_path": "/drivers/abc/escalation",
}


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ─────────────────────────────── templates ───────────────────────────────


@pytest.mark.parametrize(
    "template",
    ["fsk_failed_critical", "escalation_nag", "escalation_breached",
     "deadline_reminder"],
)
def test_sms_templates_fit_160_gsm7(template: str) -> None:
    payload = {**_FSK_PAYLOAD, "subject_name": "Max Mustermann",
               "kind": "HU", "kind_label": "Hauptuntersuchung",
               "due_date": "31.07.2026", "notch": 30}
    body = tpl.render_sms(template, payload)
    assert len(body) <= tpl.SMS_MAX_LEN, f"{template}: {len(body)} chars"
    # GSM-7 safety: no umlauts/ß that would force UCS-2 and halve the limit.
    assert not (set("äöüßÄÖÜ") & set(body)), f"{template} has non-GSM-7 chars"


def test_email_renders_subject_and_bodies() -> None:
    c = tpl.render_email("fsk_failed_critical", _FSK_PAYLOAD,
                         base_url="https://app.example.de")
    assert "Max Mustermann" in c.subject
    # HTML uses a non-breaking space (§&nbsp;21); the plain-text part uses a
    # normal space. Assert StVG + the absolute deep link in the HTML, and the
    # "§ 21" phrasing in the text body.
    assert "StVG" in c.html and "app.example.de/drivers/abc/escalation" in c.html
    assert "§ 21" in c.text and "Max Mustermann" in c.text


def test_unknown_template_raises() -> None:
    with pytest.raises(tpl.UnknownTemplate):
        tpl.render_sms("does_not_exist", {})


# ─────────────────────────────── Brevo ───────────────────────────────


@pytest.mark.asyncio
async def test_brevo_success_returns_message_id() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["api_key"] = request.headers.get("api-key")
        seen["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(201, json={"messageId": "brevo-123"})

    n = BrevoNotifier(_SETTINGS, transport=_transport(handler))
    msg_id = await n.send(recipient="chef@muster.de",
                          template="fsk_failed_critical", payload=_FSK_PAYLOAD,
                          idempotency_key="e6:abc:EMAIL:chef@muster.de")
    assert msg_id == "brevo-123"
    assert seen["api_key"] == "brevo-k"
    assert seen["idem"] == "e6:abc:EMAIL:chef@muster.de"
    assert "brevo.com" in seen["url"]


@pytest.mark.asyncio
async def test_brevo_non_2xx_maps_to_notifier_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    n = BrevoNotifier(_SETTINGS, transport=_transport(handler))
    with pytest.raises(NotifierError, match="brevo HTTP 400"):
        await n.send(recipient="x@y.de", template="fsk_failed_critical",
                     payload=_FSK_PAYLOAD, idempotency_key="k")


@pytest.mark.asyncio
async def test_brevo_timeout_maps_to_notifier_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    n = BrevoNotifier(_SETTINGS, transport=_transport(handler))
    with pytest.raises(NotifierError, match="transport error"):
        await n.send(recipient="x@y.de", template="fsk_failed_critical",
                     payload=_FSK_PAYLOAD, idempotency_key="k")


# ─────────────────────────────── seven.io ───────────────────────────────


@pytest.mark.asyncio
async def test_seven_success_returns_message_id() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("X-Api-Key")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"success": "100",
                                         "messages": [{"id": "sms-9"}]})

    n = SevenNotifier(_SETTINGS, transport=_transport(handler))
    msg_id = await n.send(recipient="+4915112345678",
                          template="fsk_failed_critical", payload=_FSK_PAYLOAD,
                          idempotency_key="e6:abc:SMS:+4915112345678")
    assert msg_id == "sms-9"
    assert seen["key"] == "seven-k"
    assert "foreign_id=e6" in seen["body"]


@pytest.mark.asyncio
async def test_seven_non_100_status_is_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # HTTP 200 but seven.io business failure code.
        return httpx.Response(200, json={"success": "402"})

    n = SevenNotifier(_SETTINGS, transport=_transport(handler))
    with pytest.raises(NotifierError, match="rejected"):
        await n.send(recipient="+49151", template="fsk_failed_critical",
                     payload=_FSK_PAYLOAD, idempotency_key="k")


class _FakeQuota:
    def __init__(self, over: bool) -> None:
        self.over = over
        self.skips: list[uuid.UUID] = []

    async def over_limit(self, company_id: uuid.UUID) -> bool:
        return self.over

    async def record_skip(self, company_id: uuid.UUID) -> None:
        self.skips.append(company_id)


@pytest.mark.asyncio
async def test_seven_skips_non_critical_when_over_cap() -> None:
    cid = uuid.uuid4()
    quota = _FakeQuota(over=True)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"success": "100", "messages": [{"id": "x"}]})

    n = SevenNotifier(_SETTINGS, quota=quota, transport=_transport(handler))
    result = await n.send(
        recipient="+49151", template="deadline_reminder",
        payload={"company_id": str(cid), "subject_name": "Max", "kind": "HU",
                 "due_date": "31.07.2026", "notch": 30},
        idempotency_key="rem:1:30:SMS:+49151")
    assert result.startswith(SKIPPED_SENTINEL_PREFIX)
    assert calls["n"] == 0          # provider was NOT called
    assert quota.skips == [cid]     # WARNING alert path invoked


@pytest.mark.asyncio
async def test_seven_critical_bypasses_cap() -> None:
    cid = uuid.uuid4()
    quota = _FakeQuota(over=True)   # over the cap …
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"success": "100", "messages": [{"id": "crit"}]})

    n = SevenNotifier(_SETTINGS, quota=quota, transport=_transport(handler))
    # … but a §21 critical SMS must still be sent.
    msg_id = await n.send(
        recipient="+49151", template="fsk_failed_critical",
        payload={**_FSK_PAYLOAD, "company_id": str(cid)},
        idempotency_key="e6:abc:SMS:+49151")
    assert msg_id == "crit"
    assert calls["n"] == 1
    assert quota.skips == []
