"""
Local Invoice Loader (Windows folder -> Google Cloud Storage + BigQuery)
=========================================================================

Same header-detection and data-safety logic as generic-invoice-loader (the
Google Drive-based version), but for when you'd rather point this at an
ordinary Windows folder you can see in File Explorer instead of setting up
a Google Drive folder and sharing it with a service account.

Trade-off vs. generic-invoice-loader: this has to run ON a computer that's
turned on, via Windows Task Scheduler (see README.md) -- it can't run as a
serverless Cloud Function, since Cloud Functions have no access to a local
filesystem. If "nobody needs to keep a computer on" matters more than
"no Google Drive setup," use generic-invoice-loader instead.

What it does, every time it runs:
  1. Scans one local folder for .csv/.xlsx files (optionally filtered by a
     keyword in the filename, e.g. "invoice").
  2. Skips any file it's already processed (tracked by content hash in
     uploaded_files_state.json), so re-running never double-loads.
  3. Uploads a permanent backup copy of each new file to Google Cloud
     Storage (optional -- leave gcs_bucket_name blank in config.json to
     skip this and load straight to BigQuery only).
  4. Parses the file: finds the header row (skipping any summary preamble
     above it, if configured), cleans money/date columns, protects
     leading-zero codes (account/tracking numbers, zip codes) from being
     turned into numbers, and nulls out junk placeholder dates instead of
     loading them as garbage -- see DATA SAFETY notes below.
  5. Appends the cleaned rows to a BigQuery table (schema auto-detected).
  6. Records the file's hash + result in a local state file so it's never
     re-loaded.

Run this periodically via Windows Task Scheduler (or cron on Mac/Linux) --
see README.md for exact steps.

--------------------------------------------------------------------------
Data safety (same protections as generic-invoice-loader)
--------------------------------------------------------------------------
  * Leading-zero codes (account numbers, tracking numbers, postal codes)
    are never auto-converted to numbers -- a column stays text if any
    value looks like "0" followed by another digit (e.g. "00000123").

  * Junk placeholder dates (Excel's classic "1/0/1900", "0/0/0000", or
    anything that parses to year <= 1900) are converted to NULL instead
    of being loaded as garbage text or a bogus date.
--------------------------------------------------------------------------
"""

import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path

import pandas as pd
from google.cloud import bigquery
from google.cloud import storage

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
STATE_PATH = SCRIPT_DIR / "uploaded_files_state.json"
LOG_PATH = SCRIPT_DIR / "local_invoice_loader.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("local_invoice_loader")

SUPPORTED_EXTENSIONS = {".csv", ".xlsx"}

JUNK_DATE_STRINGS = {"1/0/1900", "0/0/0000", "1/1/1900", "12/30/1899", "1899-12-30", "0"}
_JUNK_DATE_RE = re.compile(r"^\d{1,2}/0/\d{2,4}$|^0/\d{1,2}/\d{2,4}$")
_LEADING_ZERO_RE = re.compile(r"^0\d")


# --------------------------------------------------------------------------- #
# Config / state                                                              #
# --------------------------------------------------------------------------- #
def load_config():
    if not CONFIG_PATH.exists():
        log.error(
            "config.json not found. Copy config.example.json to config.json "
            "and fill in your values before running this script."
        )
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"processed": {}}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Data safety helpers (identical logic to generic-invoice-loader)             #
# --------------------------------------------------------------------------- #
def _clean_column_name(name, seen):
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


def _has_leading_zero_code(series):
    return series.dropna().astype(str).str.strip().str.match(_LEADING_ZERO_RE).any()


def _try_numeric(series):
    cleaned = series.astype(str).str.replace(",", "", regex=False).str.replace("$", "", regex=False).str.strip()
    converted = pd.to_numeric(cleaned, errors="coerce")
    if converted.notna().sum() == series.notna().sum() and series.notna().any():
        return converted
    return None


def _try_date(series):
    cleaned = series.astype(str).str.strip()
    junk_mask = cleaned.isin(JUNK_DATE_STRINGS) | cleaned.str.match(_JUNK_DATE_RE)
    parseable = cleaned.where(~junk_mask, None)
    parsed = pd.to_datetime(parseable, errors="coerce")
    non_null_original = series.notna().sum()
    if non_null_original == 0:
        return None
    success_ratio = (parsed.notna().sum() + junk_mask.sum()) / non_null_original
    if success_ratio < 0.9:
        return None
    parsed = parsed.where(parsed.dt.year > 1900)
    return parsed.dt.date


def parse_file(path, header_anchor):
    file_name = path.name
    if path.suffix.lower() == ".xlsx":
        raw_df = pd.read_excel(path, header=None, dtype=str, engine="openpyxl")
    else:
        raw_df = pd.read_csv(path, header=None, dtype=str)

    header_row_idx = 0
    if header_anchor:
        first_col = raw_df.iloc[:, 0].astype(str).str.strip()
        matches = first_col[first_col == header_anchor]
        if matches.empty:
            raise ValueError(
                f"header_anchor '{header_anchor}' not found in first column of {file_name}"
            )
        header_row_idx = matches.index[0]

    header_values = raw_df.iloc[header_row_idx].tolist()
    seen = set()
    columns = [_clean_column_name(v, seen) for v in header_values]

    df = raw_df.iloc[header_row_idx + 1:].copy()
    df.columns = columns
    df = df.dropna(how="all")
    if header_anchor and columns:
        df = df[df[columns[0]].astype(str).str.strip() != header_anchor]

    df = df.apply(lambda col: col.where(col.astype(str).str.strip() != "", None))

    for col in df.columns:
        if _has_leading_zero_code(df[col]):
            continue
        numeric = _try_numeric(df[col])
        if numeric is not None:
            df[col] = numeric
            continue
        date_col = _try_date(df[col])
        if date_col is not None:
            df[col] = date_col

    df["_row_hash"] = df.apply(
        lambda r: hashlib.sha256(("|".join(str(v) for v in r.values)).encode("utf-8")).hexdigest(),
        axis=1,
    )
    df["_source_file_name"] = file_name
    df["_source_row_number"] = range(1, len(df) + 1)
    df["_loaded_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return df


# --------------------------------------------------------------------------- #
# GCS backup (optional)                                                       #
# --------------------------------------------------------------------------- #
def upload_backup(config, path):
    bucket_name = config.get("gcs_bucket_name", "").strip()
    if not bucket_name:
        return None
    client = storage.Client(project=config["gcp_project_id"])
    bucket = client.bucket(bucket_name)
    prefix = config.get("gcs_prefix", "invoices").strip("/")
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y/%m/%d")
    blob_path = f"{prefix}/{today}/{path.name}"
    blob = bucket.blob(blob_path)
    blob.upload_from_filename(str(path))
    return f"gs://{bucket_name}/{blob_path}"


# --------------------------------------------------------------------------- #
# BigQuery                                                                    #
# --------------------------------------------------------------------------- #
def append_rows(config, df):
    bq_cfg = config["bigquery"]
    client = bigquery.Client(project=config["gcp_project_id"])
    table_id = f"{config['gcp_project_id']}.{bq_cfg['dataset']}.{bq_cfg['table']}"
    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_APPEND",
        autodetect=True,
        schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
    )
    job = client.load_table_from_dataframe(df, table_id, job_config=job_config)
    job.result()
    return len(df)


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #
def find_candidates(config):
    watch_dir = Path(config["local_watch_folder"]).expanduser()
    if not watch_dir.exists():
        log.warning("Watched folder does not exist: %s", watch_dir)
        return []
    keyword = config.get("filename_keyword_filter", "").lower().strip()
    candidates = []
    for path in watch_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if keyword and keyword not in path.name.lower():
            continue
        candidates.append(path)
    return candidates


def main():
    config = load_config()
    state = load_state()
    header_anchor = config.get("header_anchor", "")

    log.info("Scanning %s ...", config["local_watch_folder"])
    candidates = find_candidates(config)
    log.info("Found %d candidate file(s).", len(candidates))

    for path in candidates:
        try:
            h = file_hash(path)
        except OSError as e:
            log.warning("Could not read %s (%s) - skipping, will retry next run.", path.name, e)
            continue
        if h in state["processed"]:
            continue

        log.info("Processing new file: %s", path.name)
        try:
            gcs_uri = upload_backup(config, path)
            df = parse_file(path, header_anchor)
            n = append_rows(config, df) if len(df) else 0
            state["processed"][h] = {
                "filename": path.name,
                "gcs_uri": gcs_uri,
                "rows_loaded": n,
                "processed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            save_state(state)
            log.info("LOAD  %s: %d rows", path.name, n)
        except Exception:
            log.exception("Failed to process %s - will retry next run.", path.name)

    log.info("Scan complete.")


if __name__ == "__main__":
    main()
