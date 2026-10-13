"""Claude ranks new postings in small batches and queues the good ones."""
from __future__ import annotations

import json
import logging

from . import claude, config, db, discover

log = logging.getLogger("agent.triage")

SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "decision": {"type": "string", "enum": ["apply", "skip"]},
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "category": {"type": "string"},
            "season": {"type": "string"},
            "reason": {"type": "string"},
            "pay_text": {"type": "string"},
        },
        "required": ["id", "decision", "score", "reason"],
    }}},
    "required": ["results"],
}


DEFAULT_SCORING = """    90-100: the role is squarely one of the wanted role types and uses the candidate's strongest skills.
    70-89:  a solid match for the wanted role types with a weaker topical match.
    50-69:  borderline: partly matches, or needs background the candidate lacks.
    below 50: poor fit."""


def candidate_block(p: dict) -> str:
    """A short, factual description of the candidate built from profile.yaml (never hard-coded)."""
    lines = []
    if (p.get("research_summary") or "").strip():
        lines.append(p["research_summary"].strip())
    edu = [f"{e.get('degree', '')} in {e.get('field', '')}, {e.get('school_short') or e.get('school', '')} "
           f"({e.get('start', '')} to {e.get('end', '')}; {e.get('status', '')})"
           for e in p.get("education") or [] if e.get("school")]
    if edu:
        lines.append("Degrees: " + "; ".join(edu))
    wa = p.get("work_authorization") or {}
    lines.append(f"Work authorization: {'U.S. citizen' if wa.get('us_citizen') else 'see profile'}; authorized to "
                 f"work in the U.S.: {'yes' if wa.get('authorized_to_work_in_us') else 'no'}; needs sponsorship now "
                 f"or later: {'yes' if wa.get('requires_sponsorship_now_or_future') else 'no'}.")
    addr, av = p.get("address") or {}, p.get("availability") or {}
    if addr.get("city"):
        lines.append(f"Based in {addr.get('city')}, {addr.get('state', '')}; willing to relocate: "
                     f"{'yes' if av.get('willing_to_relocate') else 'no'}.")
    exp = [f"{x.get('title')} at {x.get('company')}" for x in (p.get("experience") or [])[:6] if x.get("title")]
    if exp:
        lines.append("Experience: " + "; ".join(exp))
    skills = [s for group in (p.get("skills") or {}).values() for s in (group or [])]
    if skills:
        lines.append("Skills: " + ", ".join(skills[:25]))
    return "\n".join(lines)


def graduation(p: dict) -> str:
    edu = [e for e in p.get("education") or [] if e.get("school")]
    current = next((e for e in edu if "progress" in str(e.get("status", "")).lower()), edu[0] if edu else {})
    return str(current.get("end") or "not stated")


def job_type_rule(p: dict) -> str:
    t, grad = discover.job_types(), graduation(p)
    intern = ("it is an internship/co-op open to a currently enrolled student at the candidate's degree level "
              "(skip roles limited to other levels). The candidate's expected graduation is " + grad + ": skip "
              "if the posting requires a graduation date or window that ends before it (e.g. \"graduating "
              "between Dec 2027 and Jun 2028\" when the candidate graduates later)")
    full = ("it is a full-time (or new-grad / entry-level) role suited to the candidate's experience, and the "
            "candidate (graduation: " + grad + ") can start by the role's start date")
    if discover.wants_internships(t) and discover.wants_full_time(t):
        return f"EITHER {intern}; OR {full}"
    if discover.wants_full_time(t):
        return full + " (skip internships and co-ops)"
    return intern + " (skip full-time and new-grad roles)"


def system_prompt() -> str:
    s, p = config.settings()["targeting"], config.profile()
    return (config.PROMPTS / "triage.md").read_text().format(
        candidate=candidate_block(p), job_type_rule=job_type_rule(p),
        countries=", ".join(discover.countries()) or "any country",
        seasons=", ".join(s.get("seasons") or []), role_types="; ".join(s.get("role_types") or []),
        never_apply="; ".join(s.get("never_apply") or []), locations=s.get("locations", ""),
        scoring_guidance=(s.get("scoring_guidance") or DEFAULT_SCORING).rstrip())


def run_triage(con, batch_size: int = 6, max_jobs: int = 60) -> dict:
    rows = con.execute(
        "SELECT * FROM jobs WHERE status='new' ORDER BY first_seen LIMIT ?", (max_jobs,)).fetchall()
    stats = {"triaged": 0, "queued": 0}
    model = config.settings()["claude"]["triage_model"]
    min_score = config.settings()["targeting"]["min_score"]
    system = system_prompt()
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        keys = {f"J{n + 1}": r for n, r in enumerate(batch)}
        parts = [
            f"### {k}\nCompany: {r['company']}\nTitle: {r['title']}\nLocation: {r['location']} ({r['country']})\n"
            f"Posted: {r['posted_on']}\nDescription:\n{(r['description'] or '')[:3500]}"
            for k, r in keys.items()
        ]
        prompt = "Screen these postings. Use the ids J1, J2, ... as given.\n\n" + "\n\n".join(parts)
        res = claude.ask(prompt, system=system, schema=SCHEMA, model=model, purpose="triage")
        for item in res.get("results", []):
            r = keys.get(item.get("id"))
            if r is None:
                continue
            good = item["decision"] == "apply" and item["score"] >= min_score
            fields = {"status": "queued" if good else "skipped", "score": item["score"], "triage": item}
            if item.get("pay_text") and not r["pay"]:
                fields["pay"] = {"text": item["pay_text"]}
            db.set_job(con, r["id"], **fields)
            stats["triaged"] += 1
            stats["queued"] += int(good)
            log.info("%s %3d %-25s %s — %s", "QUEUE" if good else "skip ", item["score"],
                     r["company"][:25], r["title"][:60], item.get("reason", "")[:100])
    return stats
