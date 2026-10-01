# Redseer Insight Hour

Daily sector intelligence dashboard for India — curated news by sector with email digests, festive-sale automation, and optional website/Instagram scraping.

## Recommended hosting (durable DB)

**Vercel Hobby loses SQLite on cold start** unless Blob works. Prefer **Render** or **Railway** with a persistent disk:

| Host | Disk setup | Notes |
|---|---|---|
| **Render** | Use `render.yaml` (disk at `/var/data`) | Set `PERSISTENT_DISK_PATH=/var/data` |
| **Railway** | Attach a Volume at `/data` | `RAILWAY_VOLUME_MOUNT_PATH` is usually auto-set |
| Vercel | Blob store connected | Works, but Hobby cannot run hourly crons |

Sub-daily automation (hourly / 3h / 5h) uses **GitHub Actions** → your app’s `/api/cron/*` endpoints with `CRON_SECRET`.

## Local setup

```powershell
cd commerce_wire-main
pip install -r requirements.txt
copy .env.example .env
# Edit .env — add API keys and ADMIN_PASSWORD
.\start.ps1
```

Open **http://localhost:8000**

## Environment variables

Copy `.env.example` to `.env`. Important variables:

| Variable | Purpose |
|---|---|
| `ADMIN_PASSWORD` | Protects Backend Management |
| `CRON_SECRET` | Protects `/api/cron/*` (required on hosted) |
| `GEMINIAPIKEY` | News classification (festive hourly summaries) |
| `INTELLIGENCE_GEMINI_API_KEY` | Intelligence Hub briefs (second Gemini key) |
| `NEWSAPIKEY` / `GNEWSAPIKEY` | Metered news APIs |
| `APIFY_TOKEN` | Instagram scraping via Apify |
| `SMTP_*` / `EMAIL_FROM` | Email digests |
| `PERSISTENT_DISK_PATH` | e.g. `/var/data` on Render |
| `APP_BASE_URL` | Public URL for email links + GitHub Actions |

## Automation schedule

| Job | Cadence | Endpoint | What it does |
|---|---|---|---|
| Free feeds | Hourly | `/api/cron/refresh-hourly` | RSS + festive GNews + **festive-only** Gemini summaries |
| Metered APIs | Every 3h | `/api/cron/refresh-metered` | Full NewsAPI + GNews |
| Festive intel | Every 5h | `/api/cron/festive-intelligence` | Scrape targets → festive brief → email |
| Weekly intel | Mondays | `/api/cron/weekly-intelligence` | Weekly subscriber briefs |

Wire GitHub → Settings → Secrets: `APP_BASE_URL`, `CRON_SECRET`. Workflow: `.github/workflows/automation-cron.yml`.

## Scraping (Backend Management)

Two boxes appear after you unlock Backend Management:

1. **Website scraping** — paste a sale/brand page URL; matching festive/sale links are ingested into Festive Sale.
2. **Instagram scraping** — paste `@handle` or profile URL; requires free `APIFY_TOKEN`.

Targets are stored in SQLite and re-run on the festive intelligence cron.

## Intelligence exports

Reports can be downloaded as **JSON**, **Markdown**, **HTML** (Print → PDF), and **Word (.docx)**.

## Deploy on Render

1. Push this repo to GitHub.
2. Render → New → Blueprint → select repo (`render.yaml`).
3. Set env vars (at least `ADMIN_PASSWORD`, `CRON_SECRET`, Gemini keys, SMTP).
4. Set GitHub secrets `APP_BASE_URL` + `CRON_SECRET` so Actions can hit cron routes.

## Deploy on Railway

1. New project from repo.
2. Add a Volume mounted at `/data`.
3. Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
4. Set the same env vars + GitHub Actions secrets.

## Features

- Sector tabs (e-commerce, ride hailing, festive sale, etc.)
- RSS + NewsAPI + GNews ingestion
- Festive Sale specialist matcher + hourly festive summaries only
- Website + Instagram scrape targets
- Newspaper upload (EPUB/PDF/images via Gemini)
- Intelligence Hub with HTML/Word/PDF-ready export
- Sector email digests + automated festive briefs
