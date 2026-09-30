"""
text_engine.py
--------------
Heuristic NLP engine that scans unstructured adjuster diary notes for
third-party liability indicators and rolls them up to one row per claim.

Approach
  1. Split each note into sentences.
  2. Drop any sentence containing a negation / insured-at-fault pattern
     (e.g. "insured rear-ended the claimant", "no evidence of defect").
  3. Match the remaining sentences against a weighted regex dictionary,
     grouped by subrogation category.
  4. Aggregate to claim level: strongest hit, distinct categories,
     total hits, and a 0-100 INDICATOR_SCORE.

Output: data/output/nlp_claim_indicators.csv -> NLP_CLAIM_INDICATORS table.

Usage:
    python src/text_engine.py
    python src/text_engine.py --notes path/to/adjuster_notes.csv --out path/to/output.csv
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass

import pandas as pd

# ---------------------------------------------------------------------------
# Indicator dictionary: category -> list of (regex, weight 0-100)
# ---------------------------------------------------------------------------
INDICATORS: dict[str, list[tuple[str, int]]] = {
    "auto_rear_end": [
        (r"\brear[\s-]?ended\b", 85),
        (r"\bstruck from behind\b", 85),
        (r"\bhit (?:\w+\s){0,2}from behind\b", 80),
    ],
    "auto_third_party_fault": [
        (r"\bran (?:a |the )?red (?:light|signal)\b", 85),
        (r"\bfail(?:ed|ure) to yield\b", 80),
        (r"\bother (?:driver|party) (?:was )?(?:at fault|cited)\b", 85),
        (r"\b(?:claimant|other) driver (?:was )?cited\b", 80),
        (r"\bcited by police\b", 70),
    ],
    "product_defect": [
        (r"\bdefective (?:valve|part|component|product|unit)\b", 85),
        (r"\bmanufacturer(?:'s)? (?:malfunction|defect)\b", 85),
        (r"\bfaulty wiring\b", 70),
        (r"\bproduct recall\b", 75),
        (r"\bmanufacturer[\s-]installed\b", 45),
    ],
    "contractor_negligence": [
        (r"\bimproper(?:ly)? install(?:ation|ed)\b", 80),
        (r"\bsub-?contractor\b", 55),
        (r"\bcontractor error\b", 80),
        (r"\b(?:hvac|roofing|plumbing|electrical) contractor\b", 60),
        (r"\bvendor failed to\b", 70),
    ],
    "utility_third_party": [
        (r"\bwater main break\b", 70),
        (r"\bmunicipal utility\b", 60),
        (r"\bneighbou?ring tenant\b", 65),
        (r"\butility company\b", 55),
        (r"\bpower surge\b", 50),
    ],
    "carrier_liability": [
        (r"\bthird[\s-]party carrier\b", 80),
        (r"\bdamaged in transit\b", 70),
        (r"\bbill of lading\b.*\bexceptions?\b", 65),
        (r"\bcarrier mishandled\b", 80),
    ],
}

# Sentences matching any of these are removed before indicator matching.
NEGATIONS: list[str] = [
    r"\binsured(?:'s)?(?: \w+)? (?:rear[\s-]?ended|struck|hit)\b",
    r"\binsured (?:was )?(?:at fault|cited)\b",
    r"\bno third[\s-]party\b",
    r"\bno evidence of (?:a )?defect\b",
    r"\bruled out\b",
    r"\bsingle vehicle\b",
    r"\binsured's own\b",
    r"\bliability (?:is )?disputed\b",
]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+")


@dataclass(frozen=True)
class CompiledIndicator:
    category: str
    pattern: re.Pattern
    weight: int


COMPILED = [
    CompiledIndicator(cat, re.compile(rx, re.IGNORECASE), w)
    for cat, rules in INDICATORS.items()
    for rx, w in rules
]
COMPILED_NEG = [re.compile(rx, re.IGNORECASE) for rx in NEGATIONS]


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT.split(str(text).strip()) if s]


def scan_text(text: str) -> tuple[list[tuple[str, str, int]], bool]:
    """Return (hits, negated) for one note. hits = [(category, matched_phrase, weight)]."""
    hits: list[tuple[str, str, int]] = []
    negated = False
    for sentence in split_sentences(text):
        if any(n.search(sentence) for n in COMPILED_NEG):
            negated = True
            continue
        for ind in COMPILED:
            m = ind.pattern.search(sentence)
            if m:
                hits.append((ind.category, m.group(0).lower(), ind.weight))
    return hits, negated


def score_claim(hits: list[tuple[str, str, int]]) -> int:
    """0-100 score: strongest phrase + corroboration bonuses, capped at 100."""
    if not hits:
        return 0
    max_w = max(h[2] for h in hits)
    n_categories = len({h[0] for h in hits})
    extra_hits = len(hits) - 1
    return int(min(100, max_w + 5 * extra_hits + 5 * (n_categories - 1)))


def run_engine(notes: pd.DataFrame) -> pd.DataFrame:
    """Scan every note and aggregate to one row per claim."""
    rows = []
    for claim_id, grp in notes.groupby("CLAIM_ID", sort=True):
        all_hits: list[tuple[str, str, int]] = []
        any_neg = False
        first_hit_date = None
        for note_date, text in zip(grp["NOTE_DATE"], grp["NOTE_TEXT"]):
            hits, neg = scan_text(text)
            any_neg = any_neg or neg
            if hits and first_hit_date is None:
                first_hit_date = note_date
            all_hits.extend(hits)

        if all_hits:
            top = max(all_hits, key=lambda h: h[2])
            # primary category = category with highest summed weight
            cat_weight: dict[str, int] = {}
            for c, _, w in all_hits:
                cat_weight[c] = cat_weight.get(c, 0) + w
            primary = max(cat_weight, key=cat_weight.get)
            phrases = "; ".join(sorted({h[1] for h in all_hits}))
        else:
            top, primary, phrases = None, None, None

        rows.append({
            "CLAIM_ID": claim_id,
            "INDICATOR_SCORE": score_claim(all_hits),
            "PRIMARY_CATEGORY": primary,
            "TOP_PHRASE": top[1] if top else None,
            "MATCHED_PHRASES": phrases,
            "HIT_COUNT": len(all_hits),
            "DISTINCT_CATEGORIES": len({h[0] for h in all_hits}),
            "NEGATION_FLAG": "Y" if any_neg else "N",
            "FIRST_INDICATOR_DATE": first_hit_date,
            "NOTES_SCANNED": len(grp),
        })
    return pd.DataFrame(rows)


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_in = os.path.join(root, "data", "output", "adjuster_notes.csv")
    default_out = os.path.join(root, "data", "output", "nlp_claim_indicators.csv")

    parser = argparse.ArgumentParser(description="Scan adjuster notes for subrogation indicators.")
    parser.add_argument("--notes", default=default_in)
    parser.add_argument("--out", default=default_out)
    args = parser.parse_args()

    notes = pd.read_csv(args.notes)
    result = run_engine(notes)
    result.to_csv(args.out, index=False)

    flagged = (result["INDICATOR_SCORE"] > 0).sum()
    print(f"Notes scanned:          {len(notes):>9,}")
    print(f"Claims scored:          {len(result):>9,}")
    print(f"Claims with indicators: {flagged:>9,}")
    print(f"Output: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
