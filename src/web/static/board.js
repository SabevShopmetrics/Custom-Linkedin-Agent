/*
 * График → много постове наведнъж (web-ui-2, преработено в web-ui-3).
 *
 *   #/grafik    — график от файл (.xlsx/.csv/.txt/.json) или поставен текст
 *   #/b/<id>    — таблото: по една карта на пост, агентите пишат паралелно, всичко на живо
 *
 * web-ui-3: снимките за графика вече не се качват от браузъра — сървърът ги взима сам от
 * папката на компютъра (src/web/library.py). Графикът от самата папка се чете в „Папка".
 * Браузърният достъп до папка (File System Access API) отпадна заедно с ограниченията си
 * (само Chrome/Edge, https) — виж docs/technical/web-ui-3.md.
 */
import {
  api, h, icon, toast, ask, copyText, storage, fmtTime, fmtDay, plural, enc, MONTHS_SHORT, STEP_SHORT, STEP_FOR, teamMember,
} from './core.js';
import * as drawer from './drawer.js';
import { store, loadBatches, loadPosts, modeList, maxJobs } from './store.js';
import { getLibrary } from './library.js';

const STATE_NAMES = {
  running: 'пише', queued: 'чака ред', done: 'готов', error: 'грешка', cancelled: 'спрян', idle: 'не е пуснат', missing: 'изтрит',
};

const B = { batchId: null, status: null, timer: null, parsed: null, busy: false, active: false, lib: null, filter: 'all' };
let board = null;

function dateParts(iso) {
  const m = (iso || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return null;
  return { day: +m[3], month: MONTHS_SHORT[+m[2] - 1], year: +m[1] };
}

export async function show(batchId) {
  board = document.getElementById('board');
  B.active = true;
  if (batchId) {
    if (batchId !== B.batchId) openBoard(batchId);
    return;
  }
  clearTimeout(B.timer);
  B.batchId = null;
  document.title = 'График — LinkedIn агент';
  B.lib = await getLibrary();
  renderImport();
}

export function hide() {
  B.active = false;
  B.batchId = null;
  clearTimeout(B.timer);
}

// ------------------------------------------------------------------ изглед: откъде е графикът

function renderImport() {
  if (!B.active || B.batchId) return;
  const busy = B.busy;
  const folderCard = h('div', { class: 'bd-source' },
    h('div', { class: 'bd-source-icon' }, icon('folder')),
    h('h3', null, 'От папката на компютъра'),
    B.lib
      ? h('p', null, `Агентът чете „${B.lib.name}“ сам — ${plural(B.lib.schedules.length, 'файл с график', 'файла с график')}, ${plural(B.lib.albums.length, 'албум', 'албума')}.`)
      : h('p', null, 'Посочете папката веднъж — агентът сам намира графиците и снимките. Без качване.'),
    h('a', { class: 'btn primary', href: '#/papka' }, B.lib ? 'Към папката' : 'Избери папка'));

  const fileCard = h('div', { class: 'bd-source' },
    h('div', { class: 'bd-source-icon' }, icon('file')),
    h('h3', null, 'Файл с график'),
    h('p', null, 'Excel (.xlsx), CSV, текст или JSON — по един пост на ред. Нужна е само колона „Бриф“.'),
    h('label', { class: 'btn ghost', 'aria-disabled': String(busy) },
      h('input', { type: 'file', accept: '.xlsx,.csv,.tsv,.txt,.md,.json', onchange: (e) => {
        const file = e.target.files[0];
        e.target.value = '';
        if (file) parseFile(file.name, file);
      } }),
      'Качи файл'));

  const area = h('textarea', { class: 'bd-paste', rows: '5', placeholder: '20.10 — Среща на членовете, 30 участници от 15 фирми…\n---\n24.10 — Ученици посетиха три фирми от клъстера…' });
  area.value = storage('bd-paste') || '';
  area.addEventListener('input', () => storage('bd-paste', area.value || null));
  const pasteCard = h('div', { class: 'bd-source' },
    h('div', { class: 'bd-source-icon' }, icon('list')),
    h('h3', null, 'Постави текст'),
    h('p', null, 'По един пост на абзац (или разделени с „---“). Дата в началото — по желание.'),
    area,
    h('button', { type: 'button', class: 'btn ghost', disabled: busy, onclick: () => {
      if (!area.value.trim()) { toast('Поставете поне един бриф.', 'err'); return; }
      parseFile('grafik.txt', new Blob([area.value], { type: 'text/plain' }));
    } }, 'Прочети'));

  const nodes = [
    h('header', { class: 'view-head' },
      h('h1', null, 'Цял график наведнъж'),
      h('p', null, `Всеки ред става пост и влиза в календара на датата си. Агентът пише до ${maxJobs()} паралелно — останалите чакат ред и тръгват сами. `,
        'Най-лесно: помолете ', h('a', { href: '#/asistent' }, 'асистента'), ' за план — той сам предлага дати и теми.')),
    h('div', { class: 'bd-sources' }, folderCard, fileCard, pasteCard),
    h('details', { class: 'bd-format' },
      h('summary', null, 'Как да изглежда графикът?'),
      h('div', null,
        h('p', null, 'Таблица с колони (заглавията на български или английски, редът няма значение):'),
        h('ul', null,
          h('li', null, h('b', null, 'Бриф'), ' (или Тема/Текст) — какво се е случило. Единствената задължителна.'),
          h('li', null, h('b', null, 'Дата'), ' — 20.10.2026, 2026-10-20 или „20 октомври“.'),
          h('li', null, h('b', null, 'Режим'), ' — кадър, фокус или панорама.'),
          h('li', null, h('b', null, 'Снимки'), ' — подпапка в папката на агента или имена на файлове. ',
            'Празно → подпапка, която започва с датата (напр. „2026-10-20 среща“).')),
        h('button', { type: 'button', class: 'link-btn', onclick: downloadExample }, '⬇ Свали примерен график (CSV за Excel)'))),
  ];
  if (busy) nodes.push(h('div', { class: 'loading' }, h('span', { class: 'spin' }), 'Чета…'));
  if (B.parsed) nodes.push(renderPreview());
  board.replaceChildren(h('div', { class: 'view-pad' }, nodes));
}

async function parseFile(name, blob) {
  B.busy = true;
  renderImport();
  try {
    const parsed = await api('POST', `/api/schedule/parse?name=${enc(name)}`, undefined, blob);
    B.parsed = { ...parsed, source: name };
    B.parsed.rows.forEach((r) => { r.on = true; });
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    B.busy = false;
    renderImport();
  }
}

function renderPreview() {
  const parsed = B.parsed;
  const chosen = parsed.rows.filter((r) => r.on);
  const modes = modeList();
  const defaultMode = storage('bd-mode') || (store.info && store.info.default_mode) || 'fokus';
  const modeSeg = h('div', { class: 'seg small', role: 'radiogroup', 'aria-label': 'Дължина по подразбиране' },
    modes.map((m) => h('button', {
      type: 'button', role: 'radio', 'aria-checked': String(defaultMode === m.key), title: `${m.hint} — ${m.best_for || ''}`,
      onclick: () => { storage('bd-mode', m.key); renderImport(); },
    }, m.label)));

  const rows = parsed.rows.map((row) => {
    const brief = h('textarea', { class: 'pv-brief', rows: '2', 'aria-label': 'Бриф' });
    brief.value = row.brief;
    brief.addEventListener('input', () => { row.brief = brief.value; });
    const dateInput = h('input', { type: 'date', value: row.date || '', 'aria-label': 'Дата' });
    dateInput.addEventListener('change', () => { row.date = dateInput.value || null; });
    const modeSelect = h('select', { 'aria-label': 'Режим', onchange: (e) => { row.mode = e.target.value || null; } },
      h('option', { value: '' }, '—'),
      modes.map((m) => h('option', { value: m.key, selected: row.mode === m.key }, m.label)));
    const photoNote = row.photos.length
      ? h('span', { class: `pv-photos${B.lib ? ' ok' : ''}`, title: row.photos.join('\n') }, B.lib ? `снимки: ${row.photos.slice(0, 2).join(', ')}` : 'снимки — изберете папка')
      : h('span', { class: 'pv-photos' }, B.lib && row.date ? 'снимки по датата' : 'без снимки');
    const check = h('input', { type: 'checkbox', checked: row.on, 'aria-label': 'Включи реда' });
    check.addEventListener('change', () => { row.on = check.checked; renderImport(); });
    return h('div', { class: `pv-row${row.on ? '' : ' off'}` },
      check, dateInput, brief, h('div', { class: 'pv-side' }, modeSelect, photoNote));
  });

  const name = h('input', { type: 'text', class: 'bd-name', value: (parsed.source || 'График').replace(/\.[^.]+$/, '').split('/').pop(), 'aria-label': 'Име на графика' });
  return h('section', { class: 'pv' },
    h('header', { class: 'pv-head' },
      h('h2', null, `${plural(parsed.rows.length, 'пост', 'поста')} в „${parsed.source}“`),
      h('span', { class: 'grow' }),
      h('span', { class: 'muted' }, 'Дължина по подразбиране:'), modeSeg),
    parsed.warnings.length ? h('ul', { class: 'pv-warn' }, parsed.warnings.map((w) => h('li', null, w))) : null,
    h('div', { class: 'pv-rows' }, rows),
    h('footer', { class: 'pv-foot' },
      name,
      h('span', { class: 'grow' }),
      h('button', { type: 'button', class: 'btn primary', disabled: !chosen.length || B.busy, onclick: () => launch(name.value, defaultMode) },
        `Напиши ${plural(chosen.length, 'пост', 'поста')}`)));
}

function downloadExample() {
  const rows = [
    ['Дата', 'Бриф', 'Режим', 'Снимки'],
    ['20.10.2026', 'Среща на членовете на клъстера във Варна — 30 участници от 15 фирми обсъдиха общите проекти за 2027.', 'фокус', 'среща'],
    ['24.10.2026', 'Ученици от варненски гимназии посетиха три фирми от клъстера и видяха реални проекти отблизо.', 'кадър', ''],
    ['03.11.2026', 'Каним членовете на уъркшоп за ИИ в бизнеса на 20 ноември от 10:00 ч. — записване до 15 ноември.', 'фокус', 'покана.png'],
  ];
  const csv = '﻿' + rows.map((r) => r.map((c) => (/[;"\n]/.test(c) ? `"${c.replace(/"/g, '""')}"` : c)).join(';')).join('\r\n');
  const link = h('a', { href: URL.createObjectURL(new Blob([csv], { type: 'text/csv' })), download: 'grafik-primer.csv' });
  document.body.appendChild(link);
  link.click();
  setTimeout(() => { URL.revokeObjectURL(link.href); link.remove(); }, 1000);
}

async function launch(name, defaultMode) {
  const rows = B.parsed.rows.filter((r) => r.on && r.brief.trim());
  if (!rows.length) return;
  B.busy = true;
  renderImport();
  try {
    const status = await api('POST', '/api/batches', {
      name, source: B.parsed.source, mode: defaultMode, start: true, library: !!B.lib,
      rows: rows.map((r) => ({ row: r.row, brief: r.brief, date: r.date, mode: r.mode, title: r.title, photos: r.photos })),
    });
    B.parsed = null;
    loadBatches();
    loadPosts();
    location.hash = `#/b/${status.id}`;
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    B.busy = false;
  }
}

/** Файл с график, пуснат или избран другаде (лентата на асистента, влачене) → преглед тук. */
export async function importFile(file) {
  await parseFile(file.name, file);
}

// ------------------------------------------------------------------ табло

async function openBoard(id) {
  B.batchId = id;
  B.status = null;
  board.replaceChildren(h('div', { class: 'view-pad' }, h('div', { class: 'loading' }, h('span', { class: 'spin' }), 'Зареждам графика…')));
  await poll();
}

async function poll() {
  clearTimeout(B.timer);
  const id = B.batchId;
  if (!id || !B.active) return;
  try {
    B.status = await api('GET', `/api/batches/${enc(id)}`);
  } catch (e) {
    board.replaceChildren(h('div', { class: 'view-pad' }, h('div', { class: 'empty-card' }, e.message)));
    return;
  }
  if (B.batchId !== id) return;
  renderBoard();
  const live = B.status.items.some((i) => i.state === 'running' || i.state === 'queued');
  B.timer = setTimeout(poll, live ? 1500 : 6000);
}

function renderBoard() {
  const st = B.status;
  if (!st || !B.active) return;
  const items = st.items;
  const counts = { done: 0, running: 0, queued: 0, error: 0, idle: 0 };
  items.forEach((i) => {
    const key = i.state === 'cancelled' ? 'error' : i.state === 'missing' ? 'idle' : i.state;
    counts[key] = (counts[key] || 0) + 1;
  });
  const total = items.length || 1;
  const bar = h('div', { class: 'bd-bar', role: 'img', 'aria-label': `${counts.done} от ${items.length} готови` },
    ['done', 'running', 'queued', 'error'].map((key) => {
      const seg = h('span', { class: `bd-bar-${key}` });
      seg.style.width = `${(counts[key] / total) * 100}%`;
      return seg;
    }));

  const running = items.filter((i) => i.state === 'running');
  const slots = [];
  for (let n = 0; n < st.max_jobs; n += 1) {
    const item = running[n];
    if (item) {
      // web-ui-5: кой от екипа работи сега (TEAM в core.js) — като лентата „Агентите“ на „Преглед“.
      const member = item.job ? teamMember(item.job.stage) : null;
      slots.push(h('button', { type: 'button', class: 'bd-slot busy', onclick: () => scrollToCard(item.post_id), title: member ? member.text : '' },
        h('span', { class: 'bd-slot-n' }, `Агент ${n + 1}`),
        h('b', null, item.title),
        h('small', { class: 'ov-agent-role' }, member ? h('i', null, member.role) : null,
          `${member ? member.doing : ''} · ${fmtTime(item.job ? item.job.elapsed : 0)}`)));
    } else {
      slots.push(h('div', { class: 'bd-slot' },
        h('span', { class: 'bd-slot-n' }, `Агент ${n + 1}`),
        h('small', null, n < st.running_total ? 'зает с друг пост' : 'свободен')));
    }
  }

  const leftovers = items.filter((i) => ['idle', 'error', 'cancelled'].includes(i.state)).length;
  const active = counts.running + counts.queued;
  const sum = st.summary || {};
  const approvable = items.filter((i) => i.state === 'done' && i.date && !i.approved && !i.attention && !i.published);
  const actions = [];
  if (approvable.length) {
    actions.push(h('button', { type: 'button', class: 'btn primary small', onclick: () => approveAll(approvable) }, icon('check'), `Одобри готовите (${approvable.length})`));
  }
  actions.push(
    h('a', { class: 'btn ghost small', href: '#/kalendar' }, icon('calendar'), 'Календар'),
    h('a', { class: 'btn ghost small', href: `/api/batches/${enc(st.id)}/export.zip`, download: '' }, '⬇ ZIP'));
  if (leftovers) actions.push(h('button', { type: 'button', class: 'btn primary small', onclick: () => startItems(null) }, `▶ Пусни ${leftovers} непуснати`));
  if (active) actions.push(h('button', { type: 'button', class: 'btn ghost small danger', onclick: stopAll }, '■ Спри всички'));

  const sorted = items.slice().sort((a, b) => (a.date || '9999').localeCompare(b.date || '9999')
    || (a.time || '').localeCompare(b.time || '') || a.index - b.index);
  const range = sum.first_date ? `${fmtDay(sum.first_date)}${sum.last_date && sum.last_date !== sum.first_date ? ` → ${fmtDay(sum.last_date)}` : ''}` : 'без дати';
  const head = h('header', { class: 'view-head row' },
    h('div', null,
      h('a', { class: 'back-link', href: st.kind === 'upload' ? '#/' : '#/grafik' }, st.kind === 'upload' ? '← Преглед' : '← Нов график'),
      h('h1', null, st.name || 'График'),
      h('p', null, `${plural(items.length, 'пост', 'поста')} · ${range}${st.source ? ` · ${st.kind === 'upload' ? st.source : `от „${st.source}“`}` : ''}`)),
    h('div', { class: 'head-actions' }, actions));

  // web-ui-5: подробното табло на пакета — всичко важно с един поглед.
  const kpis = h('section', { class: 'bd-kpis', 'aria-label': 'Обобщение' },
    kpi(`${counts.done}/${items.length}`, 'готови', 'ok'),
    kpi(String(counts.running + counts.queued), counts.queued ? `в процес (${counts.queued} чакат)` : 'в процес', 'acc'),
    kpi(sum.eta_seconds ? `~${Math.max(1, Math.round(sum.eta_seconds / 60))} мин` : active ? '…' : '—', 'остава', 'acc'),
    kpi(sum.avg_seconds ? fmtTime(sum.avg_seconds) : '—', 'средно на пост'),
    kpi(String(sum.attention || 0), 'искат внимание', sum.attention ? 'warn' : ''),
    kpi(`${sum.approved || 0}`, 'одобрени', ''));

  const filters = [
    ['all', 'Всички', items.length],
    ['active', 'В процес', counts.running + counts.queued],
    ['attention', 'Искат внимание', sum.attention || 0],
    ['done', 'Готови', counts.done],
    ['approved', 'Одобрени', sum.approved || 0],
  ];
  if (!filters.some(([key, , n]) => key === B.filter && (n || key === 'all'))) B.filter = 'all';
  const filterBar = h('div', { class: 'bd-filters', role: 'tablist' }, filters.map(([key, label, n]) => h('button', {
    type: 'button', role: 'tab', class: `tour-chip${B.filter === key ? ' current' : ''}`, 'aria-selected': String(B.filter === key),
    disabled: key !== 'all' && !n ? true : null, onclick: () => { B.filter = key; renderBoard(); },
  }, `${label} · ${n}`)));
  const visible = sorted.filter((i) => ({
    all: true,
    active: i.state === 'running' || i.state === 'queued',
    attention: i.attention,
    done: i.state === 'done',
    approved: i.approved,
  })[B.filter]);

  // Една карта: лентата на напредъка, кратко какво е в нея и агентите (без второ „13/20" — то е горе).
  const summary = h('section', { class: 'bd-summary' },
    h('div', { class: 'bd-stats' },
      stat('done', counts.done, 'готови'),
      stat('running', counts.running, 'пишат'),
      stat('queued', counts.queued, 'чакат ред'),
      counts.error ? stat('error', counts.error, 'с грешка') : null,
      counts.idle ? stat('idle', counts.idle, 'непуснати') : null,
      h('span', { class: 'grow' }),
      sum.autofixed ? h('span', { class: 'bd-stat-note' }, icon('check'), `агентът махна сам ${plural(sum.autofixed, 'недоказано твърдение', 'недоказани твърдения')}`) : null,
      sum.published ? h('span', { class: 'bd-stat-note' }, `${sum.published} публикувани`) : null),
    bar,
    h('div', { class: 'bd-slots' }, slots));

  const grid = h('section', { class: 'bd-grid' }, visible.map(card));
  const scroll = board.querySelector('.view-pad');
  const top = scroll ? scroll.scrollTop : 0;
  board.replaceChildren(h('div', { class: 'view-pad' }, head, kpis, summary, filterBar, grid));
  board.querySelector('.view-pad').scrollTop = top;
  document.title = `${counts.done}/${items.length} · ${st.name} — LinkedIn агент`;
}

function kpi(value, label, tone) {
  return h('div', { class: `bd-kpi${tone ? ` ${tone}` : ''}` }, h('b', null, value), h('small', null, label));
}

async function approveAll(list) {
  const lines = list.slice(0, 6).map((i) => `• ${fmtDay(i.date)} ${i.time || ''} — ${i.title}`).join('\n');
  const ok = await ask({
    title: `Одобрявате ли ${plural(list.length, 'пост', 'поста')}?`,
    text: `Готови, насрочени и без предупреждения. Всеки излиза в своя час с текста, който е сега; промяна по текста отменя одобрението.\n${lines}${list.length > 6 ? '\n…' : ''}`,
    ok: 'Одобри',
  });
  if (!ok) return;
  let done = 0;
  for (const i of list) {
    try { await api('PUT', `/api/posts/${enc(i.post_id)}/schedule`, { approved: true }); done += 1; } catch (e) { toast(`„${i.title}“: ${e.message}`, 'err'); }
  }
  toast(`Одобрих ${plural(done, 'пост', 'поста')}.`);
  poll();
}

function stat(kind, n, label) {
  return h('span', { class: `bd-stat ${kind}` }, h('i'), h('b', null, String(n)), ` ${label}`);
}

function card(item) {
  const state = item.state;
  const p = dateParts(item.date);
  const step = item.job ? STEP_FOR[Math.min(item.job.step, 6)] : state === 'done' ? 5 : -1;
  const steps = h('ol', { class: 'bd-steps', 'aria-label': 'Етапи' }, STEP_SHORT.map((label, i) => h('li', {
    class: i < step || state === 'done' ? 'on' : i === step && state === 'running' ? 'now' : '',
    title: label,
  }, h('span', null, label))));

  let line;
  if (state === 'running') line = `${item.job ? item.job.stage : 'Пише…'} · ${fmtTime(item.job ? item.job.elapsed : 0)}`;
  else if (state === 'queued') line = `Чака свободен агент${item.queue_position ? ` · №${item.queue_position} на опашката` : ''}`;
  else if (state === 'done') line = `${item.status_label || 'Готов'} · ${plural(item.words, 'дума', 'думи')}${item.seconds ? ` · писан за ${fmtTime(item.seconds)}` : ''}`;
  else if (state === 'error' || state === 'cancelled') line = item.error || 'Не завърши.';
  else line = 'Не е пуснат.';

  const text = state === 'running' ? item.draft : item.snippet;
  const photos = (item.photos_in_post.length ? item.photos_in_post : item.photos_included).slice(0, 4);
  const buttons = [
    h('button', { type: 'button', class: 'btn ghost small', onclick: () => drawer.open(item.post_id) }, 'Поправи'),
    h('a', { class: 'btn ghost small', href: `#/p/${enc(item.post_id)}` }, 'Чат'),
  ];
  if (state === 'running' && item.job) buttons.push(h('button', { type: 'button', class: 'btn ghost small', onclick: () => cancelJob(item.job.id) }, '■ Спри'));
  if (['idle', 'error', 'cancelled'].includes(state)) buttons.push(h('button', { type: 'button', class: 'btn primary small', onclick: () => startItems([item.post_id]) }, '▶ Пусни'));
  if (state === 'done') {
    buttons.push(h('button', { type: 'button', class: 'btn ghost small', onclick: (e) => copy(item, e.currentTarget) }, 'Копирай'));
    buttons.push(h('button', { type: 'button', class: `btn small ${item.published ? 'ghost' : 'primary'}`, onclick: () => publish(item) }, item.published ? '✓ Публикуван' : 'Публикувай'));
    buttons.push(h('button', { type: 'button', class: 'link-btn', onclick: () => rewrite(item) }, '↻ Наново'));
  }

  return h('article', { class: `bd-card ${state}${item.insufficient_brief ? ' thin' : ''}${item.attention ? ' attn' : ''}`, id: `bd-${item.post_id}` },
    h('div', { class: 'bd-day' },
      p ? [h('b', null, String(p.day)), h('span', null, p.month), h('small', null, item.weekday || ''), item.time ? h('small', { class: 'bd-time' }, item.time) : null]
        : [h('b', null, `№${item.index}`), h('small', null, 'без дата')]),
    h('div', { class: 'bd-body' },
      h('div', { class: 'bd-card-head' },
        h('h3', null, item.title),
        h('span', { class: `bd-chip ${state}` }, state === 'done' && item.published ? 'публикуван' : STATE_NAMES[state] || state)),
      state === 'running' || state === 'queued' ? steps : null, // при готов пост етапите са само шум
      h('p', { class: `bd-line${state === 'error' ? ' err' : ''}` }, line),
      item.approved || item.digest ? h('p', { class: 'bd-flags' },
        item.approved ? h('span', { class: 'bd-ok' }, icon('check'), 'одобрен') : null,
        item.digest ? h('span', { class: `bd-digest${item.attention ? ' warn' : ''}` }, item.digest) : null) : null,
      text ? h('p', { class: `bd-snippet${state === 'running' ? ' draft' : ''}` }, text) : h('p', { class: 'bd-snippet brief' }, item.brief || 'Празно място — отворете поста и кажете какво се е случило.'),
      photos.length ? h('div', { class: 'bd-thumbs' }, photos.map((name) => h('img', { src: `/api/posts/${enc(item.post_id)}/photos/${enc(name)}?size=s`, alt: '', loading: 'lazy' })),
        item.photo_count > photos.length ? h('span', null, `+${item.photo_count - photos.length}`) : null) : null,
      h('footer', { class: 'bd-card-foot' }, buttons)));
}

function scrollToCard(postId) {
  const node = document.getElementById(`bd-${postId}`);
  if (!node) return;
  node.scrollIntoView({ behavior: 'smooth', block: 'center' });
  node.classList.add('flash');
  setTimeout(() => node.classList.remove('flash'), 1200);
}

async function startItems(postIds) {
  try {
    const r = await api('POST', `/api/batches/${enc(B.batchId)}/start`, postIds ? { post_ids: postIds } : {});
    if (!r.added) toast('Няма какво да пусна — всичко е готово или вече работи.');
  } catch (e) {
    toast(e.message, 'err');
  }
  poll();
}

async function rewrite(item) {
  const ok = await ask({ title: `Да напиша ли наново „${item.title}“?`, text: 'Сегашният текст се пази като предишен вариант.', ok: 'Напиши наново' });
  if (ok) startItems([item.post_id]);
}

async function stopAll() {
  const ok = await ask({ title: 'Да спра ли всички?', text: 'Спират постовете от графика, които пишат или чакат. Готовите остават.', ok: 'Спри всички', danger: true });
  if (!ok) return;
  try { await api('POST', `/api/batches/${enc(B.batchId)}/stop`, {}); } catch (e) { toast(e.message, 'err'); }
  poll();
}

async function cancelJob(jobId) {
  try { await api('POST', `/api/jobs/${enc(jobId)}/cancel`, {}); } catch (e) { toast(e.message, 'err'); }
  poll();
}

async function copy(item, button) {
  try {
    const full = await api('GET', `/api/batches/${enc(B.batchId)}?full=1`);
    const text = (full.items.find((i) => i.post_id === item.post_id) || {}).text || '';
    await copyText(text, button);
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function publish(item) {
  const info = store.info || {};
  let ok;
  if (info.publish_mode === 'linkedin') {
    if (info.publish_problem) { toast(info.publish_problem, 'err'); return; }
    ok = await ask({ title: `Публикувам „${item.title}“?`, text: 'Излиза в LinkedIn страницата на клъстера — веднага и публично.', ok: 'Публикувай' });
  } else {
    ok = await ask({
      title: 'Режим „чернова“',
      text: 'Нищо няма да се изпрати към LinkedIn. Ще запиша как би изглеждала заявката (output/linkedin-request.json).',
      ok: 'Запиши заявката',
    });
  }
  if (!ok) return;
  try {
    const r = await api('POST', `/api/posts/${enc(item.post_id)}/publish`, {});
    toast(r.mode === 'linkedin' ? 'Публикуван в LinkedIn ✓' : 'Заявката е записана (чернова) — нищо не е изпратено.');
  } catch (e) {
    toast(e.message, 'err');
  }
  poll();
}

