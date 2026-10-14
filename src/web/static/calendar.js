/*
 * Календар (#/kalendar, #/kalendar/2026-11) — всички насрочени постове по дни (web-ui-3).
 *
 * Един източник: /api/posts (scheduled_for/scheduled_time/approved/needs_brief от web.json
 * на всеки пост). Два изгледа: „Месец" (мрежа, плъзгане между дни) и „Списък" (по дни —
 * на телефон е подразбиращ се, мрежата там е нечетима). Клик върху пост отваря панел за
 * бърза поправка (drawer.js) — текст на ръка, „помоли агента", дата/час, одобрение, изтриване.
 */
import { api, h, icon, toast, enc, storage, MONTHS, WEEKDAYS_SHORT, isoOf, parseIso, todayIso, fmtDay, postState } from './core.js';
import { store, on, loadPosts, runningJobs } from './store.js';
import * as drawer from './drawer.js';

const NARROW = 760;
const C = { month: null, view: null, runningKey: '' }; // month: Date — първо число на показания месец
let root = null;

const narrow = () => window.innerWidth < NARROW;
const currentView = () => C.view || storage('cal-view') || (narrow() ? 'list' : 'month');

export function show(monthIso) {
  root = document.getElementById('view-calendar');
  const m = (monthIso || '').match(/^(\d{4})-(\d{2})$/);
  const today = parseIso(todayIso());
  C.month = m ? new Date(+m[1], +m[2] - 1, 1) : (C.month || new Date(today.getFullYear(), today.getMonth(), 1));
  document.title = 'Календар — LinkedIn агент';
  render();
  loadPosts();
}

export function init() {
  on('posts', () => { if (store.route.name === 'calendar') render(); });
  // /api/info идва на 4 s — прерисуваме само ако се е сменило кой пише (иначе скролът подскача).
  on('info', () => {
    const key = runningJobs().map((j) => j.post_id).sort().join('|');
    if (key === C.runningKey) return;
    C.runningKey = key;
    if (store.route.name === 'calendar') render();
  });
  let wasNarrow = narrow();
  window.addEventListener('resize', () => {
    if (narrow() === wasNarrow) return;
    wasNarrow = narrow();
    if (store.route.name === 'calendar' && !C.view && !storage('cal-view')) render();
  });
}

const monthKey = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;

function go(delta) {
  const d = new Date(C.month.getFullYear(), C.month.getMonth() + delta, 1);
  location.hash = `#/kalendar/${monthKey(d)}`;
}

function setView(view) {
  C.view = view;
  storage('cal-view', view);
  render();
}

function render() {
  if (!root) return;
  const running = new Set(runningJobs().map((j) => j.post_id));
  const today = todayIso();
  const first = C.month;
  const byDay = new Map();
  for (const p of store.posts) {
    if (!p.scheduled_for) continue;
    if (!byDay.has(p.scheduled_for)) byDay.set(p.scheduled_for, []);
    byDay.get(p.scheduled_for).push(p);
  }
  for (const list of byDay.values()) list.sort((a, b) => (a.scheduled_time || '99').localeCompare(b.scheduled_time || '99'));

  const monthItems = store.posts.filter((p) => p.scheduled_for && p.scheduled_for.startsWith(monthKey(first)));
  const counts = { approved: 0, empty: 0, published: 0, warn: 0 };
  monthItems.forEach((p) => {
    const s = postState(p, false).key;
    if (s === 'approved') counts.approved += 1;
    else if (s === 'published') counts.published += 1;
    else if (s === 'empty') counts.empty += 1;
    if (p.time_warning || p.approval_stale) counts.warn += 1;
  });
  const undated = store.posts.filter((p) => !p.scheduled_for && !p.published && (p.status || running.has(p.id))).slice(0, 30);
  const view = currentView();

  const head = h('header', { class: 'view-head row cal-headbar' },
    h('div', null,
      h('h1', null, `${MONTHS[first.getMonth()][0].toUpperCase()}${MONTHS[first.getMonth()].slice(1)} ${first.getFullYear()}`),
      h('p', { class: 'cal-sum' },
        monthItems.length ? [
          `${monthItems.length} ${monthItems.length === 1 ? 'пост' : 'поста'}`,
          counts.published ? ` · ${counts.published} публикувани` : '',
          counts.approved ? ` · ${counts.approved} одобрени` : '',
          counts.empty ? ` · ${counts.empty} чакат бриф` : '',
          counts.warn ? ` · ⚠ ${counts.warn} за проверка` : '',
        ].join('') : 'Нищо не е насрочено този месец.')),
    h('div', { class: 'head-actions' },
      h('div', { class: 'seg small', role: 'radiogroup', 'aria-label': 'Изглед' },
        [['month', 'Месец'], ['list', 'Списък']].map(([key, label]) => h('button', {
          type: 'button', role: 'radio', 'aria-checked': String(view === key), onclick: () => setView(key),
        }, label))),
      h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Предишен месец', onclick: () => go(-1) }, icon('chevL')),
      h('button', { type: 'button', class: 'btn ghost small', onclick: () => { C.month = null; if (location.hash === '#/kalendar') show(); else location.hash = '#/kalendar'; } }, 'Днес'),
      h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Следващ месец', onclick: () => go(1) }, icon('chevR')),
      h('button', { type: 'button', class: 'btn primary small', onclick: () => planMonth(first) }, icon('spark'), h('span', { class: 'hide-narrow' }, 'Планирай с асистента'))));

  const main = view === 'list' ? listView(first, byDay, running, today) : monthView(first, byDay, running, today);
  const side = h('aside', { class: 'cal-side' },
    h('h2', null, 'Без дата'),
    h('p', { class: 'muted' }, undated.length ? (view === 'month' ? 'Плъзнете пост в деня, в който да излезе, или го отворете и изберете дата.' : 'Отворете пост и изберете дата.') : 'Всички готови постове имат дата.'),
    h('div', { class: 'cal-undated' }, undated.map((p) => chip(p, running.has(p.id), true))),
    legend());

  const pad = root.querySelector('.view-pad');
  const scroll = pad ? pad.scrollTop : 0;
  root.replaceChildren(h('div', { class: 'view-pad cal-wrap' }, head, h('div', { class: `cal-layout ${view}` }, main, side)));
  root.querySelector('.view-pad').scrollTop = scroll;
}

function monthView(first, byDay, running, today) {
  // 6 седмици от понеделника преди първо число
  const start = new Date(first);
  start.setDate(1 - ((first.getDay() + 6) % 7));
  const cells = [];
  for (let i = 0; i < 42; i += 1) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    const iso = isoOf(d);
    const inMonth = d.getMonth() === first.getMonth();
    const past = iso < today;
    const cell = h('div', {
      class: `cal-day${inMonth ? '' : ' out'}${iso === today ? ' today' : ''}${past ? ' past' : ''}`,
      'data-day': iso,
    },
    h('div', { class: 'cal-date' },
      h('span', null, String(d.getDate())),
      past ? null : addButton(iso)),
    (byDay.get(iso) || []).map((p) => chip(p, running.has(p.id))));
    if (!past) dropTarget(cell, iso);
    cells.push(cell);
  }
  return h('div', { class: 'cal' },
    h('div', { class: 'cal-head' }, WEEKDAYS_SHORT.map((d) => h('span', null, d))),
    h('div', { class: 'cal-grid' }, cells));
}

// Списък: само дните с постове (и днес) — четим на всеки екран.
function listView(first, byDay, running, today) {
  const key = monthKey(first);
  const days = [...byDay.keys()].filter((d) => d.startsWith(key));
  if (today.startsWith(key) && !days.includes(today)) days.push(today);
  days.sort();
  if (!days.length) {
    return h('div', { class: 'cal-list empty' },
      h('p', { class: 'muted' }, 'Нищо не е насрочено този месец.'),
      h('button', { type: 'button', class: 'btn primary small', onclick: () => planMonth(first) }, icon('spark'), 'Планирай с асистента'));
  }
  return h('ol', { class: 'cal-list' }, days.map((iso) => {
    const d = parseIso(iso);
    const items = byDay.get(iso) || [];
    const past = iso < today;
    return h('li', { class: `cal-li${iso === today ? ' today' : ''}${past ? ' past' : ''}` },
      h('div', { class: 'cal-li-day' },
        h('b', null, String(d.getDate())),
        h('span', null, WEEKDAYS_SHORT[(d.getDay() + 6) % 7])),
      h('div', { class: 'cal-li-items' },
        items.length ? items.map((p) => chip(p, running.has(p.id), true)) : h('span', { class: 'muted' }, 'Днес — нищо не е насрочено.')),
      past ? null : addButton(iso));
  }));
}

function addButton(iso) {
  const label = `Нов пост за ${fmtDay(iso, { long: true })}`;
  return h('button', { type: 'button', class: 'cal-add', title: label, 'aria-label': label, onclick: () => newOn(iso) }, icon('plus'));
}

function legend() {
  return h('ul', { class: 'cal-legend' },
    [['empty', 'чака бриф'], ['draft', 'чернова'], ['ready_with_notes', 'с бележки'], ['ready', 'готов'], ['approved', 'одобрен'], ['published', 'публикуван']]
      .map(([key, label]) => h('li', null, h('i', { class: `st-${key}` }), label)));
}

function chip(p, running, wide) {
  const state = postState(p, running);
  const problem = p.time_warning || (p.approval_stale ? 'Текстът е променен след одобрението.' : null);
  const node = h('a', {
    class: `cal-chip st-${state.key}${wide ? ' wide' : ''}${problem ? ' has-warn' : ''}`,
    href: `#/p/${enc(p.id)}`,
    draggable: p.published ? 'false' : 'true',
    title: `${p.title} — ${state.label}${problem ? `\n⚠ ${problem}` : ''}`,
    onclick: (e) => {
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return; // нов раздел — както обикновена връзка
      e.preventDefault();
      drawer.open(p.id);
    },
  },
  p.scheduled_time && (!wide || p.scheduled_for) ? h('time', null, p.scheduled_time) : null,
  problem ? h('b', { class: 'cal-warn', 'aria-label': 'Има предупреждение' }, '⚠') : null,
  h('span', null, p.title),
  wide ? h('small', { class: 'cal-chip-state' }, state.label) : null);
  node.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('text/x-post', p.id);
    e.dataTransfer.effectAllowed = 'move';
    node.classList.add('dragging');
  });
  node.addEventListener('dragend', () => node.classList.remove('dragging'));
  return node;
}

function dropTarget(cell, iso) {
  cell.addEventListener('dragover', (e) => {
    if (!Array.from(e.dataTransfer.types).includes('text/x-post')) return;
    e.preventDefault();
    cell.classList.add('drop');
  });
  cell.addEventListener('dragleave', () => cell.classList.remove('drop'));
  cell.addEventListener('drop', async (e) => {
    cell.classList.remove('drop');
    const id = e.dataTransfer.getData('text/x-post');
    if (!id) return;
    e.preventDefault();
    const p = store.posts.find((x) => x.id === id);
    if (!p || p.scheduled_for === iso) return;
    try {
      await api('PUT', `/api/posts/${enc(id)}/schedule`, { date: iso, time: p.scheduled_time || '10:00' });
      toast(`„${p.title}“ → ${fmtDay(iso, { long: true })}${p.approved ? ' (остава одобрен)' : ''}`);
    } catch (err) {
      toast(err.message, 'err');
    }
    loadPosts();
  });
}

async function newOn(iso) {
  try {
    const post = await api('POST', '/api/posts', { brief: '' });
    await api('PUT', `/api/posts/${enc(post.id)}/schedule`, { date: iso, time: '10:00' });
    await loadPosts();
    drawer.open(post.id); // празно място → панелът пита „Какво се случи?" направо тук
  } catch (e) {
    toast(e.message, 'err');
  }
}

function planMonth(first) {
  const name = MONTHS[first.getMonth()];
  storage('as-draft', `Направи план за ${name} ${first.getFullYear()} — колко поста седмично предлагаш и в кои дни? Ползвай албумите от папката, ако пасват.`);
  location.hash = '#/asistent';
}
