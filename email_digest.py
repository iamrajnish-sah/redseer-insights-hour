"""
email_digest.py

Sends sector-specific news digests by email.
Recipients come from active subscribers (preferred) or sector_recipients.json fallback.

Configure SMTP in .env (see .env.example)
"""

import json
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import database
import subscribers
from sector_keywords import SECTOR_LABELS, normalize_sector_tags

PRODUCT_NAME = "Redseer Insight Hour"
RECIPIENTS_FILE = Path(__file__).parent / "sector_recipients.json"


def load_recipients():
    env_json = os.environ.get("SECTOR_RECIPIENTS_JSON", "").strip()
    if env_json:
        try:
            data = json.loads(env_json)
            cleaned = {}
            for sector, emails in data.items():
                if isinstance(emails, list):
                    cleaned[sector] = [e.strip() for e in emails if isinstance(e, str) and e.strip()]
            return cleaned
        except json.JSONDecodeError:
            pass
    if not RECIPIENTS_FILE.exists():
        return {}
    with open(RECIPIENTS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    cleaned = {}
    for sector, emails in data.items():
        if not isinstance(emails, list):
            continue
        cleaned[sector] = [e.strip() for e in emails if isinstance(e, str) and e.strip()]
    return cleaned


def smtp_configured():
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM"))


def _base_url():
    explicit = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    vercel = os.environ.get("VERCEL_URL", "").strip().rstrip("/")
    if vercel:
        if vercel.startswith("http"):
            return vercel.rstrip("/")
        return f"https://{vercel}"
    return "http://localhost:8000"


def _article_dict(row):
    d = dict(row)
    d["sectors"] = normalize_sector_tags(json.loads(d["sectors"] or "[]"))
    return d


def _article_block(a):
    link = a.get("resolved_url") or a.get("url") or ""
    title = a.get("title") or "Untitled"
    summary = a.get("summary") or ""
    source = a.get("source") or ""
    image_url = a.get("image_url") or ""
    if link:
        title_html = f'<a href="{link}" style="color:#1e3a5f;text-decoration:none;">{title}</a>'
    else:
        title_html = title
    image_html = ""
    if image_url:
        image_html = f"""
            <div style="margin-bottom:12px;">
              <img src="{image_url}" alt="" style="width:100%;max-height:220px;object-fit:cover;border-radius:8px;border:1px solid #e2e8f0;" />
            </div>"""
    return f"""
        <tr>
          <td style="padding:16px 0;border-bottom:1px solid #e2e8f0;">
            {image_html}
            <div style="font-size:16px;font-weight:700;color:#0f172a;margin-bottom:6px;">{title_html}</div>
            <div style="font-size:14px;color:#475569;line-height:1.5;margin-bottom:8px;">{summary}</div>
            <div style="font-size:12px;color:#94a3b8;">{source}</div>
          </td>
        </tr>"""


def build_email_footer(token):
    base = _base_url()
    prefs_url = f"{base}/preferences?token={token}"
    unsub_url = f"{base}/api/unsubscribe?token={token}"
    return f"""
        <tr>
          <td style="padding:18px 28px;background:#f8fafc;border-top:1px solid #e2e8f0;">
            <p style="font-size:12px;color:#64748b;margin:0;line-height:1.7;text-align:center;">
              You are receiving this because you subscribed to {PRODUCT_NAME}.<br>
              <a href="{prefs_url}" style="color:#0d9488;">Manage preferences</a>
              &nbsp;·&nbsp;
              <a href="{unsub_url}" style="color:#64748b;">Unsubscribe</a>
            </p>
          </td>
        </tr>"""


def build_digest_html(sector_label, pub_date, articles, footer_html=""):
    items = [_article_block(_article_dict(row)) for row in articles]
    body_rows = "".join(items) if items else (
        "<tr><td style='padding:20px;color:#64748b;'>No articles for this sector on this date.</td></tr>"
    )
    return _wrap_email_html(
        header_title=PRODUCT_NAME,
        header_subtitle=f"{sector_label} · {pub_date}",
        body_rows=body_rows,
        footer_html=footer_html,
    )


def build_multi_sector_digest(sector_sections, pub_date, footer_html=""):
    """sector_sections: list of (sector_label, article_rows)."""
    parts = []
    for label, rows in sector_sections:
        items = [_article_block(_article_dict(row)) for row in rows]
        if not items:
            continue
        parts.append(
            f"""
            <tr>
              <td style="padding:20px 0 8px;">
                <div style="font-size:13px;font-weight:700;color:#0d9488;text-transform:uppercase;letter-spacing:0.06em;">
                  {label}
                </div>
              </td>
            </tr>
            {''.join(items)}"""
        )
    body_rows = "".join(parts) if parts else (
        "<tr><td style='padding:20px;color:#64748b;'>No articles for your sectors on this date.</td></tr>"
    )
    labels = [label for label, rows in sector_sections if rows]
    subtitle = f"{', '.join(labels)} · {pub_date}" if labels else pub_date
    return _wrap_email_html(
        header_title=PRODUCT_NAME,
        header_subtitle=subtitle,
        body_rows=body_rows,
        footer_html=footer_html,
    )


def _wrap_email_html(header_title, header_subtitle, body_rows, footer_html=""):
    return f"""
    <html><body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,sans-serif;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:24px 0;">
        <tr><td align="center">
          <table width="640" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;">
            <tr>
              <td style="background:#1e3a5f;color:#ffffff;padding:24px 28px;">
                <div style="font-size:22px;font-weight:700;">{header_title}</div>
                <div style="font-size:14px;opacity:0.9;margin-top:6px;">{header_subtitle}</div>
              </td>
            </tr>
            <tr>
              <td style="padding:8px 28px 8px;">
                <table width="100%" cellpadding="0" cellspacing="0">{body_rows}</table>
              </td>
            </tr>
            {footer_html}
          </table>
        </td></tr>
      </table>
    </body></html>
    """


def _send_email(to_list, subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = ", ".join(to_list)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=30) as server:
        server.ehlo()
        if os.environ.get("SMTP_USE_TLS", "true").lower() not in ("0", "false", "no"):
            server.starttls()
            server.ehlo()
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.sendmail(os.environ["EMAIL_FROM"], to_list, msg.as_string())


def _normalize_sectors(sectors):
    if not sectors:
        return list(SECTOR_LABELS.keys())
    cleaned = []
    for sector in sectors:
        sector = (sector or "").strip()
        if sector not in SECTOR_LABELS:
            raise ValueError(f"Unknown sector: {sector}")
        if sector not in cleaned:
            cleaned.append(sector)
    return cleaned


def send_sector_digests(pub_date, sectors=None):
    if not smtp_configured():
        raise RuntimeError(
            "SMTP is not configured. Add SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM to .env"
        )

    selected_sectors = _normalize_sectors(sectors)

    if subscribers.has_subscribers():
        return _send_subscriber_campaign(pub_date, selected_sectors)

    return _send_legacy_sector_digests(pub_date, selected_sectors)


def _send_subscriber_campaign(pub_date, sectors):
    active = subscribers.get_active_subscribers_for_sectors(sectors)
    if not active:
        raise RuntimeError("No active subscribers for the selected sector(s).")

    results = []
    sent_emails = set()

    for sub in active:
        email = sub["email"]
        if email in sent_emails:
            continue

        sub_sectors = [sector for sector in sub["sectors"] if sector in sectors]
        if not sub_sectors:
            continue

        sector_sections = []
        total_articles = 0
        for sector in sub_sectors:
            rows = database.get_relevant_articles(pub_date=pub_date, sector=sector)
            if rows:
                sector_sections.append((SECTOR_LABELS[sector], rows))
                total_articles += len(rows)

        if not sector_sections:
            results.append({
                "email": email,
                "name": sub["name"],
                "sectors": sub_sectors,
                "status": "skipped",
                "reason": "no articles for this date",
            })
            continue

        footer = build_email_footer(sub["token"])
        html = build_multi_sector_digest(sector_sections, pub_date, footer)
        if len(sub_sectors) == 1:
            subject = f"{PRODUCT_NAME} — {SECTOR_LABELS[sub_sectors[0]]} — {pub_date}"
        else:
            labels = ", ".join(SECTOR_LABELS[s] for s in sub_sectors[:3])
            if len(sub_sectors) > 3:
                labels += f" +{len(sub_sectors) - 3} more"
            subject = f"{PRODUCT_NAME} — {labels} — {pub_date}"

        _send_email([email], subject, html)
        sent_emails.add(email)
        results.append({
            "email": email,
            "name": sub["name"],
            "sectors": sub_sectors,
            "status": "sent",
            "articles": total_articles,
        })

    sent = [item for item in results if item["status"] == "sent"]
    if not sent:
        raise RuntimeError("No emails sent — no articles matched subscribers for this date.")

    return {
        "mode": "subscribers",
        "pub_date": pub_date,
        "sectors": sectors,
        "sent_count": len(sent),
        "skipped_count": len(results) - len(sent),
        "results": results,
    }


def _send_legacy_sector_digests(pub_date, sectors):
    recipients = load_recipients()
    if not any(recipients.get(sector) for sector in sectors):
        raise RuntimeError(
            "No email recipients configured. Add subscribers or edit sector_recipients.json."
        )

    results = []
    for sector in sectors:
        label = SECTOR_LABELS[sector]
        emails = recipients.get(sector, [])
        if not emails:
            results.append({
                "sector": sector,
                "sector_label": label,
                "status": "skipped",
                "reason": "no recipients configured",
            })
            continue

        rows = database.get_relevant_articles(pub_date=pub_date, sector=sector)
        if not rows:
            results.append({
                "sector": sector,
                "sector_label": label,
                "status": "skipped",
                "reason": "no articles for this date",
                "recipients": emails,
            })
            continue

        html = build_digest_html(label, pub_date, rows)
        subject = f"{PRODUCT_NAME} — {label} — {pub_date}"
        _send_email(emails, subject, html)
        results.append({
            "sector": sector,
            "sector_label": label,
            "status": "sent",
            "recipients": emails,
            "articles": len(rows),
        })

    return {
        "mode": "legacy",
        "pub_date": pub_date,
        "sectors": sectors,
        "sent_count": len([item for item in results if item["status"] == "sent"]),
        "skipped_count": len([item for item in results if item["status"] == "skipped"]),
        "results": results,
    }


def email_recipient_summary():
    """Counts for admin UI — subscribers preferred, legacy JSON as fallback info."""
    summary = {
        "subscriber_mode": subscribers.has_subscribers(),
        "active_subscribers": 0,
        "sectors_with_subscribers": {},
        "legacy_sectors_with_recipients": {},
    }
    analytics = subscribers.get_analytics()
    summary["active_subscribers"] = analytics["active"]
    summary["sectors_with_subscribers"] = {
        item["label"]: item["count"] for item in analytics["by_sector"] if item["count"] > 0
    }
    legacy = load_recipients()
    summary["legacy_sectors_with_recipients"] = {
        SECTOR_LABELS.get(k, k): len(v) for k, v in legacy.items() if v
    }
    return summary
