# Power BI Dashboard Guide: Claims Leakage Triage

This guide describes how to build `claims_leakage_triage.pbix` on top of the pipeline outputs.

## 1. Data sources

Run `python scripts/run_pipeline_local.py` first. It writes these extracts to `data/output/powerbi/`:

| File | Use in model |
|---|---|
| `vw_subro_base.csv` | Fact table: one row per claim |
| `vw_leakage_claims.csv` | Fact table: closed claims with missed subrogation |
| `vw_triage_queue.csv` | Fact table: open claims ranked for pursuit |
| `vw_leakage_by_segment.csv` | Pre-aggregated segment summary |
| `vw_adjuster_leakage.csv` | Adjuster benchmark |
| `vw_leakage_trend.csv` | Monthly trend with cumulative and rolling values |

To connect to Snowflake instead, use **Get Data → Snowflake** and select the views of the same names from `CLAIMS_ANALYTICS.SUBRO`.

## 2. Power Query steps

1. Set `LOSS_DATE`, `REPORT_DATE`, `CLOSE_DATE`, `SOL_EXPIRY_DATE` to type **Date**.
2. Set all currency columns (`NET_PAID_INDEMNITY`, `EST_LEAKAGE`, `EST_RECOVERY_VALUE`, `TOTAL_INCURRED`) to **Fixed decimal number**.
3. Replace nulls in `CATEGORY_LABEL` with `No Indicator`.
4. Create a `DimDate` table (see DAX below) and mark it as a date table.

## 3. Relationships

```
DimDate[Date]              1 ──── *  vw_subro_base[LOSS_DATE]
vw_subro_base[CLAIM_ID]    1 ──── 0..1 vw_leakage_claims[CLAIM_ID]
vw_subro_base[CLAIM_ID]    1 ──── 0..1 vw_triage_queue[CLAIM_ID]
vw_adjuster_leakage[ADJUSTER_ID] 1 ──── * vw_subro_base[ADJUSTER_ID]
```

Use single-direction filtering from dimension to fact.

## 4. DAX

### Date table

```dax
DimDate =
ADDCOLUMNS (
    CALENDAR ( DATE ( 2021, 1, 1 ), DATE ( 2026, 12, 31 ) ),
    "Year", YEAR ( [Date] ),
    "Month", FORMAT ( [Date], "MMM YYYY" ),
    "MonthSort", YEAR ( [Date] ) * 100 + MONTH ( [Date] )
)
```

### Core measures

```dax
Total Claims = COUNTROWS ( vw_subro_base )

Open Claims =
CALCULATE ( [Total Claims], vw_subro_base[CLAIM_STATUS] = "Open" )

Leaked Claims = COUNTROWS ( vw_leakage_claims )

Estimated Leakage = SUM ( vw_leakage_claims[EST_LEAKAGE] )

Still Recoverable =
CALCULATE (
    [Estimated Leakage],
    vw_leakage_claims[RECOVERY_WINDOW] = "Recoverable - SOL open"
)

% Leakage Recoverable = DIVIDE ( [Still Recoverable], [Estimated Leakage] )

Recovered to Date = SUM ( vw_subro_base[RECOVERED_AMOUNT] )

Net Paid Indemnity = SUM ( vw_subro_base[NET_PAID_INDEMNITY] )

Leakage % of Net Paid = DIVIDE ( [Estimated Leakage], [Net Paid Indemnity] )
```

### Triage measures

```dax
Queue Size = COUNTROWS ( vw_triage_queue )

P1 Claims =
CALCULATE ( [Queue Size], vw_triage_queue[TRIAGE_TIER] = "P1 - Pursue Now" )

Queue Recovery Value = SUM ( vw_triage_queue[EST_RECOVERY_VALUE] )

Queue % of Open Inventory = DIVIDE ( [Queue Size], [Open Claims] )

Avg Recovery Score = AVERAGE ( vw_triage_queue[RECOVERY_PROBABILITY_SCORE] )

Claims Expiring 180d =
CALCULATE ( [Queue Size], vw_triage_queue[DAYS_TO_SOL] <= 180 )
```

### Referral performance

```dax
Referral Rate on Indicated Claims =
VAR Indicated =
    CALCULATE ( [Total Claims], vw_subro_base[INDICATOR_SCORE] >= 60,
                vw_subro_base[CLAIM_STATUS] = "Closed" )
VAR Referred =
    CALCULATE ( [Total Claims], vw_subro_base[INDICATOR_SCORE] >= 60,
                vw_subro_base[CLAIM_STATUS] = "Closed",
                vw_subro_base[SUBROGATION_FLAG] = "Yes" )
RETURN DIVIDE ( Referred, Indicated )
```

## 5. Page layout

**Page 1: Leakage Overview**
KPI cards for Estimated Leakage, Still Recoverable, Leaked Claims, and Leakage % of Net Paid. A clustered bar of Estimated Leakage by `CATEGORY_LABEL` split by `LINE_OF_BUSINESS`, a line chart from `vw_leakage_trend` (monthly leakage with cumulative total on a secondary axis), and slicers for LOB, state, and close year.

**Page 2: Triage Queue**
KPI cards for Queue Size, P1 Claims, Queue Recovery Value, and Claims Expiring 180d. A table sorted by `QUEUE_RANK` showing claim ID, LOB, category, top phrase, score, tier, estimated recovery value, days to SOL, and adjuster. Apply conditional formatting to the score column (data bars) and the tier column (P1 red, P2 amber, P3 grey). A scatter of Recovery Score against Est. Recovery Value, sized by Total Incurred.

**Page 3: Adjuster Performance**
A bar chart of `MISSED_REFERRAL_RATE` by adjuster with a unit-average constant line, a table of adjusters with closed-with-indicator count, not-referred count, and estimated leakage, and drill-through to Page 2 filtered to that adjuster's queue.

## 6. Publishing the .pbix to GitHub

Save the file as `dashboard/claims_leakage_triage.pbix`. Before committing, remove any credentials from **Data source settings**, and keep the file under 100 MB (GitHub's hard limit). Add PNG screenshots of each page to `dashboard/screenshots/` and reference them in the main README, since most reviewers will not open the .pbix.
