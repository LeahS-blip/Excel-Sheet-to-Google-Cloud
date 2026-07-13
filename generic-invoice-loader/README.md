# Generic Invoice Loader — Google Drive → BigQuery

Loads long invoices (15,000+ rows, any client's format — including the UPS billing format) from a Google Drive folder into BigQuery automatically, every time a new file appears. No computer needs to stay on — this runs as a serverless Cloud Function on a schedule.

Picks up `.csv` and `.xlsx` files, plus native Google Sheets (Drive exports those as CSV automatically).

This uses the same proven pattern as the UPS billing pipeline elsewhere in this repo (Drive folder → Cloud Function → BigQuery, with a `_load_log` dedup table), but instead of a fixed 64-column schema for one invoice format, this version auto-detects whatever columns are in each file's header row — so it works across different clients' invoice layouts, including ones you haven't seen the column layout for yet.

**Data safety.** Auto-detecting columns is convenient, but naively converting "numbers" and "dates" everywhere risks quietly corrupting exactly the kind of data invoices are full of. This loader guards against the two failure modes that matter most:

- **Leading-zero codes** (account numbers, tracking numbers, postal codes like `"00000088BH"` or `"00501"`) are never auto-converted to numbers — a column is only treated as numeric if *every* value converts cleanly *and* none of them look like a leading-zero code. Otherwise the whole column stays text, so zeros are never silently dropped.
- **Junk placeholder dates** (Excel's classic `1/0/1900`, `0/0/0000`, or anything that parses to year ≤ 1900) are loaded as `NULL` instead of as garbage text or a bogus date.

## How it works

```
Google Drive folder              Cloud Scheduler (e.g. daily 7am)
  client-a-invoice.csv                    │ triggers
  client-b-invoice.xlsx                   ▼
  client-c-invoice.csv      ──▶   Cloud Function (main.py)
        ...                          │  • list files in the folder
                                     │  • skip ones already loaded (_load_log)
                                     │  • download new file, find the header
                                     │    row, read every data row
                                     ▼
                              BigQuery: invoices.invoice_line_items
                                     │
                                     ▼
                       Query / Looker Studio dashboards
```

The job is **idempotent**: re-running it never double-loads. It tracks every file it has ingested in a `_load_log` table and only loads files that are new — or that were re-uploaded to Drive since the last run.

## Getting files into Drive automatically

This pipeline picks up files from a Drive folder — it doesn't watch a local computer. To make the "drop a file in and forget it" experience complete, pair this with one of:

- **Google Drive for Desktop** (free) — install it and point it at the invoice folder; anything saved there locally syncs to Drive automatically, no custom script needed.
- **Manually dragging files into the Drive folder** in a browser — fine if invoices arrive occasionally rather than constantly.
- **Forwarding email attachments into Drive** via a simple Gmail filter + Apps Script, if invoices mostly arrive by email.

## Files in this package

| File | What it is |
|------|-----------|
| `schema.sql` | BigQuery dataset + load-log table. Run once. |
| `main.py` | The loader. Runs as a Cloud Function and from your laptop. |
| `requirements.txt` | Python dependencies. |
| `README.md` | This guide. |

---

## Part 1 — One-time Google Cloud setup

Budget ~30 minutes. Everything is in the browser plus a few copy-paste commands.

### 1. Create a project
1. Go to <https://console.cloud.google.com> and sign in with the Google account that owns the Drive folder.
2. Top bar → project dropdown → **New Project**. Name it e.g. `invoice-pipeline`. Note the **Project ID** — you'll use it everywhere `YOUR_PROJECT` appears.

### 2. Enable billing
BigQuery requires a billing account, but at typical invoice volumes you'll almost certainly stay within the free tier (1 TB of queries and 10 GB storage free per month).
- Console → **Billing** → link a billing account to the project.

### 3. Turn on the APIs
- Console → **APIs & Services → Enable APIs** → enable each of: **BigQuery API**, **Google Drive API**, **Cloud Functions API**, **Cloud Build API**, **Cloud Scheduler API**, **Cloud Run API**.

### 4. Create the dataset and load-log table
1. Console → **BigQuery**.
2. Open `schema.sql` from this package, replace every `YOUR_PROJECT` with your Project ID, paste into the query editor, **Run**.
3. You should now see an `invoices` dataset with a `_load_log` table. The main `invoice_line_items` table is created automatically the first time a file loads successfully — you don't need to create it by hand.

### 5. Create a service account (the pipeline's identity)
1. Console → **IAM & Admin → Service Accounts → Create service account**.
2. Name it `invoice-loader`. Create.
3. Grant it two roles: **BigQuery Data Editor** and **BigQuery Job User**.
4. Copy its email — it looks like `invoice-loader@invoice-pipeline-433xxx.iam.gserviceaccount.com`.

### 6. Give the service account access to your Drive folder
The service account is a separate identity, so it can't see your Drive until you share with it.
1. In Google Drive, create (or pick) the folder that will hold invoice files.
2. **Share** → paste the service account email → give it **Viewer** → Send.
3. Open the folder in a browser and copy its ID from the URL (the string after `folders/`) — this is your `DRIVE_FOLDER_ID`.

---

## Part 2 — Test it on your laptop first

Before automating, confirm it loads correctly by running once locally.

1. Install Python 3.11+ if you don't have it.
2. In a terminal:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a service-account key: Console → the `invoice-loader` account → **Keys → Add key → JSON**. Save the downloaded file as `key.json` next to `main.py`.
4. Set environment variables and run (macOS/Linux):
   ```bash
   export GCP_PROJECT="invoice-pipeline-433xxx"       # your Project ID
   export DRIVE_FOLDER_ID="your-drive-folder-id"
   export SERVICE_ACCOUNT_FILE="./key.json"
   python main.py
   ```
   On Windows PowerShell:
   ```powershell
   $env:GCP_PROJECT="invoice-pipeline-433xxx"
   $env:DRIVE_FOLDER_ID="your-drive-folder-id"
   $env:SERVICE_ACCOUNT_FILE=".\key.json"
   python main.py
   ```
5. You'll see lines like `LOAD  client-a-invoice.xlsx: 14832 rows`. Then in BigQuery:
   ```sql
   SELECT * FROM `invoice-pipeline-433xxx.invoices.invoice_line_items` LIMIT 100;
   ```

If a file has a summary/preamble above the real header row (like the UPS billing files do), set `HEADER_ANCHOR` to a value that's always in the first cell of the true header row, e.g.:
```bash
export HEADER_ANCHOR="Invoice Number"
```
Leave it unset if the header is simply row 1, which covers most straightforward exports.

This local path is also your **manual "just load it now" button** any time you don't want to wait for the schedule.

---

## Part 3 — Deploy as an automatic Cloud Function

Once the local test looks right, deploy so it runs unattended. Install the `gcloud` CLI (<https://cloud.google.com/sdk/docs/install>), then from the folder containing these files:

```bash
gcloud config set project invoice-pipeline-433xxx

gcloud functions deploy invoice-loader \
  --gen2 --runtime=python311 --region=us-central1 \
  --source=. --entry-point=run_load --trigger-http \
  --timeout=540s --memory=1Gi \
  --service-account=invoice-loader@invoice-pipeline-433xxx.iam.gserviceaccount.com \
  --set-env-vars=GCP_PROJECT=invoice-pipeline-433xxx,DRIVE_FOLDER_ID=your-drive-folder-id \
  --no-allow-unauthenticated
```

(No `SERVICE_ACCOUNT_FILE` here — running as the service account, the function authenticates automatically. Add `HEADER_ANCHOR=...` to `--set-env-vars` if you set one during testing.)

### Schedule it
Run every hour or two so invoices don't sit for long before showing up in BigQuery; adjust to taste.

```bash
FUNCTION_URL=$(gcloud functions describe invoice-loader --gen2 --region=us-central1 --format='value(serviceConfig.uri)')

gcloud scheduler jobs create http invoice-loader-hourly \
  --location=us-central1 --schedule="0 * * * *" \
  --uri="$FUNCTION_URL" --http-method=POST \
  --oidc-service-account-email=invoice-loader@invoice-pipeline-433xxx.iam.gserviceaccount.com
```

Done. Drop a new invoice file into the Drive folder (or let Google Drive for Desktop sync it there automatically) and it lands in BigQuery within the hour — or trigger immediately with `gcloud scheduler jobs run invoice-loader-hourly`.

---

## Operating notes

- **Re-uploaded a file?** Just re-save it in Drive. The loader sees the newer `modifiedTime` and reloads it. To avoid duplicate rows when reloading, delete that file's old rows first:
  ```sql
  DELETE FROM `invoice-pipeline-433xxx.invoices.invoice_line_items`
  WHERE _source_file_name = 'client-a-invoice.xlsx';
  ```
  (Each row also carries `_row_hash` if you prefer hash-based de-duplication.)
- **Check what loaded:** `SELECT * FROM \`...invoices._load_log\` ORDER BY loaded_at DESC;`
- **Different clients, different columns:** that's expected. New columns are added automatically on later loads rather than failing (rows from earlier loads just show NULL for columns that didn't exist yet in their file).
- **A column that should be numeric loaded as text:** that's the leading-zero safeguard kicking in — check whether any value in that column has a leading zero (e.g. a code like `"0123"`). If it's genuinely numeric and just happens to contain a coincidental leading zero, there's currently no per-column override; ask for one if you hit this.
- **Cost:** at typical invoice volumes, expect $0–a few dollars/month — well inside or just past the free tier.

## Verification checklist before trusting numbers

1. Row count per file in `_load_log` matches the number of detail rows in the sheet.
2. Spot-check a few rows: leading zeros in codes/tracking numbers preserved as text, dates parsed correctly, dollar amounts numeric.
3. If a file uses a different header layout than expected, check the Cloud Function logs (Console → Cloud Functions → invoice-loader → Logs) for a `FAIL` line with the error.

## When to use this vs. the UPS billing pipeline

This loader can handle the same UPS "CODED" billing files as the `Google Cloud Databse for Climb Accounting` pipeline elsewhere in this repo (set `HEADER_ANCHOR="WS Data Version"` and it'll skip the summary preamble the same way) — the leading-zero and junk-date protections above cover the same data-integrity risks that pipeline was built to guard against.

The one thing you lose by using this one for UPS billing specifically: the dedicated pipeline's reconciliation views (`v_period_totals`, `v_charge_mix`) and partitioned/clustered schema, which are wired to that exact column layout. If you rely on those views, keep using the dedicated pipeline for that client. Use this generic loader for every other client, or for UPS billing too if you'd rather run one pipeline than two and don't need those specific views.
