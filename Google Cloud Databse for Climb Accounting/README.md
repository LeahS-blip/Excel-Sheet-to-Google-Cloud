# UPS Billing → BigQuery pipeline

Loads billing files from a Google Drive folder into a Google BigQuery database, automatically, every time a new period's file appears. One row per charge line (~64 columns), nothing aggregated, totals reconcile to the penny.

## How it works

```
Google Drive folder              Cloud Scheduler (e.g. daily 7am)
  2026-05 A ...CODED.xlsx                 │ triggers
  2026-05 B ...CODED.xlsx                 ▼
  2026-06 A ...CODED.xlsx   ──▶   Cloud Function (main.py)
        ...                          │  • list files in the folder
                                     │  • skip ones already loaded (_load_log)
                                     │  • download new file, skip the summary
                                     │    preamble, read every charge line
                                     │  • clean + type each value
                                     ▼
                              BigQuery: wcc_billing.ups_charge_lines
                                     │
                                     ▼
                       Query / Looker Studio dashboards
```

The job is **idempotent**: re-running it never double-loads. It tracks every file it has ingested in a `_load_log` table and only loads files that are new — or that you re-coded in Drive since the last run.

## Why BigQuery

Serverless (no server to run or pay for around the clock), trivially handles your row counts, stays in the free tier at your volume, reads leading-zero codes safely as text, and connects straight to Looker Studio for dashboards. Cloud SQL would only make sense if an application needed to write back to the data.

## Files in this package

| File | What it is |
|------|-----------|
| `schema.sql` | BigQuery tables + helpful views. Run once. |
| `main.py` | The loader. Runs as a Cloud Function and from your laptop. |
| `requirements.txt` | Python dependencies. |
| `README.md` | This guide. |

---

## Part 1 — One-time Google Cloud setup

You'll do this once. Budget ~30 minutes. Everything is in the browser plus a few copy-paste commands.

### 1. Create a project
1. Go to <https://console.cloud.google.com> and sign in with the Google account that owns the Drive files.
2. Top bar → project dropdown → **New Project**. Name it e.g. `wcc-billing`. Note the **Project ID** (looks like `wcc-billing-433xxx`) — you'll use it everywhere `YOUR_PROJECT` appears.

### 2. Enable billing
BigQuery requires a billing account, but at your volume you'll almost certainly stay within the free tier (1 TB of queries and 10 GB storage free per month).
- Console → **Billing** → link a billing account to the project.

### 3. Turn on the APIs
- Console → **APIs & Services → Enable APIs** → enable each of: **BigQuery API**, **Google Drive API**, **Cloud Functions API**, **Cloud Build API**, **Cloud Scheduler API**, **Cloud Run API**.

### 4. Create the database tables
1. Console → **BigQuery**.
2. Open `schema.sql` from this package, replace every `YOUR_PROJECT` with your Project ID, paste into the query editor, **Run**.
3. You should now see a `wcc_billing` dataset with `ups_charge_lines`, `_load_log`, and two views.

### 5. Create a service account (the pipeline's identity)
1. Console → **IAM & Admin → Service Accounts → Create service account**.
2. Name it `billing-loader`. Create.
3. Grant it two roles: **BigQuery Data Editor** and **BigQuery Job User**.
4. Copy its email — it looks like `billing-loader@wcc-billing-433xxx.iam.gserviceaccount.com`.

### 6. Give the service account access to your Drive folder
The service account is a separate identity, so it can't see your Drive until you share with it.
1. In Google Drive, open the folder that holds the CODED files.
2. **Share** → paste the service account email → give it **Viewer** → Send.

> The folder you sent me has ID `1CUUnqHeKwTtZyUrfey-M1jv-RGf8D--3`. That is already the default in `main.py`. If the CODED files live somewhere else, share that folder instead and update `DRIVE_FOLDER_ID`.

---

## Part 2 — Test it on your laptop first

Before automating, confirm it loads correctly by running once locally.

1. Install Python 3.11+ if you don't have it.
2. In a terminal:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a service-account key: Console → the `billing-loader` account → **Keys → Add key → JSON**. Save the downloaded file as `key.json` next to `main.py`.
4. Set environment variables and run (macOS/Linux):
   ```bash
   export GCP_PROJECT="wcc-billing-433xxx"          # your Project ID
   export SERVICE_ACCOUNT_FILE="./key.json"
   python main.py
   ```
   On Windows PowerShell:
   ```powershell
   $env:GCP_PROJECT="wcc-billing-433xxx"
   $env:SERVICE_ACCOUNT_FILE=".\key.json"
   python main.py
   ```
5. You'll see lines like `LOAD  2026-05 B WS Ground BIlling CODED.xlsx: 24913 rows`. Then in BigQuery:
   ```sql
   SELECT * FROM `wcc-billing-433xxx.wcc_billing.v_period_totals`;
   ```
   Compare `total_ups_cost` and `grand_total_with_markup` against the summary block at the top of each source file. They should match.

This local path is also your **manual "just load it now" button** any time you don't want to wait for the schedule.

---

## Part 3 — Deploy as an automatic Cloud Function

Once the local test looks right, deploy so it runs unattended. Install the `gcloud` CLI (<https://cloud.google.com/sdk/docs/install>), then from the folder containing these files:

```bash
gcloud config set project wcc-billing-433xxx

gcloud functions deploy ups-billing-loader \
  --gen2 --runtime=python311 --region=us-central1 \
  --source=. --entry-point=run_load --trigger-http \
  --timeout=540s --memory=1Gi \
  --service-account=billing-loader@wcc-billing-433xxx.iam.gserviceaccount.com \
  --set-env-vars=GCP_PROJECT=wcc-billing-433xxx \
  --no-allow-unauthenticated
```

(No `SERVICE_ACCOUNT_FILE` here — running as the service account, the function authenticates automatically.)

### Schedule it
Run daily at 7:00 am; it does nothing on days with no new file, so a daily cadence comfortably covers your twice-a-month files.

```bash
FUNCTION_URL=$(gcloud functions describe ups-billing-loader --gen2 --region=us-central1 --format='value(serviceConfig.uri)')

gcloud scheduler jobs create http ups-billing-daily \
  --location=us-central1 --schedule="0 7 * * *" \
  --uri="$FUNCTION_URL" --http-method=POST \
  --oidc-service-account-email=billing-loader@wcc-billing-433xxx.iam.gserviceaccount.com
```

Done. Drop a new `...CODED.xlsx` into the Drive folder and it lands in BigQuery by the next morning (or trigger immediately with `gcloud scheduler jobs run ups-billing-daily`).

---

## Operating notes

- **Re-coded a file?** Just re-save it in Drive. The loader sees the newer `modifiedTime` and reloads it. To avoid duplicate rows when reloading, delete that file's old rows first:
  ```sql
  DELETE FROM `wcc-billing-433xxx.wcc_billing.ups_charge_lines`
  WHERE _source_file_name = '2026-05 B WS Ground BIlling CODED.xlsx';
  ```
  (Each row also carries `_row_hash` and `_source_file_id` if you prefer hash-based de-duplication.)
- **Check what loaded:** `SELECT * FROM \`...wcc_billing._load_log\` ORDER BY loaded_at DESC;`
- **Cost:** at a few hundred thousand rows total, expect $0/month — well inside the free tier.
- **Column changes:** if UPS adds/renames a column, the loader matches by header name and fills missing columns with NULL rather than breaking. To capture a brand-new column, add it to `COLUMNS` in `main.py` and to `schema.sql`.

## Verification checklist before trusting numbers

1. `v_period_totals.total_ups_cost` matches each file's "Total UPS Cost" summary line.
2. `v_period_totals.grand_total_with_markup` matches each file's "Grand Total".
3. Row count per file in `_load_log` matches the number of detail rows in the sheet.
4. Spot-check a few tracking numbers: leading zeros and letters preserved, dates correct, junk `1/0/1900` dates are NULL.
