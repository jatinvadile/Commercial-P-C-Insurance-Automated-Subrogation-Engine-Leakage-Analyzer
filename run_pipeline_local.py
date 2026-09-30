"""
run_pipeline_local.py
---------------------
Runs the analytics layer of sql/recovery_queries.sql locally with DuckDB,
so the project can be reproduced without a Snowflake or Databricks account.

  * Loads the CSVs from data/output/ as tables
  * Executes the REFERENCE DATA and ANALYTICS VIEWS sections of the SQL file
    (only DATEADD is translated; everything else runs as written)
  * Exports every view to data/output/powerbi/ for the Power BI dashboard
  * Prints the headline leakage metrics

Usage:
    python scripts/run_pipeline_local.py
"""

from __future__ import annotations

import os
import re

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "output")
SQL_FILE = os.path.join(ROOT, "sql", "recovery_queries.sql")
PBI_OUT = os.path.join(DATA, "powerbi")

SOURCE_TABLES = {
    "CLAIMS_MASTER": "claims_master.csv",
    "ADJUSTER_NOTES": "adjuster_notes.csv",
    "FINANCIAL_RESERVES": "financial_reserves.csv",
    "NLP_CLAIM_INDICATORS": "nlp_claim_indicators.csv",
}

EXPORT_VIEWS = [
    "VW_SUBRO_BASE",
    "VW_LEAKAGE_CLAIMS",
    "VW_LEAKAGE_BY_SEGMENT",
    "VW_ADJUSTER_LEAKAGE",
    "VW_TRIAGE_QUEUE",
    "VW_LEAKAGE_TREND",
]


def extract_section(sql: str, start: str, end: str) -> str:
    m = re.search(rf"--\s*{start}(.*?)--\s*{end}", sql, flags=re.S)
    if not m:
        raise ValueError(f"Section markers {start}/{end} not found in {SQL_FILE}")
    return m.group(1)


def to_duckdb(sql: str) -> str:
    """Minimal Snowflake -> DuckDB translation."""
    sql = re.sub(r"DATEADD\(\s*year\s*,\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
                 r"CAST((\2) + to_years(CAST(\1 AS INTEGER)) AS DATE)", sql, flags=re.I)
    sql = re.sub(r"\bNUMBER\(", "DECIMAL(", sql)
    return sql


def run_statements(con: duckdb.DuckDBPyConnection, sql: str) -> None:
    # strip comments, then split on semicolons
    cleaned = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
    cleaned = re.sub(r"--[^\n]*", "", cleaned)
    for stmt in (s.strip() for s in cleaned.split(";")):
        if stmt:
            con.execute(stmt)


def main() -> None:
    missing = [f for f in SOURCE_TABLES.values() if not os.path.exists(os.path.join(DATA, f))]
    if missing:
        raise SystemExit(f"Missing inputs {missing}. Run the generator and text engine first.")

    con = duckdb.connect()
    for table, fname in SOURCE_TABLES.items():
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_csv_auto('{os.path.join(DATA, fname)}', header=true)")

    sql = open(SQL_FILE, encoding="utf-8").read()
    run_statements(con, to_duckdb(extract_section(sql, "@@REFDATA_START", "@@REFDATA_END")))
    run_statements(con, to_duckdb(extract_section(sql, "@@ANALYTICS_START", "@@ANALYTICS_END")))

    os.makedirs(PBI_OUT, exist_ok=True)
    for view in EXPORT_VIEWS:
        path = os.path.join(PBI_OUT, f"{view.lower()}.csv")
        con.execute(f"COPY (SELECT * FROM {view}) TO '{path}' (HEADER, DELIMITER ',')")

    h = con.execute("""
        SELECT
            (SELECT COUNT(*) FROM CLAIMS_MASTER),
            (SELECT COUNT(*) FROM VW_LEAKAGE_CLAIMS),
            (SELECT SUM(EST_LEAKAGE) FROM VW_LEAKAGE_CLAIMS),
            (SELECT SUM(EST_LEAKAGE) FROM VW_LEAKAGE_CLAIMS WHERE RECOVERY_WINDOW = 'Recoverable - SOL open'),
            (SELECT COUNT(*) FROM CLAIMS_MASTER WHERE CLAIM_STATUS = 'Open'),
            (SELECT COUNT(*) FROM VW_TRIAGE_QUEUE),
            (SELECT COUNT(*) FROM VW_TRIAGE_QUEUE WHERE TRIAGE_TIER LIKE 'P1%'),
            (SELECT SUM(EST_RECOVERY_VALUE) FROM VW_TRIAGE_QUEUE)
    """).fetchone()

    print("=" * 60)
    print(" SUBROGATION LEAKAGE - HEADLINE METRICS (synthetic data)")
    print("=" * 60)
    print(f" Claims in portfolio          {h[0]:>14,}")
    print(f" Closed claims with leakage   {h[1]:>14,}")
    print(f" Estimated leakage            ${h[2]:>13,.0f}")
    print(f"   of which SOL still open    ${h[3]:>13,.0f}")
    print(f" Open claim inventory         {h[4]:>14,}")
    print(f" Triage queue (open, unflagged){h[5]:>13,}  ({h[5] / h[4]:.1%} of open inventory)")
    print(f"   P1 - Pursue Now            {h[6]:>14,}")
    print(f" Est. recovery value in queue ${h[7]:>13,.0f}")
    print("-" * 60)
    print(" Top leakage segments:")
    for row in con.execute("""
        SELECT SEGMENT_RANK, LINE_OF_BUSINESS, CATEGORY_LABEL, LEAKED_CLAIMS, EST_LEAKAGE, SHARE_OF_TOTAL_LEAKAGE
        FROM VW_LEAKAGE_BY_SEGMENT ORDER BY SEGMENT_RANK LIMIT 5""").fetchall():
        print(f"  {row[0]}. {row[1]:<20} {row[2]:<36} {row[3]:>4} claims  ${row[4]:>11,.0f}  ({row[5]:.1%})")

    recon = con.execute("""
        SELECT (SELECT ROUND(SUM(AMOUNT),2) FROM FINANCIAL_RESERVES WHERE TRANSACTION_TYPE='PAYMENT'),
               (SELECT ROUND(SUM(TOTAL_PAID),2) FROM VW_CLAIM_FINANCIALS)""").fetchone()
    status = "OK" if abs(recon[0] - recon[1]) < 1 else "MISMATCH"
    print("-" * 60)
    print(f" Ledger vs view paid reconciliation: {status}")
    print(f" Power BI extracts written to: {PBI_OUT}")


if __name__ == "__main__":
    main()
