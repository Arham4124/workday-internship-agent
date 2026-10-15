// Reads the current Workday page into a compact description: fields, buttons, errors, progress.
// Tags elements with data-agent-id / data-agent-btn so Python can act on them.
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const textOf = el => clean(el ? (el.innerText || el.textContent) : '');
  const vis = el => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 && r.height < 1) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0';
  };
  const byIds = ids => clean((ids || '').split(/\s+/).map(i => textOf(document.getElementById(i))).join(' '));
  const stripStar = s => clean(s).replace(/\s*\*\s*/g, ' ').replace(/\(required\)/ig, '').trim();
  document.querySelectorAll('[data-agent-id]').forEach(e => e.removeAttribute('data-agent-id'));
  document.querySelectorAll('[data-agent-btn]').forEach(e => e.removeAttribute('data-agent-btn'));

  const controlLabel = c => {
    const lb = c.getAttribute('aria-labelledby');
    if (lb) { const t = byIds(lb); if (t) return t; }
    if (c.id) { const l = document.querySelector(`label[for="${CSS.escape(c.id)}"]`); if (l && textOf(l)) return textOf(l); }
    if (c.getAttribute('aria-label')) return clean(c.getAttribute('aria-label'));
    const l = c.closest('label'); if (l && textOf(l)) return textOf(l);
    return clean(c.getAttribute('placeholder') || c.getAttribute('name') || c.getAttribute('data-automation-id') || '');
  };
  const boxLabel = (box, ctrl) => {
    const lg = box.querySelector('legend'); if (lg && textOf(lg)) return textOf(lg);
    const lb = [...box.querySelectorAll('label')].find(l => textOf(l) && !l.querySelector('input'));
    if (lb && !(ctrl && ctrl.type === 'radio')) return textOf(lb);
    if (lb) return textOf(lb);
    return ctrl ? controlLabel(ctrl) : '';
  };
  const prevHeading = el => {
    let n = el, depth = 0;
    while (n && depth < 10) {
      let s = n.previousElementSibling;
      while (s) {
        if (/^H[1-5]$/.test(s.tagName) || s.getAttribute('role') === 'heading') return textOf(s);
        const hs = s.querySelectorAll ? s.querySelectorAll('h2,h3,h4,h5,[role=heading]') : [];
        if (hs.length) return textOf(hs[hs.length - 1]);
        s = s.previousElementSibling;
      }
      n = n.parentElement; depth++;
    }
    return '';
  };
  const sectionOf = el => {
    const parts = []; let g = el.parentElement, depth = 0;
    while (g && depth < 30) {
      const role = g.getAttribute && g.getAttribute('role');
      if (role === 'group' || role === 'region' || g.tagName === 'SECTION' || g.tagName === 'FIELDSET') {
        let t = g.getAttribute('aria-label') || byIds(g.getAttribute('aria-labelledby'));
        if (t && !parts.includes(clean(t))) parts.unshift(clean(t).slice(0, 60));
      }
      g = g.parentElement; depth++;
    }
    return parts.slice(-2).join(' › ');
  };

  const BTN_INPUT = 'input[type=submit], input[type=button], input[type=reset], input[type=image]';
  const CTRL = 'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]), ' +
    'textarea, button[aria-haspopup="listbox"], [data-automation-id="multiselectInputContainer"]';
  const fields = []; const done = new Set(); let n = 0;
  const containers = new Map();
  for (const c of document.querySelectorAll(CTRL)) {
    const isDate = c.matches('input[data-automation-id^="dateSection"]');
    if (c.type === 'file' || isDate) { /* rendered hidden behind a styled box; include */ }
    else if (!vis(c) && !(c.type === 'radio' || c.type === 'checkbox')) continue;
    if (c.closest('[role="listbox"], [data-automation-id="activeListContainer"]')) continue;
    if (c.closest('header, nav, [role="banner"], [role="navigation"], [data-automation-id="utilityMenu"]') ||
        c.getAttribute('data-automation-id') === 'utilityMenuButton') continue;  // site header menus, not the form
    let box = c.closest('[data-automation-id^="formField-"]');
    if (!box && (c.type === 'radio' || c.type === 'checkbox')) box = c.closest('fieldset') || c.closest('[role="radiogroup"], [role="group"]');
    if (!box) box = c;
    if (!containers.has(box)) containers.set(box, []);
    containers.get(box).push(c);
  }
  for (const [box, ctrls] of containers) {
    if (done.has(box) || (!vis(box) && !ctrls.some(c => c.type === 'file' || (c.matches && c.matches('input[data-automation-id^="dateSection"]'))))) continue;
    done.add(box);
    const q = s => box.matches(s) ? box : box.querySelector(s);
    let type, value = '', options;
    const radios = ctrls.filter(c => c.type === 'radio');
    const checks = ctrls.filter(c => c.type === 'checkbox');
    if (q('[data-automation-id="multiselectInputContainer"], input[data-automation-id="searchBox"]')) {
      type = 'prompt';
      value = [...box.querySelectorAll('[data-automation-id="selectedItem"]')].map(textOf).filter(Boolean).join('; ');
    } else if (q('button[aria-haspopup="listbox"]')) {
      type = 'dropdown';
      const t = textOf(q('button[aria-haspopup="listbox"]'));
      value = /^select one|^select\b|^choose/i.test(t) ? '' : t;
    } else if (radios.length) {
      type = 'radio';
      options = radios.map(r => controlLabel(r));
      const ch = radios.find(r => r.checked); value = ch ? controlLabel(ch) : '';
    } else if (q('[data-automation-id^="dateSection"], [data-automation-id="dateInputWrapper"]')) {
      type = 'date';
      const seg = k => { const i = box.querySelector(`input[data-automation-id="dateSection${k}-input"]`); return i ? i.value : null; };
      const parts = ['Month', 'Day', 'Year'].filter(k => seg(k) !== null);
      options = parts;  // which segments exist
      value = parts.map(seg).join('/');
      if (!/\d/.test(value)) value = '';
    } else if (checks.length > 1) {
      type = 'checkboxes';
      options = checks.map(c => controlLabel(c));
      value = checks.filter(c => c.checked).map(c => controlLabel(c)).join('; ');
    } else if (checks.length === 1) {
      type = 'checkbox'; value = checks[0].checked;
    } else if (q('textarea')) {
      type = 'textarea'; value = q('textarea').value;
    } else if (q('input[type="file"]')) {
      type = 'file';
      value = [...box.querySelectorAll('[data-automation-id*="file-upload-item"], [data-automation-id="fileName"]')].map(textOf).join('; ');
    } else if (q('input[type="password"]')) {
      type = 'password'; value = q('input[type="password"]').value ? '(filled)' : '';
    } else if (q('input')) {
      const i = q('input'); type = 'text'; value = i.value;
      if (i.getAttribute('aria-readonly') === 'true' || i.readOnly) type = 'readonly';
    } else continue;
    const first = ctrls[0];
    const firstAid = (first.getAttribute && first.getAttribute('data-automation-id')) || '';
    if (firstAid === 'beecatcher' || /robots only|if you're human/i.test(textOf(box) + ' ' + controlLabel(first))) continue;  // bot trap: never touch
    const id = 'f' + (++n);
    box.setAttribute('data-agent-id', id);
    if (box === first && radios.length) radios.forEach(r => r.setAttribute('data-agent-id', id));
    let label = stripStar(box === first ? controlLabel(first) : boxLabel(box, first));
    if (type === 'checkbox' && box !== first) {
      const own = stripStar(controlLabel(checks[0]));
      if (own && own !== label) label = label ? `${label} — ${own}` : own;
    }
    const required = /\*/.test(textOf(box.querySelector('label, legend')) || '') ||
      ctrls.some(c => c.getAttribute('aria-required') === 'true' || c.required);
    const invalid = ctrls.some(c => c.getAttribute('aria-invalid') === 'true');
    const err = [...box.querySelectorAll('[data-automation-id="errorMessage"], [role="alert"]')].map(textOf).filter(Boolean).join(' ');
    const f = { id, type, label: label.slice(0, 400), required, value };
    if (options) f.options = options;
    const sec = sectionOf(box); if (sec) f.section = sec;
    const hd = prevHeading(box); if (hd && hd !== label) f.heading = hd.slice(0, 80);
    const aid = (first.getAttribute && first.getAttribute('data-automation-id')) || '';
    if (aid) f.aid = aid;
    if (invalid || err) f.error = err || 'invalid';
    fields.push(f);
  }

  const buttons = []; let b = 0;
  for (const el of document.querySelectorAll('button, [role="button"], a[data-automation-id], a[role="link"], ' + BTN_INPUT)) {
    if (!vis(el) || el.getAttribute('aria-haspopup') === 'listbox') continue;
    if (el.closest('[data-automation-id="selectedItem"], [role="listbox"]')) continue;
    const text = clean(el.innerText || (el.tagName === 'INPUT' ? el.value || el.alt : '') ||
                       el.getAttribute('aria-label') || el.getAttribute('title') || '');
    if (!text || text.length > 80) continue;
    const id = 'b' + (++b);
    el.setAttribute('data-agent-btn', id);
    const bb = { id, text };
    const aid = el.getAttribute('data-automation-id'); if (aid) bb.aid = aid;
    const hd = prevHeading(el); if (hd) bb.near = hd.slice(0, 60);
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') bb.disabled = true;
    buttons.push(bb);
    if (b >= 60) break;
  }

  const ERR = /^error\b|is required|must have a value|is invalid|not valid|must be|cannot be|please (enter|select)/i;
  const errors = [...new Set([...document.querySelectorAll(
    '[data-automation-id*="error" i], [role="alert"], [aria-live="assertive"], li, button')]
    .filter(vis).map(textOf)
    .filter(t => t && t.length < 300 && !/success/i.test(t) && (ERR.test(t))))].slice(0, 20);
  const steps = [...document.querySelectorAll('[data-automation-id="progressBar"] li')].map(li => {
    const lines = (li.innerText || '').split('\n').map(clean).filter(Boolean);
    return lines[lines.length - 1] || '';
  });
  const act = document.querySelector('[data-automation-id="progressBarActiveStep"]');
  let active = '';
  if (act) { const lines = (act.innerText || '').split('\n').map(clean).filter(Boolean); active = lines[lines.length - 1] || ''; }
  const captcha = [...document.querySelectorAll('iframe')].some(f => /recaptcha\/api2\/(anchor|bframe)|hcaptcha|arkoselabs|funcaptcha|captcha/i.test(f.src || f.title || '') && vis(f) && f.getBoundingClientRect().height > 40)
    || !!document.querySelector('#px-captcha');
  const main = document.querySelector('main, [data-automation-id="applyFlowPage"], [role="main"]') || document.body;
  return {
    url: location.href, title: document.title, step: active, steps,
    headings: [...document.querySelectorAll('h1, h2, h3')].filter(vis).map(textOf).filter(Boolean).slice(0, 12),
    fields, buttons, errors, captcha,
    text: clean(main.innerText).slice(0, 3000),
  };
}
