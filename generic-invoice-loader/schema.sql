-- =============================================================================
-- BigQuery schema for the generic invoice loader
-- =============================================================================
-- Run these statements once (in the BigQuery console or via `bq query`).
-- Replace YOUR_PROJECT with your actual GCP project ID before running.
--
-- Design notes:
--   * Unlike the UPS billing pipeline (which has a fixed 64-column schema),
--     this loader's main data table is created automatically on the first
--     successful load, with BigQuery auto-detecting columns from whatever
--     header row is in the source file. You do NOT need to create
--     invoice_line_items by hand -- only the dataset and the _load_log table
--     need to exist up front.
--   * Every loaded row carries _source_file_id, _source_file_name,
--     _source_row_number, _loaded_at, and _row_hash so any number can be
--     traced back to its exact source file and load run.
-- =============================================================================

-- 1) Dataset ------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS `YOUR_PROJECT.invoices`
OPTIONS (location = 'US');

-- 2) Load-log control table ---------------------------------------------------
-- The loader checks this table to know which Drive files it has already
-- ingested, so re-running the job is safe and never double-loads a file.
CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.invoices._load_log`
(
  source_file_id     STRING    NOT NULL,
  source_file_name   STRING,
  rows_loaded        INT64,
  source_modified_at TIMESTAMP,   -- Drive modifiedTime; lets you reload if a file is re-uploaded
  loaded_at          TIMESTAMP    NOT NULL,
  status             STRING       -- 'success' | 'failed'
);

-- 3) Main data table ------------------------------------------------------------
-- Nothing to run here -- invoice_line_items is created automatically the first
-- time main.py successfully loads a file, with columns matching that file's
-- header row. If different clients' invoices have different columns,
-- BigQuery adds new columns automatically on later loads
-- (schema_update_options=ALLOW_FIELD_ADDITION in main.py) rather than failing.

-- =============================================================================
-- Handy view (optional, run after the first successful load) -----------------
-- =============================================================================
-- Row counts and last-loaded time per source file, useful for a sanity check
-- that every invoice you expect actually made it into the table.
-- CREATE OR REPLACE VIEW `YOUR_PROJECT.invoices.v_load_summary` AS
-- SELECT
--   source_file_name,
--   MAX(loaded_at)              AS last_loaded_at,
--   SUM(rows_loaded)            AS rows_loaded,
--   ANY_VALUE(status)           AS last_status
-- FROM `YOUR_PROJECT.invoices._load_log`
-- GROUP BY source_file_name
-- ORDER BY last_loaded_at DESC;
