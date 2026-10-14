/*
 * Асистентът (#/, #/a/<id>) — разговор САМО за LinkedIn страницата на клъстера (web-ui-3,
 * src/web/assistant.py). Човекът пита и планира с думи; когато поиска пост или график,
 * асистентът връща ПЛАН — таблица, която човекът поправя и одобрява с един бутон. Нищо не
 * се създава, докато човекът не натисне „Създай".
 */
import { api, h, icon, toast, ask, storage, plural, enc, fmtDay, fmtTime, todayIso, libraryImage } from './core.js';
import { store, loadThreads, loadPosts, loadBatches, modeList } from './store.js';
import { getLibrary } from './library.js';
import { brandGlyph } from './chat.js';
import { routeFiles, ACCEPT } from './files.js';
import { modePicker } from './modes.js';
import { pickAlbum as pickFolderAlbum } from './library.js';

const A = { thread: null, threadId: null, waiting: null, home: null, lib: null };
let root = null;
let els = null;

export async function show(id) {
  root = document.getElementById('view-assistant');
  if (!els) build();
  const draft = storage('as-draft'); // напр. „Планирай с асистента" от календара
  if (draft && !els.prompt.value) els.prompt.value = draft;
  document.title = 'Асистент — LinkedIn агент';
  A.lib = await getLibrary();
  if (!id) {
    A.threadId = null;
    A.thread = null;
    store.route = { name: 'assistant', id: null };
    render();
    loadHome();
  } else if (id !== A.threadId || !A.thread) {
    A.threadId = id;
    A.thread = null;
    render();
    try {
      A.thread = await api('GET', `/api/assistant/${enc(id)}`);
    } catch (e) {
      toast(e.message, 'err');
      location.hash = '#/asistent';
      return;
    }
    render(true);
  } else {
    render();
  }
  setTimeout(() => els.prompt.focus(), 0);
}

export function hide() { /* нищо не тече във фон — отговорът се чака в самата заявка */ }

async function loadHome() {
  try {
    A.home = await api('GET', '/api/home');
  } catch (e) {
    return;
  }
  if (!A.threadId) render();
}

function build() {
  els = {};
  els.thread = h('div', { class: 'as-thread' });
  els.inner = h('div', { class: 'as-inner' });
  els.thread.append(els.inner);
  els.prompt = h('textarea', { rows: '1', 'aria-label': 'Съобщение към асистента', placeholder: 'Напр. „План за ноември — 2 поста седмично“ или „Какво е насрочено тази седмица?“' });
  els.send = h('button', { type: 'submit', class: 'send', 'aria-label': 'Изпрати', html: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m0 0-6 6m6-6 6 6"/></svg>' });
  // „+": снимки/ZIP → нов пост с написания текст; график (.xlsx/.csv…) → преглед на графика.
  const fileInput = h('input', { type: 'file', multiple: true, accept: ACCEPT });
  fileInput.addEventListener('change', () => {
    const files = Array.from(fileInput.files);
    fileInput.value = '';
    routeFiles(files, { text: takeDraft() });
  });
  const plus = h('label', { class: 'plus', title: 'Снимки, ZIP или файл с график' },
    fileInput, icon('plus'), h('span', { class: 'sr-only' }, 'Добави снимки, ZIP или график'));
  const album = h('button', { type: 'button', class: 'plus', title: 'Албум от папката на компютъра', 'aria-label': 'Албум от папката', onclick: albumToPost }, icon('folder'));
  els.form = h('form', { class: 'composer', autocomplete: 'off' },
    els.prompt,
    h('div', { class: 'composer-row' },
      els.modes = h('div', { class: 'modes-slot' }),
      h('span', { class: 'grow' }),
      album, plus, els.send));
  root.append(els.thread, h('div', { class: 'composer-wrap' }, els.form,
    h('p', { class: 'composer-hint' }, 'Опишете събитие — пиша поста веднага · поискайте график — предлагам план · само за LinkedIn страницата на клъстера')));
  renderModes();

  const autosize = () => {
    els.prompt.style.height = 'auto';
    els.prompt.style.height = `${Math.min(els.prompt.scrollHeight, 200)}px`;
    els.send.disabled = !els.prompt.value.trim() || !!A.waiting;
  };
  els.autosize = autosize;
  els.prompt.addEventListener('input', () => { autosize(); storage('as-draft', els.prompt.value || null); });
  els.prompt.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (!els.send.disabled) send();
    }
  });
  els.form.addEventListener('submit', (e) => { e.preventDefault(); if (!els.send.disabled) send(); });
  els.prompt.value = storage('as-draft') || '';
  autosize();
}

// Режимът важи за поста, който асистентът пише веднага (описано събитие).
function renderModes() {
  const current = storage('newMode') || (store.info && store.info.default_mode) || 'fokus';
  els.modes.replaceChildren(modePicker(current, (key) => { storage('newMode', key); renderModes(); }, { compact: true }));
}

// ------------------------------------------------------------------ изглед

function render(scroll) {
  if (!els) return;
  const messages = (A.thread && A.thread.messages) || [];
  const nodes = [];
  if (!messages.length && !A.waiting) {
    nodes.push(welcome());
  } else {
    messages.forEach((m, i) => nodes.push(message(m, i)));
  }
  if (A.waiting) nodes.push(typing());
  els.inner.replaceChildren(...nodes);
  els.autosize();
  if (scroll) requestAnimationFrame(() => { els.thread.scrollTop = els.thread.scrollHeight; });
}

function welcome() {
  const home = A.home || {};
  const upcoming = home.upcoming || [];
  const suggestions = home.suggestions || [];
  return h('div', { class: 'as-welcome' },
    h('p', { class: 'hero-kicker' }, 'Асистент'),
    h('h2', null, 'Какво се случи? Какво планираме?'),
    h('p', { class: 'lede' }, 'Опишете събитие с едно-две изречения — пиша поста веднага. Поискайте график — предлагам план. Снимки с „+“ или от папката. ',
      A.lib ? `Виждам папка „${A.lib.name}“ с ${plural(A.lib.albums.length, 'албум', 'албума')}.` : 'Изберете папка, за да виждам снимките и графиците ви.'),
    suggestions.length ? h('div', { class: 'examples' }, suggestions.map((s) => h('button', {
      type: 'button', class: `example kind-${s.kind}`,
      onclick: () => { els.prompt.value = s.text; els.autosize(); els.prompt.focus(); },
    }, h('b', null, s.title), h('span', null, s.text)))) : null,
    h('div', { class: 'as-week' },
      h('div', { class: 'as-week-head' },
        h('b', null, 'Предстои'),
        h('a', { href: '#/kalendar', class: 'link-btn' }, 'Календар →')),
      upcoming.length
        ? h('ul', null, upcoming.map((p) => h('li', null,
          h('a', { href: `#/p/${enc(p.id)}` },
            h('span', { class: 'as-day' }, fmtDay(p.scheduled_for)),
            h('span', { class: 'as-time' }, p.scheduled_time || ''),
            h('span', { class: 'as-title' }, p.title),
            h('span', { class: `dot ${p.approved ? 'approved' : p.needs_brief ? 'empty' : p.status ? 'ready' : 'draft'}` })))))
        : h('p', { class: 'muted' }, 'Нищо не е насрочено. Поискайте план — напр. „2 поста седмично до края на месеца“.')));
}

function message(m, index) {
  if (m.role === 'user') return h('div', { class: 'msg user' }, h('div', { class: 'bubble' }, m.text));
  if (m.kind === 'note') {
    return h('div', { class: 'msg note' }, h('span', null, m.text,
      m.batch ? h('a', { href: `#/b/${m.batch}`, class: 'note-link' }, ' Виж таблото →') : null));
  }
  const body = [h('div', { class: `as-reply${m.off_topic ? ' off' : ''}` }, m.text)];
  if (m.notes) body.push(h('ul', { class: 'as-notes' }, m.notes.map((n) => h('li', null, n))));
  if (m.post_id) {
    body.push(h('a', { class: 'btn primary small', href: `#/p/${enc(m.post_id)}` }, icon('pen'), 'Отвори поста'));
  } else if (m.plan) body.push(planCard(m, index));
  if (m.seconds) body.push(h('small', { class: 'as-meta' }, `отговор за ${fmtTime(m.seconds)}`));
  return h('div', { class: 'msg agent' }, h('div', { class: 'avatar', 'aria-hidden': 'true' }, brandGlyph()), h('div', { class: 'msg-body' }, body));
}

const typingLabel = () => {
  const elapsed = Math.round((Date.now() - A.waiting) / 1000);
  return elapsed < 20 ? 'Мисля…' : `Преглеждам календара и папката… ${fmtTime(elapsed)}`;
};

function typing() {
  els.typingText = h('span', null, typingLabel());
  return h('div', { class: 'msg agent' }, h('div', { class: 'avatar', 'aria-hidden': 'true' }, brandGlyph()),
    h('div', { class: 'msg-body' }, h('div', { class: 'as-typing', role: 'status' },
      h('span', { class: 'dots', 'aria-hidden': 'true' }, h('i'), h('i'), h('i')), els.typingText)));
}

// Планът — редактируема таблица. Промените стоят в браузъра, докато човекът не натисне „Създай".
function planCard(m, index) {
  const applied = m.applied;
  const rows = m.plan.map((item) => ({ ...item, on: true }));
  const albums = (A.lib && A.lib.albums) || [];
  const modes = modeList();
  const card = h('section', { class: `plan${applied ? ' applied' : ''}` });
  const count = h('span');
  const create = h('button', { type: 'button', class: 'btn primary' });
  const refresh = () => {
    const n = rows.filter((r) => r.on).length;
    const empty = rows.filter((r) => r.on && (r.needs_brief || !r.brief.trim())).length;
    create.textContent = `Създай ${plural(n, 'пост', 'поста')}`;
    create.disabled = !n || applied;
    count.textContent = empty ? `${empty} без бриф — ще стоят в календара като празни места, докато не кажете какво се е случило.` : 'Постовете с бриф започват да се пишат веднага.';
  };
  const body = rows.map((row) => {
    const tr = h('div', { class: 'plan-row' });
    const check = h('input', { type: 'checkbox', checked: true, disabled: applied, 'aria-label': 'Включи' });
    check.addEventListener('change', () => { row.on = check.checked; tr.classList.toggle('off', !row.on); refresh(); });
    const date = h('input', { type: 'date', value: row.date, min: todayIso(), disabled: applied, 'aria-label': 'Дата' });
    date.addEventListener('change', () => { row.date = date.value; });
    const time = h('input', { type: 'time', value: row.time, step: '300', disabled: applied, 'aria-label': 'Час' });
    time.addEventListener('change', () => { row.time = time.value; });
    const title = h('input', { type: 'text', value: row.title, disabled: applied, 'aria-label': 'Тема', class: 'plan-title' });
    title.addEventListener('input', () => { row.title = title.value; });
    const brief = h('textarea', {
      rows: '2', disabled: applied, 'aria-label': 'Бриф',
      placeholder: row.idea ? `Идея: ${row.idea} — напишете какво реално се случи` : 'Какво се случи? (празно = място в календара)',
    });
    brief.value = row.brief;
    brief.addEventListener('input', () => { row.brief = brief.value; row.needs_brief = !brief.value.trim(); refresh(); });
    const mode = h('select', { disabled: applied, 'aria-label': 'Режим', onchange: (e) => { row.mode = e.target.value; } },
      modes.map((x) => h('option', { value: x.key, selected: row.mode === x.key }, x.label)));
    const album = h('select', { disabled: applied || !albums.length, 'aria-label': 'Албум', onchange: (e) => { row.album = e.target.value || null; } },
      h('option', { value: '' }, albums.length ? 'без снимки' : 'няма папка'),
      albums.map((a) => h('option', { value: a.path, selected: row.album === a.path }, `${a.name} (${a.count})`)));
    const thumb = row.album ? albums.find((a) => a.path === row.album) : null;
    tr.append(check,
      h('div', { class: 'plan-when' }, date, time, h('small', null, fmtDay(row.date))),
      h('div', { class: 'plan-what' }, title, brief),
      h('div', { class: 'plan-side' }, mode, album,
        thumb ? h('img', { src: libraryImage(thumb.cover), alt: '', class: 'plan-thumb' }) : null));
    return tr;
  });
  create.addEventListener('click', async () => {
    const items = rows.filter((r) => r.on).map(({ on, ...rest }) => rest);
    create.disabled = true;
    try {
      const r = await api('POST', `/api/assistant/${enc(A.threadId)}/apply`, { index, items, name: A.thread.title });
      A.thread = r.thread;
      render(true);
      loadPosts();
      loadBatches();
      toast(`Създадох ${plural(r.batch.items.length, 'пост', 'поста')}. Вижте ги в календара.`);
    } catch (e) {
      toast(e.message, 'err');
      create.disabled = false;
    }
  });
  refresh();
  card.append(
    h('header', { class: 'plan-head' },
      icon('calendar'), h('b', null, applied ? 'Създаден план' : 'Предложен план'),
      h('small', null, plural(rows.length, 'пост', 'поста')),
      h('span', { class: 'grow' }),
      applied ? h('a', { href: `#/b/${applied}`, class: 'btn ghost small' }, 'Табло →') : null,
      applied ? h('a', { href: '#/kalendar', class: 'btn ghost small' }, 'Календар →') : null),
    h('div', { class: 'plan-rows' }, body),
    applied ? null : h('footer', { class: 'plan-foot' }, h('small', null, count), h('span', { class: 'grow' }), create));
  return card;
}

// ------------------------------------------------------------------ изпращане

async function send() {
  const text = els.prompt.value.trim();
  if (!text || A.waiting) return;
  if (!A.threadId) {
    try {
      const thread = await api('POST', '/api/assistant', {});
      A.threadId = thread.id;
      A.thread = thread;
      history.replaceState(null, '', `#/a/${thread.id}`);
      store.route = { name: 'assistant', id: thread.id };
    } catch (e) {
      toast(e.message, 'err');
      return;
    }
  }
  const threadId = A.threadId;
  A.thread.messages = [...(A.thread.messages || []), { role: 'user', text }];
  els.prompt.value = '';
  storage('as-draft', null);
  A.waiting = Date.now();
  render(true);
  // Само надписът на индикатора — пълно прерисуване би рестартирало анимациите всяка секунда.
  const tick = setInterval(() => { if (A.waiting && els.typingText) els.typingText.textContent = typingLabel(); }, 1000);
  try {
    const mode = storage('newMode') || (store.info && store.info.default_mode) || 'fokus';
    const thread = await api('POST', `/api/assistant/${enc(threadId)}/messages`, { text, mode });
    if (A.threadId === threadId) A.thread = thread;
    loadThreads();
    const last = thread.messages[thread.messages.length - 1];
    if (last.post_id && A.threadId === threadId) {
      // Описано събитие → постът вече се пише; отиваме при него (прегледът на живо е там).
      loadPosts();
      A.waiting = null;
      location.hash = `#/p/${enc(last.post_id)}`;
      return;
    }
  } catch (e) {
    toast(e.message, 'err');
    if (A.threadId === threadId) {
      A.thread.messages = A.thread.messages.slice(0, -1);
      els.prompt.value = text;
    }
  } finally {
    clearInterval(tick);
    A.waiting = null;
    if (A.threadId === threadId) render(true);
  }
}

export async function deleteThread(t) {
  const ok = await ask({ title: `Да изтрия ли „${t.title}“?`, text: 'Разговорът отива в кошчето (posts/_kosh). Създадените постове остават.', ok: 'Изтрий', danger: true });
  if (!ok) return;
  try {
    await api('DELETE', `/api/assistant/${enc(t.id)}`);
  } catch (e) {
    toast(e.message, 'err');
    return;
  }
  if (A.threadId === t.id) location.hash = '#/asistent';
  loadThreads();
}

/** Написаното в лентата отива с файловете в новия пост — тук се изчиства. */
export function takeDraft() {
  if (!els || store.route.name !== 'assistant') return '';
  const text = els.prompt.value;
  els.prompt.value = '';
  storage('as-draft', null);
  els.autosize();
  return text;
}

async function albumToPost() {
  const album = await pickFolderAlbum();
  if (!album) return;
  try {
    const post = await api('POST', '/api/library/album', { path: album.path });
    const text = takeDraft();
    storage(`draft:${post.id}`, text.trim() || post.album_brief || `${album.name}${album.date ? ` (${album.date})` : ''}: `);
    loadPosts();
    location.hash = `#/p/${enc(post.id)}`;
  } catch (e) {
    toast(e.message, 'err');
  }
}

export function newThread() {
  A.threadId = null;
  A.thread = null;
  if (location.hash === '#/asistent') show(null);
  else location.hash = '#/asistent';
}
