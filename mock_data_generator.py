"""
mock_data_generator.py
----------------------
Generates a synthetic Commercial P&C claims portfolio for the
Subrogation Engine & Leakage Analyzer project.

Outputs (written to data/output/):
    claims_master.csv        -> CLAIMS_MASTER
    adjuster_notes.csv       -> ADJUSTER_NOTES
    financial_reserves.csv   -> FINANCIAL_RESERVES (transaction ledger)
    ground_truth_subro.csv   -> evaluation only, NOT loaded to the warehouse

All data is fictitious. Names are produced by Faker and do not refer to real
insureds, claimants, or carriers.

Usage:
    python data/mock_data_generator.py                 # 50,000 claims
    python data/mock_data_generator.py --claims 5000   # smaller run
"""

from __future__ import annotations

import argparse
import os
from datetime import date, timedelta

import numpy as np
import pandas as pd
from faker import Faker

AS_OF_DATE = date(2026, 9, 30)
PORTFOLIO_START = date(2021, 1, 1)

STATES = ["CA", "TX", "FL", "NY", "IL", "GA", "OH", "PA", "NJ", "NC", "AZ", "WA"]
STATE_WEIGHTS = [0.14, 0.13, 0.11, 0.10, 0.08, 0.07, 0.07, 0.07, 0.06, 0.06, 0.05, 0.06]

# Line of business -> (probability, severity median, lognormal sigma)
LOB_CONFIG = {
    "Commercial Auto": (0.40, 7_500, 0.95),
    "Commercial Property": (0.35, 18_000, 1.10),
    "General Liability": (0.15, 12_000, 1.20),
    "Inland Marine": (0.10, 9_000, 0.90),
}

# loss cause -> (weight within LOB, probability of genuine third-party liability)
LOSS_CAUSES = {
    "Commercial Auto": {
        "Rear-End Collision": (0.24, 0.85),
        "Intersection Collision": (0.20, 0.45),
        "Sideswipe / Lane Change": (0.12, 0.40),
        "Single Vehicle": (0.16, 0.02),
        "Parking Lot": (0.14, 0.20),
        "Glass": (0.08, 0.03),
        "Theft": (0.06, 0.01),
    },
    "Commercial Property": {
        "Water Damage": (0.30, 0.28),
        "Equipment Breakdown": (0.18, 0.45),
        "Fire": (0.14, 0.20),
        "Electrical": (0.10, 0.35),
        "Wind / Hail": (0.20, 0.03),
        "Theft / Vandalism": (0.08, 0.02),
    },
    "General Liability": {
        "Slip and Fall": (0.45, 0.15),
        "Product Liability": (0.25, 0.55),
        "Premises Damage": (0.30, 0.20),
    },
    "Inland Marine": {
        "Cargo Damage": (0.55, 0.40),
        "Equipment Theft": (0.30, 0.02),
        "Contractor Equipment Damage": (0.15, 0.25),
    },
}

# loss cause -> possible subrogation categories when liability exists
CAUSE_TO_CATEGORY = {
    "Rear-End Collision": {"auto_rear_end": 1.0},
    "Intersection Collision": {"auto_third_party_fault": 1.0},
    "Sideswipe / Lane Change": {"auto_third_party_fault": 1.0},
    "Parking Lot": {"auto_third_party_fault": 1.0},
    "Single Vehicle": {"auto_third_party_fault": 1.0},
    "Glass": {"auto_third_party_fault": 1.0},
    "Theft": {"auto_third_party_fault": 1.0},
    "Water Damage": {"contractor_negligence": 0.6, "utility_third_party": 0.2, "product_defect": 0.2},
    "Equipment Breakdown": {"product_defect": 0.8, "contractor_negligence": 0.2},
    "Fire": {"product_defect": 0.5, "utility_third_party": 0.3, "contractor_negligence": 0.2},
    "Electrical": {"utility_third_party": 0.5, "product_defect": 0.5},
    "Wind / Hail": {"contractor_negligence": 1.0},
    "Theft / Vandalism": {"contractor_negligence": 1.0},
    "Slip and Fall": {"contractor_negligence": 1.0},
    "Product Liability": {"product_defect": 1.0},
    "Premises Damage": {"contractor_negligence": 0.7, "utility_third_party": 0.3},
    "Cargo Damage": {"carrier_liability": 1.0},
    "Equipment Theft": {"carrier_liability": 1.0},
    "Contractor Equipment Damage": {"contractor_negligence": 1.0},
}

# Liability language by category. The final phrase in each list is deliberately
# written in wording the NLP dictionary does NOT cover, so recall is realistic.
INDICATOR_PHRASES = {
    "auto_rear_end": [
        "Insured unit was rear-ended while stopped at a red light on {road}.",
        "Police report confirms insured vehicle was struck from behind by the other party.",
        "Dashcam footage shows third-party vehicle hit insured from behind at approx {mph} mph.",
        "Claimant REAR ENDED our insured's trailer in stop-and-go traffic.",
        "Other vehicle was tailgating and collided with the rear of insured's trailer.",
    ],
    "auto_third_party_fault": [
        "Police report indicates the other driver ran a red light at {road}.",
        "Claimant driver was cited for failure to yield.",
        "Witness statement confirms other driver at fault.",
        "Other driver cited by police for unsafe lane change.",
        "Other party drifted across the center line per witness account.",
    ],
    "product_defect": [
        "Engineer report attributes loss to a defective valve on the {equipment}.",
        "Cause and origin points to manufacturer malfunction in the {equipment}.",
        "Faulty wiring in the manufacturer-installed {equipment} identified as ignition source.",
        "{equipment} is subject to an open product recall per OEM bulletin.",
        "Fitting on the {equipment} appears to have failed prematurely; retained for exam.",
    ],
    "contractor_negligence": [
        "Water intrusion traced to improper installation by roofing subcontractor.",
        "HVAC contractor left condensate line disconnected after service call.",
        "Loss caused by contractor error during tenant build-out.",
        "Snow removal vendor failed to treat walkway per service contract.",
        "Joint separated on piping work completed last month by outside vendor.",
    ],
    "utility_third_party": [
        "Loss originated from a water main break owned by the municipal utility.",
        "Fire spread from neighboring tenant space.",
        "Power surge from utility company transformer failure damaged equipment.",
        "Adjacent building owner's drainage overflowed into insured premises.",
    ],
    "carrier_liability": [
        "Freight damaged in transit by third-party carrier; bill of lading shows exceptions.",
        "Carrier mishandled cargo during cross-dock transfer.",
        "Warehouse operator stacked pallets beyond rated height, collapse followed.",
    ],
}
# Weight on the last (un-dictionaried) phrase is lower so most hits are detectable.
PHRASE_PICK_WEIGHTS_LAST = 0.12

# Language that looks like liability but is not (insured at fault, ruled out, etc.)
NEGATIVE_PHRASES = [
    "Insured driver rear-ended the claimant vehicle; insured at fault.",
    "Single vehicle accident, no third party involved.",
    "Inspection of the valve found no evidence of defect; normal wear and tear.",
    "No third party liability identified after review of the police report.",
    "Damage caused by insured's own employee while operating forklift.",
    "Expert ruled out manufacturer defect; loss due to lack of maintenance.",
    "Insured vehicle struck a parked car; insured cited.",
]

# Liability-sounding language on files with NO real recovery potential.
# These are intended to produce realistic false positives for the NLP engine.
AMBIGUOUS_PHRASES = {
    "Commercial Auto": [
        "Liability disputed, other driver cited by police for no proof of insurance only.",
        "Checked for product recall on the unit's brake system; confirmation pending from OEM.",
    ],
    "Commercial Property": [
        "HVAC contractor retained by insured to perform emergency mitigation.",
        "Power surge suspected; awaiting utility records.",
        "Roofing contractor provided repair estimate for damaged section.",
        "Checked for product recall on the unit; confirmation pending from OEM.",
    ],
    "General Liability": [
        "Subcontractor on site at time of loss, statement requested.",
        "Plumbing contractor provided invoice for repairs.",
    ],
    "Inland Marine": [
        "Freight noted as damaged in transit; packaging was insured's responsibility.",
        "Subcontractor operating the equipment was insured's own employee.",
    ],
}

NEUTRAL_NOTES = [
    "Contacted insured, awaiting repair estimate.",
    "Reviewed photos and estimate. Scope appears consistent with loss.",
    "Recorded statement obtained from insured.",
    "Independent appraiser assigned.",
    "Reserve reviewed; no change at this time.",
    "Left voicemail for insured contact. Will follow up.",
    "Received invoice from repair vendor, forwarded for payment.",
    "Coverage confirmed under policy. Deductible applies.",
    "Site inspection scheduled with field adjuster.",
    "Rental coverage extended per policy limits.",
    "Mitigation vendor on site, drying equipment placed.",
    "Supplement received and reviewed.",
]

ROADS = ["I-10", "I-95", "Route 9", "Main St", "5th Ave", "Hwy 290", "I-75", "Elm St", "Industrial Pkwy"]
EQUIPMENT = ["sprinkler riser", "boiler", "rooftop HVAC unit", "compressor", "water heater",
             "electrical panel", "refrigeration unit", "forklift charger"]

FNOL_BY_LOB = {
    "Commercial Auto": "FNOL received from fleet manager. Unit #{unit} involved in {cause} loss. {injury}",
    "Commercial Property": "FNOL received from property manager reporting {cause} loss at insured location. {injury}",
    "General Liability": "FNOL: claim reported by insured regarding {cause} incident at premises. {injury}",
    "Inland Marine": "FNOL: insured reports {cause} involving scheduled equipment/cargo. {injury}",
}


def weighted_choice(rng: np.random.Generator, mapping: dict) -> str:
    keys = list(mapping.keys())
    w = np.array(list(mapping.values()), dtype=float)
    return keys[rng.choice(len(keys), p=w / w.sum())]


def pick_indicator(rng: np.random.Generator, category: str) -> str:
    phrases = INDICATOR_PHRASES[category]
    n = len(phrases)
    w = np.full(n, (1 - PHRASE_PICK_WEIGHTS_LAST) / (n - 1))
    w[-1] = PHRASE_PICK_WEIGHTS_LAST
    phrase = phrases[rng.choice(n, p=w)]
    return phrase.format(road=rng.choice(ROADS), mph=int(rng.integers(10, 40)),
                         equipment=rng.choice(EQUIPMENT))


def generate(n_claims: int, seed: int, out_dir: str) -> None:
    rng = np.random.default_rng(seed)
    fake = Faker("en_US")
    Faker.seed(seed)

    insured_pool = [fake.unique.company() for _ in range(min(6000, n_claims))]
    adjusters = [f"ADJ{str(i).zfill(3)}" for i in range(1, 61)]
    adjuster_names = {a: fake.name() for a in adjusters}
    # Some adjusters carry heavier caseloads and miss subrogation more often.
    adjuster_miss_rate = {a: float(rng.choice([0.04, 0.06, 0.08, 0.14], p=[0.3, 0.35, 0.2, 0.15]))
                          for a in adjusters}

    lobs = list(LOB_CONFIG.keys())
    lob_p = np.array([LOB_CONFIG[l][0] for l in lobs])

    total_days = (AS_OF_DATE - timedelta(days=15) - PORTFOLIO_START).days

    claims, notes, ledger, truth = [], [], [], []
    note_seq, txn_seq = 1, 1

    for i in range(1, n_claims + 1):
        claim_id = f"CLM{str(i).zfill(7)}"
        lob = lobs[rng.choice(len(lobs), p=lob_p)]
        causes = LOSS_CAUSES[lob]
        cause = weighted_choice(rng, {c: v[0] for c, v in causes.items()})
        p_liab = causes[cause][1]

        has_subro = rng.random() < p_liab
        category = weighted_choice(rng, CAUSE_TO_CATEGORY[cause]) if has_subro else None

        loss_date = PORTFOLIO_START + timedelta(days=int(rng.integers(0, total_days)))
        report_date = min(loss_date + timedelta(days=int(rng.exponential(5))), AS_OF_DATE)
        age_days = (AS_OF_DATE - report_date).days

        # Close probability rises with claim age
        close_lag = int(rng.gamma(2.2, 55)) + 10
        close_date = report_date + timedelta(days=close_lag)
        if close_date > AS_OF_DATE or (age_days > 365 and rng.random() < 0.04):
            status, close_date = "Open", None
        else:
            status = "Closed"

        adjuster = rng.choice(adjusters)
        state = STATES[rng.choice(len(STATES), p=STATE_WEIGHTS)]

        # Subrogation flag: missed opportunities depend on the adjuster's caseload
        if has_subro:
            if status == "Open":
                # Open files are less likely to have been referred yet
                flagged = rng.random() > (adjuster_miss_rate[adjuster] * 4.0)
            else:
                flagged = rng.random() > adjuster_miss_rate[adjuster]
        else:
            flagged = rng.random() < 0.01
        subro_flag = "Yes" if flagged else "No"

        # ---------------- financials ----------------
        _, median, sigma = LOB_CONFIG[lob]
        ultimate = float(min(np.exp(np.log(median) + sigma * rng.standard_normal()), 750_000))
        ultimate = round(max(ultimate, 250.0), 2)
        expense_ratio = float(rng.uniform(0.05, 0.18))

        initial_reserve = round(ultimate * float(rng.uniform(0.6, 1.4)), 2)
        ledger.append((f"TXN{str(txn_seq).zfill(8)}", claim_id, report_date, "RESERVE", "Indemnity", initial_reserve))
        txn_seq += 1

        end_date = close_date if close_date else AS_OF_DATE
        span = max((end_date - report_date).days, 1)
        n_pay = int(rng.integers(1, 5))
        paid_share = 1.0 if status == "Closed" else float(rng.uniform(0.0, 0.8))
        pay_total = round(ultimate * paid_share, 2)
        splits = rng.dirichlet(np.ones(n_pay)) * pay_total
        pay_days = sorted(rng.integers(1, span + 1, size=n_pay))
        running_paid = 0.0
        for amt, d in zip(splits, pay_days):
            amt = round(float(amt), 2)
            if amt <= 0:
                continue
            running_paid += amt
            ledger.append((f"TXN{str(txn_seq).zfill(8)}", claim_id, report_date + timedelta(days=int(d)),
                           "PAYMENT", "Indemnity", amt))
            txn_seq += 1

        expense_paid = round(ultimate * expense_ratio * (1.0 if status == "Closed" else paid_share), 2)
        if expense_paid > 0:
            ledger.append((f"TXN{str(txn_seq).zfill(8)}", claim_id,
                           report_date + timedelta(days=int(rng.integers(1, span + 1))),
                           "PAYMENT", "Expense", expense_paid))
            txn_seq += 1

        # Reserve true-up: closed files reserve down to paid; open files reserve to ultimate
        target_reserve = running_paid if status == "Closed" else ultimate
        adj = round(target_reserve - initial_reserve, 2)
        if abs(adj) >= 0.01:
            ledger.append((f"TXN{str(txn_seq).zfill(8)}", claim_id, end_date, "RESERVE", "Indemnity", adj))
            txn_seq += 1

        # Recoveries on files that were actually pursued
        if subro_flag == "Yes" and status == "Closed" and has_subro and rng.random() < 0.7:
            rec = round(running_paid * float(rng.uniform(0.3, 0.9)), 2)
            rec_date = min(end_date + timedelta(days=int(rng.integers(0, 60))), AS_OF_DATE)
            ledger.append((f"TXN{str(txn_seq).zfill(8)}", claim_id, rec_date,
                           "RECOVERY", "Subrogation", rec))
            txn_seq += 1

        # ---------------- adjuster notes ----------------
        note_dates = sorted({report_date + timedelta(days=int(d))
                             for d in rng.integers(0, span + 1, size=int(rng.integers(2, 6)))})
        note_dates = [report_date] + [d for d in note_dates if d != report_date]

        injury = rng.choice(["No injuries reported.", "Minor injury reported by claimant.", "No injuries."])
        claim_notes = [FNOL_BY_LOB[lob].format(unit=int(rng.integers(100, 999)), cause=cause.lower(), injury=injury)]

        body = []
        if has_subro and rng.random() < 0.92:
            body.append(pick_indicator(rng, category))
            if rng.random() < 0.25:  # corroborating second note
                body.append(pick_indicator(rng, category))
        elif not has_subro:
            r = rng.random()
            if r < 0.10:
                body.append(rng.choice(NEGATIVE_PHRASES))
            elif r < 0.125:
                body.append(rng.choice(AMBIGUOUS_PHRASES[lob]))

        filler_needed = max(len(note_dates) - 1 - len(body) - (1 if status == "Closed" else 0), 0)
        body += list(rng.choice(NEUTRAL_NOTES, size=filler_needed))
        rng.shuffle(body)
        claim_notes += body

        if status == "Closed":
            if subro_flag == "Yes":
                claim_notes.append("Subrogation referral sent to recovery unit. File closed.")
            else:
                claim_notes.append("Final payment issued. File closed.")

        # align notes to dates (extend dates if needed)
        while len(note_dates) < len(claim_notes):
            note_dates.append(note_dates[-1] + timedelta(days=int(rng.integers(1, 20))))
        for d, txt in zip(note_dates, claim_notes):
            d = min(d, close_date or AS_OF_DATE)
            notes.append((f"NTE{str(note_seq).zfill(8)}", claim_id, d, adjuster, txt))
            note_seq += 1

        claims.append((
            claim_id,
            f"POL{rng.integers(10_000_000, 99_999_999)}",
            rng.choice(insured_pool),
            lob, cause, state, loss_date, report_date, close_date, status,
            adjuster, adjuster_names[adjuster], subro_flag,
        ))
        truth.append((claim_id, int(has_subro), category))

    os.makedirs(out_dir, exist_ok=True)

    claims_df = pd.DataFrame(claims, columns=[
        "CLAIM_ID", "POLICY_NUMBER", "INSURED_NAME", "LINE_OF_BUSINESS", "LOSS_CAUSE", "LOSS_STATE",
        "LOSS_DATE", "REPORT_DATE", "CLOSE_DATE", "CLAIM_STATUS", "ADJUSTER_ID", "ADJUSTER_NAME",
        "SUBROGATION_FLAG"])
    notes_df = pd.DataFrame(notes, columns=["NOTE_ID", "CLAIM_ID", "NOTE_DATE", "ADJUSTER_ID", "NOTE_TEXT"])
    ledger_df = pd.DataFrame(ledger, columns=[
        "TRANSACTION_ID", "CLAIM_ID", "TRANSACTION_DATE", "TRANSACTION_TYPE", "COST_TYPE", "AMOUNT"])
    truth_df = pd.DataFrame(truth, columns=["CLAIM_ID", "TRUE_SUBRO_POTENTIAL", "TRUE_SUBRO_CATEGORY"])

    claims_df.to_csv(os.path.join(out_dir, "claims_master.csv"), index=False)
    notes_df.to_csv(os.path.join(out_dir, "adjuster_notes.csv"), index=False)
    ledger_df.to_csv(os.path.join(out_dir, "financial_reserves.csv"), index=False)
    truth_df.to_csv(os.path.join(out_dir, "ground_truth_subro.csv"), index=False)

    print(f"Claims:        {len(claims_df):>9,}")
    print(f"Adjuster notes:{len(notes_df):>9,}")
    print(f"Transactions:  {len(ledger_df):>9,}")
    print(f"Output folder: {os.path.abspath(out_dir)}")


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="Generate synthetic commercial P&C claims data.")
    parser.add_argument("--claims", type=int, default=50_000, help="Number of claims to generate")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument("--out", default=os.path.join(here, "output"), help="Output directory")
    args = parser.parse_args()
    generate(args.claims, args.seed, args.out)


if __name__ == "__main__":
    main()
