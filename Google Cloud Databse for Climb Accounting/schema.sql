-- =============================================================================
-- BigQuery schema for Wine Country Connect UPS billing detail
-- =============================================================================
-- Run these statements once (in the BigQuery console or via `bq query`).
-- Replace YOUR_PROJECT with your actual GCP project ID before running.
--
-- Design notes:
--   * One row per CHARGE LINE (Ground Residential, Fuel Surcharge, etc.),
--     exactly as it appears in the source file. Nothing is aggregated.
--   * Codes, account/tracking/postal numbers are STRING on purpose. Many have
--     leading zeros (e.g. "00000088BH", "0000009A516V216", postal "181032918")
--     that INTEGER columns would destroy.
--   * Money/weight fields are NUMERIC (exact decimal) so accounting totals
--     reconcile to the penny.
--   * Dates are DATE. Junk source dates like "1/0/1900" are loaded as NULL.
--   * Metadata columns (prefixed _) record provenance for every row so you can
--     trace any number back to the exact source file and load run.
-- =============================================================================

-- 1) Dataset ------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS `YOUR_PROJECT.wcc_billing`
OPTIONS (location = 'US');

-- 2) Main charge-line table ---------------------------------------------------
CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.wcc_billing.ups_charge_lines`
(
  -- Invoice / account identity
  ws_data_version              STRING,
  recipient_number             STRING,
  account_number               STRING,
  account_country              STRING,
  invoice_date                 DATE,
  invoice_number               STRING,
  invoice_amount               NUMERIC,
  invoice_currency_code        STRING,
  transaction_date             DATE,

  -- Shipment identity
  lead_shipment_number         STRING,
  shipment_reference_number_1  STRING,
  shipment_reference_number_2  STRING,
  bill_option_code             STRING,
  package_quantity             INT64,
  oversize_quantity            INT64,
  tracking_number              STRING,

  -- Weight / package
  billed_weight                NUMERIC,
  billed_weight_uom            STRING,
  container_type               STRING,
  billed_weight_type           STRING,
  package_dimensions           STRING,
  zone                         STRING,

  -- The money + what it is for
  net_amount                   NUMERIC,
  charge_description           STRING,
  charge_description_code       STRING,
  charge_classification_code   STRING,

  -- Package reference fields
  package_reference_number_1   STRING,
  package_reference_number_2   STRING,
  package_reference_number_3   STRING,
  package_reference_number_4   STRING,
  package_reference_number_5   STRING,

  entered_weight               NUMERIC,
  entered_weight_uom           STRING,
  transaction_currency_code    STRING,
  tax_indicator                STRING,
  basis_value                  NUMERIC,
  basis_currency_code          STRING,
  charged_unit_quantity        NUMERIC,
  charge_category_code         STRING,
  charge_category_detail_code  STRING,
  charge_source                STRING,
  type_code_1                  STRING,
  type_detail_code_1           STRING,
  type_detail_value_1          STRING,
  customer_reference_number    STRING,

  -- Sender
  sender_name                  STRING,
  sender_company_name          STRING,
  sender_address_line_1        STRING,
  sender_address_line_2        STRING,
  sender_city                  STRING,
  sender_state                 STRING,
  sender_postal                STRING,
  sender_country               STRING,

  -- Receiver
  receiver_name                STRING,
  receiver_company_name        STRING,
  receiver_address_line_1      STRING,
  receiver_address_line_2      STRING,
  receiver_city                STRING,
  receiver_state               STRING,
  receiver_postal              STRING,
  receiver_country             STRING,

  corrected_zone               STRING,
  activity_period              STRING,
  invoice_period               STRING,

  -- Provenance / load metadata (populated by the loader, not the source file)
  _source_file_id              STRING   NOT NULL,  -- Google Drive file ID
  _source_file_name            STRING   NOT NULL,  -- e.g. "2026-05 B WS Ground BIlling CODED.xlsx"
  _period_label                STRING,             -- e.g. "2026-05-B" parsed from the filename
  _source_row_number           INT64,              -- row position within the source sheet
  _row_hash                    STRING   NOT NULL,  -- hash of the source row, used for dedupe
  _loaded_at                   TIMESTAMP NOT NULL
)
PARTITION BY invoice_date
CLUSTER BY tracking_number, charge_description_code;

-- 3) Load-log control table ---------------------------------------------------
-- The loader checks this table to know which Drive files it has already
-- ingested, so re-running the job is safe and never double-loads a file.
CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.wcc_billing._load_log`
(
  source_file_id     STRING    NOT NULL,
  source_file_name   STRING,
  period_label       STRING,
  rows_loaded        INT64,
  source_modified_at TIMESTAMP,   -- Drive modifiedTime; lets you reload if a file is re-coded
  loaded_at          TIMESTAMP    NOT NULL,
  status             STRING       -- 'success' | 'failed'
);

-- =============================================================================
-- Handy views (optional) ------------------------------------------------------
-- =============================================================================

-- Period totals that mirror the summary block at the top of each source file.
CREATE OR REPLACE VIEW `YOUR_PROJECT.wcc_billing.v_period_totals` AS
SELECT
  _period_label,
  invoice_period,
  COUNT(DISTINCT tracking_number)                       AS packages,
  COUNT(*)                                              AS charge_lines,
  SUM(net_amount)                                       AS total_ups_cost,
  ROUND(SUM(net_amount) * 0.07, 2)                      AS markup_7pct,
  ROUND(SUM(net_amount) * 1.07, 2)                      AS grand_total_with_markup
FROM `YOUR_PROJECT.wcc_billing.ups_charge_lines`
GROUP BY _period_label, invoice_period
ORDER BY _period_label;

-- Charge mix: how much of each charge type across everything loaded.
CREATE OR REPLACE VIEW `YOUR_PROJECT.wcc_billing.v_charge_mix` AS
SELECT
  charge_description,
  charge_description_code,
  COUNT(*)            AS lines,
  SUM(net_amount)     AS total_amount
FROM `YOUR_PROJECT.wcc_billing.ups_charge_lines`
GROUP BY charge_description, charge_description_code
ORDER BY total_amount DESC;
