/*
 * Чатът за един пост (#/nov, #/p/<id>): разговор в средата, поле за писане лепнато отдолу,
 * „+" за снимки/ZIP, преглед „както в LinkedIn", ход на работата на живо. Логиката е от
 * web-ui-1/2; web-ui-3 добави дата/час и одобрение за публикуване, албуми от папката и
 * обяснение на режимите. Сървърът е src/web/server.py; състоянието живее там.
 */
import {
  api, h, icon, toast, ask, copyText, storage, esc, linkify, fmtTime, words, plural, enc, fmtDay,
  photoUrlFor, LIMIT_CHARS, STEP_LABELS, STEP_FOR, todayIso,
} from './core.js';
import { store, on, runningJobs, maxJobs, loadInfo, loadPosts, modeList } from './store.js';
import { modePicker } from './modes.js';
import { timelineView, summarizeRun } from './timeline.js';
import { pickAlbum } from './library.js';

const QUICK = ['По-кратко', 'По-дълго', 'По-официално', 'По-топло и лично'];
const FALLBACK_EXAMPLES = [
  ['Събитие', 'Вчера проведохме среща на членовете на клъстера във Варна — 30 участници от 15 фирми обсъдиха общите проекти за 2027.'],
  ['Ученици и стажове', 'Ученици от варненски гимназии посетиха три фирми от клъстера и видяха реални проекти отблизо.'],
  ['Покана', 'Каним членовете на клъстера на уъркшоп за ИИ в бизнеса на 20 ноември от 10:00 ч. — записване до 15 ноември.'],
];
const SEND_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m0 0-6 6m6-6 6 6"/></svg>';
const STOP_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor" stroke="none"/></svg>';

const S = {
  active: false,
  postId: null,
  post: null,
  mode: 'fokus',
  jobId: null,
  job: null,
  draft: null,
  draftCard: null,
  editing: false,
  editText: '',
  view: 'desktop',
  pollTimer: null,
  finalTimer: null,
  uploads: [],
  uploading: 0,
  events: [],
  trayNotes: [],
  pendingEl: null,
  lastCount: -1,
  recall: null,
  hist: null,
  prevRunning: null,
  home: null,
};

const el = {};
const photoUrl = (name, size, bytes) => photoUrlFor(S.postId, name, size, bytes);
const jobRunningHere = () => !!(S.job && S.job.state === 'running' && S.job.post_id === S.postId);
// Няколко поста пишат наведнъж, но до тавана на сървъра (WEB_MAX_JOBS).
const atCapacity = () => !jobRunningHere() && runningJobs().filter((j) => j.post_id !== S.postId).length >= maxJobs();

// ------------------------------------------------------------------ навигация

export function currentPostId() {
  return S.active ? S.postId : null;
}

export function isBusy() {
  return S.uploading > 0;
}

/** Показва чата: `id` — съществуващ пост, null — нов пост. */
export function show(id) {
  S.active = true;
  if (id && id === S.postId && S.post) return;
  if (id) openPost(id);
  else if (S.postId || !S.post) newChat();
  else renderAll();
}

export function hide() {
  S.active = false;
  stopPolling();
  clearInterval(S.finalTimer);
}

function setHash() {
  const target = S.postId ? `#/p/${enc(S.postId)}` : '#/nov';
  if (location.hash !== target) history.replaceState(null, '', target);
  store.route = { name: 'post', id: S.postId };
}

function resetView() {
  stopPolling();
  clearInterval(S.finalTimer);
  Object.assign(S, {
    post: null, job: null, jobId: null, draft: null, editing: false, pendingEl: null, trayNotes: [],
    lastCount: -1, recall: null, hist: null,
  });
}

const draftKey = () => `draft:${S.postId || 'new'}`;

function newChat() {
  resetView();
  S.postId = null;
  S.mode = storage('newMode') || (store.info && store.info.default_mode) || 'fokus';
  el.prompt.value = storage(draftKey()) || '';
  setHash();
  renderAll();
  autosize();
  loadHome();
  setTimeout(() => el.prompt.focus(), 0);
}

async function loadHome() {
  try {
    S.home = await api('GET', '/api/home');
  } catch (e) {
    return;
  }
  if (!S.postId && S.active) renderThread();
}

async function openPost(id) {
  resetView();
  S.postId = id;
  el.prompt.value = storage(draftKey()) || '';
  try {
    S.post = await api('GET', `/api/posts/${enc(id)}`);
  } catch (e) {
    toast(e.message, 'err');
    S.postId = null;
    newChat();
    return;
  }
  S.mode = S.post.mode || 'fokus';
  setHash();
  if (S.post.job && S.post.job.state === 'running') startJob(S.post.job, false);
  renderAll();
  autosize();
  scrollToLatest();
  watchFinalCheck();
}

export async function refreshPost() {
  if (!S.postId) return;
  try {
    S.post = await api('GET', `/api/posts/${enc(S.postId)}`);
  } catch (e) {
    toast(e.message, 'err');
    return;
  }
  renderAll();
}

async function ensurePost() {
  if (S.postId) return;
  const brief = el.prompt.value.trim();
  S.post = await api('POST', '/api/posts', { brief, mode: S.mode });
  const oldKey = draftKey();
  S.postId = S.post.id;
  storage(oldKey, null);
  storage('newMode', null);
  if (brief) storage(draftKey(), el.prompt.value);
  setHash();
  loadPosts();
}

/** Пост, изтрит от историята, докато е отворен тук. */
export function forget(postId) {
  storage(`draft:${postId}`, null);
  if (S.active && postId === S.postId) location.hash = '#/nov';
}

// ------------------------------------------------------------------ нишката

function scrollToBottom(smooth) {
  requestAnimationFrame(() => {
    el.thread.scrollTo({ top: el.thread.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
  });
}

// При отваряне на готов пост — горе на най-новата карта, не най-долу при бележките.
function scrollToLatest() {
  requestAnimationFrame(() => {
    const cards = el.messages.querySelectorAll('.preview');
    const last = cards[cards.length - 1];
    if (last) el.thread.scrollTop = Math.max(0, last.offsetTop - 16);
    else scrollToBottom(false);
  });
}

function hero() {
  const suggestions = [];
  const albums = (S.home && S.home.suggestions || []).filter((s) => s.kind === 'album');
  for (const s of albums) suggestions.push([s.title, s.text, s.album]);
  for (const ex of FALLBACK_EXAMPLES) if (suggestions.length < 3) suggestions.push(ex);
  return h('div', { class: 'hero' },
    h('p', { class: 'hero-kicker' }, 'Нов пост'),
    h('h2', null, 'Какво се случи?'),
    h('p', null, 'Едно-две изречения: кой, какво, кога и защо е интересно. Добавете снимки с „+“ или от папката — агентът пише поста и показва точно как ще изглежда в LinkedIn.'),
    h('div', { class: 'examples' }, suggestions.map(([title, text, album]) => h('button', {
      type: 'button',
      class: `example${album ? ' kind-album' : ''}`,
      onclick: async () => {
        if (album) await attachAlbum({ path: album, name: title });
        el.prompt.value = text;
        autosize();
        el.prompt.focus();
        el.prompt.setSelectionRange(text.length, text.length);
        storage(draftKey(), text);
        updateSendState();
      },
    }, h('b', null, title), h('span', null, text)))),
    h('button', { type: 'button', class: 'link-btn hero-album', onclick: () => attachAlbum() }, icon('folder'), ' Снимки от папката'));
}

function renderThread() {
  const messages = (S.post && S.post.messages) || [];
  const running = jobRunningHere();
  const top = [];
  if (S.post && S.post.schedule && S.post.schedule.needs_brief && !S.post.post) {
    top.push(slotBanner());
  }
  if (!S.postId || (!messages.length && !running)) {
    el.messages.replaceChildren(...top, hero());
    S.lastCount = 0;
    return;
  }
  let lastPost = -1;
  messages.forEach((m, i) => { if (m.role === 'agent' && m.kind === 'post') lastPost = i; });
  const totalVersions = messages.filter((m) => m.role === 'agent' && m.kind === 'post').length;
  let version = 0;
  const nodes = messages.map((m, i) => {
    if (m.role === 'user') return userMessage(m);
    if (m.kind === 'note') return h('div', { class: 'msg note' }, h('span', null, m.text));
    if (m.kind === 'error') return agentMessage(errorBubble(m, i === messages.length - 1 && !running));
    if (m.kind === 'stopped') return agentMessage(errorBubble(m, i === messages.length - 1 && !running, true));
    version += 1;
    const live = i === lastPost && S.post.post && !running;
    return agentMessage(live ? livePost(totalVersions > 1 ? version : 0) : oldPost(m, totalVersions > 1 ? version : 0));
  });
  if (running) {
    if (!S.pendingEl) S.pendingEl = agentMessage(h('div', { class: 'pending-slot' }));
    renderPending();
    nodes.push(S.pendingEl);
  }
  el.messages.replaceChildren(...top, ...nodes);
  const count = nodes.length;
  if (S.lastCount > 0 && count !== S.lastCount) scrollToBottom(true);
  S.lastCount = count;
}

function slotBanner() {
  const sch = S.post.schedule;
  return h('div', { class: 'slot-banner' },
    icon('calendar'),
    h('div', null,
      h('b', null, `Място в календара: ${fmtDay(sch.scheduled_for, { long: true })}${sch.scheduled_time ? ` · ${sch.scheduled_time}` : ''}`),
      h('span', null, 'Асистентът е запазил датата, но още няма какво да пише. Разкажете отдолу какво се е случило (или ще се случи) — агентът пише веднага.'),
      sch.idea ? h('span', { class: 'slot-idea' }, `Идея: ${sch.idea}`) : null));
}

function userMessage(m) {
  const names = Array.isArray(m.photos) ? m.photos : [];
  const meta = [];
  if (typeof m.photos === 'number' && m.photos) meta.push(`📎 ${plural(m.photos, 'снимка', 'снимки')}`);
  if (m.mode) {
    const mode = modeList().find((x) => x.key === m.mode);
    if (mode && store.info && m.mode !== store.info.default_mode) meta.push(mode.label);
  }
  const shown = names.slice(0, 6);
  const photos = shown.length ? h('div', { class: 'msg-photos' },
    shown.map((name) => {
      const img = h('img', { src: photoUrlFor(S.postId, name, 's'), alt: name, title: name, loading: 'lazy' });
      img.addEventListener('error', () => img.remove()); // снимка, преместена в кошчето заедно с друг пост
      img.addEventListener('click', () => openLightbox(photoUrlFor(S.postId, name, 'm')));
      return img;
    }),
    names.length > shown.length ? h('span', { class: 'more' }, `+${names.length - shown.length}`) : null) : null;
  return h('div', { class: 'msg user' }, photos, h('div', { class: 'bubble' }, m.text),
    meta.length ? h('div', { class: 'msg-meta' }, meta.join(' · ')) : null);
}

function agentMessage(...content) {
  return h('div', { class: 'msg agent' }, h('div', { class: 'avatar', 'aria-hidden': 'true' }, brandGlyph()), h('div', { class: 'msg-body' }, ...content));
}

export function brandGlyph() {
  const span = document.createElement('span');
  span.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 15c2.5 0 2.5-2 5-2s2.5 2 5 2 2.5-2 5-2 2.5 2 3 2M3 19c2.5 0 2.5-2 5-2s2.5 2 5 2 2.5-2 5-2 2.5 2 3 2M12 3v7m-3-4 3-3 3 3"/></svg>';
  return span.firstChild;
}

function errorBubble(m, canRetry, stopped) {
  return h('div', { class: `err-bubble${stopped ? ' stopped' : ''}` },
    h('b', null, stopped ? '■ Спряно. ' : 'Не се получи. '), stopped ? 'Каквото е написано дотук, остава в папката на поста.' : m.text,
    m.detail ? h('small', null, `Причина: ${m.detail}`) : null,
    canRetry && S.post && S.post.brief && !S.post.post
      ? h('div', null, h('button', { type: 'button', class: 'btn ghost small', onclick: () => generate(S.post.brief, 'Опитай пак.') }, '↻ Опитай пак'))
      : null);
}

// ------------------------------------------------------------------ картата „като в LinkedIn"

function fold(text, limit) {
  let cut = text.length;
  let nl = -1;
  for (let i = 0; i < 3; i += 1) {
    nl = text.indexOf('\n', nl + 1);
    if (nl === -1) break;
  }
  if (nl !== -1) cut = nl;
  if (cut > limit) {
    const space = text.lastIndexOf(' ', limit);
    cut = space > limit * 0.6 ? space : limit;
  }
  if (cut >= text.trimEnd().length) return { head: text, rest: '', folded: false };
  return { head: text.slice(0, cut).trimEnd(), rest: text.slice(cut), folded: true };
}

function textBlock(text) {
  // LinkedIn показва ~3 реда (≈210 знака на компютър, ≈140 на телефон) преди „…още“.
  const node = h('div', { class: 'li-text' });
  const { head, rest, folded } = fold(text, S.view === 'mobile' ? 140 : 210);
  if (!folded) {
    node.innerHTML = linkify(esc(text));
    return node;
  }
  node.innerHTML = `${linkify(esc(head))} `;
  node.appendChild(h('button', {
    type: 'button',
    class: 'more',
    onclick: () => {
      node.innerHTML = `${linkify(esc(head))}<span class="fold-line"><span>до тук се вижда без „…още“</span></span>${linkify(esc(rest.replace(/^\s+/, '')))}`;
    },
  }, '…още'));
  return node;
}

function liCard({ text, photos, ribbon, old, editor }) {
  const bytes = Object.fromEntries(((S.post && S.post.photos) || []).map((p) => [p.name, p.bytes]));
  const names = photos || [];
  const cells = names.slice(0, 5).map((name, i) => {
    const url = photoUrl(name, 'm', bytes[name]);
    return h('div', { class: 'cell' },
      h('img', { src: url, alt: '', loading: 'lazy', onclick: () => openLightbox(url) }),
      i === 4 && names.length > 5 ? h('span', { class: 'plus-n' }, `+${names.length - 5}`) : null);
  });
  return h('article', { class: `li-card${old ? ' old' : ''}` },
    ribbon ? h('div', { class: 'ribbon' }, ribbon) : null,
    h('header', { class: 'li-head', html:
      '<div class="li-avatar" aria-hidden="true">ИКТ</div>' +
      '<div class="li-who"><b>ИКТ Клъстер – Варна</b><span>1 115 последователи</span>' +
      '<span>сега · <svg class="globe" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.5"/><path d="M1.5 8h13M8 1.5c2 2 2 11 0 13M8 1.5c-2 2-2 11 0 13"/></svg></span></div>' +
      '<span class="li-dots" aria-hidden="true">•••</span>' }),
    editor || textBlock(text),
    cells.length ? h('div', { class: `li-photos n${Math.min(names.length, 5)}` }, cells) : null,
    old ? null : h('div', { class: 'li-social', html: '<span>👍❤️ 0</span><span>0 коментара</span>' }),
    old ? null : h('footer', { class: 'li-actions', html:
      '<span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 11v9H4v-9h3Zm0 0 4-7a2 2 0 0 1 3 2l-1 4h5a2 2 0 0 1 2 2l-2 6a2 2 0 0 1-2 2H7"/></svg>Харесай</span>' +
      '<span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H8l-4 4V5Z"/></svg>Коментирай</span>' +
      '<span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 3l4 4-4 4M21 7H9a5 5 0 0 0-5 5v1M7 21l-4-4 4-4M3 17h12a5 5 0 0 0 5-5v-1"/></svg>Сподели</span>' +
      '<span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 3 10 14M21 3l-7 18-4-7-7-4 18-7Z"/></svg>Изпрати</span>' }));
}

function panel(tone, statusParts, stage, ...rest) {
  return h('section', { class: `preview ${tone}` },
    h('header', { class: 'preview-status' }, statusParts),
    h('div', { class: `preview-stage${S.view === 'mobile' ? ' mobile' : ''}` }, stage),
    ...rest);
}

function oldPost(m, version) {
  // По-стар вариант: свит, само текстът — за сравнение и копиране, без действия.
  return h('details', { class: 'old-version' },
    h('summary', null, h('span', null, version ? `Вариант ${version}` : 'Предишен вариант'),
      h('small', null, (m.text || '').replace(/\s+/g, ' ').slice(0, 90) + ((m.text || '').length > 90 ? '…' : ''))),
    h('div', { class: 'old-body' },
      h('div', { class: 'old-text' }, m.text || ''),
      h('button', { type: 'button', class: 'btn ghost small', onclick: (e) => copyText(m.text || '', e.currentTarget) }, 'Копирай този вариант')));
}

// Лента „кога излиза": дата, час, одобрение. Одобрението е за ТОЗИ текст — промяна го отменя.
function scheduleBar() {
  const sch = S.post.schedule || {};
  const post = S.post.post;
  const publishMode = store.info && store.info.publish_mode;
  const date = h('input', { type: 'date', class: 'sch-date', value: sch.scheduled_for || '', min: todayIso(), 'aria-label': 'Дата на публикуване' });
  const time = h('input', { type: 'time', class: 'sch-time', value: sch.scheduled_time || '10:00', step: '300', 'aria-label': 'Час' });
  const save = async (extra) => {
    try {
      const info = await api('PUT', `/api/posts/${enc(S.postId)}/schedule`, { date: date.value, time: time.value, ...extra });
      S.post.schedule = info;
      renderThread();
      loadPosts();
      return info;
    } catch (e) {
      toast(e.message, 'err');
      return null;
    }
  };
  date.addEventListener('change', () => save({}));
  time.addEventListener('change', () => { if (date.value) save({}); });

  let state;
  if (post && post.published) {
    state = h('span', { class: 'sch-state published' }, icon('check'), 'Публикуван');
  } else if (sch.approved) {
    state = h('span', { class: 'sch-state approved' }, icon('check'),
      publishMode === 'linkedin' ? 'Одобрен — излиза автоматично в този час' : 'Одобрен — ще напомня в този час');
  } else if (sch.approval_stale) {
    state = h('span', { class: 'sch-state stale' }, 'Текстът е променен след одобрението — одобрете пак');
  } else {
    state = h('span', { class: 'sch-state' }, sch.scheduled_for ? 'Насрочен, още не е одобрен' : 'Без дата');
  }
  const approve = post && !post.published
    ? h('button', {
      type: 'button',
      class: `btn small ${sch.approved ? 'ghost' : 'primary'}`,
      disabled: !date.value && !sch.scheduled_for,
      title: date.value ? '' : 'Първо изберете дата',
      onclick: async () => {
        if (sch.approved) { await save({ approved: false }); return; }
        const when = `${fmtDay(date.value, { long: true })} в ${time.value || '10:00'}`;
        const ok = await ask({
          title: 'Одобрявате ли този текст?',
          text: publishMode === 'linkedin'
            ? `Постът излиза в LinkedIn автоматично — ${when}. Ако промените текста, одобрението отпада.`
            : `${when} ще получите напомняне да го публикувате (режим „чернова“ — нищо не се изпраща само). Ако промените текста, одобрението отпада.`,
          ok: 'Одобри',
        });
        if (ok) save({ approved: true });
      },
    }, sch.approved ? 'Отмени одобрението' : '✓ Одобри')
    : null;
  return h('div', { class: 'sch-bar' },
    h('span', { class: 'sch-label' }, icon('calendar'), 'Публикуване'),
    date, time, state, h('span', { class: 'grow' }), approve,
    sch.time_warning ? h('p', { class: 'sch-warn' }, `⚠ ${sch.time_warning}`) : null);
}

function livePost(version) {
  const post = S.post.post;
  const names = S.post.in_post || [];
  const included = S.post.photos.filter((p) => p.included).length;
  const busy = jobRunningHere() || atCapacity();
  const text = S.editing ? S.editText : post.text;

  const tone = post.insufficient_brief ? 'thin' : post.status === 'ready' ? 'ready' : 'notes';
  const label = post.insufficient_brief ? 'Брифът не стига за истински пост' : post.status_label;
  const extras = [];
  if (post.notes.length && !post.insufficient_brief) extras.push(plural(post.notes.length, 'бележка', 'бележки'));
  if (post.final_check === 'pending') extras.push('последна проверка тече…');
  if (post.published) extras.push(post.edited ? 'редактиран · публикуван' : 'публикуван');
  else if (post.edited) extras.push('редактиран от вас');
  const status = [
    h('span', { class: 'preview-icon', 'aria-hidden': 'true' }, tone === 'ready' ? '✓' : '!'),
    h('span', { class: 'preview-title' }, version ? h('small', null, `Вариант ${version} · `) : null, h('b', null, label),
      extras.length ? h('small', null, ` · ${extras.join(' · ')}`) : null),
    h('span', { class: 'grow' }),
    h('button', { type: 'button', class: 'btn copy small', onclick: (e) => copyText(text, e.currentTarget) },
      icon('copy'), h('span', null, 'Копирай текста')),
  ];

  let editor = null;
  if (S.editing) {
    const counter = h('span', { class: 'counter' });
    const area = h('textarea', { rows: '12', 'aria-label': 'Текст на поста' });
    area.value = S.editText;
    const count = () => {
      const n = area.value.length;
      counter.textContent = `${words(area.value)} думи · ${n} / ${LIMIT_CHARS} знака`;
      counter.classList.toggle('over', n > LIMIT_CHARS);
    };
    area.addEventListener('input', () => { S.editText = area.value; count(); });
    count();
    editor = h('div', { class: 'li-edit' }, area, h('div', { class: 'edit-foot' }, counter, h('span', null,
      h('button', { type: 'button', class: 'btn ghost small', onclick: () => { S.editing = false; renderThread(); } }, 'Откажи'),
      h('button', { type: 'button', class: 'btn primary small', onclick: saveEdit }, 'Запази'))));
    setTimeout(() => area.focus(), 0);
  }

  const chars = text.length;
  const stage = [
    liCard({ text: post.text, photos: names, editor }),
    h('div', { class: 'counter-line', html:
      `${words(text)} думи · <span class="${chars > LIMIT_CHARS ? 'over' : ''}">${chars} / ${LIMIT_CHARS} знака</span>` +
      (names.length ? ` · ${plural(names.length, 'снимка', 'снимки')}` : '') }),
  ];

  const rest = [scheduleBar()];
  if (post.insufficient_brief) {
    rest.push(h('div', { class: 'preview-notes thin' }, h('b', null, 'Какво да направите'),
      h('p', null, 'Напишете отдолу още подробности — число, имена, резултат. Ще ги добавя към брифа и ще пиша наново.')));
  }
  if (post.notes.length) {
    rest.push(h('div', { class: 'preview-notes' }, h('b', null, 'Проверете преди публикуване'),
      h('ul', null, post.notes.map((n) => h('li', null, n)))));
  }
  if (post.fixes.length) {
    rest.push(h('details', { class: 'preview-fixes' },
      h('summary', null, `Агентът поправи автоматично ${plural(post.fixes.length, 'дреболия', 'дреболии')}`),
      h('ul', null, post.fixes.map((n) => h('li', null, n)))));
  }

  const run = S.post.last_run;
  if (run && run.events && run.events.length) {
    const { slowest } = summarizeRun(run.events, run.seconds, false, run.kind);
    const title = `⏱ Отне ${fmtTime(run.seconds)}${slowest && slowest.dur >= 20 ? ` · най-дълго: ${slowest.label} ${fmtTime(slowest.dur)}` : ''}`;
    rest.push(h('details', { class: 'preview-logs done' },
      h('summary', null, h('span', null, title), h('small', null, ' — подробности')),
      timelineView(run.events, run.seconds, false, run.kind)));
  }

  const viewSeg = h('div', { class: 'seg small', role: 'radiogroup', 'aria-label': 'Изглед' },
    [['desktop', 'Компютър'], ['mobile', 'Телефон']].map(([key, name]) => h('button', {
      type: 'button', role: 'radio', 'aria-checked': String(S.view === key),
      onclick: () => { S.view = key; storage('view', key); renderThread(); },
    }, name)));
  rest.push(h('footer', { class: 'preview-tools' },
    h('button', { type: 'button', class: 'btn ghost small', onclick: S.editing ? saveEdit : startEdit }, S.editing ? 'Запази' : '✎ Редактирай'),
    h('button', { type: 'button', class: 'btn ghost small', onclick: openPhotos },
      included ? `Снимки · ${names.length} от ${included}` : 'Добави снимки'),
    h('button', { type: 'button', class: 'btn ghost small', onclick: markPublished }, post.published ? '✓ Публикуван' : 'Публикувах го'),
    h('a', { class: 'btn ghost small', href: 'https://www.linkedin.com/company/ict-cluster-varna/', target: '_blank', rel: 'noopener noreferrer' }, 'LinkedIn ↗'),
    h('span', { class: 'grow' }),
    viewSeg));

  const node = h('div', { class: 'msg-stack' }, panel(tone, status, stage, ...rest));
  if (!post.insufficient_brief) {
    node.appendChild(h('div', { class: 'suggest' },
      h('span', { class: 'suggest-label' }, 'Бързи промени:'),
      QUICK.map((q) => h('button', { type: 'button', class: 'chip', disabled: busy, onclick: () => revise(q) }, q)),
      included ? h('button', { type: 'button', class: 'chip', disabled: busy, onclick: repick }, '↻ Избери снимките наново') : null));
  }
  return node;
}

// ------------------------------------------------------------------ ход на работата

function logsSection(title, events, now, running, hint, kind) {
  const open = storage('logs') !== 'closed';
  const node = h('details', { class: 'preview-logs', open },
    h('summary', null, h('span', null, title), hint ? h('small', null, hint) : null),
    timelineView(events, now, running, kind));
  node.addEventListener('toggle', () => storage('logs', node.open ? null : 'closed'));
  return node;
}

function renderPending() {
  if (!S.pendingEl || !S.job) return;
  const job = S.job;
  const slot = S.pendingEl.querySelector('.msg-body');
  const sub = job.kind === 'generate'
    ? (S.draft ? 'Черновата е отдолу — още я проверявам и подобрявам.' : 'Обикновено 2–4 минути. Черновата излиза след около минута.')
    : job.kind === 'repick' ? 'Разглеждам снимките наново.' : 'Обикновено под минута.';
  const pct = job.kind === 'generate' ? Math.max(4, Math.min(96, (job.step / job.total) * 100)) : 50;
  const current = STEP_FOR[Math.min(job.step, STEP_FOR.length - 1)];
  const fill = h('div', { class: 'bar-fill' });
  fill.style.width = `${pct}%`; // през CSSOM — CSP не позволява style атрибути

  const status = [
    h('span', { class: 'spinner', 'aria-hidden': 'true' }),
    h('span', { class: 'preview-title' }, h('b', null, job.stage), h('small', null, ` · ${fmtTime(job.elapsed)}`)),
    h('span', { class: 'grow' }),
    h('button', { type: 'button', class: 'btn ghost small', disabled: job.cancel_requested, onclick: cancelJob }, 'Спри'),
  ];
  const progress = h('div', { class: 'preview-progress' },
    h('div', { class: 'bar' }, fill),
    job.kind === 'generate' ? h('ol', { class: 'steps' }, STEP_LABELS.map((label, i) =>
      h('li', { class: i < current ? 'done' : i === current ? 'now' : null }, label))) : null,
    h('small', null, sub));
  // Картата с черновата се пази между обновяванията (всяка секунда) — иначе разгънатото
  // „…още" би се свивало постоянно.
  if (S.draft && (!S.draftCard || S.draftCard.text !== S.draft)) {
    S.draftCard = { text: S.draft, node: liCard({ text: S.draft, ribbon: 'Чернова — агентът още я подобрява' }) };
  }
  const stage = S.draft
    ? [S.draftCard.node]
    : [h('div', { class: 'skeleton', 'aria-hidden': 'true' }, h('i'), h('i'), h('i'), h('i', { class: 'short' }))];
  const scrollBox = S.pendingEl.querySelector('.timeline');
  const keepScroll = scrollBox ? scrollBox.scrollTop : null;
  const follow = !scrollBox || scrollBox.scrollTop + scrollBox.clientHeight >= scrollBox.scrollHeight - 8;
  const logs = logsSection('Ход на работата', S.events, job.elapsed, true, ' — какво прави агентът и колко отнема всяка стъпка', job.kind);
  slot.replaceChildren(h('section', { class: 'preview working' },
    h('header', { class: 'preview-status' }, status),
    progress,
    logs,
    h('div', { class: `preview-stage${S.view === 'mobile' ? ' mobile' : ''}` }, stage)));
  const box = slot.querySelector('.timeline');
  if (box) box.scrollTop = follow ? box.scrollHeight : keepScroll;
}

function startJob(job, scroll = true) {
  S.draftCard = null;
  S.jobId = job.id;
  S.job = job;
  S.events = [];
  S.draft = null;
  S.editing = false;
  S.pendingEl = null;
  stopPolling();
  S.pollTimer = setInterval(pollJob, 1000);
  renderAll();
  if (scroll) scrollToBottom(true);
}

function stopPolling() {
  clearInterval(S.pollTimer);
  S.pollTimer = null;
}

async function pollJob() {
  if (!S.jobId) return;
  let data;
  try {
    data = await api('GET', `/api/jobs/${enc(S.jobId)}?since=${S.events.length}`);
  } catch (e) {
    return; // временен проблем с връзката — следващата секунда пак
  }
  if (!S.job || data.job.id !== S.jobId) return;
  S.job = data.job;
  if (data.events && data.events_from === S.events.length) S.events.push(...data.events);
  const newDraft = data.draft && data.draft !== S.draft;
  if (newDraft) S.draft = data.draft;
  if (S.job.post_id === S.postId) {
    renderPending();
    if (newDraft) scrollToBottom(true);
  }
  if (S.job.state !== 'running') {
    stopPolling();
    S.draft = null;
    S.pendingEl = null;
    await Promise.all([refreshPost(), loadInfo(), loadPosts()]);
    if (S.job.state === 'done') {
      scrollToLatest();
      watchFinalCheck();
      if (document.hidden) document.title = '✓ Готово — LinkedIn агент';
    }
  }
}

async function cancelJob() {
  if (!S.jobId) return;
  try {
    const r = await api('POST', `/api/jobs/${enc(S.jobId)}/cancel`, {});
    S.job = r.job;
    renderPending();
    renderComposer();
  } catch (e) {
    toast(e.message, 'err');
  }
}

function watchFinalCheck() {
  clearInterval(S.finalTimer);
  if (!S.post || !S.post.post || S.post.post.final_check !== 'pending') return;
  const before = S.post.post.notes.length;
  const postId = S.postId;
  let tries = 0;
  S.finalTimer = setInterval(async () => {
    tries += 1;
    if (S.postId !== postId || tries > 60) {
      clearInterval(S.finalTimer);
      return;
    }
    if (S.editing) return;
    let data;
    try { data = await api('GET', `/api/posts/${enc(postId)}`); } catch (e) { return; }
    if (!data.post || data.post.final_check === 'pending') return;
    clearInterval(S.finalTimer);
    S.post = data;
    renderAll();
    const extra = data.post.notes.length - before;
    if (extra > 0) toast(`Последната проверка добави ${plural(extra, 'бележка', 'бележки')} — вижте под поста.`);
  }, 5000);
}

// ------------------------------------------------------------------ изпращане

async function send() {
  const text = el.prompt.value.trim();
  if (!text) return;
  if (S.uploading) {
    toast('Изчакайте снимките да се качат.', 'err');
    return;
  }
  // Снимки от старо съобщение (↑): прикачват се наново точно преди изпращане.
  if (S.recall && S.recall.photos.length) {
    try {
      await ensurePost();
      const r = await api('POST', `/api/posts/${enc(S.postId)}/attach`, { from_post: S.recall.post_id, names: S.recall.photos });
      if (S.post) S.post.pending = r.pending;
    } catch (e) {
      toast(e.message, 'err');
      return;
    }
  }
  const post = S.post && S.post.post;
  let ok;
  if (!post) ok = await generate(text, text);
  else if (post.insufficient_brief) ok = await generate(`${S.post.brief}\n\n${text}`, text); // тънък бриф: добавката отива към брифа
  else ok = await revise(text);
  if (ok) {
    el.prompt.value = '';
    S.recall = null;
    S.hist = null;
    storage(draftKey(), null);
    autosize();
    renderTray();
    renderComposer();
  }
}

// ↑ / ↓ в полето: старите съобщения (от всички постове), заедно със снимките им — като в терминала.
async function historyMove(direction) {
  if (!S.hist) {
    let items = [];
    try { items = (await api('GET', '/api/prompts')).prompts; } catch (e) { /* без история */ }
    S.hist = { items, idx: -1, draft: el.prompt.value };
  }
  const next = S.hist.idx + direction;
  if (next < -1 || next >= S.hist.items.length) return;
  S.hist.idx = next;
  if (next === -1) {
    el.prompt.value = S.hist.draft;
    S.recall = null;
  } else {
    const item = S.hist.items[next];
    el.prompt.value = item.text;
    S.recall = item.photos.length ? { post_id: item.post_id, photos: item.photos.slice() } : null;
  }
  autosize();
  const end = el.prompt.value.length;
  el.prompt.setSelectionRange(end, end);
  renderTray();
  updateSendState();
}

async function generate(brief, message) {
  try {
    await ensurePost();
    const r = await api('POST', `/api/posts/${enc(S.postId)}/generate`, { brief, mode: S.mode, message });
    if (r.post_id !== S.postId) {
      const text = storage(draftKey());
      storage(draftKey(), null);
      S.postId = r.post_id;
      if (text) storage(draftKey(), text);
      setHash();
    }
    await refreshPost();
    startJob(r.job);
    loadPosts();
    return true;
  } catch (e) {
    toast(e.message, 'err');
    return false;
  }
}

async function revise(instruction) {
  if (S.editing) await saveEdit();
  try {
    const r = await api('POST', `/api/posts/${enc(S.postId)}/revise`, { instruction });
    await refreshPost();
    startJob(r.job);
    return true;
  } catch (e) {
    toast(e.message, 'err');
    return false;
  }
}

async function repick() {
  try {
    const r = await api('POST', `/api/posts/${enc(S.postId)}/repick`, {});
    await refreshPost();
    startJob(r.job);
  } catch (e) {
    toast(e.message, 'err');
  }
}

// ------------------------------------------------------------------ поле за писане

function autosize() {
  el.prompt.style.height = 'auto';
  el.prompt.style.height = `${Math.min(el.prompt.scrollHeight, 240)}px`;
}

function renderComposer() {
  const post = S.post && S.post.post;
  const running = jobRunningHere();
  const full = atCapacity();
  el.prompt.placeholder = running ? 'Агентът пише… ■ отдясно го спира веднага'
    : full ? `Агентът пише ${runningJobs().length} поста наведнъж — изчакайте един да приключи`
      : !post ? 'Какво се случи? Кой, какво, кога и защо е интересно…'
        : post.insufficient_brief ? 'Допишете подробности — число, имена, резултат…'
          : 'Какво да променя? Напр. „по-кратко“ или „добави, че е 10-то издание“';
  // Докато агентът пише този пост, бутонът за изпращане е „■ Спри" (като в чат ботовете).
  if (el.send.classList.contains('stop') !== running) {
    el.send.classList.toggle('stop', running);
    el.send.innerHTML = running ? STOP_ICON : SEND_ICON;
  }
  el.send.title = running ? 'Спри веднага' : 'Изпрати (Enter)';
  el.send.setAttribute('aria-label', running ? 'Спри' : 'Изпрати');
  updateSendState();
  const showModes = !post || post.insufficient_brief;
  el.modes.hidden = !showModes;
  if (showModes) el.modes.replaceChildren(modePicker(S.mode, setMode, { compact: true }));
}

function updateSendState() {
  el.send.disabled = jobRunningHere()
    ? !!(S.job && S.job.cancel_requested)
    : atCapacity() || S.uploading > 0 || !el.prompt.value.trim();
}

async function setMode(key) {
  S.mode = key;
  renderComposer();
  if (!S.postId) {
    storage('newMode', key);
    return;
  }
  try { await api('PUT', `/api/posts/${enc(S.postId)}`, { mode: key }); } catch (e) { toast(e.message, 'err'); }
}

// ------------------------------------------------------------------ снимки: качване и папка

async function attachAlbum(chosen) {
  if (jobRunningHere()) {
    toast('Агентът работи по този пост — изчакайте да приключи.', 'err');
    return;
  }
  const album = chosen && chosen.path !== undefined ? chosen : await pickAlbum();
  if (!album) return;
  try {
    if (!S.postId) {
      S.post = await api('POST', '/api/library/album', { path: album.path, mode: S.mode });
      S.postId = S.post.id;
      storage('draft:new', null);
      setHash();
      if (!el.prompt.value.trim()) {
        el.prompt.value = S.post.album_brief || `${album.name}${album.date ? ` (${album.date})` : ''}: `;
        autosize();
      }
    } else {
      S.post = await api('POST', `/api/posts/${enc(S.postId)}/album`, { path: album.path });
    }
    const added = S.post.added || album.count;
    toast(`Добавих ${plural(added, 'снимка', 'снимки')} от „${album.name}“.`);
    renderAll();
    loadPosts();
    el.prompt.focus();
  } catch (e) {
    toast(e.message, 'err');
  }
}

export async function collectFiles(dataTransfer) {
  // Пусната папка: обхождаме я (Chrome/Edge/Firefox) — иначе браузърът дава само името ѝ.
  const items = Array.from(dataTransfer.items || []);
  const entries = items.map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null)).filter(Boolean);
  if (!entries.length || !entries.some((e) => e.isDirectory)) return Array.from(dataTransfer.files || []);
  const files = [];
  const walk = async (entry) => {
    if (entry.isFile) {
      const file = await new Promise((ok, fail) => entry.file(ok, fail));
      file.relPath = entry.fullPath.replace(/^\//, ''); // „Папка/Подпапка/снимка.jpg" — за inbox.js
      files.push(file);
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch;
      do {
        batch = await new Promise((ok, fail) => reader.readEntries(ok, fail));
        for (const child of batch) await walk(child);
      } while (batch.length);
    }
  };
  for (const entry of entries) await walk(entry);
  return files;
}

export async function handleFiles(fileList) {
  const files = Array.from(fileList).filter((f) => f.size > 0);
  if (!files.length) return;
  if (jobRunningHere()) {
    toast('Агентът работи по този пост — добавете снимките, след като приключи.', 'err');
    return;
  }
  try {
    await ensurePost();
  } catch (e) {
    toast(e.message, 'err');
    return;
  }
  const batch = files.map((file) => ({ file, pct: 0, zip: /\.zip$/i.test(file.name), state: 'чака' }));
  S.uploads.push(...batch);
  S.uploading += batch.length;
  renderTray();
  renderComposer();
  const postId = S.postId;
  const before = S.post && S.post.post ? S.post.in_post.length : null;
  let added = 0;
  let parked = 0;
  for (const u of batch) {
    try {
      const report = await uploadOne(postId, u);
      added += report.added.length + report.parked.length;
      parked += report.parked.length;
      collectNotes({ ...report, parked: [] });
    } catch (e) {
      S.trayNotes.push(`${u.file.name} — ${e.message}`);
    }
    S.uploads = S.uploads.filter((x) => x !== u);
    S.uploading -= 1;
    if (S.postId === postId) await refreshPost();
  }
  if (parked) {
    const max = (store.info && store.info.max_agent_photos) || 12;
    S.trayNotes.push(`${plural(parked, 'снимка е качена', 'снимки са качени')}, но не са включени — агентът разглежда до ${max} наведнъж. Изберете кои в „Снимки“.`);
  }
  renderTray();
  renderComposer();
  if (added && before !== null && S.postId === postId) {
    toast('Снимките са добавени. „↻ Избери снимките наново“ под поста — ако искате агентът да ги прецени.');
  }
}

function collectNotes(report) {
  if (report.parked.length) {
    const max = (store.info && store.info.max_agent_photos) || 12;
    S.trayNotes.push(`${plural(report.parked.length, 'снимка е качена', 'снимки са качени')}, но изключени — агентът разглежда до ${max} наведнъж. Изберете кои в „Снимки“.`);
  }
  report.skipped.slice(0, 4).forEach((s) => S.trayNotes.push(`${s.name} — ${s.reason}`));
  if (report.skipped.length > 4) S.trayNotes.push(`…и още ${report.skipped.length - 4} пропуснати файла`);
}

function uploadOne(postId, u) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api/posts/${enc(postId)}/upload?name=${enc(u.file.name)}`);
    xhr.setRequestHeader('X-Requested-With', 'linkedin-agent');
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      u.pct = Math.round((e.loaded / e.total) * 100);
      u.state = u.pct < 100 ? `${u.pct}%` : u.zip ? 'разархивирам…' : 'обработвам…';
      renderTray();
    };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { /* не е JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error((data && data.error) || `Грешка ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error('връзката прекъсна — опитайте пак'));
    xhr.send(u.file);
  });
}

function renderTray() {
  // Над полето са само снимките за СЛЕДВАЩОТО съобщение. След изпращане отиват в балончето
  // и полето се изчиства. Всички снимки на поста са в „Снимки" горе вдясно.
  const bytes = Object.fromEntries(((S.post && S.post.photos) || []).map((p) => [p.name, p.bytes]));
  const pending = (S.post && S.post.pending) || [];
  const nodes = [];
  for (const name of pending) {
    nodes.push(h('div', { class: 'thumb', title: name, onclick: openPhotos },
      h('img', { src: photoUrl(name, 's', bytes[name]), alt: name, loading: 'lazy' }),
      h('button', {
        type: 'button', class: 'x', 'aria-label': `Махни ${name}`, title: 'Махни',
        onclick: (e) => { e.stopPropagation(); setIncluded({ name, included: true }, false); },
      }, '✕')));
  }
  if (S.recall && S.recall.photos.length) {
    for (const name of S.recall.photos) {
      nodes.push(h('div', { class: 'thumb recalled', title: `${name} — от старо съобщение` },
        h('img', { src: photoUrlFor(S.recall.post_id, name, 's'), alt: name, loading: 'lazy' }),
        h('button', {
          type: 'button', class: 'x', 'aria-label': `Махни ${name}`, title: 'Махни',
          onclick: (e) => {
            e.stopPropagation();
            S.recall.photos = S.recall.photos.filter((n) => n !== name);
            renderTray();
          },
        }, '✕')));
    }
  }
  for (const u of S.uploads) {
    const bar = h('i');
    bar.style.width = `${u.pct}%`;
    nodes.push(h('div', { class: 'up-chip' }, h('span', { class: 'n' }, u.file.name), h('span', { class: 'ubar' }, bar), h('span', null, u.state)));
  }
  if (S.trayNotes.length) {
    nodes.push(h('div', { class: 'tray-note' }, h('ul', null, S.trayNotes.map((n) => h('li', null, n))),
      h('button', { type: 'button', 'aria-label': 'Скрий', onclick: () => { S.trayNotes = []; renderTray(); } }, '✕')));
  }
  el.tray.hidden = !nodes.length;
  el.tray.replaceChildren(...nodes);
}

// ------------------------------------------------------------------ снимки: избор

function openPhotos() {
  if (!S.postId) {
    el.fileInput.click();
    return;
  }
  el.photosModal.hidden = false;
  renderPhotos();
}

function closePhotos() {
  el.photosModal.hidden = true;
}

export function photosOpen() {
  return !el.photosModal.hidden;
}

export { closePhotos };

function renderPhotos() {
  renderTray();
  if (el.photosModal.hidden) return;
  const photos = (S.post && S.post.photos) || [];
  const hasPost = !!(S.post && S.post.post);
  const max = (S.post && S.post.max_agent_photos) || 12;
  const included = photos.filter((p) => p.included).length;
  const off = photos.length - included;
  el.starHelp.hidden = !hasPost;
  let label = `<b>${included}</b> ${included === 1 ? 'включена' : 'включени'}`;
  if (off) label += ` · ${off} ${off === 1 ? 'изключена' : 'изключени'}`;
  if (hasPost) label += ` · <b>${S.post.in_post.length}</b> в поста`;
  if (included > max) label += ` <span class="warn">— над ${max} забавят агента</span>`;
  el.photoCount.innerHTML = photos.length ? label : 'Още няма снимки';
  el.photos.replaceChildren(...(photos.length ? photos.map((p) => photoTile(p, hasPost))
    : [h('div', { class: 'grid-empty' }, 'Добавете снимки или ZIP с бутона отдолу, от папката, или ги пуснете тук.')]));
}

function photoTile(p, hasPost) {
  const tile = h('figure', {
    class: `tile ${p.included ? 'on' : 'off'}${p.in_post ? ' inpost' : ''}`,
    tabindex: '0', role: 'checkbox', 'aria-checked': String(p.included),
    'aria-label': `${p.name} — ${p.included ? 'агентът я вижда' : 'изключена'}`,
    title: p.agent_reason ? `Агентът: ${p.agent_reason}` : null,
    onclick: () => setIncluded(p, !p.included),
    onkeydown: (e) => {
      if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        setIncluded(p, !p.included);
      }
    },
  },
  h('img', { src: photoUrl(p.name, 's', p.bytes), alt: '', loading: 'lazy' }),
  h('span', { class: 'check' }, '✓'),
  h('figcaption', { class: 'cap' }, p.name));
  if (!p.included) tile.appendChild(h('span', { class: 'off-label', html: '<span>изключена</span>' }));
  if (hasPost && p.included) {
    tile.appendChild(h('button', {
      type: 'button', class: 'star', title: p.in_post ? 'Махни от поста' : 'Сложи в поста',
      html: `${p.in_post ? '★' : '+'}<span class="long"> В поста</span>`,
      onclick: (e) => { e.stopPropagation(); toggleInPost(p); },
    }));
  }
  if (p.mismatch) {
    tile.appendChild(h('span', {
      class: 'flag', html: '⚠<span class="long"> не пасва?</span>',
      title: p.mismatch,
    }));
  }
  return tile;
}

async function setIncluded(p, target) {
  if (jobRunningHere()) {
    toast('Изчакайте агента да приключи.', 'err');
    return;
  }
  p.included = target;
  if (!target) p.in_post = false;
  renderPhotos();
  try {
    await api('PUT', `/api/posts/${enc(S.postId)}/photos/${enc(p.name)}`, { included: target });
  } catch (e) {
    toast(e.message, 'err');
  }
  await refreshPost();
}

async function setAllIncluded(target) {
  if (jobRunningHere()) return;
  for (const p of ((S.post && S.post.photos) || []).filter((x) => x.included !== target)) {
    try {
      await api('PUT', `/api/posts/${enc(S.postId)}/photos/${enc(p.name)}`, { included: target });
    } catch (e) {
      toast(e.message, 'err');
      break;
    }
  }
  await refreshPost();
}

async function toggleInPost(p) {
  if (jobRunningHere()) return;
  const current = S.post.in_post.slice();
  const names = current.includes(p.name) ? current.filter((n) => n !== p.name) : [...current, p.name];
  try {
    await api('PUT', `/api/posts/${enc(S.postId)}/selection`, { names });
  } catch (e) {
    toast(e.message, 'err');
  }
  await refreshPost();
}

// ------------------------------------------------------------------ действия с готовия пост

function startEdit() {
  S.editing = true;
  S.editText = S.post.post.text;
  renderThread();
}

async function saveEdit() {
  try {
    await api('PUT', `/api/posts/${enc(S.postId)}/text`, { text: S.editText });
    S.editing = false;
    await refreshPost();
    toast(S.post.schedule && S.post.schedule.approval_stale ? 'Запазено. Одобрението отпадна — одобрете новия текст.' : 'Запазено.');
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function markPublished() {
  const ok = await ask({
    title: 'Отбелязваме като публикуван?',
    text: 'С текущия текст. Така мерим колко се поправя преди публикуване.',
    ok: 'Да, публикуван е',
  });
  if (!ok) return;
  try {
    const text = S.editing ? S.editText : S.post.post.text;
    const r = await api('POST', `/api/posts/${enc(S.postId)}/published`, { text });
    const pct = Math.round(r.edit_ratio * 100);
    toast(pct === 0 ? 'Записано: публикуван без промени.' : `Записано: около ${pct}% от текста е променен преди публикуване.`);
    await Promise.all([refreshPost(), loadPosts()]);
  } catch (e) {
    toast(e.message, 'err');
  }
}

export function openLightbox(src) {
  el.lightboxImg.src = src;
  el.lightbox.hidden = false;
}

// ------------------------------------------------------------------ общо

function renderHeader() {
  el.chatTitle.textContent = (S.post && S.post.title) || 'Нов пост';
  const photos = (S.post && S.post.photos) || [];
  el.photosBtn.hidden = !photos.length;
  const included = photos.filter((p) => p.included).length;
  el.photosBtnText.textContent = `${plural(photos.length, 'снимка', 'снимки')}${included !== photos.length ? ` · ${included} включени` : ''}`;
  el.albumBtn.hidden = !!jobRunningHere();
  document.title = S.post && S.post.title ? `${S.post.title} — LinkedIn агент` : 'Нов пост — LinkedIn агент';
}

function renderAll() {
  if (!S.active) return;
  renderHeader();
  renderThread();
  renderPhotos();
  renderComposer();
}

function onInfo(info) {
  if (!S.active) return;
  const jobs = info.jobs || [];
  const mine = jobs.find((j) => j.post_id === S.postId);
  if (mine && !S.pollTimer) startJob(mine, false);
  renderComposer();
}

export function init() {
  [
    'thread', 'messages', 'composer', 'tray', 'prompt', 'modes', 'file-input', 'file-input-2', 'send', 'chat-title',
    'photos-btn', 'photos-btn-text', 'album-btn', 'photos-modal', 'photo-count', 'star-help', 'photos', 'include-all',
    'exclude-all', 'photos-close', 'photos-done', 'lightbox', 'lightbox-img', 'album-btn-2',
  ].forEach((id) => { el[id.replace(/-([a-z0-9])/g, (_, c) => c.toUpperCase())] = document.getElementById(id); });
  S.view = storage('view') === 'mobile' ? 'mobile' : 'desktop';

  el.composer.addEventListener('submit', (e) => {
    e.preventDefault();
    if (jobRunningHere()) cancelJob();
    else send();
  });
  el.prompt.addEventListener('input', () => {
    autosize();
    storage(draftKey(), el.prompt.value || null);
    updateSendState();
  });
  el.prompt.addEventListener('keydown', (e) => {
    if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey && !e.isComposing) {
      const value = el.prompt.value;
      const caret = el.prompt.selectionStart;
      const onFirstLine = !value.slice(0, caret).includes('\n');
      const onLastLine = !value.slice(el.prompt.selectionEnd).includes('\n');
      if (e.key === 'ArrowUp' && onFirstLine) {
        e.preventDefault();
        historyMove(1);
        return;
      }
      if (e.key === 'ArrowDown' && onLastLine && S.hist && S.hist.idx >= 0) {
        e.preventDefault();
        historyMove(-1);
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      // Enter никога не спира агента — само изпраща, когато може.
      if (!jobRunningHere() && !el.send.disabled) send();
    }
  });
  el.prompt.addEventListener('paste', (e) => {
    const files = Array.from((e.clipboardData && e.clipboardData.files) || []);
    if (files.length) {
      e.preventDefault();
      handleFiles(files);
    }
  });
  for (const input of [el.fileInput, el.fileInput2]) {
    input.addEventListener('change', async () => {
      const files = Array.from(input.files);
      input.value = '';
      const { routeFiles } = await import('./files.js'); // снимки → тук, график → преглед на графика
      routeFiles(files);
    });
  }
  el.photosBtn.addEventListener('click', openPhotos);
  el.albumBtn.addEventListener('click', () => attachAlbum());
  el.albumBtn2.addEventListener('click', () => { closePhotos(); attachAlbum(); });
  el.photosClose.addEventListener('click', closePhotos);
  el.photosDone.addEventListener('click', closePhotos);
  el.photosModal.addEventListener('click', (e) => { if (e.target === el.photosModal) closePhotos(); });
  el.includeAll.addEventListener('click', () => setAllIncluded(true));
  el.excludeAll.addEventListener('click', () => setAllIncluded(false));
  el.lightbox.addEventListener('click', () => { el.lightbox.hidden = true; });

  on('info', onInfo);
  on('visible', () => {
    if (!S.active) return;
    if (document.title.startsWith('✓')) renderHeader();
    if (S.postId && !S.editing && !S.uploading && !S.pollTimer) refreshPost();
  });
}

export function closeLightbox() {
  if (el.lightbox.hidden) return false;
  el.lightbox.hidden = true;
  return true;
}
