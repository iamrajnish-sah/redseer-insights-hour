# One-time setup checklist

**Recommended path (chosen for you): stay on Vercel + Turso free DB.**  
See **[TURSO_SETUP.md](./TURSO_SETUP.md)** for click-by-click steps.

Do **not** rely on Vercel Blob again — free limits already wiped your data once.

Render is optional and **not free** for a durable disk. Skip it unless you want to pay.

---

## What you still need to do

1. Create Turso DB → add `TURSO_DATABASE_URL` + `TURSO_AUTH_TOKEN` on Vercel → Redeploy  
2. Keep GitHub Actions secrets: `CRON_SECRET` + `APP_BASE_URL=https://YOUR-INSIGHTS-HOUR.vercel.app`  
3. Later: `APIFY_TOKEN` for Instagram  

Automation (hourly news, 3h APIs, 5h website+IG scrape) runs from GitHub Actions — **no need to open the website**.

---

## Old notes (Render / Blob)

Kept below only as reference. Prefer Turso.

---

## 1) What is `CRON_SECRET`?

It is a **password for the automation URLs** (`/api/cron/...`).

- Your `ADMIN_PASSWORD` unlocks Backend Management in the browser.
- `CRON_SECRET` lets GitHub Actions (or cron-job.org) call the hourly / 3-hour / 5-hour jobs **without** logging into the UI.
- Generate any long random string (example): `openssl rand -hex 24`
- Put the **same value** in:
  1. **Vercel** (or Render) → Environment Variables → `CRON_SECRET`
  2. **GitHub** → repo → Settings → Secrets and variables → Actions → `CRON_SECRET`
  3. Also set GitHub secret `APP_BASE_URL` = `https://YOUR-INSIGHTS-HOUR.vercel.app`  
     (or your Render URL after you move)

Without `CRON_SECRET` on a hosted app, cron endpoints reject callers (safe).  
Without the GitHub secrets, the hourly automation workflow cannot call your app.

---

## 2) Why move the database host?

On Vercel Hobby, SQLite lives in `/tmp` and is wiped on cold start.
That is why the live site can show **0 articles** even after refresh.

**Best free/cheap fix:** Render or Railway with a **persistent disk**.
We already added `render.yaml`, `Procfile`, and `railway.toml` in the repo.

### Option A — Render (recommended, click path)

1. Go to [https://dashboard.render.com](https://dashboard.render.com) and sign in (GitHub login is fine).
2. **New +** → **Blueprint**.
3. Connect your Insights Hour GitHub repo.
4. Render reads `render.yaml` (web service + disk at `/var/data`).
5. Add env vars (same values you already use on Vercel):
   - `ADMIN_PASSWORD`
   - `GEMINIAPIKEY`
   - `INTELLIGENCE_GEMINI_API_KEY`
   - `CRON_SECRET` (new — see above)
   - `NEWSAPIKEY` / `GNEWSAPIKEY` (if you have them)
   - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM`
   - `APP_BASE_URL` = your new `https://….onrender.com` URL
   - `APIFY_TOKEN` (when you have it)
   - `PERSISTENT_DISK_PATH` = `/var/data` (already in blueprint)
6. Deploy.
7. In GitHub Actions secrets, set:
   - `APP_BASE_URL` = the Render URL
   - `CRON_SECRET` = same string
8. Open Backend Management on the Render URL — news will now persist on disk.

> Note: Render’s free web tier may sleep; the blueprint uses a small paid **starter** plan so the disk stays attached. If you must stay free, use Railway trial volume or keep Vercel + fix Blob (Option C).

### Option B — Railway

1. [https://railway.app](https://railway.app) → New Project → Deploy from GitHub repo.
2. Add a **Volume**, mount path `/data`.
3. Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
4. Copy the same env vars as above (Railway usually sets `RAILWAY_VOLUME_MOUNT_PATH` for you).
5. Point GitHub `APP_BASE_URL` at the Railway URL.

### Option C — Stay on Vercel (only if Blob works)

1. Vercel project → **Storage** → create/connect **Blob**.
2. Confirm `BLOB_READ_WRITE_TOKEN` + `BLOB_STORE_ID` exist.
3. Backend Management → **Save to Cloud** / check storage status.
4. Still add `CRON_SECRET` + GitHub secrets for hourly automation (Hobby cannot run hourly Vercel cron).

---

## 3) GitHub Actions (already in the repo)

File: `.github/workflows/automation-cron.yml`

After secrets exist:

| Secret | Example |
|---|---|
| `APP_BASE_URL` | `https://YOUR-INSIGHTS-HOUR.vercel.app` or Render URL |
| `CRON_SECRET` | same as app env |

Then Actions will call:

- hourly → `/api/cron/refresh-hourly`
- every 3h → `/api/cron/refresh-metered`
- every 5h → `/api/cron/festive-intelligence`

You can also run **Actions → Automation cron → Run workflow** manually.

---

## 4) See the new UI on Vercel now

Production already deployed commit `0df6815`.

1. Open https://YOUR-INSIGHTS-HOUR.vercel.app/
2. Unlock **Backend Management** with your admin password.
3. You should see **Website scraping** and **Instagram scraping** boxes.
4. Add scrape targets later (as you said).
5. Articles will keep disappearing until disk/Blob is fixed (section 2).

---

## 5) What the agent cannot do for you

- Create a Render/Railway account or click OAuth in your browser
- Write GitHub Actions secrets (API token has no `secrets:write`)
- Read your existing Vercel env values

Everything else (code, workflow file, Render blueprint, push to `main`) is already done.
