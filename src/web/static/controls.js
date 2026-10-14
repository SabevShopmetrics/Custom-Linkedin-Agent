/*
 * Собствени контроли вместо браузърските (web-ui-5): падащо меню, дата и час.
 * (Отметките са само CSS — tour.css.)
 *
 * Браузърският елемент остава в страницата — пази стойността, `onchange`, `disabled`, `min` и
 * формите на изгледите работят без промяна — но е скрит. До него стои наш бутон, който отваря
 * изскачащ панел в дизайна на агента. MutationObserver подменя всеки нов елемент автоматично,
 * така изгледите не знаят за това. Избор → `value` + събития `input` и `change`, както при
 * истинския елемент.
 */
import { h, icon, fmtDay, parseIso, isoOf, todayIso, MONTHS, WEEKDAYS_SHORT } from './core.js';

const SELECTOR = 'select:not([data-ui]):not([multiple]), input[type="date"]:not([data-ui]), input[type="time"]:not([data-ui])';
const POP = { el: null, anchor: null, onKey: null };

export function init() {
  upgradeIn(document.body);
  new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) if (node.nodeType === 1) upgradeIn(node);
    }
  }).observe(document.body, { childList: true, subtree: true });
  document.addEventListener('pointerdown', (e) => {
    if (POP.el && !POP.el.contains(e.target) && !(POP.anchor && POP.anchor.contains(e.target))) closePop();
  }, true);
  window.addEventListener('resize', closePop);
  document.addEventListener('scroll', (e) => { if (POP.el && !POP.el.contains(e.target)) closePop(); }, true);
}

function upgradeIn(root) {
  const nodes = root.matches && root.matches(SELECTOR) ? [root] : [];
  if (root.querySelectorAll) nodes.push(...root.querySelectorAll(SELECTOR));
  for (const el of nodes) upgrade(el);
}

function upgrade(el) {
  el.dataset.ui = '1';
  const kind = el.tagName === 'SELECT' ? 'select' : el.type;
  const label = h('span', { class: 'ui-label' });
  const button = h('button', {
    type: 'button',
    class: `ui-field ui-${kind}`,
    'aria-haspopup': kind === 'select' ? 'listbox' : 'dialog',
    'aria-label': el.getAttribute('aria-label') || null,
  }, kind === 'date' ? icon('calendar') : kind === 'time' ? icon('clock') : null, label, icon('chevR', 'ui-chev'));
  const sync = () => {
    label.textContent = display(el, kind);
    label.classList.toggle('placeholder', !el.value);
    button.disabled = el.disabled;
    button.title = el.title || '';
  };
  el.classList.add('ui-native');
  el.tabIndex = -1;
  el.setAttribute('aria-hidden', 'true');
  el.after(button);
  el.addEventListener('change', sync);
  el.addEventListener('input', sync);
  // Изгледите понякога сменят `disabled` или стойността след рисуване — следим атрибутите.
  new MutationObserver(sync).observe(el, { attributes: true, attributeFilter: ['disabled', 'value', 'min'] });
  button.addEventListener('click', () => {
    if (POP.anchor === button) { closePop(); return; }
    sync();
    if (kind === 'select') openSelect(el, button);
    else if (kind === 'date') openDate(el, button);
    else openTime(el, button);
  });
  button.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); button.click(); }
  });
  sync();
}

function display(el, kind) {
  if (kind === 'select') {
    const option = el.options[el.selectedIndex];
    return option ? option.textContent : '—';
  }
  if (kind === 'date') return el.value ? fmtDay(el.value) : 'Изберете дата';
  return el.value || '—';
}

function commit(el, value) {
  if (el.value === value) { closePop(); return; }
  el.value = value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  closePop();
}

// ------------------------------------------------------------------ изскачащ панел

function openPop(anchor, content, cls) {
  closePop();
  const pop = h('div', { class: `ui-pop ${cls}` }, content);
  // Вътре в модален <dialog> панелът трябва да е в него — иначе остава под горния слой.
  (anchor.closest('dialog[open]') || document.body).append(pop);
  POP.el = pop;
  POP.anchor = anchor;
  anchor.classList.add('open');
  const r = anchor.getBoundingClientRect();
  const width = Math.max(r.width, pop.offsetWidth);
  const left = Math.max(8, Math.min(window.innerWidth - width - 8, r.left));
  const below = window.innerHeight - r.bottom;
  pop.style.minWidth = `${r.width}px`;
  pop.style.left = `${left}px`;
  if (below < pop.offsetHeight + 12 && r.top > below) pop.style.bottom = `${window.innerHeight - r.top + 6}px`;
  else pop.style.top = `${r.bottom + 6}px`;
  POP.onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closePop(); anchor.focus(); }
  };
  document.addEventListener('keydown', POP.onKey, true);
  return pop;
}

function closePop() {
  if (!POP.el) return;
  POP.el.remove();
  if (POP.anchor) POP.anchor.classList.remove('open');
  document.removeEventListener('keydown', POP.onKey, true);
  POP.el = null;
  POP.anchor = null;
}

// ------------------------------------------------------------------ падащо меню

function openSelect(el, button) {
  const options = Array.from(el.options);
  const list = h('div', { class: 'ui-list', role: 'listbox', tabindex: '-1' }, options.map((o, i) => h('button', {
    type: 'button', role: 'option', class: `ui-option${o.selected ? ' selected' : ''}`,
    'aria-selected': String(o.selected), disabled: o.disabled || null, 'data-i': String(i),
    onclick: () => { commit(el, o.value); button.focus(); },
  }, h('span', null, o.textContent), o.selected ? icon('check') : null)));
  openPop(button, list, 'ui-pop-select');
  const items = Array.from(list.querySelectorAll('.ui-option:not([disabled])'));
  const current = list.querySelector('.selected') || items[0];
  if (current) { current.focus(); current.scrollIntoView({ block: 'nearest' }); }
  list.addEventListener('keydown', (e) => {
    const index = items.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); (items[index + 1] || items[0]).focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); (items[index - 1] || items[items.length - 1]).focus(); }
    else if (e.key === 'Home') { e.preventDefault(); items[0].focus(); }
    else if (e.key === 'End') { e.preventDefault(); items[items.length - 1].focus(); }
    else if (e.key === 'Tab') closePop();
    else if (e.key.length === 1) {
      const next = items.find((b) => b.textContent.toLowerCase().startsWith(e.key.toLowerCase()));
      if (next) next.focus();
    }
  });
}

// ------------------------------------------------------------------ дата

function openDate(el, button) {
  const selected = el.value;
  const min = el.min || '';
  const today = todayIso();
  let view = parseIso(selected) || parseIso(today);
  view = new Date(view.getFullYear(), view.getMonth(), 1);
  const wrap = h('div', { class: 'ui-cal', role: 'dialog', 'aria-label': 'Изберете дата' });
  const render = () => {
    const first = new Date(view.getFullYear(), view.getMonth(), 1);
    const start = new Date(first);
    start.setDate(1 - ((first.getDay() + 6) % 7));
    const days = [];
    for (let i = 0; i < 42; i += 1) {
      const d = new Date(start);
      d.setDate(start.getDate() + i);
      const iso = isoOf(d);
      const off = d.getMonth() !== view.getMonth();
      const blocked = Boolean(min && iso < min);
      days.push(h('button', {
        type: 'button', 'data-iso': iso,
        class: `ui-day${off ? ' off' : ''}${iso === today ? ' today' : ''}${iso === selected ? ' selected' : ''}`,
        disabled: blocked || null, 'aria-label': fmtDay(iso), 'aria-pressed': String(iso === selected),
        onclick: () => { commit(el, iso); button.focus(); },
      }, String(d.getDate())));
    }
    const month = MONTHS[view.getMonth()];
    wrap.replaceChildren(
      h('div', { class: 'ui-cal-head' },
        h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Предишен месец', onclick: () => { view.setMonth(view.getMonth() - 1); render(); } }, icon('chevL')),
        h('b', null, `${month[0].toUpperCase()}${month.slice(1)} ${view.getFullYear()}`),
        h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Следващ месец', onclick: () => { view.setMonth(view.getMonth() + 1); render(); } }, icon('chevR'))),
      h('div', { class: 'ui-cal-week' }, WEEKDAYS_SHORT.map((d) => h('span', null, d))),
      h('div', { class: 'ui-cal-grid' }, days),
      h('div', { class: 'ui-cal-foot' },
        h('button', { type: 'button', class: 'link-btn', disabled: (min && today < min) || null, onclick: () => commit(el, today) }, 'Днес'),
        !el.required && el.value ? h('button', { type: 'button', class: 'link-btn', onclick: () => commit(el, '') }, 'Без дата') : null));
  };
  render();
  openPop(button, wrap, 'ui-pop-cal');
  const focusDay = (iso) => {
    const target = wrap.querySelector(`[data-iso="${iso}"]`);
    if (target) target.focus();
  };
  focusDay(selected || today);
  wrap.addEventListener('keydown', (e) => {
    const iso = document.activeElement && document.activeElement.dataset.iso;
    if (!iso) return;
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[e.key];
    if (!step) return;
    e.preventDefault();
    const d = parseIso(iso);
    d.setDate(d.getDate() + step);
    if (d.getMonth() !== view.getMonth()) { view = new Date(d.getFullYear(), d.getMonth(), 1); render(); }
    focusDay(isoOf(d));
  });
}

// ------------------------------------------------------------------ час

function openTime(el, button) {
  const current = el.value || '10:00';
  const times = [];
  for (let m = 7 * 60; m <= 21 * 60 + 45; m += 15) times.push(`${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`);
  if (!times.includes(current)) {
    times.push(current);
    times.sort();
  }
  const field = h('input', { type: 'text', class: 'ui-time-input', value: current, inputmode: 'numeric', 'aria-label': 'Точен час', placeholder: 'чч:мм' });
  const accept = () => {
    const m = field.value.trim().match(/^([01]?\d|2[0-3])[:.]?([0-5]\d)$/);
    if (!m) { field.classList.add('bad'); return; }
    commit(el, `${m[1].padStart(2, '0')}:${m[2]}`);
    button.focus();
  };
  field.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); accept(); } });
  field.addEventListener('input', () => field.classList.remove('bad'));
  const list = h('div', { class: 'ui-list ui-times', role: 'listbox' }, times.map((t) => h('button', {
    type: 'button', role: 'option', class: `ui-option${t === current ? ' selected' : ''}`, 'aria-selected': String(t === current),
    onclick: () => { commit(el, t); button.focus(); },
  }, h('span', null, t), t === current ? icon('check') : null)));
  openPop(button, h('div', { class: 'ui-timepick' }, h('div', { class: 'ui-timepick-top' }, field, h('button', { type: 'button', class: 'btn primary small', onclick: accept }, 'OK')), list), 'ui-pop-time');
  const selected = list.querySelector('.selected');
  if (selected) selected.scrollIntoView({ block: 'center' });
  field.focus();
  field.select();
}
