# Local Invoice Loader — Windows Folder → Google Cloud Storage + BigQuery

Loads long invoices (15,000+ rows, any client's format — including the UPS billing format) from a plain Windows folder into BigQuery automatically, on a schedule. No Google Drive setup needed — just point this at a folder you can see in File Explorer.

Picks up `.csv` and `.xlsx` files.

**Trade-off vs. `generic-invoice-loader`:** that version runs as a serverless Cloud Function and needs no computer to stay on, but requires setting up a Google Drive folder and sharing it with a service account. This version skips Google Drive entirely, but has to run *on* a computer that's turned on, via Windows Task Scheduler. Pick whichever setup is easier for your situation.

**Data safety.** Same protections as `generic-invoice-loader`:

- **Leading-zero codes** (account numbers, tracking numbers, postal codes like `"00000088BH"` or `"00501"`) are never auto-converted to numbers.
- **Junk placeholder dates** (Excel's classic `1/0/1900`, `0/0/0000`, or anything that parses to year ≤ 1900) are loaded as `NULL` instead of garbage text.

## How it works

```
Windows folder (File Explorer)     Task Scheduler (e.g. every 15 min)
  client-a-invoice.csv                    │ triggers
  client-b-invoice.xlsx                   ▼
  client-c-invoice.csv      ──▶   main.py
        ...                          │  • scan the folder
                                     │  • skip files already processed
                                     │    (uploaded_files_state.json)
                                     │  • back up raw file to GCS (optional)
                                     │  • parse + clean, find header row
                                     ▼
                              BigQuery: invoices.invoice_line_items
                                     │
                                     ▼
                       Query / Looker Studio dashboards
```

Every processed file is fingerprinted (content hash) and recorded locally in `uploaded_files_state.json`, so re-running the script — or the same file appearing twice — never double-loads it.

## Files in this package

| File | What it is |
|------|-----------|
| `main.py` | The loader. Run manually or on a schedule. |
| `config.example.json` | Copy to `config.json` and fill in your values. |
| `requirements.txt` | Python dependencies. |
| `README.md` | This guide. |

---

## Part 1 — One-time Google Cloud setup

Budget ~20 minutes. No Google Drive folder needed for this version.

### 1. Create a project
1. Go to <https://console.cloud.google.com> and sign in.
2. Top bar → project dropdown → **New Project**. Name it e.g. `invoice-pipeline`. Note the **Project ID**.

### 2. Enable billing
BigQuery (and optionally Cloud Storage) require a billing account, but at typical invoice volumes you'll stay within the free tier.
- Console → **Billing** → link a billing account to the project.

### 3. Turn on the APIs
- Console → **APIs & Services → Enable APIs** → enable **BigQuery API** (and **Cloud Storage API** if you'll use the optional backup).

### 4. Create the BigQuery dataset
1. Console → **BigQuery**.
2. Run this once (replace `YOUR_PROJECT`):
   ```sql
   CREATE SCHEMA IF NOT EXISTS `YOUR_PROJECT.invoices` OPTIONS (location = 'US');
   ```
3. The `invoice_line_items` table itself is created automatically the first time a file loads — no need to create it by hand.

### 5. (Optional) Create a Cloud Storage bucket for backups
Skip this if you only want data in BigQuery, not a raw-file backup.
1. Console → **Cloud Storage → Buckets → Create**.
2. Give it a globally unique name, e.g. `yourname-invoice-backup`.
3. Put that name in `config.json` as `gcs_bucket_name`.

### 6. Create a service account
1. Console → **IAM & Admin → Service Accounts → Create service account**. Name it `invoice-loader`.
2. Grant it: **BigQuery Data Editor**, **BigQuery Job User**, and (if using backups) **Storage Object Admin**.
3. Open it → **Keys → Add key → Create new key → JSON**. Save the downloaded file as `key.json` next to `main.py`. Keep this file private.

---

## Part 2 — Configure and test

1. Install Python 3.9+ if you don't have it.
2. Put `main.py`, `config.example.json`, and `requirements.txt` in one folder, e.g. `C:\Invoicing\loader\`.
3. Open a terminal there and run:
   ```
   pip install -r requirements.txt
   ```
4. Copy `config.example.json` to `config.json` and fill in:
   - `gcp_project_id` — your Project ID
   - `local_watch_folder` — the Windows folder to watch, e.g. `C:/Users/YourName/Invoices` (this is the folder your client would just drop files into via normal File Explorer)
   - `filename_keyword_filter` — optional, e.g. `"invoice"` to only pick up matching filenames
   - `header_anchor` — only needed if a file has a summary preamble above the real header row (e.g. `"WS Data Version"` for UPS billing files); leave blank if the header is row 1
   - `gcs_bucket_name` — optional, leave blank to skip the backup copy and load straight to BigQuery
5. Point the script at your credentials:
   ```
   setx GOOGLE_APPLICATION_CREDENTIALS "C:\Invoicing\loader\key.json"
   ```
   (Close and reopen the terminal after running `setx`.)
6. Drop a real invoice file into the watched folder, then run:
   ```
   python main.py
   ```
7. Check `local_invoice_loader.log` and the terminal output. Then check BigQuery:
   ```sql
   SELECT * FROM `YOUR_PROJECT.invoices.invoice_line_items` LIMIT 100;
   ```

---

## Part 3 — Make it run automatically

### Windows Task Scheduler
1. Open Task Scheduler → **Create Task**.
2. General tab: name it "Invoice Loader," check **Run whether user is logged on or not**.
3. Triggers tab: New → Daily, recurring, then check **Repeat task every 15 minutes for a duration of 1 day**.
4. Actions tab: New → Start a program. Program: path to `python.exe`. Arguments: full path to `main.py`. Start in: the script's folder.
5. Save. It'll now check the folder every 15 minutes, even if nobody's watching.

### Mac/Linux (cron)
```bash
*/15 * * * * cd /path/to/loader && /usr/bin/python3 main.py
```

---

## Operating notes

- **Nothing runs if the computer is off or asleep.** This is the main limitation vs. the Drive-based version — Task Scheduler only fires while the machine is on.
- **Re-uploaded a file?** If the file's content actually changed, its hash changes too, so it gets reprocessed as new. If it's byte-for-byte identical, it's skipped — that's the point of the dedup.
- **Different clients, different columns:** expected. New columns are added automatically on later loads rather than failing.
- **A column that should be numeric loaded as text:** the leading-zero safeguard — check whether any value in that column starts with a zero followed by another digit.
- **Check what's been processed:** open `uploaded_files_state.json` next to the script.

## Verification checklist before trusting numbers

1. Row count in BigQuery for a file matches the number of detail rows in the sheet.
2. Spot-check leading zeros in codes/tracking numbers are preserved, dates parsed correctly, dollar amounts numeric.
3. If a file fails, check `local_invoice_loader.log` for the error — it'll retry that file on the next run without you doing anything.
