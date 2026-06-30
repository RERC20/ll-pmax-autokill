# Hourly PMax Auto-Kill — GitHub setup

This runs `kill_engine_google_auto.py` **every hour on GitHub's servers** (no laptop needed),
drafts losing products, emails you the report, and **commits the run log into this repo** so you
can read it any time you open the repo.

## Files this needs in the repo
- `kill_engine_google_auto.py`  (the unattended engine)
- `kill_engine_google.py`, `kill_engine_v4.py`, `google_ads_connect.py`  (it imports these)
- `requirements.txt`
- `.github/workflows/hourly-kill.yml`  (the hourly schedule)

## One-time setup (≈3 minutes)
1. **Create a NEW PRIVATE GitHub repo** (must be private — your Google/Shopify tokens are inside
   `google_ads_connect.py` and `kill_engine_v4.py`).
2. **Push the files above** into it.
3. **Add repo secrets** — in GitHub: `Settings → Secrets and variables → Actions → New repository secret`.
   The credential VALUES are in your local (git-ignored) `_secrets_local.py` — copy each into the matching secret:
   - `GOOGLE_DEVELOPER_TOKEN`
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
   - `GOOGLE_REFRESH_TOKEN`
   - `GOOGLE_CUSTOMER_ID`
   - `SHOPIFY_STORE`
   - `SHOPIFY_CLIENT_ID`
   - `SHOPIFY_CLIENT_SECRET`
   - `GMAIL_ADDRESS` = the Gmail you send from
   - `GMAIL_APP_PASSWORD` = a 16-char Gmail **App Password** (create at
     https://myaccount.google.com/apppasswords — NOT your normal password)

   (No secrets live in the code — the repo is safe even if it leaks. The values exist only in
   your local `_secrets_local.py` and in GitHub's encrypted Secrets.)
4. Done. It now runs at the top of every hour automatically.

## Where to see results
- **Run log (text):** `kill_engine_auto_runs.log` in the repo — updated and committed every hour.
- **Kills log (csv):** `kills_log_auto.csv` in the repo.
- **Per-run report:** emailed to you, subject "<N> Products Draft".
- **Live console:** the repo's **Actions** tab → latest run.

## Run it on demand
Repo → **Actions** tab → "PMax Auto-Kill (hourly)" → **Run workflow**.

## Notes
- Schedule is UTC, top of each hour; GitHub may delay a few minutes under load.
- The hourly commit also keeps the schedule alive (GitHub disables idle schedules after 60 days).
- To test without drafting, run it locally first: `python kill_engine_google_auto.py --dry`.
