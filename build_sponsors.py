#!/usr/bin/env python3
"""
Regenerate sponsors.json — every U.S. employer with an H-1B approval record.

Dev-only tool: it needs openpyxl, which check.py deliberately does not. The
committed sponsors.json is what the watcher reads, so the Action installs
nothing.

    python3 build_sponsors.py

Two sources, unioned:

  data/All_H1B_Sponsors_FY2025_FY2026YTD.xlsx  "All sponsors" sheet
      ~62k employers, FY2025 totals plus FY2026 YTD as of an April 2026
      compilation.

  data/uscis_employer_information_fy2026.csv   USCIS hub export, FY2026
      ~44k employers, UTF-16 tab-separated. Newer FY2026 data than the xlsx,
      and it carries ~15k employers the xlsx never had.

Petition count per employer:

    FY2025 total  +  max(xlsx FY2026 YTD, csv FY2026 approvals)

The max avoids double-counting FY2026 while still preferring whichever source
saw more of the year. An employer present in only one source uses that source.
"""

import csv
import json
import re
import sys
from collections import defaultdict

try:
    import openpyxl
except ImportError:
    sys.exit("needs openpyxl: pip3 install openpyxl")

XLSX = "data/All_H1B_Sponsors_FY2025_FY2026YTD.xlsx"
CSV = "data/uscis_employer_information_fy2026.csv"
OUT = "sponsors.json"

# "All sponsors" sheet column indices (0-based).
X_NAME, X_FY25_TOTAL, X_FY26_TOTAL = 0, 6, 11

# USCIS hub CSV column indices (0-based). Every "* Approval" column counts as an
# approved petition; denials are ignored.
C_NAME = 2
C_APPROVALS = (8, 10, 12, 14, 16, 18)

# Acronyms and brand names a posting uses that no USCIS legal name starts with.
# Prefix matching handles the ordinary cases on its own -- "Google" already
# reaches GOOGLE LLC, "Meta" reaches META PLATFORMS INC -- so this list only
# covers what prefix matching genuinely cannot.
ALIASES = {
    "aws": "amazon web services",
    "amazon web services": "amazon web services",
    "twitch": "twitch interactive",
    "whole foods": "whole foods market",
    "audible": "audible",
    "instagram": "meta platforms",
    "whatsapp": "meta platforms",
    "facebook": "meta platforms",
    "youtube": "google",
    "waymo": "waymo",
    "alphabet": "google",
    "github": "github",
    "linkedin": "linkedin",
    "xbox": "microsoft",
    "azure": "microsoft",
    "ibm": "ibm",
    "amd": "advanced micro devices",
    "j j": "johnson johnson",
    "jnj": "johnson johnson",
    "p g": "procter gamble",
    "bms": "bristol myers squibb",
    "adp": "automatic data processing",
    "ti": "texas instruments",
    "hpe": "hewlett packard enterprise",
    "hp": "hp",
    "bny": "bank of new york mellon",
    "bny mellon": "bank of new york mellon",
    "jpmorgan": "jpmorgan chase",
    "jp morgan": "jpmorgan chase",
    "chase": "jpmorgan chase",
    "goldman": "goldman sachs",
    "disney": "walt disney",
    "espn": "espn",
    "hulu": "hulu",
    "geico": "geico",
    "optum": "optum",
    "aetna": "aetna",
    "exxon": "exxon mobil",
    "raytheon": "raytheon",
    "rtx": "raytheon",
    "ups": "united parcel service",
    "square": "block",
    "cash app": "block",
    "venmo": "paypal",
    "nbcuniversal": "nbcuniversal",
    "spectrum": "charter communications",
    "us bank": "us bank",
    "usbank": "us bank",
    "deloitte": "deloitte",
    "pwc": "pricewaterhousecoopers",
    "ey": "ernst young",
    "kpmg": "kpmg",
    "bcg": "boston consulting group",
    "mckinsey": "mckinsey company",
    "tcs": "tata consultancy services",
    "byte dance": "bytedance",
    "tiktok": "tiktok",
}

# Dropped when normalizing, so "NVIDIA CORPORATION" == "Nvidia". Must stay in
# sync with check.py, which uses the same rule; a test pins the two together.
_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "llc", "lp",
    "llp", "ltd", "limited", "plc", "holdings", "holding", "group", "the",
    "and", "usa", "us", "na",
}


# Must match check.py's _PARENTHETICAL; a test pins the two normalizers.
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)")


def normalize(name):
    """Lowercase, strip parentheticals, punctuation and corporate suffixes."""
    s = _PARENTHETICAL.sub("", (name or "")).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(w for w in s.split() if w and w not in _SUFFIXES)


def num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def read_xlsx():
    """{raw name: (fy2025_total, fy2026_ytd_total)}"""
    wb = openpyxl.load_workbook(XLSX, read_only=True)
    out = {}
    for r in wb["All sponsors"].iter_rows(min_row=2, values_only=True):
        if not r or not r[X_NAME]:
            continue
        name = str(r[X_NAME]).strip()
        fy25, fy26 = num(r[X_FY25_TOTAL]), num(r[X_FY26_TOTAL])
        prev = out.get(name, (0, 0))
        out[name] = (prev[0] + fy25, prev[1] + fy26)
    return out


def read_csv():
    """{raw name: fy2026_approvals}. Rows repeat per employer, so they sum."""
    out = defaultdict(int)
    with open(CSV, encoding="utf-16") as f:
        rd = csv.reader(f, delimiter="\t")
        next(rd, None)
        for row in rd:
            if len(row) <= max(C_APPROVALS) or not row[C_NAME].strip():
                continue
            out[row[C_NAME].strip()] += sum(num(row[i]) for i in C_APPROVALS)
    return dict(out)


def main():
    xlsx, csv_data = read_xlsx(), read_csv()

    # Union both sources, keyed on the normalized name so the same employer
    # filed under punctuation variants collapses into one entry.
    totals = defaultdict(int)
    for raw, (fy25, fy26) in xlsx.items():
        key = normalize(raw)
        if key:
            totals[key] += fy25 + max(fy26, csv_data.get(raw, 0))
    for raw, fy26 in csv_data.items():
        if raw in xlsx:
            continue  # already counted above, don't double it
        key = normalize(raw)
        if key:
            totals[key] += fy26

    sponsors = {k: v for k, v in totals.items() if v > 0}

    # Aliases point at a normalized name that must already be a real sponsor
    # prefix; resolved here so check.py can stay a plain lookup.
    alias_out = {}
    for alias, target in ALIASES.items():
        a, t = normalize(alias), normalize(target)
        if a and t and a != t:
            alias_out[a] = t

    payload = {
        "_comment": "Generated by build_sponsors.py - do not hand-edit. "
                    "USCIS H-1B approvals, FY2025 + FY2026 YTD.",
        "sources": [XLSX, CSV],
        "employer_count": len(sponsors),
        "petitions": sponsors,
        "aliases": alias_out,
    }
    with open(OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"), sort_keys=True)
        f.write("\n")

    top = sorted(sponsors.items(), key=lambda kv: -kv[1])[:10]
    print(f"{len(sponsors)} employers with >=1 approval -> {OUT}")
    print(f"  xlsx={len(xlsx)}  csv={len(csv_data)}  csv-only={len(set(csv_data) - set(xlsx))}")
    print("  top:", ", ".join(f"{k}={v}" for k, v in top[:5]))


if __name__ == "__main__":
    main()
