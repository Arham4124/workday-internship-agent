"""Playwright Chromium session with a persistent profile (cookies survive between runs)."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import Page, sync_playwright

from . import config

log = logging.getLogger("agent.browser")
DOM_JS = (Path(__file__).parent / "dom.js").read_text()


class Browser:
    def __init__(self, headless: bool | None = None):
        s = config.settings()["browser"]
        self.headless = s["headless"] if headless is None else headless
        self.slow_mo = s.get("slow_mo_ms", 0)

    def __enter__(self):
        self.pw = sync_playwright().start()
        self.ctx = self.pw.chromium.launch_persistent_context(
            str(config.BROWSER_PROFILE), headless=self.headless, slow_mo=self.slow_mo,
            channel="chromium", viewport={"width": 1400, "height": 950}, locale="en-US",
            timezone_id="America/Chicago", accept_downloads=False)
        self.ctx.set_default_timeout(15000)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self

    def __exit__(self, *exc):
        try:
            self.ctx.close()
        finally:
            self.pw.stop()


STABLE_JS = """() => {
  const busy = document.querySelector('[aria-busy="true"], [data-automation-id*="loadingSpinner" i], [data-automation-id*="skeleton" i]');
  const b = document.body;
  return busy ? -1 : (b ? b.innerText.length : 0) + 1000 * document.querySelectorAll('input, button, textarea').length;
}"""


def settle(page: Page, ms: int = 300) -> None:
    """Wait until the page stops changing (two identical readings 0.3 s apart), max ~8 s."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except PWError:
        pass
    last, stable, deadline = None, 0, time.time() + 8
    while time.time() < deadline:
        try:
            cur = page.evaluate(STABLE_JS)
        except PWError:
            cur = None
        stable = stable + 1 if (cur is not None and cur != -1 and cur == last) else 0
        if stable >= 2:
            break
        last = cur
        time.sleep(0.3)
    if ms:
        page.wait_for_timeout(min(ms, 600))


def _extract_once(page: Page) -> dict:
    for _ in range(3):
        try:
            return page.evaluate(DOM_JS)
        except PWError as e:  # navigation in progress
            log.debug("extract retry: %s", e)
            settle(page, 800)
    return page.evaluate(DOM_JS)


def extract(page: Page, wait_s: float = 20) -> dict:
    """Read the page; if an application step is still a loading placeholder, wait for its form."""
    st = _extract_once(page)
    deadline = time.time() + wait_s
    def waiting(st):
        auth_buttons = any("signin" in (b.get("aid") or "").lower() and "utility" not in (b.get("aid") or "").lower()
                           for b in st["buttons"])
        broken = re.search(r"something went wrong|please refresh the page", st.get("text", ""), re.I)
        return (st["step"] and not re.search(r"review", st["step"], re.I) and not st["fields"]
                and not auth_buttons and not broken)
    while waiting(st) and time.time() < deadline:
        page.wait_for_timeout(1000)
        st = _extract_once(page)
    return st


SUBMIT_RE = re.compile(r"\bsubmit\b", re.I)


class ClickRefused(Exception):
    pass


def _label(h) -> str:
    return " ".join((h.evaluate("el => el.innerText || el.getAttribute('aria-label') || ''") or "").split())


def robust_click(page: Page, loc, allow_submit: bool = False) -> None:
    """Click a button once, safely.
    - A button labelled Submit is refused unless the caller is the deliberate final submit.
    - Fallbacks (Workday's click overlay, force click) run only if the SAME element still shows the SAME
      label, so a retry can never land on whatever replaced it (e.g. 'Next' turning into 'Submit')."""
    loc.scroll_into_view_if_needed(timeout=5000)
    h = loc.element_handle(timeout=5000)
    label = _label(h)
    if SUBMIT_RE.search(label) and not allow_submit:
        raise ClickRefused(f"refused to click '{label}' outside the final submit step")

    def unchanged() -> bool:
        try:
            return h.is_visible() and _label(h) == label
        except PWError:
            return False

    try:
        h.click(timeout=5000)
        return
    except PWError as e:
        log.debug("click failed (%s): %s", label, str(e)[:120])
    if not unchanged():
        return
    try:
        ov = h.evaluate_handle(
            "el => el.parentElement && el.parentElement.querySelector('[data-automation-id=\"click_filter\"]')").as_element()
        if ov:
            ov.click(timeout=4000)
            return
    except PWError:
        pass
    if not unchanged():
        return
    try:
        h.click(force=True, timeout=4000)
        return
    except PWError:
        pass
    if unchanged():
        h.evaluate("el => el.click()")


def click_button(page: Page, bid: str) -> None:
    robust_click(page, page.locator(f'[data-agent-btn="{bid}"]').first)


def click_text(page: Page, *texts: str, exact: bool = False, allow_submit: bool = False) -> bool:
    for t in texts:
        loc = page.get_by_role("button", name=t, exact=exact)
        if not loc.count():
            loc = page.get_by_role("link", name=t, exact=exact)
        if loc.count():
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    robust_click(page, loc.nth(i), allow_submit=allow_submit)
                    return True
    return False


def screenshot(page: Page, path: Path, full: bool = True) -> None:
    try:
        page.screenshot(path=str(path), full_page=full)
    except PWError as e:
        log.debug("screenshot failed: %s", e)
