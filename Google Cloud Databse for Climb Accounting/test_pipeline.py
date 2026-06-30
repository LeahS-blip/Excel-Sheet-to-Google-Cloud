"""Run the REAL parsing logic from main.py against the synthetic file, then
reconcile and sanity-check. Stubs the google libs so main.py imports without them."""
import sys, types, csv, openpyxl

# --- stub the google imports (only used inside functions, not at parse time) ---
for name in ["google","google.cloud","google.oauth2","googleapiclient",
             "googleapiclient.discovery","googleapiclient.http"]:
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["google.cloud"].bigquery = types.SimpleNamespace()
sys.modules["google.oauth2"].service_account = types.SimpleNamespace()
sys.modules["googleapiclient.discovery"].build = lambda *a, **k: None
sys.modules["googleapiclient.http"].MediaIoBaseDownload = object

import main  # the real shipped code

BASE = "/sessions/affectionate-keen-pasteur/mnt/outputs"
FILE = f"{BASE}/FAKE_2026-05 B WS Ground BIlling CODED.xlsx"
NAME = "FAKE_2026-05 B WS Ground BIlling CODED.xlsx"

# 1) Parse with the real loader function
with open(FILE, "rb") as f:
    rows = main.parse_workbook(f, "FAKE_FILE_ID", NAME)

# 2) Pull the preamble "Total UPS Cost" straight from the sheet to compare against
wb = openpyxl.load_workbook(FILE, read_only=True, data_only=True)
ws = wb.active
preamble_total = None
for r in ws.iter_rows(values_only=True):
    if r and r[1] == "Total UPS Cost":
        preamble_total = float(str(r[3]).replace("$", "").replace(",", ""))
        break

loaded_sum = round(sum(x["net_amount"] for x in rows if x["net_amount"]), 2)

print("=" * 60)
print("RECONCILIATION")
print(f"  Preamble Total UPS Cost : ${preamble_total:,.2f}")
print(f"  Sum of loaded net_amount: ${loaded_sum:,.2f}")
print(f"  MATCH: {abs(preamble_total - loaded_sum) < 0.005}")
print("=" * 60)
print("SANITY CHECKS")
print(f"  Rows parsed                 : {len(rows)}")
print(f"  Distinct tracking numbers   : {len(set(x['tracking_number'] for x in rows))}")
sample = rows[0]
print(f"  Tracking # preserved as text: {sample['tracking_number']!r}")
print(f"  Leading-zero code preserved : zone={sample['zone']!r} (expect '008')")
nulldates = sum(1 for x in rows if x["transaction_date"] is None)
print(f"  Junk '1/0/1900' -> NULL     : {nulldates} null transaction_date(s) (expect >=1)")
print(f"  Receiver postal as text     : {sample['receiver_postal']!r}")
print(f"  Metadata stamped            : period={sample['_period_label']!r}, hash set={bool(sample['_row_hash'])}")
print("=" * 60)

# 3) Write a BigQuery-ready CSV (column order EXACTLY matches the table schema)
COLS = [b for _, b, _ in main.COLUMNS] + [
    "_source_file_id", "_source_file_name", "_period_label",
    "_source_row_number", "_row_hash", "_loaded_at"]
out = f"{BASE}/FAKE_charge_lines_for_bigquery.csv"
with open(out, "w", newline="") as f:
    w = csv.writer(f, quoting=csv.QUOTE_ALL)
    w.writerow(COLS)
    for rec in rows:
        w.writerow(["" if rec.get(c) is None else rec.get(c) for c in COLS])
print(f"Wrote BigQuery-ready CSV: {out}  ({len(rows)} data rows + 1 header)")
