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

    def test_matches_build_script_rule(self):
        """normalize_company is duplicated in build_sponsors.py; if the two ever
        drift, every alias key silently stops matching."""
        with open("sponsors.json") as f:
            keys = set(json.load(f)["keys"])
        # Keys are stored already-normalized, so normalizing again is a no-op.
        for k in list(keys)[:50]:
            self.assertEqual(check.normalize_company(k), k, k)


class TestSponsorsH1b(unittest.TestCase):
    def test_known_sponsors(self):
        for company in [
            "Nvidia", "Amazon", "Microsoft", "Apple", "Goldman Sachs",
            "Capital One", "Intel", "Salesforce", "Visa", "Adobe",
        ]:
            self.assertTrue(check.sponsors_h1b(job(company)), company)

    def test_aliases_resolve(self):
        """Postings say 'Google', the Fortune list says 'Alphabet'."""
        for company in [
            "Google", "Meta", "Facebook", "IBM", "AMD", "Disney",
            "JPMorgan", "AWS", "LinkedIn", "Square", "Uber", "Raytheon",
        ]:
            self.assertTrue(check.sponsors_h1b(job(company)), company)

    def test_legal_suffix_variants(self):
        for company in ["Nvidia Corp", "Uber Technologies, Inc.", "Oracle Corporation"]:
            self.assertTrue(check.sponsors_h1b(job(company)), company)

    def test_known_non_sponsors(self):
        """In the Fortune 500 but with zero H-1B approvals in the window."""
        for company in [
            "Northrop Grumman", "Chipotle Mexican Grill", "Tenet Healthcare",
            "D.R. Horton", "Toll Brothers", "TransDigm Group",
        ]:
            self.assertFalse(check.sponsors_h1b(job(company)), company)

    def test_non_fortune500_is_excluded(self):
        """Not in the data at all -> excluded. This is the intended narrowing,
        and it is the behavior most likely to surprise, so it is pinned here."""
        for company in ["IXL Learning", "Datadog", "Figma", "Stripe", "Databricks"]:
            self.assertFalse(check.sponsors_h1b(job(company)), company)

    def test_subsidiary_matches_parent(self):
        """Feeds name subsidiaries as 'X, A Y Company'; the parent files."""
        self.assertTrue(check.sponsors_h1b(job("Progress Rail, A Caterpillar Company")))
        self.assertTrue(check.sponsors_h1b(job("Bighorn, an Amazon Company")))

    def test_subsidiary_of_non_sponsor_still_excluded(self):
        self.assertFalse(check.sponsors_h1b(job("Widgets, A Chipotle Mexican Grill Company")))

    def test_fails_open_when_data_missing(self):
        """A missing/corrupt sponsors.json must not drop every role."""
        saved = check.SPONSOR_KEYS
        try:
            check.SPONSOR_KEYS = None
            self.assertTrue(check.sponsors_h1b(job("Some Company Nobody Knows")))
        finally:
            check.SPONSOR_KEYS = saved


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

    def test_drops_us_non_fortune500(self):
        self.assertEqual(len(self.merge([job("Datadog")])), 0)

    def test_keeps_non_us_regardless_of_sponsorship(self):
        """The filter is US-only, so a London role at a non-sponsor survives."""
        kept = self.merge([job("Chipotle Mexican Grill", locations=["London, UK"])])
        self.assertEqual(len(kept), 1)

    def test_masters_filter_still_applies(self):
        kept = self.merge([job("Nvidia", title="Product Manager Intern, Masters")])
        self.assertEqual(len(kept), 0)

    def test_toggle_off_restores_old_behavior(self):
        saved = check.SPONSORS_ONLY
        try:
            check.SPONSORS_ONLY = False
            self.assertEqual(len(self.merge([job("Datadog")])), 1)
        finally:
            check.SPONSORS_ONLY = saved

    def test_dedupe_still_works(self):
        kept = self.merge([job("Nvidia", id="a"), job("Nvidia", id="b")])
        self.assertEqual(len(kept), 1)


class TestSponsorsData(unittest.TestCase):
    def test_counts_match_source(self):
        with open("sponsors.json") as f:
            data = json.load(f)
        self.assertEqual(data["sponsor_count"], 429)
        self.assertEqual(data["non_sponsor_count"], 42)
        self.assertEqual(data["sponsor_count"] + data["non_sponsor_count"], 471)

    def test_keys_cover_every_sponsor_plus_aliases(self):
        with open("sponsors.json") as f:
            data = json.load(f)
        self.assertGreaterEqual(len(data["keys"]), data["sponsor_count"])

    def test_non_sponsors_are_not_in_keys(self):
        with open("sponsors.json") as f:
            data = json.load(f)
        keys = set(data["keys"])
        for name in data["non_sponsors"]:
            self.assertNotIn(check.normalize_company(name), keys, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
