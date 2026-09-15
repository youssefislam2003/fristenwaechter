"""German notification copy (Sie-Form), rendered from an OutboxMessage's
``template`` name + metadata-only ``payload``.

Two hard constraints baked in and tested:
  * SMS bodies must fit 160 GSM-7 characters — so no ä/ö/ü/ß (each would flip
    the whole message to UCS-2 and halve the limit to 70). We write ae/oe/ue/ss.
  * Email may use full German typography (HTML), no such restriction.

Every renderer tolerates a partial payload: a missing key degrades to a
neutral placeholder rather than raising, because a notification that renders
slightly generically still beats a delivery that crashes the relay.
"""
from __future__ import annotations

from dataclasses import dataclass

# GSM-7 basic charset would be the fully correct check; for our copy (ASCII +
# the few GSM punctuation marks) this length cap is the operative limit.
SMS_MAX_LEN = 160


@dataclass(frozen=True, slots=True)
class EmailContent:
    subject: str
    html: str
    text: str


class UnknownTemplate(KeyError):
    """No renderer registered for this template name."""


def _g(payload: dict[str, object], key: str, default: str = "—") -> str:
    val = payload.get(key)
    return str(val) if val not in (None, "") else default


def _link(payload: dict[str, object], base_url: str) -> str:
    """Build an absolute deep-link from a relative path in the payload."""
    path = payload.get("deep_link") or payload.get("case_path") or "/dashboard"
    return f"{base_url}{path}"


# ─────────────────────────────── email ───────────────────────────────


def render_email(template: str, payload: dict[str, object], *, base_url: str) -> EmailContent:
    try:
        renderer = _EMAIL[template]
    except KeyError as exc:
        raise UnknownTemplate(template) from exc
    return renderer(payload, base_url)


def _email_fsk_failed(payload: dict[str, object], base_url: str) -> EmailContent:
    name = _g(payload, "driver_name")
    deadline = _g(payload, "deadline")
    link = _link(payload, base_url)
    subject = f"KRITISCH: Fahrer {name} gesperrt – Führerscheinkontrolle fehlgeschlagen"
    text = (
        f"Sehr geehrte Damen und Herren,\n\n"
        f"die Führerscheinkontrolle für {name} ist FEHLGESCHLAGEN. "
        f"Der Fahrer wurde mit sofortiger Wirkung gesperrt.\n\n"
        f"Ein weiterer Fahrzeugeinsatz kann den Tatbestand des § 21 Abs. 1 "
        f"Nr. 2 StVG (Halterverantwortung) erfuellen und ist strafbewehrt.\n\n"
        f"Frist zur Klaerung: {deadline}.\n\n"
        f"Vorgang oeffnen: {link}\n\n"
        f"Mit freundlichen Gruessen\nIhr Fristenwaechter"
    )
    html = (
        f"<p>Sehr geehrte Damen und Herren,</p>"
        f"<p>die Führerscheinkontrolle für <strong>{name}</strong> ist "
        f"<strong>fehlgeschlagen</strong>. Der Fahrer wurde mit sofortiger "
        f"Wirkung gesperrt.</p>"
        f"<p>Ein weiterer Fahrzeugeinsatz kann den Tatbestand des "
        f"§&nbsp;21 Abs.&nbsp;1 Nr.&nbsp;2 StVG (Halterverantwortung) "
        f"erfüllen und ist strafbewehrt.</p>"
        f"<p><strong>Frist zur Klärung: {deadline}.</strong></p>"
        f'<p><a href="{link}">Vorgang öffnen</a></p>'
        f"<p>Mit freundlichen Grüßen<br>Ihr Fristenwächter</p>"
    )
    return EmailContent(subject=subject, html=html, text=text)


def _email_escalation_nag(payload: dict[str, object], base_url: str) -> EmailContent:
    name = _g(payload, "driver_name")
    deadline = _g(payload, "deadline")
    link = _link(payload, base_url)
    subject = f"Erinnerung: Offener Sperrvorgang für {name} – Frist {deadline}"
    text = (
        f"Sehr geehrte Damen und Herren,\n\n"
        f"der Sperrvorgang fuer {name} ist weiterhin OFFEN. Bitte klaeren "
        f"Sie den Fall bis zum {deadline}.\n\n"
        f"Solange der Fahrer gesperrt ist, darf er keine Firmenfahrzeuge "
        f"fuehren.\n\nVorgang: {link}\n\nIhr Fristenwaechter"
    )
    html = (
        f"<p>Sehr geehrte Damen und Herren,</p>"
        f"<p>der Sperrvorgang für <strong>{name}</strong> ist weiterhin "
        f"<strong>offen</strong>. Bitte klären Sie den Fall bis zum "
        f"<strong>{deadline}</strong>.</p>"
        f"<p>Solange der Fahrer gesperrt ist, darf er keine Firmenfahrzeuge "
        f"führen.</p>"
        f'<p><a href="{link}">Vorgang öffnen</a></p>'
        f"<p>Ihr Fristenwächter</p>"
    )
    return EmailContent(subject=subject, html=html, text=text)


def _email_escalation_breached(payload: dict[str, object], base_url: str) -> EmailContent:
    name = _g(payload, "driver_name")
    link = _link(payload, base_url)
    subject = f"FRIST ÜBERSCHRITTEN: Sperrvorgang für {name} nicht geklärt"
    text = (
        f"Sehr geehrte Damen und Herren,\n\n"
        f"die Frist zur Klaerung des Sperrvorgangs fuer {name} ist "
        f"ABGELAUFEN, ohne dass eine gueltige Führerscheinkontrolle "
        f"vorliegt.\n\n"
        f"Der Fahrer bleibt gesperrt. Jeder Einsatz begruendet ein "
        f"erhebliches Haftungs- und Strafbarkeitsrisiko nach § 21 StVG.\n\n"
        f"Vorgang: {link}\n\nIhr Fristenwaechter"
    )
    html = (
        f"<p>Sehr geehrte Damen und Herren,</p>"
        f"<p>die Frist zur Klärung des Sperrvorgangs für "
        f"<strong>{name}</strong> ist <strong>abgelaufen</strong>, ohne dass "
        f"eine gültige Führerscheinkontrolle vorliegt.</p>"
        f"<p>Der Fahrer bleibt gesperrt. Jeder Einsatz begründet ein "
        f"erhebliches Haftungs- und Strafbarkeitsrisiko nach § 21 StVG.</p>"
        f'<p><a href="{link}">Vorgang öffnen</a></p>'
        f"<p>Ihr Fristenwächter</p>"
    )
    return EmailContent(subject=subject, html=html, text=text)


def _email_deadline_reminder(payload: dict[str, object], base_url: str) -> EmailContent:
    subject_name = _g(payload, "subject_name")
    kind = _g(payload, "kind_label", _g(payload, "kind"))
    due = _g(payload, "due_date")
    notch = payload.get("notch")
    link = _link(payload, base_url)

    if notch == 0:
        lead = f"Die Frist ({kind}) fuer {subject_name} ist HEUTE faellig ({due})."
        lead_html = (
            f"Die Frist (<strong>{kind}</strong>) für "
            f"<strong>{subject_name}</strong> ist <strong>heute</strong> "
            f"fällig ({due})."
        )
    elif isinstance(notch, int) and notch < 0:
        lead = f"Die Frist ({kind}) fuer {subject_name} ist SEIT {due} ueberfaellig."
        lead_html = (
            f"Die Frist (<strong>{kind}</strong>) für "
            f"<strong>{subject_name}</strong> ist seit {due} "
            f"<strong>überfällig</strong>."
        )
    else:
        lead = (
            f"Die Frist ({kind}) fuer {subject_name} ist in {notch} Tagen "
            f"faellig ({due})."
        )
        lead_html = (
            f"Die Frist (<strong>{kind}</strong>) für "
            f"<strong>{subject_name}</strong> ist in <strong>{notch} Tagen</strong> "
            f"fällig ({due})."
        )

    subject = f"Fristerinnerung: {kind} für {subject_name} ({due})"
    text = (
        f"Sehr geehrte Damen und Herren,\n\n{lead}\n\n"
        f"Bitte planen Sie die Pruefung rechtzeitig.\n\n"
        f"Details: {link}\n\nIhr Fristenwaechter"
    )
    html = (
        f"<p>Sehr geehrte Damen und Herren,</p><p>{lead_html}</p>"
        f"<p>Bitte planen Sie die Prüfung rechtzeitig.</p>"
        f'<p><a href="{link}">Details öffnen</a></p>'
        f"<p>Ihr Fristenwächter</p>"
    )
    return EmailContent(subject=subject, html=html, text=text)


_EMAIL = {
    "fsk_failed_critical": _email_fsk_failed,
    "escalation_nag": _email_escalation_nag,
    "escalation_breached": _email_escalation_breached,
    "deadline_reminder": _email_deadline_reminder,
}


# ─────────────────────────────── SMS ───────────────────────────────
# ASCII-only (ae/oe/ue/ss), each ≤160 chars. Enforced by tests.


def render_sms(template: str, payload: dict[str, object]) -> str:
    try:
        renderer = _SMS[template]
    except KeyError as exc:
        raise UnknownTemplate(template) from exc
    body = renderer(payload)
    return body


def _sms_fsk_failed(payload: dict[str, object]) -> str:
    name = _g(payload, "driver_name")
    deadline = _g(payload, "deadline")
    return (
        f"Fristenwaechter: FSK fehlgeschlagen fuer {name}. Fahrer gesperrt "
        f"(§21 StVG). Frist {deadline}. Bitte sofort im Portal klaeren."
    )


def _sms_escalation_nag(payload: dict[str, object]) -> str:
    name = _g(payload, "driver_name")
    deadline = _g(payload, "deadline")
    return (
        f"Fristenwaechter: Sperrvorgang fuer {name} noch offen. Frist "
        f"{deadline}. Bitte im Portal klaeren."
    )


def _sms_escalation_breached(payload: dict[str, object]) -> str:
    name = _g(payload, "driver_name")
    return (
        f"Fristenwaechter: FRIST ABGELAUFEN fuer {name}. Fahrer bleibt "
        f"gesperrt. Haftungsrisiko §21 StVG. Bitte umgehend handeln."
    )


def _sms_deadline_reminder(payload: dict[str, object]) -> str:
    subject_name = _g(payload, "subject_name")
    kind = _g(payload, "kind_label", _g(payload, "kind"))
    due = _g(payload, "due_date")
    notch = payload.get("notch")
    if notch == 0:
        when = f"HEUTE faellig ({due})"
    elif isinstance(notch, int) and notch < 0:
        when = f"ueberfaellig seit {due}"
    else:
        when = f"in {notch} Tagen faellig ({due})"
    return f"Fristenwaechter: {kind} fuer {subject_name} {when}. Details im Portal."


_SMS = {
    "fsk_failed_critical": _sms_fsk_failed,
    "escalation_nag": _sms_escalation_nag,
    "escalation_breached": _sms_escalation_breached,
    "deadline_reminder": _sms_deadline_reminder,
}
