"""Gmail access with an app password: read Workday verification/reset emails, send the digest."""
from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

import truststore

from . import config, secrets

log = logging.getLogger("agent.mail")
CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
LINK = re.compile(r"https?://[^\s\"'<>]+")


def _address() -> str:
    return config.profile()["contact"]["email"]


def _imap() -> imaplib.IMAP4_SSL:
    pw = secrets.gmail_app_password()
    if not pw:
        raise RuntimeError("Gmail app password not set; run ./agent.sh setup")
    m = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=CTX)
    m.login(_address(), pw)
    return m


def _body_text(msg) -> str:
    parts = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                parts.append(part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace"))
            except Exception:
                pass
    return "\n".join(parts).replace("&amp;", "&")


def find_link(kind: str, tenant: str, since: datetime, wait_seconds: int = 240) -> str | None:
    """Poll Gmail for a Workday email (kind: 'verify' or 'reset') and return its link."""
    if not secrets.gmail_app_password():
        log.warning("no Gmail app password saved; cannot read the %s e-mail", kind)
        return None
    words = {"verify": ("verif", "activat", "confirm"), "reset": ("reset", "password")}[kind]
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        try:
            m = _imap()
            for box in ("INBOX", "[Gmail]/Spam"):
                if m.select(f'"{box}"', readonly=True)[0] != "OK":
                    continue
                day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
                _, ids = m.search(None, f'(SINCE "{day}")')
                for mid in reversed(ids[0].split()[-40:]):
                    _, data = m.fetch(mid, "(RFC822)")
                    msg = email.message_from_bytes(data[0][1])
                    try:
                        sent = parsedate_to_datetime(msg["Date"])
                    except Exception:
                        continue
                    if sent < since - timedelta(minutes=2):
                        continue
                    body = _body_text(msg)
                    blob = f"{msg.get('From', '')} {msg.get('Subject', '')} {body}".lower()
                    if "workday" not in blob or not any(w in blob for w in words):
                        continue
                    links = [u.rstrip(".,)") for u in LINK.findall(body) if "myworkday" in u]
                    pick = [u for u in links if tenant.lower() in u.lower() and any(w in u.lower() for w in words)]
                    pick = pick or [u for u in links if any(w in u.lower() for w in words)]
                    if pick:
                        m.logout()
                        return pick[0]
            m.logout()
        except Exception as e:
            log.warning("imap check failed: %s", e)
        time.sleep(15)
    return None


def send(subject: str, html_body: str) -> None:
    """Deliver a note to yourself. Placed straight into the Gmail inbox over IMAP (port 993) because many
    campus and corporate networks block the mail-sending ports; falls back to SMTP if that fails."""
    pw = secrets.gmail_app_password()
    if not pw:
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, _address(), _address()
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.set_content("Open in an HTML-capable mail client.")
    msg.add_alternative(html_body, subtype="html")
    try:
        m = _imap()
        m.append("INBOX", "", imaplib.Time2Internaldate(time.time()), msg.as_bytes())  # no \\Seen flag = unread
        m.logout()
        return
    except Exception as e:
        log.warning("inbox delivery failed (%s); trying SMTP", e)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=CTX, timeout=20) as srv:
        srv.login(_address(), pw)
        srv.send_message(msg)


def test_login() -> str:
    m = _imap()
    m.logout()
    return "ok"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
