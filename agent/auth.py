"""Workday sign-in / account creation / password reset / e-mail verification.

Passwords come from the Keychain and are typed by this code; Claude never sees them.
"""
from __future__ import annotations

import logging
import re

from playwright.sync_api import Page

from . import config, db, mail, secrets
from .browser import click_text, extract, robust_click, settle

log = logging.getLogger("agent.auth")
EXISTS = re.compile(r"already (exists|in use|registered|associated)|account (with|for) this email|email (address )?is (already )?(in use|taken|registered)", re.I)
BAD_LOGIN = re.compile(r"wrong|invalid|incorrect|not recognized|try again|locked", re.I)
VERIFY = re.compile(r"verif|activate your account|check your email|confirm your email", re.I)


class NeedsHuman(Exception):
    pass


def _first(page: Page, *sels):
    for s in sels:
        loc = page.locator(s)
        for i in range(loc.count()):
            if loc.nth(i).is_visible():
                return loc.nth(i)
    return None


def _fill(loc, value: str) -> None:
    loc.click()
    loc.fill("")
    loc.fill(value)


def _email_box(page):
    return _first(page, 'input[data-automation-id="email"]', 'input[type="email"]',
                  'input[autocomplete="email"]', 'input[name*="email" i]')


def _pw_boxes(page):
    loc = page.locator('input[type="password"]')
    return [loc.nth(i) for i in range(loc.count()) if loc.nth(i).is_visible()]


def _submit(page, aid: str, *texts: str) -> None:
    btn = _first(page, f'[data-automation-id="{aid}"]')
    if btn:
        robust_click(page, btn)
    elif not click_text(page, *texts, exact=True):
        raise NeedsHuman(f"could not find the {texts[0]} button")
    settle(page, 2000)


def _page_errors(page) -> str:
    st = extract(page)
    return " ".join(st["errors"]) + " " + st["text"][:600]


def _remember(con, tenant, host, source, pw):
    secrets.set_tenant_password(tenant, pw)
    con.execute("INSERT OR REPLACE INTO accounts(tenant,host,email,password_source,created_at,verified,last_ok)"
                " VALUES(?,?,?,?,?,1,?)", (tenant, host, config.profile()["contact"]["email"], source, db.now(), db.now()))
    con.commit()


def sign_in(page, job, con, state) -> None:
    tenant, email = job["tenant"], config.profile()["contact"]["email"]
    tries = state.setdefault("pw_tried", [])
    stored, common = secrets.tenant_password(tenant), secrets.common_workday_password()
    pw, source = None, None
    if stored and "stored" not in tries:
        pw, source = stored, "stored"
    elif common and "common" not in tries and common != stored:
        pw, source = common, "common"
    if pw is None:
        if state.get("account_exists") or state.get("created"):
            return reset_password(page, job, con, state)
        state["want_create"] = True
        link = _first(page, '[data-automation-id="createAccountLink"]')
        if link:
            robust_click(page, link)
        elif not click_text(page, "Create Account"):
            raise NeedsHuman("sign-in failed and no Create Account option")
        settle(page)
        return
    tries.append(source)
    box = _email_box(page)
    if box:
        _fill(box, email)
    _fill(_pw_boxes(page)[0], pw)
    _submit(page, "signInSubmitButton", "Sign In")
    text = _page_errors(page)
    if _pw_boxes(page) and VERIFY.search(text) and not BAD_LOGIN.search(text):
        return verify_email(page, job, con, state)
    if _pw_boxes(page):
        log.info("[%s] sign-in with %s password rejected", tenant, source)
        if state.get("unconfirmed_pw") and source == "stored":
            state["account_exists"] = True  # creation was silently refused: an account already existed
        return
    _remember(con, tenant, job["host"], source, pw)
    log.info("[%s] signed in (%s password)", tenant, source)


def create_account(page, job, con, state) -> None:
    tenant, email = job["tenant"], config.profile()["contact"]["email"]
    if not state.get("want_create"):
        link = _first(page, '[data-automation-id="signInLink"]')
        if link and (secrets.tenant_password(tenant) or secrets.common_workday_password()):
            robust_click(page, link)  # try existing credentials first
            settle(page)
            return
    if state.get("created"):
        raise NeedsHuman("account creation did not complete")
    mode = config.settings()["accounts"]["new_account_password"]
    pw = secrets.common_workday_password() if mode == "common" and secrets.common_workday_password() else secrets.new_password()
    box = _email_box(page)
    if box:
        _fill(box, email)
    boxes = _pw_boxes(page)
    for b in boxes[:2]:
        _fill(b, pw)
    for cb in page.locator('input[type="checkbox"]:visible').all():
        if not cb.is_checked():
            cb.set_checked(True, force=True)
    since = mail.utcnow()
    _submit(page, "createAccountSubmitButton", "Create Account")
    state["created"] = True
    text = _page_errors(page)
    if EXISTS.search(text):
        state["account_exists"] = True
        log.info("[%s] account already exists -> password reset", tenant)
        return reset_password(page, job, con, state)
    _remember(con, tenant, job["host"], mode, pw)
    st = extract(page)
    in_form = bool(st.get("step")) and not re.search(r"sign in|create account", st["step"], re.I) and st["fields"]
    if in_form:
        log.info("[%s] account created", tenant)
        return
    log.info("[%s] account submitted; checking Gmail for a verification e-mail", tenant)
    state["unconfirmed_pw"] = True
    link = mail.find_link("verify", tenant, since, wait_seconds=150)
    if link:
        state["verified_once"] = True
        page.goto(link)
        settle(page, 2000)
    page.goto(job["url"])
    settle(page)


def verify_email(page, job, con, state, since=None) -> None:
    if state.get("verified_once"):
        raise NeedsHuman("Workday still asks for e-mail verification")
    link = mail.find_link("verify", job["tenant"], since or mail.utcnow())
    if not link:
        raise NeedsHuman("verification e-mail did not arrive (check Gmail, then re-run)")
    state["verified_once"] = True
    page.goto(link)
    settle(page, 2500)
    page.goto(job["url"])
    settle(page)


def reset_password(page, job, con, state) -> None:
    if state.get("reset_done"):
        raise NeedsHuman("password reset did not let the agent sign in")
    state["reset_done"] = True
    if not click_text(page, "Forgot your password?", "Forgot Password", "Forgot your password"):
        link = _first(page, '[data-automation-id="forgotPasswordLink"]')
        if not link:
            if not click_text(page, "Sign In"):
                raise NeedsHuman("no 'Forgot password' link")
            settle(page)
            link = _first(page, '[data-automation-id="forgotPasswordLink"]')
            if not link:
                raise NeedsHuman("no 'Forgot password' link")
        robust_click(page, link)
    settle(page)
    _fill(_email_box(page), config.profile()["contact"]["email"])
    since = mail.utcnow()
    if not click_text(page, "Submit", "Send", "Reset Password", "Continue", "Next", exact=True, allow_submit=True):
        raise NeedsHuman("could not submit password reset")
    settle(page)
    link = mail.find_link("reset", job["tenant"], since)
    if not link:
        raise NeedsHuman("password reset e-mail did not arrive")
    page.goto(link)
    settle(page, 2000)
    pw = secrets.new_password()
    boxes = _pw_boxes(page)
    if len(boxes) < 2:
        raise NeedsHuman("unexpected password reset page")
    for b in boxes[-2:]:
        _fill(b, pw)
    click_text(page, "Submit", "Reset Password", "Save", "Continue", "Change Password", exact=True, allow_submit=True)
    settle(page, 2000)
    _remember(con, job["tenant"], job["host"], "reset", pw)
    state["pw_tried"] = []
    page.goto(job["url"])
    settle(page)
