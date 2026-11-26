-- SQL version of the "blind" detector from data/stress_detectors.py, plus scoring.
--
-- Run against the stress Postgres after data/stress_load.py:
--   docker exec -i stress-postgres psql -U postgres -d logistics_db \
--     < data/stress_eval.sql
--
-- Nothing here reads the labels until the final scoring queries. The rules learn
-- the ID shapes, the monthly fuel rate and the lateness cut-off from the data
-- itself. Writes only to the stress_eval schema.

\timing on
\set ON_ERROR_STOP on

DROP TABLE IF EXISTS stress_eval.findings;
CREATE TABLE stress_eval.findings (issue_type TEXT, record_id TEXT);

-- ---------------------------------------------------------------- LATE_ARRIVAL
-- Lag between the scan and the row reaching the feed; the cut-off is the median
-- plus 10 robust sigmas (1.4826 * MAD), so it adapts to whatever "normal" is.
CREATE TEMP TABLE wms_lag AS
SELECT package_id,
       extract(epoch FROM ingestion_timestamp - scan_timestamp) AS lag_s
FROM source_enterprise.raw_wms_package_scans;

CREATE TEMP TABLE lag_cutoff AS
WITH med AS (
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY lag_s) AS m FROM wms_lag
)
SELECT med.m + 10 * 1.4826 * (
           SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(lag_s - med.m))
           FROM wms_lag
       ) AS cutoff_s
FROM med;

INSERT INTO stress_eval.findings
SELECT 'LATE_ARRIVAL', package_id
FROM wms_lag, lag_cutoff
WHERE lag_s > cutoff_s;

-- ------------------------------------------------------------------ EDI prepare
CREATE TEMP TABLE edi_norm AS
SELECT edi_record_id,
       upper(trim(tracking_id))        AS trk,
       upper(trim(carrier_invoice_id)) AS inv,
       base_rate, fuel_surcharge, total_billed_amount,
       extract(month FROM invoice_date) AS inv_month
FROM source_enterprise.raw_edi_ups_invoices;

CREATE TEMP TABLE wms_keys AS
SELECT DISTINCT upper(trim(tracking_id)) AS trk
FROM source_enterprise.raw_wms_package_scans;
CREATE INDEX ON wms_keys (trk);

-- ----------------------------------------------------------------- MISSING_KEY
-- A key is valid if it has the most common shape AND exists in the warehouse feed.
CREATE TEMP TABLE edi_missing AS
WITH shape AS (
    SELECT length(trk) AS len, left(trk, 2) AS pre
    FROM edi_norm
    WHERE trk IS NOT NULL
    GROUP BY 1, 2
    ORDER BY count(*) DESC
    LIMIT 1
)
SELECT e.edi_record_id
FROM edi_norm e
CROSS JOIN shape s
LEFT JOIN wms_keys w ON w.trk = e.trk
WHERE e.trk IS NULL
   OR length(e.trk) <> s.len
   OR left(e.trk, 2) <> s.pre
   OR w.trk IS NULL;

INSERT INTO stress_eval.findings
SELECT 'MISSING_KEY', edi_record_id FROM edi_missing;

-- ------------------------------------------------------------- TRUNCATED_FIELD
INSERT INTO stress_eval.findings
WITH shape AS (
    SELECT length(inv) AS len FROM edi_norm
    GROUP BY 1 ORDER BY count(*) DESC LIMIT 1
)
SELECT 'TRUNCATED_FIELD', e.edi_record_id
FROM edi_norm e, shape s
WHERE length(e.inv) <> s.len;

-- ------------------------------------------------------------- FUEL_TIER_JUMP
-- Each month's rate is the median fuel/base ratio seen that month.
INSERT INTO stress_eval.findings
WITH rate AS (
    SELECT inv_month,
           percentile_cont(0.5) WITHIN GROUP (
               ORDER BY fuel_surcharge / greatest(base_rate, 0.01)
           ) AS pct
    FROM edi_norm
    GROUP BY 1
)
SELECT 'FUEL_TIER_JUMP', e.edi_record_id
FROM edi_norm e
JOIN rate r USING (inv_month)
WHERE abs(e.fuel_surcharge - round((e.base_rate * r.pct)::numeric, 2)) > 0.01;

-- ------------------------------------------------------------ DUPLICATE_INVOICE
-- Same shipment, same invoice, same amount, billed again. A second line with a
-- different amount is treated as a legitimate adjustment.
INSERT INTO stress_eval.findings
SELECT 'DUPLICATE_INVOICE', edi_record_id
FROM (
    SELECT e.edi_record_id,
           row_number() OVER (
               PARTITION BY e.trk, e.inv, e.total_billed_amount
               ORDER BY e.edi_record_id
           ) AS occurrence
    FROM edi_norm e
    WHERE e.edi_record_id NOT IN (SELECT edi_record_id FROM edi_missing)
) ranked
WHERE occurrence > 1;

ANALYZE stress_eval.findings;

-- ---------------------------------------------------------------------- SCORING
-- Only labels whose row has actually been loaded count (after the initial phase
-- the last 10% of rows is still absent).
-- Joins, not OR-ed EXISTS: the planner cannot turn the latter into a semi-join.
CREATE TEMP TABLE loaded_labels AS
SELECT l.issue_type, l.variant, l.record_id
FROM stress_eval.defect_labels l
JOIN source_enterprise.raw_edi_ups_invoices e ON e.edi_record_id = l.record_id
WHERE l.dataset = 'edi'
UNION ALL
SELECT l.issue_type, l.variant, l.record_id
FROM stress_eval.defect_labels l
JOIN source_enterprise.raw_wms_package_scans w ON w.package_id = l.record_id
WHERE l.dataset = 'wms';

CREATE TEMP TABLE truth AS
SELECT * FROM loaded_labels WHERE issue_type NOT LIKE 'DECOY%';
ANALYZE truth;

\echo
\echo '== Recall and precision per defect'
SELECT t.issue_type,
       t.injected,
       coalesce(f.flagged, 0)                                  AS flagged,
       coalesce(h.hits, 0)                                     AS caught,
       round(100.0 * coalesce(h.hits, 0) / t.injected, 1)      AS recall_pct,
       round(100.0 * coalesce(h.hits, 0) / nullif(f.flagged, 0), 1) AS precision_pct
FROM (SELECT issue_type, count(*) AS injected FROM truth GROUP BY 1) t
LEFT JOIN (SELECT issue_type, count(*) AS flagged
           FROM stress_eval.findings GROUP BY 1) f USING (issue_type)
LEFT JOIN (SELECT f.issue_type, count(*) AS hits
           FROM stress_eval.findings f
           JOIN (SELECT DISTINCT issue_type, record_id FROM truth) x
             USING (issue_type, record_id)
           GROUP BY 1) h USING (issue_type)
ORDER BY t.issue_type;

\echo
\echo '== Recall by variant'
SELECT x.issue_type, x.variant, x.injected,
       coalesce(h.hits, 0) AS caught,
       round(100.0 * coalesce(h.hits, 0) / x.injected, 1) AS recall_pct
FROM (SELECT issue_type, variant, count(*) AS injected FROM truth GROUP BY 1, 2) x
LEFT JOIN (SELECT t.issue_type, t.variant, count(*) AS hits
           FROM truth t
           JOIN stress_eval.findings f USING (issue_type, record_id)
           GROUP BY 1, 2) h USING (issue_type, variant)
ORDER BY x.issue_type, x.variant;

\echo
\echo '== Legitimate look-alike rows flagged by mistake (any rule)'
SELECT l.issue_type AS decoy,
       count(*) AS present,
       count(*) FILTER (WHERE f.record_id IS NOT NULL) AS wrongly_flagged
FROM loaded_labels l
LEFT JOIN (SELECT DISTINCT record_id FROM stress_eval.findings) f
       ON f.record_id = l.record_id
WHERE l.issue_type LIKE 'DECOY%'
GROUP BY 1
ORDER BY 1;
