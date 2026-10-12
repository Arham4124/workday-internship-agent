"""Tailored cover letter (only when a form requires one), rendered to PDF."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pymupdf as fitz

from . import claude, config, planner

SCHEMA = {"type": "object", "properties": {"body": {"type": "string"}}, "required": ["body"]}


def make(job, run_dir: Path) -> Path:
    p = config.profile()
    system = ("You write concise, specific cover letters strictly from the applicant profile; never invent "
              "facts. 3 short paragraphs, under 280 words, no greeting or sign-off lines (added separately).\n\n"
              "APPLICANT PROFILE (YAML)\n" + config.profile_text(False))
    res = claude.ask(planner.job_block(job), system=system, schema=SCHEMA,
                     model=config.settings()["claude"]["apply_model"], purpose="cover-letter")
    name = p["name"]["full"]
    text = (f"{name}\n{p['contact']['email']} | {p['contact']['phone_formatted']}\n\n"
            f"{date.today():%B %d, %Y}\n\nDear Hiring Team at {job['company']},\n\n"
            f"{res['body'].strip()}\n\nSincerely,\n{name}")
    out = run_dir / f"Cover_Letter_{name.replace(' ', '_')}.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(fitz.Rect(72, 72, 540, 720), text, fontsize=11, fontname="tiro", lineheight=1.35)
    doc.save(out)
    (run_dir / "cover_letter.txt").write_text(text)
    return out
