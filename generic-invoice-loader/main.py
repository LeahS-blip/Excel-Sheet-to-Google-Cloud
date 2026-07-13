"""
Generic invoice loader (Google Drive -> BigQuery)
==================================================

This is the same battle-tested pattern as the Wine Country Connect UPS billing
loader in this repo (Drive folder -> Cloud Function -> BigQuery, with a
_load_log table so re-runs never double-load a file) but with the column
mapping generalized: instead of a hard-coded 64-column schema for one specific
invoice format, this version reads whatever columns are in the header row and
lets BigQuery auto-detect the schema. Use this version when invoices come from
different clients/vendors and don't all share one fixed column layout --
including the UPS billing format, which this version can also handle.

Supported file types: .csv, .xlsx, and native Google Sheets (exported as CSV).

What it does, every time it runs:
  1. Lists matching spreadsheet files in one Google Drive folder.
  2. Skips any file already loaded (tracked in the `_load_log` table), unless
     the file was modified in Drive since the last successful load.
  3. Downloads each new/changed file, finds the header row (see HEADER
     detection below), and reads every data row underneath it.
  4. Cleans each column defensively (see DATA SAFETY below) and appends the
     rows to a BigQuery table -- schema is auto-detected from the columns
     present, so it adapts to each client's invoice layout.
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
name you know is always present, like "Invoice Number" or, for UPS billing
files, "WS Data Version"). Leave HEADER_ANCHOR blank to just use row 1 as
the header, which covers most straightforward exports.

--------------------------------------------------------------------------
Data safety (why this is safe for codes/tracking numbers and dates too)
--------------------------------------------------------------------------
Unlike a naive auto-convert-everything approach, this loader applies two
generic protections so it doesn't silently corrupt data:

  * Leading-zero codes (account numbers, tracking numbers, postal codes)
    are never auto-converted to numbers. Any column where at least one
    value looks like "0" followed by another digit (e.g. "00000123") is
    kept as text for every row, so leading zeros are never dropped.

  * Junk placeholder dates (Excel's classic "1/0/1900", "0/0/0000", or any
    parsed date with year <= 1900) are converted to NULL instead of being
    loaded as garbage text or a bogus date.

Columns that aren't caught by either rule are tried as numbers, then as
dates, and otherwise loaded as text.
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
FILE_NAME_REGEX = os.environ.get("FILE_NAME_REGEX", r"\.(csv|xlsx)$")
# First-cell value of the true header row, for files with a preamble above the
# header (e.g. "WS Data Version" for UPS billing files). Leave blank to just
# treat row 1 as the header.
HEADER_ANCHOR = os.environ.get("HEADER_ANCHOR", "")
# Optional path to a service-account key file. If unset, Application Default
# Credentials are used (the normal case inside Cloud Functions / Cloud Run).
SA_KEY_FILE = os.environ.get("SERVICE_ACCOUNT_FILE")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly",
          "https://www.googleapis.com/auth/bigquery"]

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MIME = "text/csv"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"

JUNK_DATE_STRINGS = {"1/0/1900", "0/0/0000", "1/1/1900", "12/30/1899", "1899-12-30", "0"}
# Broader pattern for junk dates with a literal zero day/month, e.g. "3/0/1900",
# "0/5/2026" -- catches variants that don't exactly match JUNK_DATE_STRINGS above.
_JUNK_DATE_RE = re.compile(r"^\d{1,2}/0/\d{2,4}$|^0/\d{1,2}/\d{2,4}$")


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
SPREADSHEET_MIME_TYPES = [CSV_MIME, XLSX_MIME, GOOGLE_SHEET_MIME]


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
            # Native Google Sheets don't have a file extension, so the name
            # regex doesn't apply to them -- always include those.
            if f["mimeType"] == GOOGLE_SHEET_MIME or pat is None or pat.search(f["name"]):
                files.append(f)
        token = resp.get("nextPageToken")
        if not token:
            break
    return files


def download_file(drive, file_meta):
    """Returns (buf, file_format) where file_format is 'csv' or 'xlsx'."""
    file_id = file_meta["id"]
    mime = file_meta["mimeType"]

    if mime == GOOGLE_SHEET_MIME:
        request = drive.files().export_media(fileId=file_id, mimeType=CSV_MIME)
        file_format = "csv"
    elif mime == XLSX_MIME:
        request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
        file_format = "xlsx"
    else:
        request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
        file_format = "csv"

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf, file_format


# --------------------------------------------------------------------------- #
# Parse one file into a DataFrame                                            #
# --------------------------------------------------------------------------- #
def _clean_column_name(name, seen):
    """BigQuery column names: letters, numbers, underscores, must start with a
    letter/underscore. Make each name safe and de-duplicate blanks/repeats."""
    name = "" if pd.isna(name) else str(name).strip()
    name = re.sub(r"[^0-9a-zA-Z_]", "_", name).strip("_") or "column"
    if name[0].isdigit():
        name = f"_{name}"
    base, i = name, 1
    while name.lower() in seen:
        i += 1
        name = f"{base}_{i}"
    seen.add(name.lower())
    return name


_LEADING_ZERO_RE = re.compile(r"^0\d")


def _has_leading_zero_code(series):
    """True if any value in this column looks like a code with a leading zero
    (e.g. '00000123', '0000009A516V216') -- these must never become numbers."""
    return series.dropna().astype(str).str.strip().str.match(_LEADING_ZERO_RE).any()


def _try_numeric(series):
    """Return a converted numeric series, or None if any non-null value fails
    to convert (so a column is only switched to numeric when it's safe)."""
    cleaned = series.astype(str).str.replace(",", "", regex=False).str.replace("$", "", regex=False).str.strip()
    converted = pd.to_numeric(cleaned, errors="coerce")
    if converted.notna().sum() == series.notna().sum() and series.notna().any():
        return converted
    return None


def _try_date(series):
    """Return a column of datetime.date objects (junk/placeholder dates ->
    None), or None if this doesn't look like a date column."""
    cleaned = series.astype(str).str.strip()
    junk_mask = cleaned.isin(JUNK_DATE_STRINGS) | cleaned.str.match(_JUNK_DATE_RE)
    parseable = cleaned.where(~junk_mask, None)
    parsed = pd.to_datetime(parseable, errors="coerce")
    non_null_original = series.notna().sum()
    if non_null_original == 0:
        return None
    # A junk placeholder is a *recognized* date-shaped value (just one we null
    # out on purpose), so it counts as a success -- otherwise files with a lot
    # of legitimate junk dates would never get detected as date columns at all.
    success_ratio = (parsed.notna().sum() + junk_mask.sum()) / non_null_original
    if success_ratio < 0.9:
        return None  # not consistently date-like -- leave it as text
    # Placeholder/junk years (e.g. Excel's 1899/1900 zero-date) -> NULL
    parsed = parsed.where(parsed.dt.year > 1900)
    return parsed.dt.date


def parse_file(buf, file_meta, file_format):
    file_name = file_meta["name"]

    if file_format == "xlsx":
        raw_df = pd.read_excel(buf, header=None, dtype=str, engine="openpyxl")
    else:
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

    # Normalize blank cells to real nulls before any type inference
    df = df.apply(lambda col: col.where(col.astype(str).str.strip() != "", None))

    for col in df.columns:
        if _has_leading_zero_code(df[col]):
            continue  # protected: always stays text
        numeric = _try_numeric(df[col])
        if numeric is not None:
            df[col] = numeric
            continue
        date_col = _try_date(df[col])
        if date_col is not None:
            df[col] = date_col
        # else: leave as cleaned text

    # Compute the content hash from the invoice data only, before adding any
    # metadata columns below -- _loaded_at changes on every run, so including
    # it (or anything added after this point) would make the hash different
    # every time the same file is loaded, defeating its use as a stable
    # dedup key.
    df["_row_hash"] = df.apply(
        lambda r: hashlib.sha256(
            (file_meta["id"] + "|" + "|".join(str(v) for v in r.values)).encode("utf-8")
        ).hexdigest(),
        axis=1,
    )

    loaded_at = dt.datetime.now(dt.timezone.utc).isoformat()
    df["_source_file_id"] = file_meta["id"]
    df["_source_file_name"] = file_name
    df["_source_row_number"] = range(1, len(df) + 1)
    df["_loaded_at"] = loaded_at
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
            buf, file_format = download_file(drive, f)
            df = parse_file(buf, f, file_format)
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
