# Deploying for free: Render + Neon + Upstash

No credit card needed. Takes about 30 minutes.

```
GitHub ──webhook──► Render free web service (one container)
                     ├─ FastAPI: webhooks + dashboard
                     └─ the review worker, inside the same process (EMBEDDED_WORKER=true)
                              │                          │
                        Upstash Redis (free)       Neon Postgres (free)

cron-job.org (free) ── pings /healthz every 10 min ──► keeps the service awake
```

**Why one container?** Render's free plan gives one web service, and background workers are paid. The worker mostly waits on E2B and the LLM over the network, so it fits next to the API in 512 MB.

**Why the ping?** A free Render service sleeps after 15 minutes without traffic, and waking takes 30–60 seconds. GitHub gives a webhook only 10 seconds. A ping every 10 minutes keeps it awake. The 750 free hours a month cover one service running all month.

---

## Step 1: Push your code to GitHub

Render deploys from your GitHub repo, so everything must be pushed:
```
git add .
git status          # .env and *.pem must NOT appear in the list
git commit -m "Free deploy on Render"
git push
```

## Step 2: Postgres on Neon

1. Sign up at https://neon.tech (GitHub login works).
2. **Create project**, using the region closest to you (for example *Singapore*).
3. Copy the **connection string**. It looks like:
   `postgresql://user:password@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require`
4. Change it in **two places**, because our driver is asyncpg:
   - the start: `postgresql://` → `postgresql+asyncpg://`
   - the end: `?sslmode=require&channel_binding=require` → `?ssl=require`

   Result: `postgresql+asyncpg://user:password@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?ssl=require`

   This is your **DATABASE_URL**.

## Step 3: Redis on Upstash

1. Sign up at https://upstash.com.
2. **Create database** → Redis, in the same region as Neon.
3. Copy the URL that starts with **`rediss://`** (two s's means encrypted):
   `rediss://default:xxxx@xxxx.upstash.io:6379`

   This is your **REDIS_URL**.

## Step 4: Create the Render service

1. Sign up at https://render.com with GitHub and allow access to your **PRReview-Agent** repo.
2. **New +** → **Blueprint** → pick the repo. Render reads `render.yaml` and creates a free web service called `prreview`.
3. Render asks for the secret values. Use the same values as your `.env`, except:

| Key | Value |
|---|---|
| `GITHUB_APP_ID` | same as `.env` |
| `GITHUB_PRIVATE_KEY` | open your `.pem` file in Notepad and paste **everything**, including the BEGIN/END lines |
| `GITHUB_WEBHOOK_SECRET` | same as `.env` |
| `BOT_LOGIN` | same as `.env` (e.g. `prreview-dev-chaithanya[bot]`) |
| `TOKEN_ENCRYPTION_KEY` | same as `.env` |
| `DATABASE_URL` | the Neon URL from Step 2 |
| `REDIS_URL` | the Upstash URL from Step 3 |
| `OPENROUTER_API_KEY` | same as `.env` |
| `E2B_API_KEY` | same as `.env` |
| `DEMO_REPOS` | `ChaithanyaK230/prreview-demo` |
| `PUBLIC_URL` | leave empty for now (set in Step 5) |

4. Click **Apply**. The first build takes about 5 minutes. In the **Logs** tab, look for:
   ```
   INFO  [alembic.runtime.migration] ...      ← tables created in Neon
   worker.embedded.started                     ← the worker is running
   Uvicorn running on http://0.0.0.0:10000
   ```

## Step 5: Check it's alive

Your URL appears at the top of the Render page, for example `https://prreview-xxxx.onrender.com`.

1. Open `https://prreview-xxxx.onrender.com/readyz`. It should show `{"status":"ready"}`, which means Neon and Upstash are connected.
2. Open `https://prreview-xxxx.onrender.com/`. You'll see the dashboard (empty for now).
3. In Render → **Environment**, set `PUBLIC_URL` = `https://prreview-xxxx.onrender.com` and save. It redeploys by itself.

## Step 6: Point GitHub at Render

GitHub → **Settings** → **Developer settings** → **GitHub Apps** → your app → **Edit**:
- **Webhook URL:** `https://prreview-xxxx.onrender.com/webhooks/github`
- **Webhook secret:** the same `GITHUB_WEBHOOK_SECRET`

Save. Stop `smee` on your laptop, since webhooks now go to Render.

## Step 7: Keep it awake

1. Sign up at https://cron-job.org (free).
2. **Create cronjob**: URL `https://prreview-xxxx.onrender.com/healthz`, schedule **every 10 minutes**, then save.

## Step 8: The real test

Open a PR with a bug on **prreview-demo**. Within about 2 minutes:
1. the **PR Review** check appears,
2. inline comments and the summary comment appear, with a **View full trace** link,
3. the link opens your live dashboard.

🎉 It's deployed. Put the dashboard link in your README and on your resume.

---

## If something goes wrong

| What you see | Fix |
|---|---|
| The build fails | Render → **Logs**: copy the red error and paste it to me |
| `/readyz` fails or the log shows a database error | `DATABASE_URL` must start with `postgresql+asyncpg://` and end with `?ssl=require` |
| A Redis connection error | `REDIS_URL` must start with `rediss://` (two s's) |
| No review on the PR | GitHub App → **Advanced** → **Recent Deliveries**. A red ✗ or a timeout means the service was asleep: click **Redeliver**. Then check Render **Logs** |
| `401 Bad credentials` in the logs | `GITHUB_PRIVATE_KEY` was pasted incompletely (it needs the BEGIN and END lines) |
| The dashboard is empty | `DEMO_REPOS` is missing or misspelled |

**Updating later:** just `git push`. Render rebuilds automatically, and database migrations run at start. A review that's running during a redeploy is retried and continues from where it stopped.

**Developing locally again:** point the GitHub App webhook URL back to your smee URL. Local dev doesn't use `EMBEDDED_WORKER`; keep running the API and worker in separate terminals as before.

---

## Paid alternative: Fly.io

If you later get a card, `fly.toml` runs the API and worker as separate machines (needs `fly launch`, `fly secrets set ...`, `fly deploy`). Migrations run as Fly's release command. Same Neon and Upstash setup.
