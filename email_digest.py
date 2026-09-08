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
from html import escape as html_escape
from pathlib import Path
from datetime import date, datetime

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


def _parse_iso_date(value, field_name):
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc


def _period_label(start_date, end_date):
    if start_date == end_date:
        return start_date
    return f"{start_date} to {end_date}"


def _articles_for_sector(sector, start_date, end_date, limit=80):
    rows = database.get_relevant_articles(
        sector=sector, start_date=start_date, end_date=end_date
    )
    return list(rows)[:limit]


def send_sector_digests(pub_date, sectors=None, start_date=None, end_date=None):
    if not smtp_configured():
        raise RuntimeError(
            "SMTP is not configured. Add SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM to .env"
        )

    selected_sectors = _normalize_sectors(sectors)
    start_date = _parse_iso_date(start_date or pub_date or str(date.today()), "start_date")
    end_date = _parse_iso_date(end_date or start_date, "end_date")
    if start_date > end_date:
        raise ValueError("start_date cannot be after end_date")

    if subscribers.has_subscribers():
        return _send_subscriber_campaign(start_date, selected_sectors, end_date)

    return _send_legacy_sector_digests(start_date, selected_sectors, end_date)


def _send_subscriber_campaign(start_date, sectors, end_date=None):
    end_date = end_date or start_date
    period = _period_label(start_date, end_date)
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
            rows = _articles_for_sector(sector, start_date, end_date)
            if rows:
                sector_sections.append((SECTOR_LABELS[sector], rows))
                total_articles += len(rows)

        if not sector_sections:
            results.append({
                "email": email,
                "name": sub["name"],
                "sectors": sub_sectors,
                "status": "skipped",
                "reason": "no articles for this date range",
            })
            continue

        footer = build_email_footer(sub["token"])
        html = build_multi_sector_digest(sector_sections, period, footer)
        if len(sub_sectors) == 1:
            subject = f"{PRODUCT_NAME} — {SECTOR_LABELS[sub_sectors[0]]} — {period}"
        else:
            labels = ", ".join(SECTOR_LABELS[s] for s in sub_sectors[:3])
            if len(sub_sectors) > 3:
                labels += f" +{len(sub_sectors) - 3} more"
            subject = f"{PRODUCT_NAME} — {labels} — {period}"

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
        raise RuntimeError("No emails sent — no articles matched subscribers for these dates.")

    return {
        "mode": "subscribers",
        "pub_date": period,
        "start_date": start_date,
        "end_date": end_date,
        "sectors": sectors,
        "sent_count": len(sent),
        "skipped_count": len(results) - len(sent),
        "results": results,
    }


def _send_legacy_sector_digests(start_date, sectors, end_date=None):
    end_date = end_date or start_date
    period = _period_label(start_date, end_date)
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

        rows = _articles_for_sector(sector, start_date, end_date)
        if not rows:
            results.append({
                "sector": sector,
                "sector_label": label,
                "status": "skipped",
                "reason": "no articles for this date range",
                "recipients": emails,
            })
            continue

        html = build_digest_html(label, period, rows)
        subject = f"{PRODUCT_NAME} — {label} — {period}"
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
        "pub_date": period,
        "start_date": start_date,
        "end_date": end_date,
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


def _brief_card_html(report):
    payload = report.get("report") or {}
    label = report.get("sector_label") or SECTOR_LABELS.get(report.get("sector"), report.get("sector"))
    period = report.get("period_label") or f"{report.get('start_date')} – {report.get('end_date')}"
    summary = html_escape((payload.get("executive_summary") or "").strip() or "Open the hub for this week's brief.")
    href = f"{_base_url()}/intelligence?report={report['id']}"
    return f"""
        <tr>
          <td style="padding:16px 0;border-bottom:1px solid #e2e8f0;">
            <div style="font-size:12px;font-weight:700;color:#0d9488;text-transform:uppercase;letter-spacing:0.04em;margin-bottom:6px;">
              {html_escape(label)} · {html_escape(period)}
            </div>
            <div style="font-size:15px;font-weight:700;color:#1e3a5f;margin-bottom:8px;">{html_escape(label)}</div>
            <div style="font-size:14px;color:#475569;line-height:1.55;margin-bottom:10px;">{summary}</div>
            <a href="{href}" style="color:#0d9488;font-weight:700;text-decoration:none;">Read the full brief →</a>
          </td>
        </tr>"""


def send_weekly_intelligence_emails(start_date=None, end_date=None):
    """Email each active subscriber the weekly Intelligence Hub briefs for their sectors."""
    if not smtp_configured():
        raise RuntimeError(
            "SMTP is not configured. Add SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM to .env"
        )

    import intelligence_hub

    if not start_date or not end_date:
        start_date, end_date = intelligence_hub.week_for_email()
    start_date, end_date = intelligence_hub.validate_dates(start_date, end_date)
    period = intelligence_hub.period_label(start_date, end_date)

    reports_by_sector = {}
    for sector in SECTOR_LABELS:
        report = intelligence_hub.get_cached_report(sector, start_date, end_date)
        if not report:
            overlapping = intelligence_hub.reports_for_period(sector, start_date, end_date)
            report = overlapping[0] if overlapping else None
        if report:
            reports_by_sector[sector] = intelligence_hub.enrich_report_with_sources(report)

    if not reports_by_sector:
        raise LookupError(
            f"No Intelligence Hub briefs for {period}. Generate the week first, then send."
        )

    active = subscribers.list_subscribers(status=subscribers.STATUS_SUBSCRIBED)
    if not active:
        raise RuntimeError("No active subscribers to email.")

    results = []
    sent_emails = set()
    for sub in active:
        email = sub["email"]
        if email in sent_emails:
            continue
        matched = [reports_by_sector[s] for s in (sub.get("sectors") or []) if s in reports_by_sector]
        if not matched:
            results.append({
                "email": email,
                "name": sub.get("name"),
                "status": "skipped",
                "reason": "no briefs for subscribed sectors",
            })
            continue

        cards = "".join(_brief_card_html(report) for report in matched)
        footer = build_email_footer(sub["token"])
        html = _wrap_email_html(
            header_title=f"{PRODUCT_NAME} — Weekly Intelligence",
            header_subtitle=period,
            body_rows=cards,
            footer_html=footer,
        )
        labels = [report.get("sector_label") or report.get("sector") for report in matched]
        subject = f"{PRODUCT_NAME} — Weekly brief — {period}"
        if len(labels) == 1:
            subject = f"{PRODUCT_NAME} — {labels[0]} — {period}"
        _send_email([email], subject, html)
        sent_emails.add(email)
        results.append({
            "email": email,
            "name": sub.get("name"),
            "status": "sent",
            "sectors": [report.get("sector") for report in matched],
        })

    sent = [item for item in results if item["status"] == "sent"]
    if not sent:
        raise RuntimeError("No weekly emails sent — subscribers have no matching briefs this week.")

    return {
        "mode": "weekly_intelligence",
        "start_date": start_date,
        "end_date": end_date,
        "period_label": period,
        "briefs": len(reports_by_sector),
        "sent_count": len(sent),
        "skipped_count": len(results) - len(sent),
        "results": results,
    }
