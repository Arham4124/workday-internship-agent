"""Ask Claude what to enter on the current page (and to double-check the Review page)."""
from __future__ import annotations

import json
import re
from datetime import date

from . import claude, config

EEO = re.compile(r"gender|race|ethnic|hispanic|veteran|disabilit|self.?identif|sexual orientation|transgender|pronoun", re.I)

PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "fill": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "value": {"type": "string"},
            "values": {"type": "array", "items": {"type": "string"}},
            "fallbacks": {"type": "array", "items": {"type": "string"}}}, "required": ["id"]}},
        "add": {"type": "array", "items": {"type": "object", "properties": {
            "button": {"type": "string"}, "times": {"type": "integer"}}, "required": ["button", "times"]}},
        "remove": {"type": "array", "items": {"type": "string"}},
        "click": {"type": "string"},
        "proceed": {"type": "boolean"},
        "needs_human": {"type": "object", "properties": {
            "questions": {"type": "array", "items": {"type": "string"}},
            "question": {"type": "string"}, "reason": {"type": "string"}}},
        "essays": {"type": "array", "items": {"type": "object", "properties": {
            "question": {"type": "string"}, "answer": {"type": "string"}}}},
        "note": {"type": "string"},
    },
    "required": ["fill", "proceed"],
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "problems": {"type": "array", "items": {"type": "string"}}},
    "required": ["ok", "problems"],
}


def _system(include_self_id: bool) -> str:
    t = (config.PROMPTS / "apply_system.md").read_text()
    return (t.replace("{profile}", config.profile_text(include_self_id))
             .replace("{answers}", config.answers_text())
             .replace("{today}", date.today().strftime("%m/%d/%Y")))


def job_block(job) -> str:
    pay = json.loads(job["pay"]) if job["pay"] else None
    pay_s = pay.get("text") if pay else "not stated (use the $40/hour default)"
    desc = (job["description"] or "")[:2500]
    return (f"JOB: {job['title']} at {job['company']} ({job['location']}). Req {job['req_id']}.\n"
            f"Posted pay: {pay_s}\nDescription (truncated):\n{desc}")


def page_block(st: dict, failures: list[str]) -> str:
    fields = []
    for f in st["fields"]:
        if f["type"] == "password":
            continue
        g = {k: f[k] for k in ("id", "type", "label", "required", "value") if k in f}
        for k in ("options", "section", "heading", "error"):
            if f.get(k):
                g[k] = f[k]
        fields.append(g)
    buttons = [{k: b[k] for k in ("id", "text", "near") if k in b} for b in st["buttons"]]
    parts = [f"PAGE: step = {st.get('step') or '(none)'}; all steps = {st.get('steps')}",
             f"URL: {st['url']}", f"Headings: {st.get('headings')}",
             "FIELDS:\n" + "\n".join(json.dumps(f, ensure_ascii=False) for f in fields),
             "BUTTONS: " + json.dumps(buttons, ensure_ascii=False)]
    if st.get("errors"):
        parts.append("ERRORS SHOWN ON PAGE: " + json.dumps(st["errors"], ensure_ascii=False))
    if failures:
        parts.append("YOUR PREVIOUS ATTEMPT ON THIS PAGE HAD PROBLEMS:\n- " + "\n- ".join(failures))
    if not fields:
        parts.append("PAGE TEXT: " + st.get("text", "")[:2000])
    return "\n\n".join(parts)


def plan_page(job, st: dict, failures: list[str]) -> dict:
    blob = json.dumps(st["fields"]) + " " + " ".join(st.get("headings", [])) + " " + (st.get("step") or "")
    prompt = job_block(job) + "\n\n" + page_block(st, failures)
    return claude.ask(prompt, system=_system(bool(EEO.search(blob))), schema=PAGE_SCHEMA,
                      model=config.settings()["claude"]["apply_model"], purpose="apply-page")


def review(job, review_text: str) -> dict:
    prompt = (job_block(job) + "\n\nThis is Workday's final Review page for the application, just before"
              " Submit. Check it against the profile and answer bank. Report only real problems: wrong or"
              " missing required information, invented facts, wrong self-identification, wrong dates,"
              " wrong work authorization or sponsorship answers. Formatting differences are fine, and so are the"
              " approximations the rules allow when a list lacks the exact answer (a school_alternatives or"
              " field_alternatives value, or the closest related Field of Study).\n\n"
              "REVIEW PAGE TEXT:\n" + review_text[:12000])
    cfg = config.settings()["claude"]
    return claude.ask(prompt, system=_system(True), schema=REVIEW_SCHEMA, model=cfg["apply_model"],
                      purpose="apply-review", effort=cfg.get("review_effort", "medium"))
