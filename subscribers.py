"""
subscribers.py

Subscriber management — SQLite tables, CRUD, analytics, and token-based
unsubscribe / manage-preferences links.
"""

import csv
import io
import re
import uuid
from datetime import datetime, timedelta

import database
from sector_keywords import SECTOR_LABELS, normalize_sector_tags

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
STATUS_SUBSCRIBED = "subscribed"
STATUS_UNSUBSCRIBED = "unsubscribed"
VALID_STATUSES = {STATUS_SUBSCRIBED, STATUS_UNSUBSCRIBED}

SUBSCRIBER_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    company TEXT,
    designation TEXT,
    status TEXT NOT NULL DEFAULT 'subscribed',
    token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriber_sectors (
    subscriber_id INTEGER NOT NULL,
    sector TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (subscriber_id, sector),
    FOREIGN KEY (subscriber_id) REFERENCES subscribers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_subscriber_sectors_sector ON subscriber_sectors(sector);
CREATE INDEX IF NOT EXISTS idx_subscribers_status ON subscribers(status);
CREATE INDEX IF NOT EXISTS idx_subscribers_created ON subscribers(created_at);
"""


def init_subscriber_tables(conn):
    conn.executescript(SUBSCRIBER_SCHEMA)


def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _normalize_email(email):
    return (email or "").strip().lower()


def validate_email(email):
    email = _normalize_email(email)
    if not email or not EMAIL_RE.match(email):
        raise ValueError("Invalid email address")
    return email


def validate_sectors(sectors):
    if not sectors:
        raise ValueError("Select at least one sector")
    cleaned = []
    for sector in sectors:
        sector = (sector or "").strip()
        if sector not in SECTOR_LABELS:
            raise ValueError(f"Unknown sector: {sector}")
        if sector not in cleaned:
            cleaned.append(sector)
    return cleaned


def validate_status(status):
    status = (status or STATUS_SUBSCRIBED).strip().lower()
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")
    return status


def _row_to_subscriber(row, sectors=None):
    if not row:
        return None
    data = dict(row)
    data["sectors"] = sectors if sectors is not None else get_subscriber_sectors(data["id"])
    return data


def get_subscriber_sectors(subscriber_id):
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT sector FROM subscriber_sectors WHERE subscriber_id = ? ORDER BY sector",
            (subscriber_id,),
        ).fetchall()
    return [row["sector"] for row in rows]


def _set_subscriber_sectors(conn, subscriber_id, sectors):
    now = _now_iso()
    conn.execute("DELETE FROM subscriber_sectors WHERE subscriber_id = ?", (subscriber_id,))
    for sector in sectors:
        conn.execute(
            "INSERT INTO subscriber_sectors (subscriber_id, sector, created_at) VALUES (?, ?, ?)",
            (subscriber_id, sector, now),
        )


def subscribe(name, email, sectors, company=None, designation=None):
    name = (name or "").strip()
    if not name:
        raise ValueError("Full name is required")

    email = validate_email(email)
    sectors = validate_sectors(sectors)
    company = (company or "").strip() or None
    designation = (designation or "").strip() or None
    now = _now_iso()

    with database.get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM subscribers WHERE email = ?",
            (email,),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE subscribers
                   SET name = ?, company = ?, designation = ?, status = ?, updated_at = ?
                   WHERE id = ?""",
                (name, company, designation, STATUS_SUBSCRIBED, now, existing["id"]),
            )
            _set_subscriber_sectors(conn, existing["id"], sectors)
            subscriber_id = existing["id"]
            created = False
            token = existing["token"]
        else:
            token = uuid.uuid4().hex
            cur = conn.execute(
                """INSERT INTO subscribers
                   (name, email, company, designation, status, token, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, email, company, designation, STATUS_SUBSCRIBED, token, now, now),
            )
            subscriber_id = cur.lastrowid
            _set_subscriber_sectors(conn, subscriber_id, sectors)
            created = True

    database.persist()
    return _row_to_subscriber(get_subscriber_by_id(subscriber_id)), created


def get_subscriber_by_id(subscriber_id):
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)).fetchone()
    return _row_to_subscriber(row)


def get_subscriber_by_email(email):
    email = _normalize_email(email)
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM subscribers WHERE email = ?", (email,)).fetchone()
    return _row_to_subscriber(row)


def get_subscriber_by_token(token):
    token = (token or "").strip()
    if not token:
        return None
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM subscribers WHERE token = ?", (token,)).fetchone()
    return _row_to_subscriber(row)


def unsubscribe_by_token(token):
    subscriber = get_subscriber_by_token(token)
    if not subscriber:
        raise ValueError("Invalid or expired unsubscribe link")
    now = _now_iso()
    with database.get_conn() as conn:
        conn.execute(
            "UPDATE subscribers SET status = ?, updated_at = ? WHERE id = ?",
            (STATUS_UNSUBSCRIBED, now, subscriber["id"]),
        )
    database.persist()
    return get_subscriber_by_id(subscriber["id"])


def update_preferences_by_token(token, sectors):
    subscriber = get_subscriber_by_token(token)
    if not subscriber:
        raise ValueError("Invalid preferences link")
    sectors = validate_sectors(sectors)
    now = _now_iso()
    with database.get_conn() as conn:
        conn.execute(
            "UPDATE subscribers SET status = ?, updated_at = ? WHERE id = ?",
            (STATUS_SUBSCRIBED, now, subscriber["id"]),
        )
        _set_subscriber_sectors(conn, subscriber["id"], sectors)
    database.persist()
    return get_subscriber_by_id(subscriber["id"])


def update_subscriber(subscriber_id, name=None, email=None, company=None, designation=None,
                      status=None, sectors=None):
    subscriber = get_subscriber_by_id(subscriber_id)
    if not subscriber:
        raise ValueError("Subscriber not found")

    new_name = (name if name is not None else subscriber["name"]).strip()
    if not new_name:
        raise ValueError("Full name is required")

    new_email = validate_email(email if email is not None else subscriber["email"])
    new_company = subscriber["company"] if company is None else ((company or "").strip() or None)
    new_designation = (
        subscriber["designation"] if designation is None else ((designation or "").strip() or None)
    )
    new_status = validate_status(status if status is not None else subscriber["status"])
    new_sectors = validate_sectors(sectors if sectors is not None else subscriber["sectors"])
    now = _now_iso()

    with database.get_conn() as conn:
        conflict = conn.execute(
            "SELECT id FROM subscribers WHERE email = ? AND id != ?",
            (new_email, subscriber_id),
        ).fetchone()
        if conflict:
            raise ValueError("Another subscriber already uses this email")

        conn.execute(
            """UPDATE subscribers
               SET name = ?, email = ?, company = ?, designation = ?, status = ?, updated_at = ?
               WHERE id = ?""",
            (new_name, new_email, new_company, new_designation, new_status, now, subscriber_id),
        )
        _set_subscriber_sectors(conn, subscriber_id, new_sectors)

    database.persist()
    return get_subscriber_by_id(subscriber_id)


def delete_subscriber(subscriber_id):
    with database.get_conn() as conn:
        row = conn.execute("SELECT id FROM subscribers WHERE id = ?", (subscriber_id,)).fetchone()
        if not row:
            raise ValueError("Subscriber not found")
        conn.execute("DELETE FROM subscribers WHERE id = ?", (subscriber_id,))
    database.persist()


def list_subscribers(search=None, sector=None, status=None):
    query = """
        SELECT s.*, GROUP_CONCAT(ss.sector, ',') AS sector_csv
        FROM subscribers s
        LEFT JOIN subscriber_sectors ss ON ss.subscriber_id = s.id
        WHERE 1=1
    """
    params = []

    if search:
        term = f"%{search.strip()}%"
        query += " AND (s.name LIKE ? OR s.email LIKE ? OR IFNULL(s.company,'') LIKE ?)"
        params.extend([term, term, term])

    if status:
        query += " AND s.status = ?"
        params.append(validate_status(status))

    query += " GROUP BY s.id"

    if sector:
        sector = sector.strip()
        if sector not in SECTOR_LABELS:
            raise ValueError(f"Unknown sector: {sector}")
        query += " HAVING SUM(CASE WHEN ss.sector = ? THEN 1 ELSE 0 END) > 0"
        params.append(sector)

    query += " ORDER BY s.created_at DESC, s.id DESC"

    with database.get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    results = []
    for row in rows:
        data = dict(row)
        sector_csv = data.pop("sector_csv", "") or ""
        data["sectors"] = [s for s in sector_csv.split(",") if s] if sector_csv else []
        results.append(data)
    return results


def get_active_subscribers_for_sectors(sectors):
    """Return unique active subscribers for any of the given sectors."""
    sectors = validate_sectors(sectors)
    placeholders = ",".join("?" * len(sectors))
    query = f"""
        SELECT DISTINCT s.*
        FROM subscribers s
        INNER JOIN subscriber_sectors ss ON ss.subscriber_id = s.id
        WHERE s.status = ? AND ss.sector IN ({placeholders})
        ORDER BY s.email
    """
    params = [STATUS_SUBSCRIBED] + sectors
    with database.get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    results = []
    for row in rows:
        results.append(_row_to_subscriber(row))
    return results


def subscriber_count():
    with database.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM subscribers").fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) c FROM subscribers WHERE status = ?",
            (STATUS_SUBSCRIBED,),
        ).fetchone()["c"]
    return total, active


def has_subscribers():
    with database.get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) c FROM subscribers WHERE status = ?", (STATUS_SUBSCRIBED,)).fetchone()
    return (row["c"] if row else 0) > 0


def get_analytics():
    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%dT00:00:00")
    month_start = now.replace(day=1).strftime("%Y-%m-%dT00:00:00")

    with database.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM subscribers").fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) c FROM subscribers WHERE status = ?",
            (STATUS_SUBSCRIBED,),
        ).fetchone()["c"]
        unsubscribed = conn.execute(
            "SELECT COUNT(*) c FROM subscribers WHERE status = ?",
            (STATUS_UNSUBSCRIBED,),
        ).fetchone()["c"]
        new_week = conn.execute(
            "SELECT COUNT(*) c FROM subscribers WHERE created_at >= ?",
            (week_start,),
        ).fetchone()["c"]
        new_month = conn.execute(
            "SELECT COUNT(*) c FROM subscribers WHERE created_at >= ?",
            (month_start,),
        ).fetchone()["c"]
        sector_rows = conn.execute(
            """SELECT ss.sector, COUNT(DISTINCT s.id) c
               FROM subscriber_sectors ss
               INNER JOIN subscribers s ON s.id = ss.subscriber_id
               WHERE s.status = ?
               GROUP BY ss.sector
               ORDER BY c DESC, ss.sector""",
            (STATUS_SUBSCRIBED,),
        ).fetchall()

    by_sector = []
    for row in sector_rows:
        by_sector.append({
            "sector": row["sector"],
            "label": SECTOR_LABELS.get(row["sector"], row["sector"]),
            "count": row["c"],
        })

    for sector, label in SECTOR_LABELS.items():
        if not any(item["sector"] == sector for item in by_sector):
            by_sector.append({"sector": sector, "label": label, "count": 0})

    by_sector.sort(key=lambda item: (-item["count"], item["label"]))

    return {
        "total": total,
        "active": active,
        "unsubscribed": unsubscribed,
        "new_this_week": new_week,
        "new_this_month": new_month,
        "by_sector": by_sector,
    }


def export_csv(search=None, sector=None, status=None):
    rows = list_subscribers(search=search, sector=sector, status=status)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "name", "email", "company", "designation", "status",
        "sectors", "created_at", "updated_at",
    ])
    for row in rows:
        writer.writerow([
            row["id"],
            row["name"],
            row["email"],
            row.get("company") or "",
            row.get("designation") or "",
            row["status"],
            "; ".join(row.get("sectors") or []),
            row["created_at"],
            row["updated_at"],
        ])
    return output.getvalue()
