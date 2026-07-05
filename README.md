# Redseer Insight Hour

Daily sector intelligence dashboard for India — curated news by sector with email digests.

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
|----------|---------|
| `ADMIN_PASSWORD` | Protects Backend Management (required on public hosting) |
| `GEMINIAPIKEY` | Gemini classification + PDF/image newspaper reading |
| `NEWSAPIKEY` | NewsAPI ingestion (optional) |
| `SMTP_*` / `EMAIL_FROM` | Send sector email digests |
| `GEMINI_ORIGINS` | `epub,newspaper` (default) |
| `SECTOR_RECIPIENTS_JSON` | Email lists on Vercel (instead of local JSON file) |

**Never commit `.env`, `news.db`, or `sector_recipients.json`** — they are in `.gitignore`.

## Deploy on Vercel

1. Push this repo to GitHub.
2. Go to [vercel.com](https://vercel.com) → **Add New Project** → import `redseer-insights-hour`.
3. **Environment Variables** — add at minimum:
   - `ADMIN_PASSWORD` — your secret backend password
   - `GEMINIAPIKEY`
   - `NEWSAPIKEY` (optional)
   - SMTP vars if using email
   - `SECTOR_RECIPIENTS_JSON` — copy content from `sector_recipients.example.json` as one line
4. Deploy.

### Vercel notes

- The app uses **SQLite**. On Vercel, data is stored in `/tmp` and may **reset between deployments or cold starts**. For production persistence, plan to move to a hosted database later.
- Backend Management asks for `ADMIN_PASSWORD` when opening the panel.
- Public visitors only see the news dashboard; admin API routes reject requests without the password header.

## Features

- Sector tabs (e-commerce, ride hailing, quick commerce, etc.)
- RSS + NewsAPI ingestion with keyword filtering
- Newspaper upload: EPUB, PDF, images, TXT
- Sector-specific email digests
- Duplicate story removal (URL + headline)
