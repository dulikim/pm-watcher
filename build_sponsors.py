#!/usr/bin/env python3
"""
Regenerate sponsors.json from the Fortune 500 / USCIS H-1B spreadsheet.

Dev-only tool: it needs openpyxl, which check.py deliberately does not. The
committed sponsors.json is what the watcher actually reads, so the Action
never installs anything.

    python3 build_sponsors.py

Source: data/Fortune500_H1B_Sponsors_FY2025_FY2026.xlsx, "All F500" sheet.
A company counts as a sponsor when FY2025 total approvals + FY2026 YTD total
approvals > 0 — the same rule as the sheet's own "Sponsors?" formula, which is
stored as a formula and so can't be read back as a value.
"""

import json
import re
import sys

try:
    import openpyxl
except ImportError:
    sys.exit("needs openpyxl: pip3 install openpyxl")

XLSX = "data/Fortune500_H1B_Sponsors_FY2025_FY2026.xlsx"
OUT = "sponsors.json"

# Column indices in the "All F500" sheet (0-based).
COL_RANK, COL_COMPANY = 0, 1
COL_FY25_TOTAL, COL_FY26_TOTAL = 4, 8

# Legal names in the Fortune list vs. what a job posting actually says.
# Only aliases that change the match are listed; normalize() already handles
# suffix noise like "Inc", "Corp", "Holdings", "Group".
ALIASES = {
    "google": "Alphabet",
    "youtube": "Alphabet",
    "waymo": "Alphabet",
    "meta": "Meta Platforms",
    "facebook": "Meta Platforms",
    "instagram": "Meta Platforms",
    "whatsapp": "Meta Platforms",
    "ibm": "International Business Machines",
    "amd": "Advanced Micro Devices",
    "disney": "Walt Disney",
    "espn": "Walt Disney",
    "hulu": "Walt Disney",
    "jpmorgan": "JPMorgan Chase",
    "jp morgan": "JPMorgan Chase",
    "chase": "JPMorgan Chase",
    "goldman sachs": "Goldman Sachs Group",
    "aws": "Amazon",
    "amazon web services": "Amazon",
    "twitch": "Amazon",
    "whole foods": "Amazon",
    "zappos": "Amazon",
    "audible": "Amazon",
    "linkedin": "Microsoft",
    "github": "Microsoft",
    "xbox": "Microsoft",
    "azure": "Microsoft",
    "aetna": "CVS Health",
    "optum": "UnitedHealth Group",
    "geico": "Berkshire Hathaway",
    "bnsf": "Berkshire Hathaway",
    "exxon": "ExxonMobil Holdings",
    "exxon mobil": "ExxonMobil Holdings",
    "mobil": "ExxonMobil Holdings",
    "at t": "AT&T",
    "att": "AT&T",
    "ups": "United Parcel Service",
    "fedex": "FedEx",
    "pwc": None,  # not F500-listed; here so the miss is deliberate, not a typo
    "bny mellon": "Bank of New York (BNY)",
    "bank of new york mellon": "Bank of New York (BNY)",
    "bny": "Bank of New York (BNY)",
    "us bank": "U.S. Bancorp",
    "usbank": "U.S. Bancorp",
    "hpe": "Hewlett Packard Enterprise",
    "hewlett packard": "HP",
    "p g": "Procter & Gamble",
    "procter and gamble": "Procter & Gamble",
    "j j": "Johnson & Johnson",
    "johnson and johnson": "Johnson & Johnson",
    "jnj": "Johnson & Johnson",
    "bms": "Bristol-Myers Squibb",
    "bristol myers squibb": "Bristol-Myers Squibb",
    "3m": "3M",
    "adp": "Automatic Data Processing",
    "ti": "Texas Instruments",
    "ge aerospace": "GE Aerospace",
    "ge healthcare": "GE HealthCare Technologies",
    "ge vernova": "GE Vernova",
    "cognizant": "Cognizant Technology Solutions",
    "paramount": "Paramount Skydance",
    "warner bros": "Warner Bros. Discovery",
    "hbo": "Warner Bros. Discovery",
    "square": "Block",
    "cash app": "Block",
    "paypal": "PayPal Holdings",
    "venmo": "PayPal Holdings",
    "uber": "Uber Technologies",
    "booking": "Booking Holdings",
    "priceline": "Booking Holdings",
    "kayak": "Booking Holdings",
    "american airlines": "American Airlines Group",
    "united airlines": "United Airlines Holdings",
    "delta": "Delta Air Lines",
    "capital one": "Capital One Financial",
    "schwab": "Charles Schwab",
    "charles schwab": "Charles Schwab",
    "state farm": None,  # private; not in the public F500 list
    "lockheed": "Lockheed Martin",
    "l3harris": "L3Harris Technologies",
    "rtx": "RTX",
    "raytheon": "RTX",
    "micron": "Micron Technology",
    "applied materials": "Applied Materials",
    "lam": "Lam Research",
    "dell": "Dell Technologies",
    "cisco": "Cisco Systems",
    "verizon": "Verizon Communications",
    "comcast": "Comcast",
    "nbcuniversal": "Comcast",
    "charter": "Charter Communications",
    "spectrum": "Charter Communications",
    "mondelez": "Mondelez International",
    "philip morris": "Philip Morris International",
    "marriott": "Marriott International",
    "mgm": "MGM Resorts International",
    "jll": "Jones Lang LaSalle",
    "cbre": "CBRE Group",
    "grainger": "W.W. Grainger",
    "deere": "Deere",
    "john deere": "Deere",
    "caterpillar": "Caterpillar",
    "cat": "Caterpillar",
}

# Dropped when normalizing, so "Nvidia Corp" == "Nvidia".
_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "llc", "lp",
    "ltd", "limited", "plc", "holdings", "holding", "group", "the", "and",
}


def normalize(name):
    """Lowercase, strip punctuation and corporate suffixes, collapse spaces."""
    s = (name or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    words = [w for w in s.split() if w and w not in _SUFFIXES]
    return " ".join(words)


def main():
    wb = openpyxl.load_workbook(XLSX, data_only=False)
    rows = list(wb["All F500"].iter_rows(min_row=2, values_only=True))

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    sponsors, non_sponsors = [], []
    for r in rows:
        company = r[COL_COMPANY]
        if not company:
            continue
        total = num(r[COL_FY25_TOTAL]) + num(r[COL_FY26_TOTAL])
        (sponsors if total > 0 else non_sponsors).append(company)

    # Normalized lookup keys. Aliases resolve to a canonical Fortune name, which
    # must itself be a sponsor to count.
    sponsor_names = set(sponsors)
    keys = {normalize(c) for c in sponsors}
    for alias, canonical in ALIASES.items():
        if canonical and canonical in sponsor_names:
            keys.add(normalize(alias))

    payload = {
        "_comment": "Generated by build_sponsors.py — do not hand-edit. "
                    "Fortune 500 (2026) x USCIS H-1B approvals, FY2025 + FY2026 YTD.",
        "source": XLSX,
        "sponsor_count": len(sponsors),
        "non_sponsor_count": len(non_sponsors),
        "keys": sorted(keys),
        "non_sponsors": sorted(non_sponsors),
    }
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=1)
        f.write("\n")

    print(f"{len(sponsors)} sponsors, {len(non_sponsors)} non-sponsors, "
          f"{len(keys)} lookup keys -> {OUT}")


if __name__ == "__main__":
    main()
