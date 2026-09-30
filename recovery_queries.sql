/* =============================================================================
   Commercial P&C Subrogation Engine & Leakage Analyzer
   recovery_queries.sql  |  Target dialect: Snowflake
   -----------------------------------------------------------------------------
   Sections
     0. Environment
     1. Tables (DDL)
     2. Load from stage (template)
     3. Reference data (SOL by state, recovery rates, run config)
     4. Analytics views          <-- portable section, also run locally by
                                     scripts/run_pipeline_local.py (DuckDB)
     5. Validation / headline queries

   Databricks SQL notes: replace NUMBER(18,2) with DECIMAL(18,2),
   TIMESTAMP_NTZ with TIMESTAMP, and COPY INTO syntax per Unity Catalog volumes.
   DATEADD / DATEDIFF / QUALIFY in section 4 are supported by both platforms.
   ============================================================================= */


/* =============================================================================
   0. ENVIRONMENT
   ============================================================================= */
CREATE DATABASE IF NOT EXISTS CLAIMS_ANALYTICS;
CREATE SCHEMA IF NOT EXISTS CLAIMS_ANALYTICS.SUBRO;
USE SCHEMA CLAIMS_ANALYTICS.SUBRO;


/* =============================================================================
   1. TABLES
   ============================================================================= */
CREATE OR REPLACE TABLE CLAIMS_MASTER (
    CLAIM_ID            VARCHAR(12)   NOT NULL PRIMARY KEY,
    POLICY_NUMBER       VARCHAR(12)   NOT NULL,
    INSURED_NAME        VARCHAR(200),
    LINE_OF_BUSINESS    VARCHAR(50)   NOT NULL,
    LOSS_CAUSE          VARCHAR(60)   NOT NULL,
    LOSS_STATE          VARCHAR(2)    NOT NULL,
    LOSS_DATE           DATE          NOT NULL,
    REPORT_DATE         DATE          NOT NULL,
    CLOSE_DATE          DATE,
    CLAIM_STATUS        VARCHAR(10)   NOT NULL,     -- Open | Closed
    ADJUSTER_ID         VARCHAR(10)   NOT NULL,
    ADJUSTER_NAME       VARCHAR(100),
    SUBROGATION_FLAG    VARCHAR(3)    NOT NULL      -- Yes | No
)
CLUSTER BY (LINE_OF_BUSINESS, CLAIM_STATUS);

CREATE OR REPLACE TABLE ADJUSTER_NOTES (
    NOTE_ID             VARCHAR(12)   NOT NULL PRIMARY KEY,
    CLAIM_ID            VARCHAR(12)   NOT NULL REFERENCES CLAIMS_MASTER (CLAIM_ID),
    NOTE_DATE           DATE          NOT NULL,
    ADJUSTER_ID         VARCHAR(10)   NOT NULL,
    NOTE_TEXT           VARCHAR(4000) NOT NULL
)
CLUSTER BY (CLAIM_ID);

CREATE OR REPLACE TABLE FINANCIAL_RESERVES (
    TRANSACTION_ID      VARCHAR(12)   NOT NULL PRIMARY KEY,
    CLAIM_ID            VARCHAR(12)   NOT NULL REFERENCES CLAIMS_MASTER (CLAIM_ID),
    TRANSACTION_DATE    DATE          NOT NULL,
    TRANSACTION_TYPE    VARCHAR(10)   NOT NULL,     -- RESERVE | PAYMENT | RECOVERY
    COST_TYPE           VARCHAR(15)   NOT NULL,     -- Indemnity | Expense | Subrogation
    AMOUNT              NUMBER(18,2)  NOT NULL      -- RESERVE rows are changes (+/-)
)
CLUSTER BY (CLAIM_ID);

-- Output of src/text_engine.py (one row per claim)
CREATE OR REPLACE TABLE NLP_CLAIM_INDICATORS (
    CLAIM_ID              VARCHAR(12)  NOT NULL PRIMARY KEY,
    INDICATOR_SCORE       NUMBER(3,0)  NOT NULL,    -- 0-100
    PRIMARY_CATEGORY      VARCHAR(40),
    TOP_PHRASE            VARCHAR(200),
    MATCHED_PHRASES       VARCHAR(2000),
    HIT_COUNT             NUMBER(5,0),
    DISTINCT_CATEGORIES   NUMBER(3,0),
    NEGATION_FLAG         VARCHAR(1),
    FIRST_INDICATOR_DATE  DATE,
    NOTES_SCANNED         NUMBER(5,0)
);


/* =============================================================================
   2. LOAD FROM STAGE (template - adjust stage name / path)
   ============================================================================= */
CREATE OR REPLACE FILE FORMAT FF_CSV
    TYPE = CSV
    SKIP_HEADER = 1
    FIELD_OPTIONALLY_ENCLOSED_BY = '"'
    NULL_IF = ('', 'NULL')
    EMPTY_FIELD_AS_NULL = TRUE;

CREATE STAGE IF NOT EXISTS STG_SUBRO FILE_FORMAT = FF_CSV;

-- From SnowSQL:  PUT file://data/output/*.csv @STG_SUBRO AUTO_COMPRESS=TRUE;
COPY INTO CLAIMS_MASTER        FROM @STG_SUBRO/claims_master.csv.gz        FILE_FORMAT = FF_CSV;
COPY INTO ADJUSTER_NOTES       FROM @STG_SUBRO/adjuster_notes.csv.gz       FILE_FORMAT = FF_CSV;
COPY INTO FINANCIAL_RESERVES   FROM @STG_SUBRO/financial_reserves.csv.gz   FILE_FORMAT = FF_CSV;
COPY INTO NLP_CLAIM_INDICATORS FROM @STG_SUBRO/nlp_claim_indicators.csv.gz FILE_FORMAT = FF_CSV;


/* =============================================================================
   3. REFERENCE DATA
   ============================================================================= */
-- @@REFDATA_START
-- Illustrative statute-of-limitations (years) for property damage subrogation.
-- Values are simplified for modeling purposes; verify against counsel before use.
CREATE OR REPLACE TABLE REF_STATE_SOL (
    LOSS_STATE   VARCHAR(2) PRIMARY KEY,
    SOL_YEARS    NUMBER(2,0) NOT NULL
);
INSERT INTO REF_STATE_SOL VALUES
    ('CA',3),('TX',2),('FL',4),('NY',3),('IL',5),('GA',4),
    ('OH',4),('PA',2),('NJ',6),('NC',3),('AZ',2),('WA',3);

-- Modeled expected recovery rate (share of net paid) when pursued.
CREATE OR REPLACE TABLE REF_RECOVERY_RATES (
    SUBRO_CATEGORY       VARCHAR(40) PRIMARY KEY,
    CATEGORY_LABEL       VARCHAR(60),
    EXPECTED_RECOVERY    NUMBER(4,2) NOT NULL
);
INSERT INTO REF_RECOVERY_RATES VALUES
    ('auto_rear_end',          'Commercial Auto - Rear End',            0.75),
    ('auto_third_party_fault', 'Commercial Auto - Third-Party Fault',   0.55),
    ('product_defect',         'Property - Defective Equipment',        0.35),
    ('contractor_negligence',  'Contractor / Vendor Negligence',        0.45),
    ('utility_third_party',    'Utility / Adjacent Third Party',        0.30),
    ('carrier_liability',      'Inland Marine - Carrier Liability',     0.50);

-- Single-row run configuration so every view uses the same as-of date and thresholds.
CREATE OR REPLACE TABLE REF_RUN_CONFIG (
    AS_OF_DATE             DATE        NOT NULL,
    MIN_INDICATOR_SCORE    NUMBER(3,0) NOT NULL,   -- NLP confidence threshold
    MIN_NET_PAID           NUMBER(18,2) NOT NULL   -- ignore files too small to pursue
);
INSERT INTO REF_RUN_CONFIG VALUES ('2026-09-30', 60, 1000);
-- @@REFDATA_END


/* =============================================================================
   4. ANALYTICS VIEWS
   ============================================================================= */
-- @@ANALYTICS_START

/* 4.1  Claim-level financial position.
        Window functions give the running incurred path and the last reserve
        movement per claim; conditional aggregation gives paid / recovered. */
CREATE OR REPLACE VIEW VW_CLAIM_FINANCIALS AS
WITH ledger AS (
    SELECT
        f.CLAIM_ID,
        f.TRANSACTION_DATE,
        f.TRANSACTION_TYPE,
        f.COST_TYPE,
        f.AMOUNT,
        SUM(CASE WHEN f.TRANSACTION_TYPE = 'RESERVE' THEN f.AMOUNT ELSE 0 END)
            OVER (PARTITION BY f.CLAIM_ID ORDER BY f.TRANSACTION_DATE, f.TRANSACTION_ID
                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)        AS RUNNING_CASE_RESERVE
    FROM FINANCIAL_RESERVES f
),
last_reserve AS (
    SELECT CLAIM_ID, TRANSACTION_DATE AS LAST_RESERVE_CHANGE_DATE
    FROM ledger
    WHERE TRANSACTION_TYPE = 'RESERVE'
    QUALIFY ROW_NUMBER() OVER (PARTITION BY CLAIM_ID ORDER BY TRANSACTION_DATE DESC) = 1
),
agg AS (
    SELECT
        CLAIM_ID,
        SUM(CASE WHEN TRANSACTION_TYPE = 'RESERVE' THEN AMOUNT ELSE 0 END)                             AS INDEMNITY_RESERVE_TOTAL,
        SUM(CASE WHEN TRANSACTION_TYPE = 'PAYMENT' AND COST_TYPE = 'Indemnity' THEN AMOUNT ELSE 0 END) AS PAID_INDEMNITY,
        SUM(CASE WHEN TRANSACTION_TYPE = 'PAYMENT' AND COST_TYPE = 'Expense'   THEN AMOUNT ELSE 0 END) AS PAID_EXPENSE,
        SUM(CASE WHEN TRANSACTION_TYPE = 'RECOVERY' THEN AMOUNT ELSE 0 END)                            AS RECOVERED_AMOUNT,
        MAX(RUNNING_CASE_RESERVE)                                                                      AS PEAK_CASE_RESERVE
    FROM ledger
    GROUP BY CLAIM_ID
)
SELECT
    a.CLAIM_ID,
    a.PAID_INDEMNITY,
    a.PAID_EXPENSE,
    a.PAID_INDEMNITY + a.PAID_EXPENSE                                         AS TOTAL_PAID,
    GREATEST(a.INDEMNITY_RESERVE_TOTAL - a.PAID_INDEMNITY, 0)                 AS OUTSTANDING_RESERVE,
    a.PAID_INDEMNITY + a.PAID_EXPENSE
        + GREATEST(a.INDEMNITY_RESERVE_TOTAL - a.PAID_INDEMNITY, 0)           AS TOTAL_INCURRED,
    a.RECOVERED_AMOUNT,
    a.PAID_INDEMNITY - a.RECOVERED_AMOUNT                                     AS NET_PAID_INDEMNITY,
    a.PEAK_CASE_RESERVE,
    lr.LAST_RESERVE_CHANGE_DATE
FROM agg a
LEFT JOIN last_reserve lr ON lr.CLAIM_ID = a.CLAIM_ID;


/* 4.2  Claim-level subrogation base: claims + financials + NLP + SOL. */
CREATE OR REPLACE VIEW VW_SUBRO_BASE AS
SELECT
    c.CLAIM_ID,
    c.POLICY_NUMBER,
    c.INSURED_NAME,
    c.LINE_OF_BUSINESS,
    c.LOSS_CAUSE,
    c.LOSS_STATE,
    c.LOSS_DATE,
    c.REPORT_DATE,
    c.CLOSE_DATE,
    c.CLAIM_STATUS,
    c.ADJUSTER_ID,
    c.ADJUSTER_NAME,
    c.SUBROGATION_FLAG,
    f.PAID_INDEMNITY,
    f.PAID_EXPENSE,
    f.TOTAL_PAID,
    f.OUTSTANDING_RESERVE,
    f.TOTAL_INCURRED,
    f.RECOVERED_AMOUNT,
    f.NET_PAID_INDEMNITY,
    COALESCE(n.INDICATOR_SCORE, 0)                          AS INDICATOR_SCORE,
    n.PRIMARY_CATEGORY,
    rr.CATEGORY_LABEL,
    COALESCE(rr.EXPECTED_RECOVERY, 0)                       AS EXPECTED_RECOVERY_RATE,
    n.TOP_PHRASE,
    n.MATCHED_PHRASES,
    COALESCE(n.DISTINCT_CATEGORIES, 0)                      AS DISTINCT_CATEGORIES,
    COALESCE(n.NEGATION_FLAG, 'N')                          AS NEGATION_FLAG,
    n.FIRST_INDICATOR_DATE,
    DATEADD(year, s.SOL_YEARS, c.LOSS_DATE)                 AS SOL_EXPIRY_DATE,
    DATEDIFF('day', cfg.AS_OF_DATE, DATEADD(year, s.SOL_YEARS, c.LOSS_DATE)) AS DAYS_TO_SOL,
    cfg.AS_OF_DATE
FROM CLAIMS_MASTER c
JOIN VW_CLAIM_FINANCIALS f          ON f.CLAIM_ID = c.CLAIM_ID
LEFT JOIN NLP_CLAIM_INDICATORS n    ON n.CLAIM_ID = c.CLAIM_ID
LEFT JOIN REF_RECOVERY_RATES rr     ON rr.SUBRO_CATEGORY = n.PRIMARY_CATEGORY
LEFT JOIN REF_STATE_SOL s           ON s.LOSS_STATE = c.LOSS_STATE
CROSS JOIN REF_RUN_CONFIG cfg;


/* 4.3  Leakage: closed claims, never referred to subrogation, no recovery,
        but the diary shows a credible third-party liability indicator.
        Estimated leakage = net paid x expected recovery rate x NLP confidence. */
CREATE OR REPLACE VIEW VW_LEAKAGE_CLAIMS AS
WITH candidates AS (
    SELECT b.*
    FROM VW_SUBRO_BASE b
    CROSS JOIN REF_RUN_CONFIG cfg
    WHERE b.CLAIM_STATUS = 'Closed'
      AND b.SUBROGATION_FLAG = 'No'
      AND b.RECOVERED_AMOUNT = 0
      AND b.INDICATOR_SCORE >= cfg.MIN_INDICATOR_SCORE
      AND b.NET_PAID_INDEMNITY >= cfg.MIN_NET_PAID
)
SELECT
    c.*,
    ROUND(c.NET_PAID_INDEMNITY * c.EXPECTED_RECOVERY_RATE * (c.INDICATOR_SCORE / 100.0), 2) AS EST_LEAKAGE,
    CASE WHEN c.DAYS_TO_SOL > 0 THEN 'Recoverable - SOL open'
         ELSE 'Lost - SOL expired' END                                                    AS RECOVERY_WINDOW,
    RANK() OVER (ORDER BY c.NET_PAID_INDEMNITY * c.EXPECTED_RECOVERY_RATE * c.INDICATOR_SCORE DESC) AS LEAKAGE_RANK
FROM candidates c;


/* 4.4  Leakage by line of business and subrogation category,
        with share of total via a window over the grouped result. */
CREATE OR REPLACE VIEW VW_LEAKAGE_BY_SEGMENT AS
SELECT
    LINE_OF_BUSINESS,
    CATEGORY_LABEL,
    COUNT(*)                                                         AS LEAKED_CLAIMS,
    ROUND(SUM(NET_PAID_INDEMNITY), 2)                                AS NET_PAID_ON_LEAKED,
    ROUND(SUM(EST_LEAKAGE), 2)                                       AS EST_LEAKAGE,
    ROUND(SUM(CASE WHEN RECOVERY_WINDOW = 'Recoverable - SOL open' THEN EST_LEAKAGE ELSE 0 END), 2) AS STILL_RECOVERABLE,
    ROUND(SUM(EST_LEAKAGE) / SUM(SUM(EST_LEAKAGE)) OVER (), 4)       AS SHARE_OF_TOTAL_LEAKAGE,
    DENSE_RANK() OVER (ORDER BY SUM(EST_LEAKAGE) DESC)               AS SEGMENT_RANK
FROM VW_LEAKAGE_CLAIMS
GROUP BY LINE_OF_BUSINESS, CATEGORY_LABEL;


/* 4.5  Adjuster-level missed-subrogation rate: benchmarks each adjuster
        against the unit using PERCENT_RANK. */
CREATE OR REPLACE VIEW VW_ADJUSTER_LEAKAGE AS
WITH closed_with_indicator AS (
    SELECT b.*
    FROM VW_SUBRO_BASE b
    CROSS JOIN REF_RUN_CONFIG cfg
    WHERE b.CLAIM_STATUS = 'Closed'
      AND b.INDICATOR_SCORE >= cfg.MIN_INDICATOR_SCORE
),
per_adjuster AS (
    SELECT
        ADJUSTER_ID,
        ADJUSTER_NAME,
        COUNT(*)                                                             AS CLOSED_WITH_INDICATOR,
        SUM(CASE WHEN SUBROGATION_FLAG = 'No' THEN 1 ELSE 0 END)             AS NOT_REFERRED,
        SUM(CASE WHEN SUBROGATION_FLAG = 'No' THEN NET_PAID_INDEMNITY * EXPECTED_RECOVERY_RATE * INDICATOR_SCORE / 100.0 ELSE 0 END) AS EST_LEAKAGE
    FROM closed_with_indicator
    GROUP BY ADJUSTER_ID, ADJUSTER_NAME
)
SELECT
    ADJUSTER_ID,
    ADJUSTER_NAME,
    CLOSED_WITH_INDICATOR,
    NOT_REFERRED,
    ROUND(NOT_REFERRED * 1.0 / NULLIF(CLOSED_WITH_INDICATOR, 0), 4)          AS MISSED_REFERRAL_RATE,
    ROUND(EST_LEAKAGE, 2)                                                    AS EST_LEAKAGE,
    ROUND(PERCENT_RANK() OVER (ORDER BY NOT_REFERRED * 1.0 / NULLIF(CLOSED_WITH_INDICATOR, 0)), 4) AS MISSED_RATE_PERCENTILE
FROM per_adjuster;


/* 4.6  Triage queue: open claims not yet referred, scored 0-100.
          Indicator strength    up to 45 pts
          Recovery rate         up to 15 pts  (category expected recovery x 20)
          Financial exposure    up to 25 pts  (log-scaled incurred, capped at $250K)
          SOL urgency           up to 15 pts  (sooner expiry = higher priority)
          Negation present      -10 pts       (mixed signals in diary)            */
CREATE OR REPLACE VIEW VW_TRIAGE_QUEUE AS
WITH open_candidates AS (
    SELECT b.*
    FROM VW_SUBRO_BASE b
    WHERE b.CLAIM_STATUS = 'Open'
      AND b.SUBROGATION_FLAG = 'No'
      AND b.INDICATOR_SCORE > 0
      AND b.DAYS_TO_SOL > 0
),
scored AS (
    SELECT
        o.*,
        o.INDICATOR_SCORE * 0.45                                                         AS PTS_INDICATOR,
        LEAST(o.EXPECTED_RECOVERY_RATE * 20, 15)                                         AS PTS_RECOVERY_RATE,
        LEAST(LN(1 + o.TOTAL_INCURRED) / LN(1 + 250000), 1) * 25                         AS PTS_EXPOSURE,
        CASE WHEN o.DAYS_TO_SOL <= 180 THEN 15
             WHEN o.DAYS_TO_SOL <= 365 THEN 10
             WHEN o.DAYS_TO_SOL <= 730 THEN 6
             ELSE 3 END                                                                  AS PTS_SOL_URGENCY,
        CASE WHEN o.NEGATION_FLAG = 'Y' THEN -10 ELSE 0 END                              AS PTS_NEGATION
    FROM open_candidates o
),
final AS (
    SELECT
        s.*,
        ROUND(GREATEST(LEAST(PTS_INDICATOR + PTS_RECOVERY_RATE + PTS_EXPOSURE + PTS_SOL_URGENCY + PTS_NEGATION, 100), 0), 1)
            AS RECOVERY_PROBABILITY_SCORE,
        ROUND(s.TOTAL_INCURRED * s.EXPECTED_RECOVERY_RATE * (s.INDICATOR_SCORE / 100.0), 2) AS EST_RECOVERY_VALUE
    FROM scored s
)
SELECT
    f.*,
    CASE WHEN RECOVERY_PROBABILITY_SCORE >= 75 THEN 'P1 - Pursue Now'
         WHEN RECOVERY_PROBABILITY_SCORE >= 60 THEN 'P2 - Review This Week'
         ELSE 'P3 - Monitor' END                                                          AS TRIAGE_TIER,
    RANK()       OVER (ORDER BY RECOVERY_PROBABILITY_SCORE DESC, EST_RECOVERY_VALUE DESC) AS QUEUE_RANK,
    ROW_NUMBER() OVER (PARTITION BY ADJUSTER_ID
                       ORDER BY RECOVERY_PROBABILITY_SCORE DESC, EST_RECOVERY_VALUE DESC) AS ADJUSTER_QUEUE_POSITION
FROM final f;


/* 4.7  Monthly leakage trend by close month (running cumulative total). */
CREATE OR REPLACE VIEW VW_LEAKAGE_TREND AS
WITH monthly AS (
    SELECT
        DATE_TRUNC('month', CLOSE_DATE)  AS CLOSE_MONTH,
        COUNT(*)                         AS LEAKED_CLAIMS,
        SUM(EST_LEAKAGE)                 AS EST_LEAKAGE
    FROM VW_LEAKAGE_CLAIMS
    GROUP BY DATE_TRUNC('month', CLOSE_DATE)
)
SELECT
    CLOSE_MONTH,
    LEAKED_CLAIMS,
    ROUND(EST_LEAKAGE, 2)                                                       AS EST_LEAKAGE,
    ROUND(SUM(EST_LEAKAGE) OVER (ORDER BY CLOSE_MONTH
                                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 2) AS CUMULATIVE_LEAKAGE,
    ROUND(AVG(EST_LEAKAGE) OVER (ORDER BY CLOSE_MONTH
                                 ROWS BETWEEN 2 PRECEDING AND CURRENT ROW), 2)  AS ROLLING_3M_AVG
FROM monthly;

-- @@ANALYTICS_END


/* =============================================================================
   5. VALIDATION / HEADLINE QUERIES
   ============================================================================= */
-- Portfolio headline
SELECT
    (SELECT COUNT(*) FROM CLAIMS_MASTER)                                    AS TOTAL_CLAIMS,
    (SELECT COUNT(*) FROM VW_LEAKAGE_CLAIMS)                                AS LEAKED_CLAIMS,
    (SELECT ROUND(SUM(EST_LEAKAGE), 0) FROM VW_LEAKAGE_CLAIMS)              AS EST_TOTAL_LEAKAGE,
    (SELECT ROUND(SUM(EST_LEAKAGE), 0) FROM VW_LEAKAGE_CLAIMS
      WHERE RECOVERY_WINDOW = 'Recoverable - SOL open')                     AS STILL_RECOVERABLE,
    (SELECT COUNT(*) FROM VW_TRIAGE_QUEUE)                                  AS OPEN_QUEUE_SIZE,
    (SELECT COUNT(*) FROM CLAIMS_MASTER WHERE CLAIM_STATUS = 'Open')        AS OPEN_INVENTORY;

-- Top segments
SELECT * FROM VW_LEAKAGE_BY_SEGMENT ORDER BY SEGMENT_RANK LIMIT 10;

-- Top 25 open claims for the subrogation unit
SELECT QUEUE_RANK, CLAIM_ID, LINE_OF_BUSINESS, CATEGORY_LABEL, TOP_PHRASE,
       RECOVERY_PROBABILITY_SCORE, TRIAGE_TIER, EST_RECOVERY_VALUE, DAYS_TO_SOL, ADJUSTER_NAME
FROM VW_TRIAGE_QUEUE
ORDER BY QUEUE_RANK
LIMIT 25;

-- Reconciliation: ledger totals must equal view totals
SELECT
    (SELECT SUM(AMOUNT) FROM FINANCIAL_RESERVES WHERE TRANSACTION_TYPE = 'PAYMENT')  AS LEDGER_PAID,
    (SELECT SUM(TOTAL_PAID) FROM VW_CLAIM_FINANCIALS)                                AS VIEW_PAID,
    (SELECT SUM(AMOUNT) FROM FINANCIAL_RESERVES WHERE TRANSACTION_TYPE = 'RECOVERY') AS LEDGER_RECOVERED,
    (SELECT SUM(RECOVERED_AMOUNT) FROM VW_CLAIM_FINANCIALS)                          AS VIEW_RECOVERED;
