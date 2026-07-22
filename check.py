#!/usr/bin/env python3
"""
PM intern / new-grad watcher.

Pulls product-management postings from public GitHub repos, works out which are
new since the last run, and sends ONE email per new role. State (what you've
already been told about) lives in seen.json, committed back by the Action.

Subject line: "<role> <company> <new-grad/internships>", where the trailing
type is only added when the role title doesn't already say it.

Stdlib only, nothing to install.
"""

import html
import json
import os
import re
import smtplib
import ssl
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

# ---- sources ---------------------------------------------------------------

# Each source is tagged "internship" or "new-grad" so the subject line can label
# it. Simplify's data lives in its GitHub repo (website == repo). LinkedIn is
# intentionally absent: its terms forbid automation and it blocks GitHub's IPs.

JSON_SOURCES = [
    ("https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json", "internship"),
    ("https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json", "internship"),
    ("https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json", "new-grad"),
]

MARKDOWN_SOURCES = [
    ("https://raw.githubusercontent.com/jobright-ai/2026-Product-Management-Internship/master/README.md", "internship"),
    ("https://raw.githubusercontent.com/jobright-ai/2026-Product-Management-New-Grad/master/README.md", "new-grad"),
]

# We only want EARLY-CAREER roles: interns/co-ops in the product / program /
# project management family (including "technical ..." and PM/TPM), plus APMs.
# Senior, lead, principal, staff, and plain mid-level roles are filtered out
# because they don't match either rule below.
_INTERN = re.compile(r"\b(intern|interns|internship|internships|co-?op)\b", re.I)
_PM_FAMILY = re.compile(
    r"\b(?:product|program|project)\s+(?:manage(?:ment|rs?)?|mgmt|mgr)\b"
    r"|\btechnical\s+(?:product|program|project)\b"
    r"|\b(?:tpm|pm)\b",
    re.I,
)
_APM_FULL = re.compile(r"\bassociate\s+product\s+manager\b", re.I)
_APM_ABBR = re.compile(r"\bapm\b", re.I)
# "APM" also means Application Performance Monitoring / APM Terminals, etc. Only
# treat a bare "APM" as product-manager when it's not clearly one of those.
_APM_NOISE = re.compile(r"\b(engineer(?:ing)?|developer|reliability|monitoring|terminals?|devops|sre)\b", re.I)

# If a single run turns up more than this many "new" roles, something upstream
# changed (ids reshuffled) — send one digest instead of flooding your inbox.
MAX_INDIVIDUAL_EMAILS = 25

# Only email roles posted within this many days. The source repos constantly add
# roles that were posted days ago; this skips those so you only hear about fresh
# postings. Bump it up if you want a wider net. (Roles with no known post date,
# e.g. some jobright rows, are always let through.)
FRESH_DAYS = 1

STATE_FILE = "seen.json"

# ---- fetch + parse ---------------------------------------------------------


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pm-intern-watcher"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def strip_md(text):
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)        # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)    # links -> label
    return text.replace("**", "").replace("`", "").strip()


def first_url(cell):
    m = re.search(r"\((https?://[^)\s]+)\)", cell) or re.search(r"(https?://\S+)", cell)
    return m.group(1) if m else None


def parse_md_date(cell):
    """jobright tables show a date like 'Jul 20'. Turn it into a unix timestamp.
    Returns 0 if it can't be parsed (which is_fresh treats as 'let it through')."""
    s = strip_md(cell).strip()
    now = datetime.now(timezone.utc)
    for fmt in ("%b %d", "%B %d"):
        try:
            dt = datetime.strptime(f"{s} {now.year}", fmt + " %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt - now > timedelta(days=7):  # e.g. 'Dec 30' seen in January -> last year
            dt = dt.replace(year=now.year - 1)
        return int(dt.timestamp())
    return 0


def is_fresh(job):
    dp = job.get("date_posted", 0)
    if not dp:
        return True  # unknown post date -> don't exclude it
    # Use a calendar-day window (start of today, minus FRESH_DAYS). jobright
    # dates are day-only (midnight UTC), so a rolling now-24h window would drop
    # "yesterday" roles the moment the clock passes midnight. FRESH_DAYS=1 means
    # "posted today or yesterday."
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return dp >= (midnight - timedelta(days=FRESH_DAYS)).timestamp()


def parse_json_feed(raw, role_type):
    jobs = []
    for j in json.loads(raw):
        if not is_open(j) or not is_pm(j):
            continue
        jobs.append(
            {
                "id": j.get("id") or j.get("url"),
                "company_name": j.get("company_name", "?"),
                "title": j.get("title", "?"),
                "locations": j.get("locations") or [],
                "url": j.get("url", "#"),
                "date_posted": j.get("date_posted", 0),
                "role_type": role_type,
            }
        )
    return jobs


def parse_markdown_feed(raw, role_type):
    """Tolerant parser for jobright-style README tables. Those repos are already
    PM-only, so we keep every real row and just skip header/separator lines."""
    jobs = []
    last_company = "?"
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        joined = " ".join(cells).lower()
        if set(joined.replace(" ", "")) <= set("-:|") or "job title" in joined or ("company" in joined and "location" in joined):
            continue  # header / separator row

        company = strip_md(cells[0])
        if company in ("", "↳"):        # continuation row -> reuse company
            company = last_company
        else:
            last_company = company
        title = strip_md(cells[1])
        # These repos mix in non-PM "Product Development / Design" rows, so keep
        # only the ones that actually read as PM.
        if not title or not is_pm({"title": title}):
            continue
        urls = [u for c in cells if (u := first_url(c))]
        url = next((u for u in urls if "jobright.ai/jobs" in u or "apply" in u.lower()),
                   urls[-1] if urls else "#")
        url = url.split("?")[0]  # drop tracking query so the id stays stable run-to-run
        jobs.append(
            {
                "id": url if url != "#" else f"{company}|{title}",
                "company_name": company,
                "title": title,
                "locations": [strip_md(cells[2])],
                "url": url,
                "date_posted": parse_md_date(cells[-1]),
                "role_type": role_type,
            }
        )
    return jobs


# ---- filters ---------------------------------------------------------------


def is_pm(job):
    t = job.get("title", "")
    if _APM_FULL.search(t):
        return True
    if _APM_ABBR.search(t) and not _APM_NOISE.search(t):
        return True
    return bool(_INTERN.search(t) and _PM_FAMILY.search(t))


def is_open(job):
    return job.get("active", True) is not False and job.get("is_visible", True) is not False


def collect_pm_jobs():
    """Returns {id: job}. Dedupes by id AND by (company, title) so the same role
    appearing in multiple source repos only emails once. Returns None if EVERY
    source failed to fetch (so callers can avoid acting on an empty snapshot)."""
    jobs = {}
    seen_keys = set()
    ok = 0
    for url, role_type in JSON_SOURCES:
        try:
            parsed = parse_json_feed(fetch(url), role_type)
            ok += 1
            _merge(jobs, seen_keys, parsed)
        except Exception as e:  # noqa: BLE001 - a dead feed shouldn't kill the run
            print(f"::warning::skip json {url}: {e}")
    for url, role_type in MARKDOWN_SOURCES:
        try:
            parsed = parse_markdown_feed(fetch(url), role_type)
            ok += 1
            _merge(jobs, seen_keys, parsed)
        except Exception as e:  # noqa: BLE001
            print(f"::warning::skip md {url}: {e}")
    return jobs if ok else None


def _merge(jobs, seen_keys, parsed):
    for j in parsed:
        if not is_fresh(j):
            continue
        key = (j["company_name"].strip().lower(), j["title"].strip().lower())
        if j["id"] in jobs or key in seen_keys:
            continue
        jobs[j["id"]] = j
        seen_keys.add(key)


# ---- state -----------------------------------------------------------------


def load_seen():
    """Returns a set of ids, or None if the state file is missing/unreadable
    (which the caller treats as 'first run, seed instead of emailing')."""
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f).get("ids", []))
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return None


def save_seen(ids):
    with open(STATE_FILE, "w") as f:
        json.dump({"ids": sorted(ids), "updated": datetime.now(timezone.utc).isoformat()}, f, indent=1)


# ---- subject + body --------------------------------------------------------

_MENTIONS_TYPE = re.compile(r"\b(intern|new[\s-]?grad|graduate)", re.I)


def subject_for(job):
    """<role> <company> <new-grad/internships>. The type is appended only when
    the role title doesn't already mention intern / new grad / graduate."""
    title = job["title"].strip()
    label = "" if _MENTIONS_TYPE.search(title) else (
        "internships" if job.get("role_type") == "internship" else "new-grad")
    return " ".join(p for p in (title, job["company_name"], label) if p)


def fmt_when(ts):
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%b %d, %Y") if ts else ""
    except Exception:  # noqa: BLE001
        return ""


def render_one(job):
    e = html.escape
    loc = ", ".join(job.get("locations") or []) or "—"
    meta = " · ".join(x for x in [loc, fmt_when(job.get("date_posted")), job.get("role_type", "")] if x)
    return (
        f'<div style="font-family:system-ui,Arial,sans-serif;color:#231F20">'
        f'<h2 style="margin:0 0 4px;font-size:20px">{e(job["title"])}</h2>'
        f'<div style="font-size:16px;color:#555;margin:0 0 12px">{e(job["company_name"])}</div>'
        f'<div style="color:#888;margin:0 0 18px">{e(meta)}</div>'
        f'<a href="{e(job["url"], quote=True)}" style="display:inline-block;padding:10px 18px;'
        f'background:#231F20;color:#fff;text-decoration:none;border-radius:8px">apply →</a>'
        f'</div>'
    )


def render_digest(jobs):
    e = html.escape
    rows = []
    for j in sorted(jobs, key=lambda x: x.get("date_posted", 0), reverse=True):
        loc = ", ".join(j.get("locations") or []) or "—"
        rows.append(
            f'<tr><td style="padding:8px 12px;border-bottom:1px solid #eee"><b>{e(j["company_name"])}</b><br>'
            f'<span style="color:#555">{e(j["title"])}</span></td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;color:#555">{e(loc)}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee">'
            f'<a href="{e(j["url"], quote=True)}">apply</a></td></tr>'
        )
    return (
        f'<div style="font-family:system-ui,Arial,sans-serif;color:#231F20">'
        f'<h2>{len(jobs)} new PM roles</h2>'
        f'<table style="border-collapse:collapse;width:100%">{"".join(rows)}</table></div>'
    )


# ---- email -----------------------------------------------------------------


def _connect():
    s = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context())
    s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
    return s


def _send(server, subject, html_body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ.get("MAIL_TO", os.environ["SMTP_USER"])
    msg.set_content("Open in an HTML-capable client to see the listing.")
    msg.add_alternative(html_body, subtype="html")
    server.send_message(msg)


# ---- main ------------------------------------------------------------------


def main():
    current = collect_pm_jobs()
    if current is None:
        print("all sources failed to fetch; doing nothing this run")
        return
    print(f"found {len(current)} open PM roles across sources")

    seen = load_seen()

    if seen is None:
        # First run (no/unreadable state). Don't seed off an empty snapshot —
        # wait for a run that actually saw roles, so a later recovery can't look
        # like hundreds of "new" roles.
        if not current:
            print("first run but no roles fetched; will seed on a later run")
            return
        save_seen(current.keys())
        try:
            with _connect() as s:
                _send(
                    s,
                    f"PM watcher is live — tracking {len(current)} roles",
                    f'<div style="font-family:system-ui,Arial,sans-serif;color:#231F20">'
                    f"<h2>You're set up.</h2><p>Watching {len(current)} open PM postings. "
                    f"From now on you'll get one email per new role.</p></div>",
                )
        except Exception as e:  # noqa: BLE001
            print(f"seed email failed (state still saved): {e}")
        return

    new_jobs = [current[jid] for jid in current if jid not in seen]
    if not new_jobs:
        print("no new roles")
        return

    # Too many at once => one digest email, then mark everything seen.
    if len(new_jobs) > MAX_INDIVIDUAL_EMAILS:
        print(f"{len(new_jobs)} new roles (> {MAX_INDIVIDUAL_EMAILS}); sending one digest")
        try:
            with _connect() as s:
                _send(s, f"{len(new_jobs)} new PM roles", render_digest(new_jobs))
        finally:
            save_seen(seen | set(current.keys()))
        return

    # One email per role. Mark each seen only after it actually sends, and save
    # state no matter what — a mid-loop failure won't re-send what already went.
    print(f"{len(new_jobs)} new role(s):", [subject_for(j) for j in new_jobs])
    sent = set(seen)
    try:
        with _connect() as s:
            for job in new_jobs:
                _send(s, subject_for(job), render_one(job))
                sent.add(job["id"])
    finally:
        save_seen(sent)


if __name__ == "__main__":
    main()
