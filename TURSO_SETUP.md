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
3. Open the database → copy **both** of these from the Turso dashboard (not from this chat):

| What | Example of a **real** value (yours will differ) |
|---|---|
| **Database URL** | `libsql://redseer-insights-rajnish.aws-ap-south-1.turso.io` |
| **Auth Token** | a long string like `eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9…` (Create Token if needed) |

**Do not** paste the placeholder text from any guide:

- ❌ `libsql://....turso.io`
- ❌ `your_token_here`
- ❌ `your-turso-token`

Those fake values crash the site with `500 FUNCTION_INVOCATION_FAILED`.

---

## 2) Add Turso to Vercel (keep same site URL)

1. Vercel → your project **redseer-insights-hour** → **Settings → Environment Variables**
2. Add for Production (and Preview if you want):

| Name | Value |
|---|---|
| `TURSO_DATABASE_URL` | paste the **real** `libsql://…turso.io` URL from Turso |
| `TURSO_AUTH_TOKEN` | paste the **real** token from Turso → Tokens |
| `CRON_SECRET` | the same secret you already set |

3. **Redeploy** the project (Deployments → … → Redeploy, or push to `main`).

### Quick check after redeploy

Open: https://redseer-insights-hour.vercel.app/api/health

You want:

- `"turso_configured": true`
- `"turso_ping": "ok"`
- `"db_driver": "http"` (HTTPS — correct for Vercel)

If `"turso_problem"` mentions placeholders, replace the Vercel env values with the real Turso URL + token and redeploy again.

**Do not paste your Turso token into chat / Slack / email.** Keep it only in Vercel env vars.

### If the site 500s after adding real credentials

Older deploys used WebSockets (`libsql://` → `wss://`), which often crash on Vercel serverless.
Current code connects over **HTTPS** instead. Merge the latest fix, redeploy, then re-check `/api/health`.

After deploy, Backend Management should show something like:  
`Turso durable DB OK · N articles`

Past Blob data is already gone — this starts a **fresh durable** database that will not wipe on cold start.

---

## 3) Confirm GitHub automation secrets

GitHub → repo → **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `CRON_SECRET` | **exactly the same string** as Vercel `CRON_SECRET` |
| `APP_BASE_URL` | `https://redseer-insights-hour.vercel.app` |

### Cron schedule (GitHub Actions)

| When | Job |
|---|---|
| `:05` every hour | Free RSS + festive summarize |
| `:25` every hour | Website + Instagram round-robin scrape |
| `:20` every 3 hours | NewsAPI + GNews (metered) |
| `:35` every 5 hours | Festive intelligence brief + email |

### If hourly refresh is broken

Check Actions → **Automation cron**:

- **"Secrets missing"** → GitHub secrets not set (was exiting green before — now fails loudly).
- **HTTP 401** → `CRON_SECRET` on GitHub ≠ Vercel. Paste the same value in both places, redeploy Vercel, re-run the workflow.
- **HTTP 200** → working. Also check `/api/health` → `last_cron.hourly` should update.

Open: https://redseer-insights-hour.vercel.app/api/health — look for `cron_secret_configured` and `last_cron`.

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

### How to know data is on Turso

Open: https://redseer-insights-hour.vercel.app/api/health

You want:

- `"turso_configured": true`
- `"db_driver": "http"`
- `"turso_ping": "ok"`
- `"article_count":` a rising number after refresh

Also in Backend Management the green line should say:  
`Turso durable DB OK · N articles · Turso Cloud (….turso.io)`

If that line is green and count goes up after **Refresh All Sources**, news is storing in Turso.

---

## 6) Instagram / Apify token (for IG scrape)

1. Create a free Apify account: https://console.apify.com/sign-up  
2. Get your API token: https://console.apify.com/settings/integrations  
   (or Account → Integrations → API tokens)
3. Vercel → project **redseer-insights-hour** → **Settings → Environment Variables**
4. Add:

| Name | Value |
|---|---|
| `APIFY_TOKEN` | paste the Apify API token |

5. **Redeploy**
6. Backend → Load default IG list (if needed) → **Run all IG**  
   Posts appear in **Instagram scrape news** + Excel download.  
   Hint should turn green: `Apify ready · …`
