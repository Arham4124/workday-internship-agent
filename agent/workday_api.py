"""Workday public job-search API (the JSON 'cxs' endpoints behind every career site)."""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import truststore

truststore.inject_into_ssl()
import requests  # noqa: E402

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
INTERN_FACET = re.compile(r"\b(intern|interns|internship|internships|co-?op|student)\b", re.I)

_session = requests.Session()
_session.headers.update({"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA})


@dataclass
class Site:
    name: str
    url: str
    host: str
    tenant: str
    site: str
    public_base: str  # prefix for public job URLs

    @property
    def api(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"


def parse_site(name: str, url: str) -> Site:
    u = urlparse(url.strip())
    parts = [p for p in u.path.split("/") if p and not LOCALE.match(p)]
    host = u.netloc
    if host.endswith("myworkdaysite.com"):
        # https://wd1.myworkdaysite.com/recruiting/<tenant>/<site>
        i = parts.index("recruiting")
        tenant, site = parts[i + 1], parts[i + 2]
        base = f"https://{host}/recruiting/{tenant}/{site}"
    else:
        tenant, site = host.split(".")[0].replace("-", "_"), parts[0]  # "vhr-otsuka" host -> "vhr_otsuka" tenant
        base = f"https://{host}/{site}"
    return Site(name=name, url=url, host=host, tenant=tenant, site=site, public_base=base)


def _post(site: Site, body: dict) -> dict:
    for attempt in range(3):
        r = _session.post(f"{site.api}/jobs", json=body, timeout=30)
        if r.status_code in (429, 500, 502, 503) and attempt < 2:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    return {}


def _flatten(values, out):
    for v in values or []:
        if "values" in v:
            _flatten(v["values"], out)
        else:
            out.append(v)


def intern_facet(site: Site) -> dict | None:
    """Find a facet value meaning 'internship' (e.g. Worker Sub-Type = Intern)."""
    d = _post(site, {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    found: dict[str, list[str]] = {}

    def walk(facets):
        for f in facets or []:
            param = f.get("facetParameter")
            leaves = []
            for v in f.get("values", []):
                if "values" in v and v.get("facetParameter"):
                    walk([v])
                else:
                    leaves.append(v)
            for v in leaves:
                if v.get("descriptor") and INTERN_FACET.search(v["descriptor"]) and v.get("id"):
                    found.setdefault(param, []).append(v["id"])

    walk(d.get("facets"))
    return found or None


def search(site: Site, search_text: str = "", facets: dict | None = None, cap: int = 400,
           relevant=None) -> list[dict]:
    """Page through results. With `relevant` (a title test), stop after two pages in a row with no
    relevant titles: keyword search ranks the best matches first, the tail is noise."""
    out, offset, total, dry = [], 0, None, 0
    while offset < cap:
        d = _post(site, {"appliedFacets": facets or {}, "limit": 20, "offset": offset, "searchText": search_text})
        posts = d.get("jobPostings") or []
        if total is None:
            total = d.get("total") or 0
        out.extend(posts)
        offset += 20
        if relevant is not None:
            dry = 0 if any(relevant(p.get("title", "")) for p in posts) else dry + 1
            if dry >= 2:
                break
        if not posts or offset >= (total or 0):
            break
        time.sleep(0.4)
    return out


def detail(site: Site, external_path: str) -> dict:
    r = _session.get(f"{site.api}{external_path}", timeout=30)
    r.raise_for_status()
    return r.json()


def html_to_text(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", s or "")
    s = re.sub(r"(?i)<li[^>]*>", "• ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


_HOURLY = re.compile(
    r"\$\s?(\d{2,3}(?:\.\d{1,2})?)\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:\.\d{1,2})?)\s*(?:/|per|an)?\s*(?:hr|hour)", re.I)
_HOURLY_ONE = re.compile(r"\$\s?(\d{2,3}(?:\.\d{1,2})?)\s*(?:/|per|an)\s*(?:hr|hour)", re.I)
_ANNUAL = re.compile(r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}[kK])\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:,\d{3})+|\d{2,3}[kK])")


def _num(x: str) -> float:
    x = x.replace(",", "")
    return float(x[:-1]) * 1000 if x.lower().endswith("k") else float(x)


def parse_pay(text: str) -> dict | None:
    if m := _HOURLY.search(text or ""):
        lo, hi = sorted((_num(m.group(1)), _num(m.group(2))))
        return {"min": lo, "max": hi, "unit": "hour", "text": m.group(0)}
    if m := _HOURLY_ONE.search(text or ""):
        v = _num(m.group(1))
        return {"min": v, "max": v, "unit": "hour", "text": m.group(0)}
    if m := _ANNUAL.search(text or ""):
        lo, hi = sorted((_num(m.group(1)), _num(m.group(2))))
        if lo >= 20000:
            return {"min": lo, "max": hi, "unit": "year", "text": m.group(0)}
    return None
