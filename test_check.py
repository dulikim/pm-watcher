#!/usr/bin/env python3
"""
Tests for the watcher's filters, with the sponsorship filter as the focus.

    python3 -m unittest -v          (or just: python3 test_check.py)

Stdlib unittest only, matching check.py's no-dependencies rule, so CI can run
this without installing anything.
"""

import json
import unittest

import check


def job(company="Nvidia", title="Product Manager Intern", locations=None, **kw):
    j = {
        "id": kw.get("id", f"{company}|{title}"),
        "company_name": company,
        "title": title,
        "locations": ["San Jose, CA"] if locations is None else locations,
        "url": "https://example.com/job",
        "date_posted": 0,          # unknown -> is_fresh lets it through
        "role_type": "internship",
    }
    j.update(kw)
    return j


class TestNormalizeCompany(unittest.TestCase):
    def test_strips_parentheticals(self):
        self.assertEqual(check.normalize_company("Acme (ACM)"), "acme")
        self.assertEqual(check.normalize_company("Bank of New York (BNY)"),
                         "bank of new york")

    def test_strips_suffixes_and_punctuation(self):
        for raw, want in [
            ("Nvidia Corp", "nvidia"),
            ("Nvidia Corporation", "nvidia"),
            ("Uber Technologies, Inc.", "uber technologies"),
            ("The Walt Disney Company", "walt disney"),
            ("Lowe's", "lowe s"),
            ("AT&T", "at t"),
            ("Johnson & Johnson", "johnson johnson"),
            ("  Micron   Technology  ", "micron technology"),
        ]:
            self.assertEqual(check.normalize_company(raw), want, raw)

    def test_handles_empty(self):
        self.assertEqual(check.normalize_company(""), "")
        self.assertEqual(check.normalize_company(None), "")



class TestPetitionsFor(unittest.TestCase):
    def test_known_heavy_filers(self):
        for company, floor in [
            ("Amazon", 5000), ("Microsoft", 2000), ("Google", 1000),
            ("Nvidia", 1000), ("Apple", 1000), ("Meta", 1000),
        ]:
            self.assertGreaterEqual(check.petitions_for(job(company)), floor, company)

    def test_mid_size_sponsors_are_found(self):
        """The whole point of dropping the Fortune 500 gate: these are real
        sponsors that the F500-only list threw away."""
        for company in [
            "Datadog", "IXL Learning", "Databricks", "Stripe",
            "Roblox", "Atlassian", "Appian", "Qorvo", "Clearwater Analytics",
        ]:
            self.assertGreater(check.petitions_for(job(company)), 0, company)

    def test_sums_sibling_entities(self):
        """One company files under many legal entities; the total is the answer.
        Visa's own VISA INC row is tiny, so taking the exact hit alone reported
        2 petitions for a company that files over a thousand."""
        self.assertGreater(check.petitions_for(job("Visa")), 500)
        self.assertGreater(check.petitions_for(job("Amazon")), 10000)

    def test_prefix_match_is_whole_word(self):
        """'Meta' must not collect METAPICKS; 'Block' must not eat BLOCKCHAIN."""
        meta = check.petitions_for(job("Meta"))
        block = check.petitions_for(job("Block"))
        self.assertGreater(meta, 1000)      # META PLATFORMS is really there
        self.assertLess(block, 1000)        # but Block stays Block-sized
        self.assertEqual(check.petitions_for(job("Metapickle")), 0)

    def test_aliases_resolve(self):
        for company in ["AWS", "IBM", "AMD", "Raytheon", "JPMorgan"]:
            self.assertGreater(check.petitions_for(job(company)), 0, company)

    def test_parenthetical_alias_is_stripped(self):
        """Feeds write 'PricewaterhouseCoopers (PwC)'; the paren text is a
        second name, and leaving it in matched nothing at all."""
        self.assertGreater(
            check.petitions_for(job("PricewaterhouseCoopers (PwC)")), 0)
        self.assertEqual(
            check.petitions_for(job("PricewaterhouseCoopers (PwC)")),
            check.petitions_for(job("PricewaterhouseCoopers")))

    def test_legal_suffix_variants(self):
        base = check.petitions_for(job("Nvidia"))
        for variant in ["Nvidia Corp", "NVIDIA CORPORATION", "Nvidia, Inc."]:
            self.assertEqual(check.petitions_for(job(variant)), base, variant)

    def test_genuine_non_sponsors(self):
        """Zero in the USCIS data, verified against the raw source -- not a
        name-matching miss. Defense clearances explain Northrop."""
        for company in ["Northrop Grumman", "Chipotle Mexican Grill"]:
            self.assertEqual(check.petitions_for(job(company)), 0, company)

    def test_unknown_company_is_zero(self):
        self.assertEqual(check.petitions_for(job("Totally Fake Nonexistent Co")), 0)

    def test_subsidiary_matches_parent(self):
        """Feeds name subsidiaries as 'X, A Y Company'; the parent files."""
        self.assertGreater(
            check.petitions_for(job("Progress Rail, A Caterpillar Company")), 0)

    def test_fails_open_when_data_missing(self):
        """A missing/corrupt sponsors.json must not drop every role."""
        saved = check.PETITIONS
        try:
            check.PETITIONS = None
            self.assertTrue(check.sponsors_h1b(job("Some Company Nobody Knows")))
        finally:
            check.PETITIONS = saved

    def test_min_petitions_threshold(self):
        saved = check.MIN_PETITIONS
        try:
            check.MIN_PETITIONS = 10_000
            self.assertTrue(check.sponsors_h1b(job("Amazon")))
            self.assertFalse(check.sponsors_h1b(job("Datadog")))
        finally:
            check.MIN_PETITIONS = saved


class TestH1bSummary(unittest.TestCase):
    def test_shows_a_grabbable_number(self):
        s = check.h1b_summary(job("Amazon"))
        self.assertIn("approved petitions", s)
        self.assertIn(",", s)                     # thousands separator
        self.assertIn("heavy filer", s)

    def test_bands(self):
        for company, band in [
            ("Amazon", "heavy filer"),            # 12,135
            ("Datadog", "files regularly"),       # 145
            ("IXL Learning", "files occasionally"),  # 34
        ]:
            self.assertIn(band, check.h1b_summary(job(company)), company)

    def test_no_record_is_stated_not_blank(self):
        self.assertEqual(
            check.h1b_summary(job("Totally Fake Nonexistent Co")),
            "no H-1B record found")

    def test_blank_for_non_us(self):
        """H-1B is moot abroad, so don't print a misleading zero."""
        self.assertEqual(check.h1b_summary(job("Tencent", locations=["London, UK"])), "")


class TestIsUs(unittest.TestCase):
    def test_us_locations(self):
        for loc in ["San Jose, CA", "New York, NY", "Austin, TX", "Remote", ""]:
            self.assertTrue(check.is_us(job(locations=[loc])), loc)

    def test_non_us_locations(self):
        for loc in [
            "London, UK", "Toronto, Canada", "Bangalore, India",
            "Shanghai, China", "Dublin, Ireland", "Tel Aviv, Israel",
            "Sydney, Australia", "Munich, Germany", "Singapore",
        ]:
            self.assertFalse(check.is_us(job(locations=[loc])), loc)

    def test_mixed_counts_as_non_us(self):
        """Don't drop a role on the strength of its foreign office."""
        self.assertFalse(check.is_us(job(locations=["New York, NY", "London, UK"])))

    def test_missing_locations_defaults_to_us(self):
        self.assertTrue(check.is_us({"company_name": "X"}))


class TestMergeIntegration(unittest.TestCase):
    """_merge is where the filter actually runs."""

    def merge(self, jobs):
        out, keys = {}, set()
        check._merge(out, keys, jobs)
        return out

    def test_keeps_us_sponsor(self):
        self.assertEqual(len(self.merge([job("Nvidia")])), 1)

    def test_drops_us_non_sponsor(self):
        self.assertEqual(len(self.merge([job("Chipotle Mexican Grill")])), 0)
        self.assertEqual(len(self.merge([job("Totally Fake Nonexistent Co")])), 0)

    def test_keeps_non_fortune500_sponsor(self):
        """The point of this change: Datadog sponsors (145 petitions) and the
        old Fortune-500-only gate was throwing it away."""
        self.assertEqual(len(self.merge([job("Datadog")])), 1)
        self.assertEqual(len(self.merge([job("IXL Learning")])), 1)

    def test_keeps_non_us_regardless_of_sponsorship(self):
        """The filter is US-only, so a London role at a non-sponsor survives."""
        kept = self.merge([job("Chipotle Mexican Grill", locations=["London, UK"])])
        self.assertEqual(len(kept), 1)

    def test_masters_filter_still_applies(self):
        kept = self.merge([job("Nvidia", title="Product Manager Intern, Masters")])
        self.assertEqual(len(kept), 0)

    def test_toggle_off_restores_old_behavior(self):
        """Use a company with a genuine zero, so the toggle is what's tested."""
        self.assertEqual(len(self.merge([job("Northrop Grumman")])), 0)
        saved = check.SPONSORS_ONLY
        try:
            check.SPONSORS_ONLY = False
            self.assertEqual(len(self.merge([job("Northrop Grumman")])), 1)
        finally:
            check.SPONSORS_ONLY = saved

    def test_dedupe_still_works(self):
        kept = self.merge([job("Nvidia", id="a"), job("Nvidia", id="b")])
        self.assertEqual(len(kept), 1)


class TestSponsorsData(unittest.TestCase):
    def test_covers_all_employers_not_just_f500(self):
        with open("sponsors.json") as f:
            data = json.load(f)
        self.assertGreater(data["employer_count"], 50_000)
        self.assertEqual(data["employer_count"], len(data["petitions"]))

    def test_every_entry_has_a_positive_count(self):
        with open("sponsors.json") as f:
            petitions = json.load(f)["petitions"]
        self.assertTrue(all(v > 0 for v in petitions.values()))

    def test_keys_are_already_normalized(self):
        """normalize_company is duplicated in build_sponsors.py; if the two ever
        drift, every lookup silently stops matching."""
        with open("sponsors.json") as f:
            petitions = json.load(f)["petitions"]
        for k in list(petitions)[:200]:
            self.assertEqual(check.normalize_company(k), k, k)


if __name__ == "__main__":
    unittest.main(verbosity=2)
