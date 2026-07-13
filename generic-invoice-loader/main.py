"""
Generic invoice loader (Google Drive -> BigQuery)
==================================================

This is the same battle-tested pattern as the Wine Country Connect UPS billing
loader in this repo (Drive folder -> Cloud Function -> BigQuery, with a
_load_log table so re-runs never double-load a file) but with the column
mapping generalized: instead of a hard-coded 64-column schema for one specific
invoice format, this version reads whatever columns are in the header row and
lets BigQuery auto-detect the schema. Use this version when invoices come from
different clients/vendors and don't all share one fixed column layout.

Only .csv files (and native Google Sheets, exported as CSV) are picked up.

What it does, every time it runs:
  1. Lists the CSV files (and native Google Sheets, exported as CSV) in one Google Drive folder.
  2. Skips any file already loaded (tracked in the `_load_log` table), unless
     the file was modified in Drive since the last successful load.
  3. Downloads each new/changed file, finds the header row (see HEADER
     detection below), and reads every data row underneath it.
  4. Appends the rows to a BigQuery table named after the source file's
     spreadsheet (or a fixed table, if configured) -- schema is auto-detected
     from the columns present, so it adapts to each client's invoice layout.
  5. Records the result in `_load_log`.

Runs two ways:
  * Cloud Function / Cloud Run entry point:  run_load(request)
  * Locally / manually:                       python main.py

Configuration is via environment variables (see CONFIG below).

--------------------------------------------------------------------------
Header detection
--------------------------------------------------------------------------
Most invoice exports either start the header on row 1, or have a short
summary/preamble above the real header row. Set HEADER_ANCHOR to a value
that always appears in the first cell of the true header row (e.g. a column
name you know is always present, like "Invoice Number" or "Tracking Number").
Leave HEADER_ANCHOR blank to just use row 1 as the header, which covers most
straightforward exports.
--------------------------------------------------------------------------
"""

import datetime as dt
import hashlib
import io
import os
import re

import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# --------------------------------------------------------------------------- #
# CONFIG (set these as environment variables)                                 #
# --------------------------------------------------------------------------- #
PROJECT_ID    = os.environ.get("GCP_PROJECT", "YOUR_PROJECT")
DATASET       = os.environ.get("BQ_DATASET", "invoices")
TABLE         = os.environ.get("BQ_TABLE", "invoice_line_items")
LOG_TABLE     = os.environ.get("BQ_LOG_TABLE", "_load_log")
DRIVE_FOLDER  = os.environ.get("DRIVE_FOLDER_ID", "")
# Only load files whose name matches this (case-insensitive regex). Empty = all.
FILE_NAME_REGEX = os.environ.get("FILE_NAME_REGEX", r"\.csv$")
# First-cell value of the true header row, for files with a preamble above the
# header. Leave blank to just treat row 1 as the header.
HEADER_ANCHOR = os.environ.get("HEADER_ANCHOR", "")
# Optional path to a service-account key file. If unset, Application Default
# Credentials are used (the normal case inside Cloud Functions / Cloud Run).
SA_KEY_FILE = os.environ.get("SERVICE_ACCOUNT_FILE")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly",
          "https://www.googleapis.com/auth/bigquery"]


# --------------------------------------------------------------------------- #
# Auth                                                                        #
# --------------------------------------------------------------------------- #
def _credentials():
    if SA_KEY_FILE:
        return service_account.Credentials.from_service_account_file(
            SA_KEY_FILE, scopes=SCOPES)
    return None  # use Application Default Credentials


# --------------------------------------------------------------------------- #
# Drive                                                                       #
# --------------------------------------------------------------------------- #
SPREADSHEET_MIME_TYPES = [
    "text/csv",
    "application/vnd.google-apps.spreadsheet",  # native Google Sheet, exported below as CSV
]


def list_drive_files(drive):
    mime_clause = " or ".join(f"mimeType = '{m}'" for m in SPREADSHEET_MIME_TYPES)
    q = f"'{DRIVE_FOLDER}' in parents and trashed = false and ({mime_clause})"
    files, token = [], None
    pat = re.compile(FILE_NAME_REGEX, re.IGNORECASE) if FILE_NAME_REGEX else None
    while True:
        resp = drive.files().list(
            q=q, fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
            pageSize=200, pageToken=token,
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        for f in resp.get("files", []):
            if pat is None or pat.search(f["name"]):
                files.append(f)
        token = resp.get("nextPageToken")
        if not token:
            break
    return files


def download_file(drive, file_meta):
    file_id = file_meta["id"]
    if file_meta["mimeType"] == "application/vnd.google-apps.spreadsheet":
        # Native Google Sheet -> export as CSV
        request = drive.files().export_media(fileId=file_id, mimeType="text/csv")
    else:
        request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf


# --------------------------------------------------------------------------- #
# Parse one file into a DataFrame                                            #
# --------------------------------------------------------------------------- #
def _clean_column_name(name, seen):
    """BigQuery column names: letters, numbers, underscores, must start with a
    letter/underscore. Make each name safe and de-duplicate blanks/repeats."""
    name = str(name).strip() if name is not None else ""
    name = re.sub(r"[^0-9a-zA-Z_]", "_", name).strip("_") or "column"
    if name[0].isdigit():
        name = f"_{name}"
    base, i = name, 1
    while name.lower() in seen:
        i += 1
        name = f"{base}_{i}"
    seen.add(name.lower())
    return name


def parse_file(buf, file_meta):
    file_name = file_meta["name"]
    raw_df = pd.read_csv(buf, header=None, dtype=str)

    header_row_idx = 0
    if HEADER_ANCHOR:
        first_col = raw_df.iloc[:, 0].astype(str).str.strip()
        matches = first_col[first_col == HEADER_ANCHOR]
        if matches.empty:
            raise ValueError(
                f"HEADER_ANCHOR '{HEADER_ANCHOR}' not found in first column of {file_name}"
            )
        header_row_idx = matches.index[0]

    header_values = raw_df.iloc[header_row_idx].tolist()
    seen = set()
    columns = [_clean_column_name(v, seen) for v in header_values]

    df = raw_df.iloc[header_row_idx + 1:].copy()
    df.columns = columns
    df = df.dropna(how="all")  # drop fully blank rows
    # Drop rows that just repeat the header (some exports repeat it periodically)
    if HEADER_ANCHOR and columns:
        df = df[df[columns[0]].astype(str).str.strip() != HEADER_ANCHOR]

    # Best-effort numeric conversion: try each column as numeric, keep as text if it fails
    for col in df.columns:
        converted = pd.to_numeric(
            df[col].astype(str).str.replace(",", "").str.replace("$", "", regex=False),
            errors="coerce",
        )
        # Only switch to numeric if every non-null cell converted successfully
        if converted.notna().sum() == df[col].notna().sum() and df[col].notna().any():
            df[col] = converted

    loaded_at = dt.datetime.now(dt.timezone.utc).isoformat()
    df["_source_file_id"] = file_meta["id"]
    df["_source_file_name"] = file_name
    df["_source_row_number"] = range(1, len(df) + 1)
    df["_loaded_at"] = loaded_at
    df["_row_hash"] = df.apply(
        lambda r: hashlib.sha256(
            (file_meta["id"] + "|" + "|".join(str(v) for v in r.values)).encode("utf-8")
        ).hexdigest(),
        axis=1,
    )
    return df


# --------------------------------------------------------------------------- #
# BigQuery                                                                    #
# --------------------------------------------------------------------------- #
def _to_dt(value):
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def already_loaded(bq):
    """Return {file_id: latest_source_modified_datetime} for successful past loads."""
    table = f"{PROJECT_ID}.{DATASET}.{LOG_TABLE}"
    try:
        q = (f"SELECT source_file_id, MAX(source_modified_at) AS m "
             f"FROM `{table}` WHERE status = 'success' GROUP BY source_file_id")
        return {r["source_file_id"]: _to_dt(r["m"]) for r in bq.query(q).result()}
    except Exception:
        return {}  # log table not created yet


def append_rows(bq, df):
    table_id = f"{PROJECT_ID}.{DATASET}.{TABLE}"
    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_APPEND",
        autodetect=True,
        schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
    )
    job = bq.load_table_from_dataframe(df, table_id, job_config=job_config)
    job.result()
    return len(df)


def log_load(bq, file_meta, rows_loaded, status):
    table = f"{PROJECT_ID}.{DATASET}.{LOG_TABLE}"
    bq.load_table_from_json(
        [{
            "source_file_id":     file_meta["id"],
            "source_file_name":   file_meta["name"],
            "rows_loaded":        rows_loaded,
            "source_modified_at": file_meta["modifiedTime"],
            "loaded_at":          dt.datetime.now(dt.timezone.utc).isoformat(),
            "status":             status,
        }],
        table,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"),
    ).result()


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #
def load_new_files():
    creds = _credentials()
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    bq = bigquery.Client(project=PROJECT_ID, credentials=creds)

    done = already_loaded(bq)
    files = list_drive_files(drive)
    summary = []

    for f in files:
        prior_mod = done.get(f["id"])
        drive_mod = _to_dt(f["modifiedTime"])
        if prior_mod is not None and drive_mod is not None and prior_mod >= drive_mod:
            summary.append(f"SKIP  {f['name']} (already loaded)")
            continue
        try:
            buf = download_file(drive, f)
            df = parse_file(buf, f)
            n = append_rows(bq, df) if len(df) else 0
            log_load(bq, f, n, "success")
            summary.append(f"LOAD  {f['name']}: {n} rows")
        except Exception as e:  # noqa: BLE001 - log and continue with other files
            log_load(bq, f, 0, "failed")
            summary.append(f"FAIL  {f['name']}: {e}")

    msg = "\n".join(summary) if summary else "No files found."
    print(msg)
    return msg


# Cloud Function / Cloud Run HTTP entry point
def run_load(request=None):
    return load_new_files(), 200


if __name__ == "__main__":
    load_new_files()
