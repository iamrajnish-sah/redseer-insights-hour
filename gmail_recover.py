"""
gmail_recover.py

Recover subscriber emails from Gmail *Sent* mail using IMAP + the same
SMTP_USER / SMTP_PASSWORD (Gmail app password) already used to send digests.

This is NOT the Gmail REST API — SMTP alone cannot read mail. IMAP with the
app password can, when IMAP is enabled on the Google account.

What we can recover:
  - recipient email addresses (To / Cc)
  - likely sector preferences inferred from digest subjects

What we cannot recover perfectly:
  - original manage-preferences tokens (were in the lost DB)
  - exact multi-sector prefs if someone got several campaigns over time
    (we union sectors seen across Sent messages)
"""

from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta

import subscribers
from sector_keywords import SECTOR_LABELS

PRODUCT_MARKERS = (
    "insights hour",
    "redseer insight hour",
    "redseer intelligence",
    "festive sale intelligence",
    "weekly intelligence",
    "weekly brief",
)

# Reverse map: "E-commerce" -> e_commerce
_LABEL_TO_SECTOR = {label.lower(): key for key, label in SECTOR_LABELS.items()}
# Also accept raw keys in subject
_LABEL_TO_SECTOR.update({key.lower(): key for key in SECTOR_LABELS})


def imap_configured() -> bool:
    user = (os.environ.get("SMTP_USER") or os.environ.get("EMAIL_FROM") or "").strip()
    password = (os.environ.get("SMTP_PASSWORD") or "").strip()
    host = (os.environ.get("IMAP_HOST") or "").strip()
    if not host:
        smtp_host = (os.environ.get("SMTP_HOST") or "").strip().lower()
        if "gmail" in smtp_host or user.endswith("@gmail.com"):
            host = "imap.gmail.com"
    return bool(user and password and host)


def _imap_settings():
    user = (os.environ.get("SMTP_USER") or os.environ.get("EMAIL_FROM") or "").strip()
    password = (os.environ.get("SMTP_PASSWORD") or "").strip()
    host = (os.environ.get("IMAP_HOST") or "").strip()
    if not host:
        smtp_host = (os.environ.get("SMTP_HOST") or "").strip().lower()
        if "gmail" in smtp_host or user.endswith("@gmail.com"):
            host = "imap.gmail.com"
        else:
            host = smtp_host.replace("smtp.", "imap.") if smtp_host else ""
    port = int(os.environ.get("IMAP_PORT", "993"))
    return host, port, user, password


def _decode_header(value: str) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _parse_addresses(header_value: str) -> list[str]:
    emails = []
    for _name, addr in email.utils.getaddresses([header_value or ""]):
        addr = (addr or "").strip().lower()
        if addr and "@" in addr:
            emails.append(addr)
    return emails


def _looks_like_product_mail(subject: str, body_hint: str = "") -> bool:
    hay = f"{subject or ''} {body_hint or ''}".lower()
    return any(marker in hay for marker in PRODUCT_MARKERS)


def _sectors_from_subject(subject: str) -> list[str]:
    """Infer sectors from subjects like 'Insights Hour — E-commerce — …'."""
    text = subject or ""
    found = []
    # Split on em-dash / hyphen separators used in our subjects
    chunks = re.split(r"\s*[—–|-]\s*", text)
    for chunk in chunks:
        key = _LABEL_TO_SECTOR.get(chunk.strip().lower())
        if key and key not in found:
            found.append(key)
    # Festive Sale short form
    if "festive" in text.lower() and "festive_sale" not in found:
        if "festive sale" in text.lower() or "festive" in text.lower():
            # Only add if product mail already matched; avoid random festive spam
            if _looks_like_product_mail(text):
                found.append("festive_sale")
    # Weekly intelligence without a sector → leave empty (caller may default)
    return found


def _connect_imap():
    if not imap_configured():
        raise RuntimeError(
            "IMAP not configured. Need SMTP_USER + SMTP_PASSWORD (Gmail app password) "
            "and IMAP enabled on the Google account. Optional: IMAP_HOST=imap.gmail.com"
        )
    host, port, user, password = _imap_settings()
    client = imaplib.IMAP4_SSL(host, port)
    client.login(user, password)
    return client, user


def scan_sent_for_recipients(days: int = 180, max_messages: int = 400) -> dict:
    """
    Scan Gmail Sent for Insights Hour digests and collect recipients + sectors.
    """
    days = max(7, min(int(days or 180), 730))
    max_messages = max(20, min(int(max_messages or 400), 2000))
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%d-%b-%Y")

    client, user = _connect_imap()
    by_email = defaultdict(lambda: {"sectors": set(), "subjects": [], "count": 0})
    scanned = 0
    matched = 0
    errors = []

    try:
        # Gmail Sent mailbox names vary slightly
        for mailbox in ('"[Gmail]/Sent Mail"', "Sent", "INBOX.Sent", '"Sent Items"'):
            typ, _ = client.select(mailbox, readonly=True)
            if typ == "OK":
                break
        else:
            raise RuntimeError(
                "Could not open Sent mailbox. Enable IMAP in Gmail settings "
                "(Settings → See all settings → Forwarding and POP/IMAP)."
            )

        typ, data = client.search(None, f'(SINCE {since})')
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        ids = (data[0] or b"").split()
        # Newest first
        ids = list(reversed(ids))[:max_messages]

        for msg_id in ids:
            scanned += 1
            try:
                typ, msg_data = client.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT)])")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                if isinstance(raw, bytes):
                    msg = email.message_from_bytes(raw)
                else:
                    msg = email.message_from_string(str(raw))
                subject = _decode_header(msg.get("Subject", ""))
                if not _looks_like_product_mail(subject):
                    continue
                matched += 1
                recipients = _parse_addresses(msg.get("To", "")) + _parse_addresses(msg.get("Cc", ""))
                # Skip self
                recipients = [r for r in recipients if r != user.lower()]
                sectors = _sectors_from_subject(subject)
                for addr in recipients:
                    entry = by_email[addr]
                    entry["count"] += 1
                    for s in sectors:
                        entry["sectors"].add(s)
                    if subject and subject not in entry["subjects"] and len(entry["subjects"]) < 5:
                        entry["subjects"].append(subject)
            except Exception as exc:
                if len(errors) < 8:
                    errors.append(str(exc)[:160])
    finally:
        try:
            client.logout()
        except Exception:
            pass

    people = []
    for addr, meta in sorted(by_email.items()):
        sectors = sorted(meta["sectors"])
        people.append(
            {
                "email": addr,
                "name": addr.split("@")[0].replace(".", " ").replace("_", " ").title(),
                "sectors": sectors,
                "messages_seen": meta["count"],
                "sample_subjects": meta["subjects"],
            }
        )

    return {
        "ok": True,
        "imap_user": user,
        "days": days,
        "scanned_messages": scanned,
        "matched_product_messages": matched,
        "unique_recipients": len(people),
        "recipients": people,
        "errors": errors,
        "note": (
            "Sectors are inferred from Sent digest subjects. "
            "If someone only got a weekly multi-sector brief, sectors may be empty "
            "and will default to festive_sale on import."
        ),
    }


def recover_subscribers_from_sent(days: int = 180, max_messages: int = 400) -> dict:
    """Scan Sent and upsert into the live subscribers table."""
    scan = scan_sent_for_recipients(days=days, max_messages=max_messages)
    if not scan.get("ok"):
        return scan

    rows = []
    sectors_map = {}
    for person in scan.get("recipients") or []:
        email_addr = person["email"]
        rows.append(
            {
                "name": person.get("name") or email_addr,
                "email": email_addr,
                "status": subscribers.STATUS_SUBSCRIBED,
                "company": "",
                "designation": "",
            }
        )
        sectors = person.get("sectors") or []
        if not sectors:
            sectors = ["festive_sale"] if "festive_sale" in SECTOR_LABELS else []
        sectors_map[email_addr] = sectors

    stats = subscribers.import_subscriber_rows(rows, sectors_by_email=sectors_map)
    total, active = subscribers.subscriber_count()
    return {
        "ok": True,
        "source": "gmail_imap_sent",
        "days": scan.get("days"),
        "scanned_messages": scan.get("scanned_messages"),
        "matched_product_messages": scan.get("matched_product_messages"),
        "unique_recipients": scan.get("unique_recipients"),
        "recipients_preview": (scan.get("recipients") or [])[:40],
        "errors": scan.get("errors") or [],
        "live_total": total,
        "live_active": active,
        **stats,
        "message": (
            f"Gmail Sent recovery: {stats['inserted']} new, {stats['updated']} updated "
            f"from {scan.get('unique_recipients', 0)} recipients "
            f"({scan.get('matched_product_messages', 0)} product emails scanned). "
            f"Live total now {total}."
        ),
        "note": scan.get("note"),
    }
