"""
email_digest.py

Sends sector-specific news digests by email.
Each sector's team receives ONLY articles tagged for that sector.

Configure recipients in sector_recipients.json
Configure SMTP in .env (see .env.example)
"""

import json
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import database
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


def _article_dict(row):
    d = dict(row)
    d["sectors"] = normalize_sector_tags(json.loads(d["sectors"] or "[]"))
    return d


def build_digest_html(sector_label, pub_date, articles):
    items = []
    for row in articles:
        a = _article_dict(row)
        link = a.get("url") or ""
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
        items.append(
            f"""
            <tr>
              <td style="padding:16px 0;border-bottom:1px solid #e2e8f0;">
                {image_html}
                <div style="font-size:16px;font-weight:700;color:#0f172a;margin-bottom:6px;">{title_html}</div>
                <div style="font-size:14px;color:#475569;line-height:1.5;margin-bottom:8px;">{summary}</div>
                <div style="font-size:12px;color:#94a3b8;">{source}</div>
              </td>
            </tr>"""
        )

    body_rows = "".join(items) if items else (
        "<tr><td style='padding:20px;color:#64748b;'>No articles for this sector on this date.</td></tr>"
    )

    return f"""
    <html><body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,sans-serif;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:24px 0;">
        <tr><td align="center">
          <table width="640" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;">
            <tr>
              <td style="background:#1e3a5f;color:#ffffff;padding:24px 28px;">
                <div style="font-size:22px;font-weight:700;">{PRODUCT_NAME}</div>
                <div style="font-size:14px;opacity:0.9;margin-top:6px;">{sector_label} · {pub_date}</div>
              </td>
            </tr>
            <tr>
              <td style="padding:8px 28px 28px;">
                <table width="100%" cellpadding="0" cellspacing="0">{body_rows}</table>
              </td>
            </tr>
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


def send_sector_digests(pub_date):
    if not smtp_configured():
        raise RuntimeError(
            "SMTP is not configured. Add SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM to .env"
        )

    recipients = load_recipients()
    if not any(recipients.values()):
        raise RuntimeError(
            "No email recipients configured. Edit sector_recipients.json and add emails per sector."
        )

    results = []
    for sector, label in SECTOR_LABELS.items():
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

    return results
