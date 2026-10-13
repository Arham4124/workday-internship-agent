"""Scan every Workday career site for new internship postings (no Claude usage)."""
from __future__ import annotations

import functools
import json
import logging
import re
import time

from . import config, db
from . import workday_api as wd

log = logging.getLogger("agent.discover")

INTERN_TITLE = re.compile(
    r"\b(intern|interns|internship|co-?op|summer (associate|analyst|student|scholar|fellow)|"
    r"student researcher|phd (resident|residency))\b", re.I)
US_HINT = re.compile(
    r"united states|\bUSA?\b|\bremote\b|, ?(AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|"
    r"MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b|"
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|"
    r"illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    r"mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|"
    r"north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
    r"south dakota|tennessee|texas|utah|vermont|virginia|washington|wisconsin|wyoming", re.I)

# Title keyword defaults (case-insensitive regex fragments; plain words work). Override any list in
# settings.yaml -> targeting: title_keywords / strong_title_keywords / exclude_title_keywords, and add
# personal exclusions with targeting.exclude_title_words (e.g. ["biostat", "statistic"]).
DEFAULT_TITLE_KEYWORDS = [
    "data", "machine learning", r"\bml\b", r"\bai\b", r"a\.i\.", "artificial intelligence", "deep learning",
    "research", "scien", "computational", "bioinformatic", "genom", "genetic", "biolog", "drug discovery",
    r"\bllm", "language model", r"\bnlp\b", "vision", "generative", "gen ?ai", "foundation model", "analytics",
    "algorithm", "model", "quantitative", "neural", r"ph\.?d", "graduate", "computing"]
DEFAULT_STRONG_KEYWORDS = [
    "data scien", "machine learning", r"\bml\b", r"\bai\b", "artificial intelligence", "deep learning",
    "computational biolog", "bioinformatic", "genomic", r"\bllm", "language model", r"\bnlp\b", "computer vision",
    "generative", "foundation model", "applied scien", "research scien"]
DEFAULT_EXCLUDE_KEYWORDS = [
    "account", "audit", r"\btax\b", "legal", "paralegal", "marketing", "sales", "human resources", r"\bhr\b",
    "recruit", "supply chain", "procurement", "treasury", "facilities", "mechanical", "electrical", "civil",
    "manufactur", "process eng", "quality", "regulatory", "nurs", "pharmacist", "communications", "graphic",
    r"\bux\b", "ui design", "product design", "hardware", "asic", "fpga", r"\brtl\b", "analog", "circuit",
    "compiler", "undergrad", "high school", r"\bmba\b", "finance", "construction", "logistics", "warehouse",
    "retail", "store", "safety", "environmental health", "clinical trial"]
# For priority industries titles are often generic, so only clearly non-technical roles are dropped.
DEFAULT_NONTECH_KEYWORDS = [
    "account", "audit", r"\btax\b", "legal", "paralegal", "marketing", "sales", "human resources", r"\bhr\b",
    "recruit", "supply chain", "procurement", "treasury", "facilities", "communications", "graphic", r"\bux\b",
    "ui design", r"\bmba\b", "finance", "pricing", "government affairs", "public affairs", "policy",
    "patient advocacy", "medical affairs", "sterile", "warehouse"]


@functools.lru_cache(maxsize=1)
def _filters() -> dict:
    t = config.settings().get("targeting", {}) or {}
    personal = list(t.get("exclude_title_words") or [])
    rx = lambda words: re.compile("|".join(words) or r"(?!x)x", re.I)  # noqa: E731
    return {
        "relevant": rx(t.get("title_keywords") or DEFAULT_TITLE_KEYWORDS),
        "strong": rx(t.get("strong_title_keywords") or DEFAULT_STRONG_KEYWORDS),
        "exclude": rx((t.get("exclude_title_keywords") or DEFAULT_EXCLUDE_KEYWORDS) + personal),
        "nontech": rx(DEFAULT_NONTECH_KEYWORDS + personal),
        "priority": set(t.get("priority_sectors") or []),
        "job_types": job_types(),
        "countries": countries(),
    }


INTERN_TYPES, FULLTIME_TYPES = {"internship", "intern", "co-op", "coop"}, {"full_time", "full-time", "fulltime", "new_grad", "entry_level"}
US_NAMES = {"united states", "united states of america", "usa", "us", "u.s."}


def job_types() -> set:
    """targeting.job_types: [internship] (default), [full_time], [internship, full_time] or [any]."""
    v = (config.settings().get("targeting", {}) or {}).get("job_types") or ["internship"]
    v = {str(x).strip().lower() for x in v}
    if "any" in v:
        v |= {"internship", "full_time"}
    return v


def wants_internships(types=None) -> bool:
    return bool((types or job_types()) & INTERN_TYPES)


def wants_full_time(types=None) -> bool:
    return bool((types or job_types()) & FULLTIME_TYPES)


def countries() -> list:
    """targeting.countries: e.g. ["United States"] (default), ["United States", "Canada"], or [any]."""
    v = (config.settings().get("targeting", {}) or {}).get("countries")
    v = ["United States"] if v is None else [str(c).strip() for c in v if str(c).strip()]
    return [] if not v or any(c.lower() in ("any", "all", "*", "anywhere") for c in v) else v


def title_filter(title: str, sector: str = "other") -> str | None:
    """Return None if the title passes, else a reason it was filtered out.
    For priority industries only clearly non-technical roles are dropped here; Claude reads the rest."""
    f = _filters()
    is_intern = bool(INTERN_TITLE.search(title))
    if is_intern and not wants_internships(f["job_types"]):
        return "internship (targeting.job_types excludes internships)"
    if not is_intern and not wants_full_time(f["job_types"]):
        return "not an internship title (targeting.job_types is internships only)"
    if sector in f["priority"]:
        return "non-technical role" if f["nontech"].search(title) and not f["strong"].search(title) else None
    if not f["relevant"].search(title):
        return "title does not match the wanted role types"
    if f["exclude"].search(title) and not f["strong"].search(title):
        return "excluded field (see targeting.exclude_title_words)"
    return None


def is_us(info: dict) -> bool:
    country = (info.get("country") or {}).get("descriptor", "")
    if "United States" in country:
        return True
    locs = " | ".join([info.get("location") or ""] + list(info.get("additionalLocations") or []))
    hint = US_HINT.search(re.sub(r"(?i)\bremote\b", "", locs))
    if country:  # a non-U.S. primary country: only keep if another listed location is in the U.S.
        return bool(info.get("additionalLocations")) and bool(hint)
    return bool(hint) or bool(re.search(r"(?i)remote.{0,12}(us|u\.s\.|united states)", locs))


def location_ok(info: dict, wanted: list) -> bool:
    """True if the posting is in one of the wanted countries (always True when wanted is empty = anywhere)."""
    if not wanted:
        return True
    country = (info.get("country") or {}).get("descriptor", "").lower()
    locs = " | ".join([info.get("location") or ""] + list(info.get("additionalLocations") or [])).lower()
    for c in (w.lower() for w in wanted):
        if c in US_NAMES:
            if is_us(info):
                return True
        elif (country and c in country) or c in locs:
            return True
    return False


DEFAULT_FULLTIME_TERMS = ["data scientist", "machine learning", "artificial intelligence", "research scientist",
                          "applied scientist"]


def fetch_site(company: dict, known: set, facet) -> dict:
    """Network only (safe to run in a thread): list postings and fetch details for new candidates."""
    site = wd.parse_site(company["name"], company["url"])
    f = _filters()
    postings = []
    if wants_internships(f["job_types"]):
        if facet is None:
            facet = wd.intern_facet(site) or {}
        is_intern = lambda t: bool(INTERN_TITLE.search(t or ""))  # noqa: E731
        postings += wd.search(site, "", facet, cap=400) if facet else []
        postings += wd.search(site, "intern", None, cap=100 if facet else 300, relevant=is_intern)
    if wants_full_time(f["job_types"]):  # no "intern" filter to lean on: search the wanted role terms
        terms = (config.settings().get("targeting", {}) or {}).get("search_terms") or DEFAULT_FULLTIME_TERMS
        for term in terms:
            postings += wd.search(site, term, None, cap=150, relevant=lambda t: bool(f["relevant"].search(t or "")))
    seen, rows = set(), []
    for p in postings:
        path = p.get("externalPath")
        if not path or path in seen:
            continue
        seen.add(path)
        job_id = f"{site.tenant}|{path}"
        if job_id in known:
            continue
        title = p.get("title", "")
        reason = title_filter(title, company.get("sector", "other"))
        base = dict(id=job_id, company=company["name"], host=site.host, tenant=site.tenant, site=site.site,
                    title=title, location=p.get("locationsText", ""), external_path=path,
                    posted_on=p.get("postedOn", ""), req_id=(p.get("bulletFields") or [""])[0],
                    url=site.public_base + path, first_seen=db.now(), updated_at=db.now())
        if reason:
            base.update(status="filtered", triage=json.dumps({"reason": reason}))
        else:
            try:
                info = wd.detail(site, path).get("jobPostingInfo") or {}
            except Exception as e:  # retry details on the next scan
                log.warning("detail failed %s: %s", job_id, e)
                continue
            desc = wd.html_to_text(info.get("jobDescription", ""))
            pay = wd.parse_pay(desc)
            base.update(description=desc, url=info.get("externalUrl") or base["url"],
                        country=(info.get("country") or {}).get("descriptor", ""),
                        location=info.get("location") or base["location"], pay=json.dumps(pay) if pay else None)
            if not info.get("canApply", True):
                base.update(status="closed")
            elif not location_ok(info, f["countries"]):
                base.update(status="filtered", triage=json.dumps({"reason": "outside targeting.countries"}))
            else:
                base.update(status="new")
            time.sleep(0.3)
        rows.append(base)
    return {"site": site, "facet": facet, "rows": rows, "seen": len(seen)}


def run_discovery(con, workers: int = 4) -> dict:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    stats = {"sites_ok": 0, "sites_failed": 0, "new_jobs": 0}
    known = {r[0] for r in con.execute("SELECT id FROM jobs")}
    facets = {r["url"]: (json.loads(r["intern_facet"]) or None if r["intern_facet"] is not None else None)
              for r in con.execute("SELECT url, intern_facet FROM sites")}  # empty -> look for a filter again
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fetch_site, c, known, facets.get(c["url"])): c for c in config.companies()}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                stats["sites_failed"] += 1
                log.warning("site failed %s: %s", c["name"], e)
                con.execute(
                    "INSERT INTO sites(url,name,ok,last_checked,last_error) VALUES(?,?,0,?,?) "
                    "ON CONFLICT(url) DO UPDATE SET ok=0,last_checked=excluded.last_checked,last_error=excluded.last_error",
                    (c["url"], c["name"], db.now(), str(e)[:300]))
                con.commit()
                continue
            for base in res["rows"]:
                cols = ",".join(base)
                con.execute(f"INSERT OR IGNORE INTO jobs({cols}) VALUES({','.join('?' * len(base))})", tuple(base.values()))
            con.execute("INSERT OR REPLACE INTO sites(url,name,ok,last_checked,last_error,intern_facet,jobs_seen) "
                        "VALUES(?,?,?,?,?,?,?)", (c["url"], c["name"], 1, db.now(), None, json.dumps(res["facet"]), res["seen"]))
            con.commit()
            n = sum(1 for b in res["rows"] if b["status"] == "new")
            stats["sites_ok"] += 1
            stats["new_jobs"] += n
            log.info("scanned %-28s new candidate internships: %d", c["name"], n)
    db.kv_set(con, "last_discovery", db.now())
    return stats
