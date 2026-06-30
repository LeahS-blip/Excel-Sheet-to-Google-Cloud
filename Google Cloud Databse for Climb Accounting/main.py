"""
Wine Country Connect — UPS billing loader (Google Drive -> BigQuery)
====================================================================

What it does, every time it runs:
  1. Lists the *.xlsx files in one Google Drive folder.
  2. Skips any file already loaded (tracked in the `_load_log` table), unless the
     file was re-modified in Drive since the last load (e.g. you re-coded it).
  3. Downloads each new/changed file, finds the real "WS Data Version" header row
     (skipping the invoice summary preamble at the top), and reads every charge line.
  4. Cleans + types each value (dates, money, leading-zero codes) and appends the
     rows to `ups_charge_lines` in BigQuery.
  5. Records the result in `_load_log`.

Runs two ways:
  * Cloud Function / Cloud Run entry point:  run_load(request)
  * Locally / manually:                       python main.py

Configuration is via environment variables (see CONFIG below).
"""

import datetime as dt
import hashlib
import io
import os
import re

from google.cloud import bigquery
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import openpyxl

# --------------------------------------------------------------------------- #
# CONFIG (set these as environment variables)                                 #
# --------------------------------------------------------------------------- #
PROJECT_ID   = os.environ.get("GCP_PROJECT", "YOUR_PROJECT")
DATASET      = os.environ.get("BQ_DATASET", "wcc_billing")
TABLE        = os.environ.get("BQ_TABLE", "ups_charge_lines")
LOG_TABLE    = os.environ.get("BQ_LOG_TABLE", "_load_log")
DRIVE_FOLDER = os.environ.get("DRIVE_FOLDER_ID", "1CUUnqHeKwTtZyUrfey-M1jv-RGf8D--3")
# Only load files whose name matches this (case-insensitive). Keeps stray files out.
FILE_NAME_REGEX = os.environ.get("FILE_NAME_REGEX", r"WS Ground BIlling CODED.*\.xlsx$")
# Optional path to a service-account key file. If unset, Application Default
# Credentials are used (the normal case inside Cloud Functions / Cloud Run).
SA_KEY_FILE = os.environ.get("SERVICE_ACCOUNT_FILE")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly",
          "https://www.googleapis.com/auth/bigquery"]

HEADER_KEY = "WS Data Version"  # first cell of the real table header row

# Ordered (source_header, bq_field, kind). kind in {str, int, num, date}.
# This is the single source of truth for column mapping; schema.sql matches it.
COLUMNS = [
    ("WS Data Version",               "ws_data_version",              "str"),
    ("Recipient Number",              "recipient_number",             "str"),
    ("Account Number",                "account_number",               "str"),
    ("Account Country",               "account_country",              "str"),
    ("Invoice Date",                  "invoice_date",                 "date"),
    ("Invoice Number",                "invoice_number",               "str"),
    ("Invoice Amount",                "invoice_amount",               "num"),
    ("Invoice Currency Code",         "invoice_currency_code",        "str"),
    ("Transaction Date",              "transaction_date",             "date"),
    ("Lead Shipment Number",          "lead_shipment_number",         "str"),
    ("Shipment Reference Number 1",   "shipment_reference_number_1",  "str"),
    ("Shipment Reference Number 2",   "shipment_reference_number_2",  "str"),
    ("Bill Option Code",              "bill_option_code",             "str"),
    ("Package Quantity",              "package_quantity",             "int"),
    ("Oversize Quantity",             "oversize_quantity",            "int"),
    ("Tracking Number",               "tracking_number",              "str"),
    ("Billed Weight",                 "billed_weight",                "num"),
    ("Billed Weight Unit of Measure", "billed_weight_uom",            "str"),
    ("Container Type",                "container_type",               "str"),
    ("Billed Weight Type",            "billed_weight_type",           "str"),
    ("Package Dimensions",            "package_dimensions",           "str"),
    ("Zone",                          "zone",                         "str"),
    ("Net Amount",                    "net_amount",                   "num"),
    ("Charge Description",            "charge_description",           "str"),
    ("Charge Description Code",       "charge_description_code",      "str"),
    ("Charge Classification Code",    "charge_classification_code",   "str"),
    ("Package Reference Number 1",    "package_reference_number_1",   "str"),
    ("Package Reference Number 2",    "package_reference_number_2",   "str"),
    ("Package Reference Number 3",    "package_reference_number_3",   "str"),
    ("Package Reference Number 4",    "package_reference_number_4",   "str"),
    ("Package Reference Number 5",    "package_reference_number_5",   "str"),
    ("Entered Weight",                "entered_weight",               "num"),
    ("Entered Weight Unit of Measure","entered_weight_uom",           "str"),
    ("Transaction Currency Code",     "transaction_currency_code",    "str"),
    ("Tax Indicator",                 "tax_indicator",                "str"),
    ("Basis Value",                   "basis_value",                  "num"),
    ("Basis Currency Code",           "basis_currency_code",          "str"),
    ("Charged Unit Quantity",         "charged_unit_quantity",        "num"),
    ("Charge Category Code",          "charge_category_code",         "str"),
    ("Charge Category Detail Code",   "charge_category_detail_code",  "str"),
    ("Charge Source",                 "charge_source",                "str"),
    ("Type Code 1",                   "type_code_1",                  "str"),
    ("Type Detail Code 1",            "type_detail_code_1",           "str"),
    ("Type Detail Value 1",           "type_detail_value_1",          "str"),
    ("Customer Reference Number",     "customer_reference_number",    "str"),
    ("Sender Name",                   "sender_name",                  "str"),
    ("Sender Company Name",           "sender_company_name",          "str"),
    ("Sender Address Line 1",         "sender_address_line_1",        "str"),
    ("Sender Address Line 2",         "sender_address_line_2",        "str"),
    ("Sender City",                   "sender_city",                  "str"),
    ("Sender State",                  "sender_state",                 "str"),
    ("Sender Postal",                 "sender_postal",                "str"),
    ("Sender Country",                "sender_country",               "str"),
    ("Receiver Name",                 "receiver_name",                "str"),
    ("Receiver Company Name",         "receiver_company_name",        "str"),
    ("Receiver Address Line 1",       "receiver_address_line_1",      "str"),
    ("Receiver Address Line 2",       "receiver_address_line_2",      "str"),
    ("Receiver City",                 "receiver_city",                "str"),
    ("Receiver State",                "receiver_state",               "str"),
    ("Receiver Postal",               "receiver_postal",              "str"),
    ("Receiver Country",              "receiver_country",             "str"),
    ("Corrected Zone",                "corrected_zone",               "str"),
    ("ActivityPeriod",                "activity_period",              "str"),
    ("InvoicePeriod",                 "invoice_period",               "str"),
]

# --------------------------------------------------------------------------- #
# Value cleaning                                                              #
# --------------------------------------------------------------------------- #
def _credentials():
    if SA_KEY_FILE:
        return service_account.Credentials.from_service_account_file(
            SA_KEY_FILE, scopes=SCOPES)
    return None  # use Application Default Credentials


def clean_str(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        # openpyxl reads numeric-looking text as float; "8.0" -> "8"
        v = int(v)
    s = str(v).strip()
    return s if s != "" else None


def clean_num(v):
    s = clean_str(v)
    if s is None:
        return None
    s = s.replace("$", "").replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def clean_int(v):
    n = clean_num(v)
    return int(n) if n is not None else None


def clean_date(v):
    """Return 'YYYY-MM-DD' string or None. Handles datetime cells, M/D/YYYY text,
    and treats the classic '1/0/1900' Excel zero-date and 0 as NULL."""
    if v is None:
        return None
    if isinstance(v, (dt.datetime, dt.date)):
        if v.year <= 1900:
            return None
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    if s in ("", "0", "1/0/1900", "0/0/0000"):
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(s, fmt)
            return None if d.year <= 1900 else d.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


_CLEANERS = {"str": clean_str, "int": clean_int, "num": clean_num, "date": clean_date}


def period_from_name(name):
    """'2026-05 B WS Ground BIlling CODED.xlsx' -> '2026-05-B'."""
    m = re.match(r"\s*(\d{4})-(\d{2})\s+([AB])", name)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


# --------------------------------------------------------------------------- #
# Drive                                                                       #
# --------------------------------------------------------------------------- #
def list_drive_files(drive):
    q = (f"'{DRIVE_FOLDER}' in parents and trashed = false "
         "and mimeType = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'")
    files, token = [], None
    pat = re.compile(FILE_NAME_REGEX, re.IGNORECASE)
    while True:
        resp = drive.files().list(
            q=q, fields="nextPageToken, files(id, name, modifiedTime)",
            pageSize=200, pageToken=token,
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        for f in resp.get("files", []):
            if pat.search(f["name"]):
                files.append(f)
        token = resp.get("nextPageToken")
        if not token:
            break
    return files


def download_file(drive, file_id):
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(
        buf, drive.files().get_media(fileId=file_id, supportsAllDrives=True))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf


# --------------------------------------------------------------------------- #
# Parse one workbook                                                          #
# --------------------------------------------------------------------------- #
def parse_workbook(buf, file_id, file_name):
    wb = openpyxl.load_workbook(buf, read_only=True, data_only=True)
    ws = wb.active

    rows_iter = ws.iter_rows(values_only=True)

    # 1) Find the header row (skip the invoice summary preamble at the top).
    header = None
    for raw in rows_iter:
        if raw and clean_str(raw[0]) == HEADER_KEY:
            header = [clean_str(c) for c in raw]
            break
    if header is None:
        raise ValueError(f"Header row '{HEADER_KEY}' not found in {file_name}")

    # 2) Map source-header -> column index (by name, robust to column reordering).
    idx = {}
    for src_header, _, _ in COLUMNS:
        try:
            idx[src_header] = header.index(src_header)
        except ValueError:
            idx[src_header] = None  # column missing in this file -> NULLs

    period = period_from_name(file_name)
    loaded_at = dt.datetime.now(dt.timezone.utc).isoformat()

    out = []
    src_row_no = 0
    for raw in rows_iter:
        src_row_no += 1
        if raw is None or all(c is None or str(c).strip() == "" for c in raw):
            continue  # blank line
        # A row is real data only if the first column repeats the WS Data Version
        first = clean_str(raw[0]) if len(raw) else None
        if first is None or first == HEADER_KEY:
            continue

        rec = {}
        for src_header, bq_field, kind in COLUMNS:
            j = idx[src_header]
            val = raw[j] if (j is not None and j < len(raw)) else None
            rec[bq_field] = _CLEANERS[kind](val)

        rec["_source_file_id"]    = file_id
        rec["_source_file_name"]  = file_name
        rec["_period_label"]      = period
        rec["_source_row_number"] = src_row_no
        rec["_loaded_at"]         = loaded_at
        rec["_row_hash"] = hashlib.sha256(
            (file_id + "|" + "|".join(
                "" if rec[b] is None else str(rec[b])
                for _, b, _ in COLUMNS)).encode("utf-8")
        ).hexdigest()
        out.append(rec)

    wb.close()
    return out


# --------------------------------------------------------------------------- #
# BigQuery                                                                    #
# --------------------------------------------------------------------------- #
def _to_dt(value):
    """Parse a Drive RFC3339 string or a BigQuery datetime into an aware datetime."""
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


def append_rows(bq, rows):
    table = f"{PROJECT_ID}.{DATASET}.{TABLE}"
    job = bq.load_table_from_json(
        rows, table,
        job_config=bigquery.LoadJobConfig(
            write_disposition="WRITE_APPEND",
            schema_update_options=[],
        ))
    job.result()  # wait, raises on error
    return len(rows)


def log_load(bq, file_meta, rows_loaded, status):
    table = f"{PROJECT_ID}.{DATASET}.{LOG_TABLE}"
    bq.load_table_from_json(
        [{
            "source_file_id":     file_meta["id"],
            "source_file_name":   file_meta["name"],
            "period_label":       period_from_name(file_meta["name"]),
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
            buf = download_file(drive, f["id"])
            rows = parse_workbook(buf, f["id"], f["name"])
            n = append_rows(bq, rows) if rows else 0
            log_load(bq, f, n, "success")
            summary.append(f"LOAD  {f['name']}: {n} rows")
        except Exception as e:  # noqa: BLE001 - we want to log and continue
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
