"""Fill each kind of Workday form control. Every handler returns (ok, note)."""
from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import Page

from .browser import robust_click

log = logging.getLogger("agent.widgets")
OPTION_SEL = '[data-automation-id="promptOption"]:visible, [role="option"]:visible'
OPTIONS_JS = """() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  document.querySelectorAll('[data-agent-opt]').forEach(e => e.removeAttribute('data-agent-opt'));
  const out = [], seen = new Set();
  for (const el of document.querySelectorAll('[data-automation-id="promptOption"], [role="option"]')) {
    if (!vis(el) || el.closest('[data-automation-id="selectedItem"], [data-automation-id="selectedItemList"]')) continue;
    const outer = el.closest('[role="option"]') || el;
    if (seen.has(outer)) continue;
    seen.add(outer);
    const t = (outer.innerText || '').replace(/\\s+/g, ' ').trim();
    if (!t || /^no items\\.?$/i.test(t)) continue;
    outer.setAttribute('data-agent-opt', String(out.length));
    out.push(t);
  }
  return out;
}"""
PILL_X = ('[data-automation-id="selectedItem"] [data-automation-id="DELETE_charm"], '
          '[data-automation-id="selectedItem"] [aria-label*="remove" i], [data-automation-id="selectedItem"] [aria-label*="delete" i]')
LABELS_JS = """els => els.map(e => { const l = e.id && document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
  return ((l && l.innerText) || e.getAttribute('aria-label') || e.value || '').replace(/\\s+/g, ' ').trim(); })"""


def _n(s: str) -> str:
    s = (s or "").lower().replace("'", "").replace("\u2019", "").replace(".", "")  # M.D. -> md
    return " ".join(re.sub(r"[^a-z0-9+]", " ", s).split())


def _same(a: str, b: str) -> bool:
    strip = lambda x: _n(x)[4:] if _n(x).startswith("the ") else _n(x)
    return strip(a) == strip(b)


def strict_match(want: str, options: list[str]) -> int | None:
    """For multi-select lists (skills): the same item, allowing only a parenthetical or a "programming language"
    suffix ("Python (Programming Language)" for "Python", never "Python Requests")."""
    for i, o in enumerate(options):
        base = re.sub(r" (programming language|programming|language)$", "", _n(re.sub(r"\([^)]*\)", " ", o)))
        if base == _n(want) or _same(o, want):
            return i
    return None


def best_match(want: str, options: list[str]) -> int | None:
    """Pick the option that clearly corresponds to `want`; None if nothing is a confident match.
    Deliberately strict: a wrong selection on an application is worse than asking again."""
    w = _n(want)
    if not w:
        return None
    w = w[4:] if w.startswith("the ") else w
    best, best_score, scores = None, 0.0, []
    for i, o in enumerate(options):
        o2 = _n(o)
        o2 = o2[4:] if o2.startswith("the ") else o2
        if not o2:
            continue
        if o2 == w:
            return i
        wt, ot = w.split(), o2.split()
        if len(w) <= 3:  # Yes/No style: accept "No, ..." / "Yes, ..."
            score = 0.9 if ot[0] == w else 0.0
        elif o2.startswith(w + " ") or w.startswith(o2 + " "):
            score = 0.9
        elif len(w) >= 8 and w in o2 and "not" not in set(o2.split()) - set(wt):
            score = 0.8  # option is a more specific version of what was asked (and doesn't add a negation)
        elif len(wt) == 1 and len(ot) == 1 and len(w) >= 6 and w[:6] == o2[:6]:
            score = 0.75  # doctorate / doctoral, bachelors / bachelor
        else:
            score = difflib.SequenceMatcher(None, w, o2).ratio()
            score = score if score >= 0.88 else 0.0  # typos only
        scores.append((score, _n(o)))
        if score > best_score:
            best, best_score = i, score
    if best_score < 0.7:
        return None
    # Ambiguous: another, different option matches just as well (e.g. two answers that both start with "No").
    if sum(1 for sc, txt in scores if sc == best_score) > 1 and len({t for sc, t in scores if sc == best_score}) > 1:
        return None
    return best


def _ctrl(box, sel):
    loc = box.locator(sel)
    return loc.first if loc.count() else box


def _visible_texts(loc) -> list[str]:
    try:
        return [" ".join(t.split()) for t in loc.all_inner_texts()]
    except PWError:
        return []


def _popup_open(page: Page) -> bool:
    return bool(page.locator('[role="listbox"]:visible, [data-automation-id="activeListContainer"]:visible').count()
                or page.locator('text="No Items." >> visible=true').count())


def _close_popups(page: Page) -> None:
    for _ in range(2):
        if not _popup_open(page):
            return
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    if _popup_open(page):
        try:
            page.locator("h2:visible").first.click(force=True, timeout=2000)
            page.wait_for_timeout(300)
        except PWError:
            pass


def text(page, box, value):
    inp = _ctrl(box, 'textarea, input:not([type=hidden]):not([type=radio]):not([type=checkbox]):not([type=file])')
    inp.scroll_into_view_if_needed()
    inp.click()
    inp.fill("")
    inp.fill(str(value))
    inp.press("Tab")
    page.wait_for_timeout(250)
    got = inp.input_value()
    return _n(got) == _n(str(value)) or bool(got.strip()), got[:80]


def dropdown(page, box, value):
    _close_popups(page)  # a list left open by the previous field would be read instead
    btn = _ctrl(box, 'button[aria-haspopup="listbox"]')
    robust_click(page, btn)
    lb = page.locator('[role="listbox"]:visible').last
    try:
        lb.wait_for(state="visible", timeout=5000)
    except PWError:
        return False, "dropdown did not open"
    opts = lb.locator('[role="option"]')
    texts = _visible_texts(opts)
    idx = best_match(str(value), texts)
    if idx is None:  # long lists are virtualized: type-ahead then re-read
        page.keyboard.type(str(value)[:5], delay=90)
        page.wait_for_timeout(600)
        texts = _visible_texts(opts)
        idx = best_match(str(value), texts)
    if idx is None:
        page.keyboard.press("Escape")
        return False, f"no option like {value!r}. Options: {texts[:50]}"
    opt = opts.nth(idx)
    opt.scroll_into_view_if_needed()
    opt.click()
    page.wait_for_timeout(500)
    return True, " ".join(btn.inner_text().split())[:80]


def _pills(box) -> list[str]:
    return [t for t in _visible_texts(box.locator('[data-automation-id="selectedItem"]')) if t]


def _clear_pills(page, box) -> None:
    for _ in range(6):
        x = box.locator(PILL_X)
        if not x.count():
            return
        try:
            x.first.click(timeout=3000)
        except PWError:
            x.first.click(force=True, timeout=3000)
        page.wait_for_timeout(350)


def _poll_options(page, seconds: float) -> list[str]:
    for _ in range(int(seconds / 0.3)):
        page.wait_for_timeout(300)
        texts = page.evaluate(OPTIONS_JS)
        if texts or page.locator('text="No Items." >> visible=true').count():
            return texts
    return page.evaluate(OPTIONS_JS)


def _search(page, inp, query: str) -> list[str]:
    """Type like a person. Lists that filter as you type need no Enter (Enter would pick the highlighted
    item); server-search lists ignore typing until Enter, so press Enter only if typing changed nothing."""
    try:
        inp.click(timeout=4000)
    except PWError:
        inp.click(force=True, timeout=4000)
    page.wait_for_timeout(400)
    before = page.evaluate(OPTIONS_JS)
    inp.fill("")
    inp.press_sequentially(query, delay=35)
    texts = _poll_options(page, 1.2)
    if texts == before or (not texts and not before):
        inp.press("Enter")
        texts = _poll_options(page, 2.7)
    return texts


def _queries(value: str) -> list[str]:
    words = value.split()
    if words and words[0].lower() == "the":
        words = words[1:]
    stop = {"of", "at", "the", "and", "for", "in", "&"}
    qs = [value] + [" ".join(words[:n]) for n in (4, 3, 2) if len(words) > n and words[n - 1].lower() not in stop]
    qs += sorted({w.strip(",()") for w in words if len(w) >= 6}, key=len, reverse=True)[:2]
    return list(dict.fromkeys(q for q in qs if q))


def _remove_pills(page, box, texts: list[str]) -> None:
    for t in texts:
        items = box.locator('[data-automation-id="selectedItem"]')
        for i in range(items.count()):
            it = items.nth(i)
            if " ".join(it.inner_text().split()) == t:
                x = it.locator('[data-automation-id="DELETE_charm"], [aria-label*="remove" i], [aria-label*="delete" i]')
                if x.count():
                    try:
                        x.first.click(timeout=3000)
                    except PWError:
                        x.first.click(force=True, timeout=3000)
                    page.wait_for_timeout(350)
                break


def _pick(page, box, part: str, depth: int, probe_other: bool = True, strict: bool = False) -> tuple[list[str], list[str]]:
    """Try to select `part` in the open list: click a matching option, or search for it.
    Returns (search results or a category's contents, the list's first page), so that a miss can be
    reported with the real choices."""
    match = strict_match if strict else best_match
    inp = _ctrl(box, 'input[data-automation-id="searchBox"], input')
    if depth == 0:
        try:
            inp.click(timeout=4000)
        except PWError:
            inp.click(force=True, timeout=4000)
        page.wait_for_timeout(500)
    before = _pills(box)
    texts = page.evaluate(OPTIONS_JS)
    if depth > 0 and not texts:  # a sub-list (Category > Option) is still loading
        texts = _poll_options(page, 3.0)
    default_page = list(texts)
    found = []
    idx = match(part, texts)
    if idx is not None and depth == 0 and not _same(texts[idx], part):
        # only a partial match on the first page (e.g. "Applied Statistics" for "Statistics"): look for the exact one
        results = _search(page, inp, part)
        exact = next((i for i, t in enumerate(results) if _same(t, part)), None)
        if exact is not None:
            texts, idx = results, exact
        elif _pills(box) != before:  # select-on-Enter list picked something: let the caller verify it
            return results, default_page
        else:
            texts = page.evaluate(OPTIONS_JS)
            idx = match(part, texts)
    if idx is None and depth == 0:
        for q in _queries(part):
            texts = _search(page, inp, q)
            found += [t for t in texts if t not in found]
            if _pills(box) != before:  # this list selects on Enter: let the caller verify it
                return found, default_page
            idx = match(part, texts)
            if idx is not None:
                break
        if idx is None and probe_other and not any(_n(t).startswith("other") for t in found + default_page):
            # Nothing like it exists: show whether the list offers "Other" (its alphabetical first page never does)
            found += [t for t in _search(page, inp, "Other") if t not in found]
    if idx is not None:
        chosen = texts[idx]
        page.locator(f'[data-agent-opt="{idx}"]').first.click()
        page.wait_for_timeout(700)
        after = page.evaluate(OPTIONS_JS)
        if _pills(box) == before and after and after != texts:  # it was a category: report its contents
            return [f"{chosen} > {t}" for t in after], []
    return found, default_page


def _select_one(page, box, v: str, probe_other: bool, strict: bool = False) -> tuple[bool, list[str], list[str], str]:
    """Select one value ('Category > Option' paths allowed). Returns (selected?, search results, first page, note)."""
    path = [p.strip() for p in str(v).split(">")] if ">" in str(v) else [str(v)]
    before = _pills(box)
    found, default, note = [], [], ""
    for depth, part in enumerate(path):
        f, d = _pick(page, box, part, depth, probe_other, strict)
        if depth:  # options inside a category are reported with their full path
            d = [f"{' > '.join(path[:depth])} > {t}" for t in d]
        found, default = (f or found), (d or default)
        if depth < len(path) - 1 and _pills(box) != before:
            note = f"'{part}' is an option itself, not a category. "
            break
    _close_popups(page)
    after = _pills(box)
    new = [p for p in after if p not in before]
    match = strict_match if strict else best_match
    good = [p for p in new if match(path[-1], [p]) is not None]
    bad = [p for p in new if p not in good]
    if bad:
        _remove_pills(page, box, bad)  # never leave a value that wasn't asked for
    return bool(good) or match(path[-1], after) is not None, found, default, note


def prompt(page, box, value, fallbacks=()):
    """Searchable Workday list (three kinds: filter-as-you-type, search-on-Enter, select-on-Enter).
    Success is judged by what actually got selected in the field, never by what the list showed.
    A single value may come with fallbacks (other true answers, in order): the first one offered is kept."""
    _close_popups(page)
    pills = _pills(box)
    single = not isinstance(value, list)  # multi-select lists (e.g. skills) never need an "Other"
    if not single:
        groups = [[str(v)] for v in value if strict_match(str(v).split(">")[-1].strip(), pills) is None]
    else:
        group = list(dict.fromkeys(str(c) for c in [value, *(fallbacks or [])] if c))
        if pills and any(best_match(c.split(">")[-1].strip(), pills) is not None for c in group):
            return True, "; ".join(pills)[:120]
        if pills:
            _clear_pills(page, box)
        groups = [group]
    failed, found, default, notes, used = [], [], [], "", ""
    for group in groups:
        for i, v in enumerate(group):
            ok, f, d, n = _select_one(page, box, v, probe_other=single and i == len(group) - 1, strict=not single)
            found += [t for t in f if t not in found]
            default, notes = default or d, notes + n
            if ok:
                used = f" (fallback for {group[0]!r})" if i else ""
                break
        else:
            failed += group
    pills = _pills(box)
    if failed:
        seen = found + [t for t in default if t not in found]
        return False, (f"no option like {failed}. {notes}Options seen in this list (search results first): "
                       f"{seen[:50]}. Pick one of those (nested lists: 'Category > Option'). If none fits: the "
                       f"closest related option ('Other' only if nothing related), or leave the field empty if it "
                       f"is optional. Selected now: {pills}")
    return True, "; ".join(pills)[:120] + used


def read_options(page, field: dict, cap: int = 60) -> list[str]:
    box = page.locator(f'[data-agent-id="{field["id"]}"]').first
    try:
        if field["type"] == "dropdown":
            robust_click(page, _ctrl(box, 'button[aria-haspopup="listbox"]'))
            lb = page.locator('[role="listbox"]:visible').last
            lb.wait_for(state="visible", timeout=4000)
            texts = _visible_texts(lb.locator('[role="option"]'))
        else:
            _ctrl(box, 'input[data-automation-id="searchBox"], input').click()
            page.wait_for_timeout(900)
            texts = _visible_texts(page.locator(OPTION_SEL))
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        return [t for t in dict.fromkeys(texts) if t][:cap]
    except PWError:
        _close_popups(page)
        return []


def radio(page, box, value, group=None):
    radios = box.locator('input[type="radio"]') if box.locator('input[type="radio"]').count() else (group or box)
    labels = radios.evaluate_all(LABELS_JS)
    idx = best_match(str(value), labels)
    if idx is None:
        return False, f"no choice like {value!r}. Choices: {labels}"
    r = radios.nth(idx)
    try:
        r.check(force=True, timeout=4000)
    except PWError:
        r.evaluate("el => el.click()")
    page.wait_for_timeout(250)
    return r.is_checked(), labels[idx]


def checkbox(page, box, value):
    cb = _ctrl(box, 'input[type="checkbox"]')
    want = value if isinstance(value, bool) else str(value).strip().lower() in ("true", "yes", "1", "checked")
    if cb.is_checked() != want:
        try:
            cb.set_checked(want, force=True, timeout=4000)
        except PWError:
            cb.evaluate("el => el.click()")
        page.wait_for_timeout(250)
    return cb.is_checked() == want, str(want)


def checkboxes(page, box, value):
    wants = value if isinstance(value, list) else [v.strip() for v in str(value).split(";")]
    cbs = box.locator('input[type="checkbox"]')
    labels = cbs.evaluate_all(LABELS_JS)
    targets = {best_match(w, labels) for w in wants} - {None}
    if not targets:
        return False, f"no choice like {wants}. Choices: {labels}"
    for i in range(cbs.count()):
        want = i in targets
        if cbs.nth(i).is_checked() != want:
            try:
                cbs.nth(i).set_checked(want, force=True, timeout=4000)
            except PWError:
                cbs.nth(i).evaluate("el => el.click()")
            page.wait_for_timeout(200)
    return True, "; ".join(labels[i] for i in sorted(targets))


def date(page, box, value, segments):
    """Workday dates: click the visible month (or year) box, then type the digits; it auto-advances."""
    _close_popups(page)
    nums = re.findall(r"\d+", str(value))
    if not nums:
        return False, "no digits in date"
    year = next((x for x in reversed(nums) if len(x) == 4), nums[-1])
    month = nums[0] if len(nums) >= 2 else "01"
    day = nums[1] if len(nums) >= 3 else "01"
    vals = {"Month": month.zfill(2), "Day": day.zfill(2), "Year": year}
    segs = segments or ["Month", "Day", "Year"]
    for seg in segs:
        target = box.locator(f'[data-automation-id="dateSection{seg}-display"]').first
        if not target.count():
            target = box.locator(f'input[data-automation-id="dateSection{seg}-input"]').first
        try:
            target.click(timeout=4000)
        except PWError:
            target.click(force=True, timeout=4000)
        focus_js = "() => (document.activeElement && document.activeElement.getAttribute('data-automation-id')) || ''"
        if not page.evaluate(focus_js).startswith("dateSection"):
            inp = box.locator(f'input[data-automation-id="dateSection{seg}-input"]').first
            if inp.count():
                inp.focus()
            if not page.evaluate(focus_js).startswith("dateSection"):
                return False, "could not focus the date box (nothing typed)"
        page.keyboard.type(vals[seg], delay=90)
        page.wait_for_timeout(200)
    page.keyboard.press("Tab")
    page.wait_for_timeout(300)
    got = [box.locator(f'input[data-automation-id="dateSection{seg}-input"]').first.input_value()
           for seg in segs if box.locator(f'input[data-automation-id="dateSection{seg}-input"]').count()]
    want = [vals[s].lstrip("0") for s in segs]
    ok = [g.lstrip("0") for g in got] == want if got else True
    return ok, "/".join(got or [vals[s] for s in segs])


def upload(page, box, path: Path):
    fi = box.locator('input[type="file"]')
    if not fi.count():
        fi = page.locator('input[type="file"]')
    fi.first.set_input_files(str(path))
    for _ in range(40):
        page.wait_for_timeout(750)
        if path.name.lower() in (box.inner_text() or "").lower() or path.name.lower() in page.inner_text("body").lower():
            return True, path.name
    return False, "upload did not finish"


def apply_value(page: Page, field: dict, value, docs: dict, fallbacks=()) -> tuple[bool, str]:
    group = page.locator(f'[data-agent-id="{field["id"]}"]')
    box = group.first
    if not box.count():
        return False, "field no longer on page"
    t = field["type"]
    try:
        if t in ("dropdown", "radio") and isinstance(value, str) and fallbacks:
            cands = list(dict.fromkeys([value, *map(str, fallbacks)]))
            for i, v in enumerate(cands):
                ok, note = dropdown(page, box, v) if t == "dropdown" else radio(page, box, v, group)
                if ok:
                    return True, note + (f" (fallback for {value!r})" if i else "")
            return False, f"none of {cands} is offered. {note}"
        if t in ("text", "textarea"):
            return text(page, box, value)
        if t == "dropdown":
            return dropdown(page, box, value)
        if t == "prompt":
            return prompt(page, box, value, fallbacks if isinstance(value, str) else ())
        if t == "radio":
            return radio(page, box, value, group)
        if t == "checkbox":
            return checkbox(page, box, value)
        if t == "checkboxes":
            return checkboxes(page, box, value)
        if t == "date":
            return date(page, box, value, field.get("options"))
        if t == "file":
            p = docs.get(str(value))
            if not p:
                return False, f"no document named {value!r} available"
            return upload(page, box, p)
        return False, f"cannot fill a {t} field"
    except PWError as e:
        _close_popups(page)
        return False, f"browser error: {str(e).splitlines()[0][:160]}"
