# LegaTell Pipeline

The backend data pipeline that powers legatell.com reports.

## How it works

```
User enters index number
        ↓
1. CSV fast path — check SupremeOutput.csv (720 known cases, instant)
        ↓ (if not found)
2. Live NYSCEF fetch — scrape iapps.courts.state.ny.us
        ↓
3. Parse HTML — extract docket entries, parties, status
        ↓
4. Analytics engine — compute timeline, PowerNodes, recommendation
        ↓
5. Return JSON report → legatell.com frontend
```

## Local setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Install Playwright browser (one-time)
playwright install chromium

# 3. Put your SupremeOutput.csv in this directory

# 4. Test the pipeline directly
python pipeline.py --index "2023-00005029" --court "Chenango County Supreme Court" --no-browser

# 5. Start the API server
python api.py
# → Running on http://localhost:5000
```

## API endpoints

### Generate a report
```
GET  /report?index=614882/2023
POST /report  {"index": "614882/2023", "court": "Nassau County Supreme Court"}
```

### Search cases
```
GET /search?q=wells+fargo&limit=5
```

### List courts
```
GET /courts
```

### Health check
```
GET /health
```

## Deploying to Railway (recommended — free tier available)

1. Create account at railway.app
2. New Project → Deploy from GitHub (push this folder to a repo first)
   OR: New Project → Deploy from CLI
   ```bash
   npm install -g @railway/cli
   railway login
   railway init
   railway up
   ```
3. Set environment variables in Railway dashboard:
   - `CSV_PATH` = path to your SupremeOutput.csv (upload as a volume)
   - `USE_BROWSER` = false (use requests-only mode on Railway)
   - `PORT` = 5000 (Railway sets this automatically)
4. Railway gives you a URL like `legatell-pipeline.railway.app`
5. Update legatell.com frontend to call this URL

## Connecting to legatell.com

In `js/report.js`, replace the static CASES lookup with:

```javascript
async function loadReport() {
  const index = document.getElementById('case-input').value.trim();
  const res = await fetch(`https://your-railway-url.railway.app/report?index=${encodeURIComponent(index)}`);
  if (!res.ok) {
    // show not found message
    return;
  }
  const report = await res.json();
  renderReport(report);
}
```

## Scaling the dataset

The current CSV has ~720 cases from Jan 2023.
To grow the dataset, re-run your friend's scraper with a broader date range:

```bash
python runner.py --from-date 2020-01-01 --to-date 2023-12-31 --court Nassau
```

The pipeline will automatically pick up the new SupremeOutput.csv.

## Next: judge-specific stats

Once we have more data, run `build_judge_stats.py` (coming next)
to compute real per-judge profiles from the docket history.
