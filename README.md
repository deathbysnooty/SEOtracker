# SEO Rank Tracker for ibtuition.sg

Automated daily SEO rank tracking system for **ibtuition.sg** on Google Singapore (`google.com.sg`). Tracks desktop and mobile rankings separately, monitors competitors, auto-discovers new competitors entering the top 5, and tracks 301 redirect consolidation from legacy domains.

## How It Works

- **3x daily** automated runs via GitHub Actions (7 AM, 1 PM, 7 PM SGT)
- Queries Google Singapore SERPs via [SerpApi](https://serpapi.com) for each keyword
- Desktop and mobile tracked **separately** (they rank differently)
- Results stored in SQLite, exported to JSON for the live dashboard
- Telegram report sent after every run
- Live dashboard hosted on GitHub Pages

## Quick Setup

### 1. Fork & Clone

```bash
git clone https://github.com/YOUR_USERNAME/seo-tracker.git
cd seo-tracker
```

### 2. Add GitHub Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

Add these three secrets:

| Secret | Value |
|--------|-------|
| `SERPAPI_KEY` | Your SerpApi API key from [serpapi.com/manage-api-key](https://serpapi.com/manage-api-key) |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather (see below) |
| `TELEGRAM_CHAT_ID` | Your chat ID from @userinfobot (see below) |

### 3. Edit config.json

Add your target keywords and any known competitors:

```json
{
  "keywords": [
    "ib math tutor singapore",
    "ib tuition singapore",
    "ib physics tutor singapore"
  ],
  "known_competitors": [
    "competitor1.com",
    "competitor2.sg"
  ]
}
```

You can start with an empty `known_competitors` array — the system will auto-discover competitors in the top 5.

### 4. Enable GitHub Pages

Go to repo → **Settings → Pages**:
- Source: **Deploy from a branch**
- Branch: **main**, folder: **/docs**
- Save

Update `github_pages_url` in config.json with your Pages URL (e.g., `https://yourusername.github.io/seo-tracker/dashboard.html`).

### 5. Test It

Go to **Actions** tab → **SEO Rank Tracker** → **Run workflow** → **Run workflow**

Check the Actions log and your Telegram for the first report.

## Telegram Bot Setup

### Create the Bot

1. Open Telegram, search for **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g., "SEO Tracker Bot")
4. Choose a username (e.g., `ibtuition_seo_bot`)
5. Copy the **bot token** — this is your `TELEGRAM_BOT_TOKEN`

### Get Your Chat ID

1. Open Telegram, search for **@userinfobot**
2. Send `/start`
3. It replies with your **user ID** — this is your `TELEGRAM_CHAT_ID`

### Send a Test Message

You must send a message to your bot first (search for it and click Start), then the bot can message you back.

## API Usage Calculator

Each tracking run makes this many SerpApi calls:

```
keywords × 2 devices × 1 = API calls per run
API calls per run × 3 runs/day = daily calls
daily calls × 30 = monthly calls
```

| Keywords | Per Run | Per Day (3x) | Per Month |
|----------|---------|--------------|-----------|
| 5 | 10 | 30 | 900 |
| 10 | 20 | 60 | 1,800 |
| 15 | 30 | 90 | 2,700 |
| 25 | 50 | 150 | 4,500 |

**Plans:**
- **Free tier** (100/month): Best used with `workflow_dispatch` only (manual runs)
- **Hobby plan** ($50/month, 5,000 searches): Good for up to ~25 keywords at 3x daily
- **Professional** ($130/month, 15,000 searches): For larger keyword sets

## Configuration Guide

### Add a Discovered Competitor to Tracking

When the system detects a new competitor, it appears in Telegram alerts and the dashboard. To track them permanently:

1. Open `config.json`
2. Add their domain to `known_competitors`:
   ```json
   "known_competitors": ["existingcompetitor.com", "newcompetitor.sg"]
   ```
3. Commit and push — they'll be tracked from the next run

### Change Run Schedule

Edit `.github/workflows/track.yml` and modify the cron expressions:

```yaml
schedule:
  - cron: '0 23 * * *'  # 7 AM SGT
  - cron: '0 5 * * *'   # 1 PM SGT
  - cron: '0 11 * * *'  # 7 PM SGT
```

Use [crontab.guru](https://crontab.guru) to calculate new times. Remember: GitHub Actions uses UTC.

### Disable Redirect Tracking

In `config.json`, set `tracking_active` to `false` for the domain:

```json
"redirect_domains": [
  {
    "old_domain": "ibmath.sg",
    "redirects_to": "ibtuition.sg",
    "tracking_active": false
  }
]
```

## What to Do If Old Domain Keeps Outranking You

If ibmath.sg or ibmathandphysics.sg keeps appearing in Google results above ibtuition.sg:

1. **Submit old URLs for removal** in [Google Search Console](https://search.google.com/search-console) → Removals tool
2. **Verify redirect is 301** (not 302) — the tracker's health check monitors this automatically
3. **Check old domain has no active sitemap** — remove or disavow any sitemap.xml on the old domain
4. **Disavow old domain backlinks** if needed — use [Google's Disavow Tool](https://search.google.com/search-console/disavow)
5. **Wait** — Google can take weeks to months to fully consolidate 301 redirects. The tracker's consolidation percentage will show progress over time.

## File Structure

```
seo-tracker/
├── .github/workflows/track.yml   # GitHub Actions schedule
├── docs/
│   ├── dashboard.html             # Live dashboard (GitHub Pages)
│   └── data.json                  # Auto-generated data for dashboard
├── tracker.py                     # Main SERP tracking script
├── export_data.py                 # SQLite → JSON exporter
├── send_telegram.py               # Telegram notification sender
├── config.json                    # Keywords, competitors, settings
├── requirements.txt               # Python dependencies
└── README.md
```

**Note:** `rankings.db` is stored in GitHub Actions cache, never committed to git.

## Dashboard

The dashboard is a single self-contained HTML file with no external dependencies. It shows:

- Current rankings for desktop and mobile
- Position changes with sparkline trend charts
- Competitor comparison
- Auto-discovered new competitors
- 301 redirect consolidation progress
- Run history and API usage

Access it at your GitHub Pages URL after enabling Pages.
