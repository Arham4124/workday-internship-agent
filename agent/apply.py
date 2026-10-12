"""Fill one Workday application, page by page, up to the Review page.

Safeguards: answers come only from your profile/answer bank; questions it cannot answer
pause the job for you; Claude re-checks the Review page; the Submit button is clicked
only when settings.yaml says mode: auto; every value entered is logged in data/runs/.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime

from . import auth, config, planner, widgets
from .browser import Browser, ClickRefused, click_button, click_text, extract, robust_click, screenshot, settle

log = logging.getLogger("agent.apply")
SUBMITTED = re.compile(r"application (has been |was )?(successfully )?submitted|thank you for (applying|your application)|"
                       r"we('ve| have) received your application|successfully applied", re.I)
ALREADY = re.compile(r"already applied|you have applied for this|previously applied for this", re.I)
CLOSED = re.compile(r"no longer (accepting|available)|job (posting )?(is )?(closed|expired)|"
                    r"page you are looking for|position has been filled", re.I)
NEXT_TEXTS = ("Save and Continue", "Next", "Continue")


class NeedsAnswer(Exception):
    def __init__(self, question, reason=""):
        super().__init__(question)
        self.question, self.reason = question, reason


class Run:
    def __init__(self, job):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{job['company']}_{job['req_id'] or job['title']}")[:60]
        self.dir = config.RUNS / f"{stamp}_{safe}"
        (self.dir / "pages").mkdir(parents=True, exist_ok=True)
        self.n = 0

    def save(self, name, obj):
        self.n += 1
        with open(self.dir / "pages" / f"{self.n:03d}_{name}.json", "w") as f:
            json.dump(obj, f, indent=1, ensure_ascii=False, default=str)

    def action(self, **kw):
        with open(self.dir / "actions.jsonl", "a") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), **kw}, ensure_ascii=False) + "\n")


def classify(st: dict) -> str:
    t = f"{st['text']} {' '.join(st['headings'])}"
    btns = [b["text"].lower() for b in st["buttons"]]
    aids = {b.get("aid") for b in st["buttons"]}
    pws = [f for f in st["fields"] if f["type"] == "password"]
    if st["captcha"]:
        return "captcha"
    if re.search(r"something went wrong|please refresh the page", t, re.I) and not st["fields"]:
        return "wd_error"
    if ALREADY.search(t):
        return "already_applied"
    if pws:
        return "create_account" if len(pws) >= 2 else "sign_in"
    if not st["step"] and SUBMITTED.search(t):
        return "submitted"
    if not st["step"] and CLOSED.search(t):
        return "closed"
    if st["step"] and re.search(r"sign in|create account", st["step"], re.I):
        return "auth_choice"
    if st["step"]:
        return "review" if re.search(r"review", st["step"], re.I) else "form"
    if any(b in btns for b in ("apply manually", "autofill with resume", "use my last application", "continue application")):
        return "apply_options"
    if "adventureButton" in aids or any(b in ("apply", "apply now") for b in btns):
        return "job_page"
    if aids & {"SignInWithEmailButton", "createAccountLink", "signInLink"} or "sign in with email" in btns:
        return "auth_choice"
    header_only = all((b.get("aid") or "").startswith(("utility", "navigationItem", "accessibility", "socialIcon", "privacy"))
                      or b["text"].lower() in ("sign in", "home", "search for jobs", "close") for b in st["buttons"])
    if header_only and not st["fields"]:
        return "loading"  # only the site header is drawn so far (its "Sign In" is NOT the application's sign-in)
    if re.search(r"verify your|check your email|activate your account", t, re.I):
        return "verify"
    return "unknown"


def _next_button(page, allow_submit=False, wait_s: float = 10):
    deadline = time.time() + wait_s
    while True:  # the bottom navigation can render a moment after the fields
        loc = page.locator('[data-automation-id="bottom-navigation-next-button"]:visible')
        if not loc.count():
            for t in NEXT_TEXTS + (("Submit",) if allow_submit else ()):
                cand = page.get_by_role("button", name=t, exact=True)
                if cand.count() and cand.first.is_visible():
                    loc = cand
                    break
        if loc.count() or time.time() > deadline:
            break
        page.wait_for_timeout(500)
    if not loc.count():
        return None
    if re.search(r"submit", loc.first.inner_text(), re.I) and not allow_submit:
        raise auth.NeedsHuman("refused to click Submit outside the Review step")
    return loc.first


def _find_btn(st, ref):
    for b in st["buttons"]:
        if b["text"] == ref["text"] and b.get("near") == ref.get("near") and b.get("aid") == ref.get("aid"):
            return b
    return None


def ensure_entries(page, st):
    """Add blank Work Experience / Education entries up to what the profile needs, without Claude."""
    prof = config.profile()
    want = {"Work Experience": min(prof.get("max_experience_entries_on_forms", 4), len(prof["experience"])),
            "Education": len(prof["education"])}
    for section, n_want in want.items():
        for _ in range(8):
            have = len({f.get("heading") for f in st["fields"]
                        if re.fullmatch(rf"{section} \d+", f.get("heading") or "")})
            if have >= n_want:
                break
            btn = next((b for b in st["buttons"] if b["text"] in ("Add", "Add Another")
                        and section.lower() in (b.get("near") or "").lower()), None)
            if not btn:
                break
            click_button(page, btn["id"])
            settle(page, 700)
            st = extract(page)
    return st


def fill_step(page, job, st, state, run, docs):
    step = st["step"]
    rounds = state["rounds"][step] = state["rounds"].get(step, 0) + 1
    if rounds == 1:
        st = ensure_entries(page, st)
    if rounds > 8:
        raise auth.NeedsHuman(f"stuck on '{step}': {st['errors'][:3] or state['failures'].get(step)}")
    empties = [f for f in st["fields"] if f["required"] and f["value"] in ("", None, False) and f["type"] != "readonly"]
    if state.get("fast") and not empties and not st["errors"] and rounds == 1:
        plan = {"fill": [], "proceed": True, "note": "already complete (approved re-run)"}
    else:
        # Options are not pre-read in general (~1 s per field): Claude answers from the question text and
        # any miss comes back with the real options. Exception: "How did you hear about us" lists, whose
        # categories differ at every company, are read up front.
        for f in st["fields"]:
            if f["type"] == "prompt" and not f["value"] and re.search(r"hear about|source", f["label"], re.I):
                if opts := widgets.read_options(page, f):
                    f["options"] = opts
        notes = [f"A previous attempt's final check flagged: {n} -> fix this if it is on this page" for n in state["notes"]]
        plan = planner.plan_page(job, st, state["failures"].get(step, []) + notes)
    run.save("plan_" + re.sub(r"[^a-z]+", "_", step.lower()), {"state": st, "plan": plan})
    if nh := plan.get("needs_human"):
        qs = [q for q in (nh.get("questions") or []) + [nh.get("question")] if q]
        if qs:
            raise NeedsAnswer(list(dict.fromkeys(qs)), nh.get("reason", ""))
    for e in plan.get("essays") or []:
        run.action(step=step, kind="essay", question=e.get("question"), answer=e.get("answer"))
    by_id = {f["id"]: f for f in st["fields"]}
    failures = []
    for item in plan.get("fill") or []:
        f = by_id.get(item.get("id"))
        value = item.get("values") or item.get("value")
        if not f or f["type"] in ("password", "readonly") or value in (None, "", []):
            continue
        if f["type"] == "file" and value == "cover_letter" and "cover_letter" not in docs:
            from .cover_letter import make
            docs["cover_letter"] = make(job, run.dir)
        ok, note = widgets.apply_value(page, f, value, docs)
        shown = value if f["type"] != "textarea" else str(value)[:300]
        run.action(step=step, label=f["label"][:120], type=f["type"], value=shown, ok=ok, note=note)
        if not ok:
            failures.append(f"{f['id']} ({f['label'][:60]}): {note}")
    for bid in plan.get("remove") or []:
        if any(b["id"] == bid for b in st["buttons"]):
            click_button(page, bid)
            settle(page, 800)
            return  # ids shift after a delete; re-plan
    added = False
    for add in plan.get("add") or []:
        ref = next((b for b in st["buttons"] if b["id"] == add["button"]), None)
        cur = st
        for _ in range(max(1, min(int(add.get("times", 1)), 6)) if ref else 0):
            b = _find_btn(cur, ref)
            if not b:
                break
            click_button(page, b["id"])
            settle(page, 700)
            cur = extract(page)
            added = True
    if added:
        return
    if plan.get("click"):
        b = next((b for b in st["buttons"] if b["id"] == plan["click"]), None)
        if b and not re.search(r"submit", b["text"], re.I):
            click_button(page, b["id"])
            settle(page)
            return
    state["failures"][step] = failures
    idle = not (plan.get("fill") or plan.get("add") or plan.get("remove") or plan.get("click") or plan.get("proceed"))
    if idle:  # Claude saw nothing to do (e.g. page half-loaded): reload instead of asking again
        state.setdefault("idle", {})[step] = state.get("idle", {}).get(step, 0) + 1
        if state["idle"][step] >= 2:
            page.reload()
            settle(page, 2500)
        return
    if failures or not plan.get("proceed"):
        return
    btn = _next_button(page)
    if not btn:
        raise auth.NeedsHuman(f"no Next button on '{step}'")
    robust_click(page, btn)
    settle(page, 1500)
    after = extract(page)
    if after["step"] == step and after["errors"]:
        state["failures"][step] = [f"page errors after Next: {after['errors'][:8]}"]


def review_and_submit(page, job, mode, run):
    screenshot(page, run.dir / "review.png")
    text = page.inner_text("body")
    (run.dir / "review.txt").write_text(text)
    r = planner.review(job, text)
    run.save("review_check", r)
    if not r["ok"]:
        state_notes = "; ".join(r["problems"])[:600]
        return "needs_human", "Final check found problems: " + state_notes, r["problems"]
    if mode != "auto":
        page.wait_for_timeout(1500)
        if SUBMITTED.search(page.inner_text("body")) or not extract(page, wait_s=0)["step"]:
            return "submitted", "WARNING: Workday shows this as submitted although the run was in review mode.", []
        return "ready_for_review", "Filled and checked; stopped at the Review page.", []
    btn = _next_button(page, allow_submit=True)
    if btn is not None and re.search(r"submit", btn.inner_text(), re.I):
        robust_click(page, btn, allow_submit=True)  # the one place a Submit button may be pressed
    elif not click_text(page, "Submit", exact=True, allow_submit=True):
        return "needs_human", "could not find the Submit button", []
    settle(page, 4000)
    screenshot(page, run.dir / "after_submit.png")
    after = extract(page)
    if SUBMITTED.search(after["text"]) or (not after["step"] and not after["errors"]):
        return "submitted", "Submitted.", []
    return "needs_human", "Clicked Submit but saw no confirmation; check the company's candidate home page.", []


def apply_job(con, job, mode: str, headed: bool | None = None, fast: bool = False,
              notes: list[str] | None = None) -> dict:
    run = Run(job)
    docs = {k: p for k in ("resume", "transcript") if (p := config.doc_path(k))}
    state = {"rounds": {}, "failures": {}, "fast": fast and not notes, "notes": notes or []}
    counts: dict[str, int] = {}
    t0 = time.monotonic()  # pauses while the Mac sleeps

    def done(status, reason, **extra):
        return {"status": status, "reason": reason, "run_dir": str(run.dir), **extra}

    with Browser(headless=None if headed is None else not headed) as b:
        b.page.goto(job["url"])
        settle(b.page, 2000)
        for _ in range(60):
            page = b.ctx.pages[-1]  # follow new tabs
            if time.monotonic() - t0 > 25 * 60:
                return done("failed", "timed out after 25 minutes")
            st = extract(page)
            kind = classify(st)
            counts[kind] = counts.get(kind, 0) + 1
            log.info("[%s] page=%s step=%s", job["tenant"], kind, st["step"])
            run.save(kind, {k: st[k] for k in ("url", "step", "headings", "errors", "buttons")} | {"fields": len(st["fields"])})
            if counts[kind] > 12 and kind != "form":
                screenshot(page, run.dir / "stuck.png")
                return done("needs_human", f"stuck on a '{kind}' page")
            try:
                if kind == "captcha":
                    screenshot(page, run.dir / "captcha.png")
                    return done("needs_human", "Workday showed a CAPTCHA; please apply to this one manually")
                if kind in ("submitted", "already_applied", "closed"):
                    return done(kind, kind.replace("_", " "))
                if kind == "wd_error":  # transient Workday error page: reload, then restart from the posting
                    if counts[kind] <= 2:
                        page.reload()
                    else:
                        page.goto(job["url"])
                    settle(page, 2500)
                    continue
                if kind == "job_page":
                    if not click_text(page, "Apply", "Apply Now", exact=True):
                        robust_click(page, page.locator('[data-automation-id="adventureButton"]').first)
                    settle(page)
                elif kind == "apply_options":
                    applied_here = con.execute(
                        "SELECT COUNT(*) FROM jobs WHERE tenant=? AND status IN ('submitted','ready_for_review')",
                        (job["tenant"],)).fetchone()[0]
                    order = ("Continue Application", "Use My Last Application", "Apply Manually") if applied_here \
                        else ("Continue Application", "Apply Manually", "Autofill with Resume")
                    click_text(page, *order, exact=True)
                    settle(page)
                elif kind == "loading":
                    if counts[kind] in (4, 8):
                        page.goto(job["url"])  # stuck half-drawn: start again from the posting
                    settle(page, 1000)
                elif kind == "auth_choice":
                    target = ('[data-automation-id="createAccountLink"]' if state.get("want_create")
                              else '[data-automation-id="SignInWithEmailButton"], [data-automation-id="signInLink"]')
                    loc = page.locator(target + ', ' + '[data-automation-id="SignInWithEmailButton"]')
                    if loc.count():
                        robust_click(page, loc.first)
                    elif not click_text(page, "Sign in with email", "Sign In with Email"):
                        click_text(page, "Create Account" if state.get("want_create") else "Sign In", exact=True)
                    settle(page)
                elif kind == "sign_in":
                    auth.sign_in(page, job, con, state)
                elif kind == "create_account":
                    auth.create_account(page, job, con, state)
                elif kind == "verify":
                    auth.verify_email(page, job, con, state)
                elif kind == "review":
                    status, reason, problems = review_and_submit(page, job, mode, run)
                    return done(status, reason, review_problems=problems)
                else:
                    if kind == "unknown":
                        st["step"] = f"unknown:{st['title'][:40]}"
                    fill_step(page, job, st, state, run, docs)
            except NeedsAnswer as e:
                return done("needs_answer", " | ".join(e.question), questions=e.question, context=e.reason)
            except (auth.NeedsHuman, ClickRefused) as e:
                screenshot(page, run.dir / "needs_human.png")
                return done("needs_human", str(e))
        return done("needs_human", "too many steps without reaching Review")
