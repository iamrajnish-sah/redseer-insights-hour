# Stay on Vercel + Turso (chosen path)

You already hit **Vercel Blob free limits and lost data**.  
Do **not** rely on Blob again.

**Choice: stay on Vercel + Turso free DB (~5GB, always on).**  
No need to move the app to Render.

Automation (news + website scrape + Instagram) runs via **GitHub Actions + CRON_SECRET** — you do **not** need to open the website for refresh.

---

## 1) Create free Turso database (2 minutes)

1. Open https://turso.tech and sign up / log in (GitHub login is fine).
2. Create a database, e.g. name: `redseer-insights`.
3. Open the database → copy:
   - **Database URL** (looks like `libsql://redseer-insights-xxxx.turso.io`)
   - **Auth Token** (create a token if needed)

---

## 2) Add Turso to Vercel (keep same site URL)

1. Vercel → your project **redseer-insights-hour** → **Settings → Environment Variables**
2. Add for Production (and Preview if you want):

| Name | Value |
|---|---|
| `TURSO_DATABASE_URL` | `libsql://…turso.io` |
| `TURSO_AUTH_TOKEN` | your Turso token |
| `CRON_SECRET` | the same secret you already set |

3. **Redeploy** the project (Deployments → … → Redeploy, or push to `main`).

After deploy, Backend Management should show something like:  
`Turso durable DB OK · N articles`

Past Blob data is already gone — this starts a **fresh durable** database that will not wipe on cold start.

---

## 3) Confirm GitHub automation secrets

GitHub → repo → **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `CRON_SECRET` | same as Vercel |
| `APP_BASE_URL` | `https://redseer-insights-hour.vercel.app` |

Workflow `.github/workflows/automation-cron.yml` will then hit:

- every hour → news + festive summarize  
- every 3h → NewsAPI/GNews  
- every 5h → website + Instagram scrape + festive intelligence/email  

No page visit required.

Manual test (optional):

```bash
curl -H "Authorization: Bearer YOUR_CRON_SECRET" \
  "https://redseer-insights-hour.vercel.app/api/cron/refresh-hourly"
```

---

## 4) Why not Blob / Render?

| Option | Verdict for you |
|---|---|
| Vercel Blob | Already exceeded free limit and wiped data — **reject** |
| Render free | App sleeps; **no free persistent disk** — not safer |
| **Turso + Vercel** | Free ~5GB durable SQLite-compatible DB — **chosen** |

---

## 5) After Turso is live

1. Hard-refresh the website.  
2. Backend → **Load default festive websites + Instagram list** (if empty).  
3. **Run festive sites**. Results appear in the **Website scrape news** box.  
4. When `APIFY_TOKEN` is set, **Run all IG** → **Instagram scrape news** box + Excel.  
5. Leave the site — GitHub Actions keeps refreshing in the background.
