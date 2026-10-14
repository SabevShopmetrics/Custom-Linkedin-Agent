/*
 * „Преглед" (#/) — целият път на постовете с един поглед (web-ui-4; подредено наново в web-ui-5).
 *
 *   ┌ Заглавие: всичко наред ли е · следващият пост ─────────── [Качи папка] [+ Нов пост] ┐
 *   │ Състояние: Агент 1–3 (кой пост, коя роля) · опашка · готови днес · неуспешни · средно │
 *   │ Качвания в ход → табло                                                               │
 *   │ Пътят: Входящи → Пише се → За преглед → Готови → Публикувани   (стъпки с числа)     │
 *   │ Списъкът на избраната стъпка — широки редове: снимка · заглавие · бележка · действие │
 *   └──────────────────────────────────────────────────────────────────────────────────────┘
 *
 * Защо така (web-ui-5): пет тесни колони с 11-пикселов текст не се четяха, а „Иска внимание"
 * повтаряше колоната „За преглед". Сега една стъпка е отворена — по подразбиране тази, която
 * иска човек — и редовете ѝ са на цялата ширина. Клик върху пост → панелът за поправка.
 */
import { api, h, icon, toast, ask, enc, plural, fmtDay, fmtTime, photoUrlFor, storage, STEP_SHORT, STEP_FOR, TEAM, teamMember } from './core.js';
import { store, on, loadPosts, loadInfo, maxJobs } from './store.js';
import * as drawer from './drawer.js';
import { uploadFolderButton } from './inbox.js';

const STAGES = [
  ['inbox', 'Входящи', 'чакат бриф'],
  ['writing', 'Пише се', 'агентите работят'],
  ['review', 'За преглед', 'искат вашия поглед'],
  ['ready', 'Готови', 'проверени — за одобрение'],
  ['published', 'Публикувани', 'последните дни'],
];
const O = { data: null, timer: null, teamOpen: false, stage: storage('ov-stage') || null, picked: false };
let root = null;

export async function show() {
  root = document.getElementById('view-overview');
  document.title = 'Преглед — LinkedIn агент';
  if (!O.data) root.replaceChildren(h('div', { class: 'view-pad' }, h('div', { class: 'loading' }, h('span', { class: 'spin' }), 'Зареждам…')));
  await load();
}

export function hide() {
  clearTimeout(O.timer);
}

export function init() {
  on('visible', () => { if (store.route.name === 'overview') load(); });
  on('posts', () => { if (store.route.name === 'overview') schedule(300); });
  on('batches', () => { if (store.route.name === 'overview') render(); });
}

function schedule(ms) {
  clearTimeout(O.timer);
  O.timer = setTimeout(load, ms);
}

async function load() {
  clearTimeout(O.timer);
  if (store.route.name !== 'overview') return;
  try {
    O.data = await api('GET', '/api/overview');
  } catch (e) {
    schedule(8000);
    return;
  }
  render();
  const busy = O.data.lanes.writing.length || O.data.agents.queued.length;
  if (!document.hidden) schedule(busy ? 2500 : 8000);
}

// ------------------------------------------------------------------ изглед

function render() {
  if (!root || store.route.name !== 'overview' || !O.data) return;
  const d = O.data;
  const pad = root.querySelector('.view-pad');
  const scroll = pad ? pad.scrollTop : 0;
  root.replaceChildren(h('div', { class: 'view-pad ov' },
    hero(d),
    status(d.agents),
    O.teamOpen ? team() : null,
    uploads(d),
    d.folder_waiting.length ? waiting(d) : null,
    pipeline(d)));
  root.querySelector('.view-pad').scrollTop = scroll;
}

function problemCount(d) {
  return d.lanes.review.length + (d.alerts.errors || 0);
}

function hero(d) {
  const problems = problemCount(d);
  const writing = d.lanes.writing.length;
  const title = problems
    ? `${problems} ${problems === 1 ? 'пост иска' : 'поста искат'} вашия поглед`
    : writing ? `Агентите пишат ${plural(writing, 'пост', 'поста')}. Всичко друго е наред.` : 'Всичко върви.';
  const next = d.next;
  return h('header', { class: 'ov-head' },
    h('div', { class: 'ov-head-text' },
      h('p', { class: 'hero-kicker' }, 'Преглед'),
      h('h1', { class: problems ? 'warn' : '' }, title),
      h('p', { class: 'ov-sub' },
        next ? [icon('clock'), 'Следващ: ', h('b', null, `${fmtDay(next.scheduled_for)}${next.scheduled_time ? ` · ${next.scheduled_time}` : ''}`), ` — ${next.title}`]
          : [icon('clock'), 'Нищо не е насрочено.'],
        d.ingest.folder && d.ingest.auto
          ? h('a', { class: 'ov-watch', href: '#/nastroiki', title: 'Нова подпапка там → пост сам. Смяна в „Настройки“.' }, icon('folder'), `Следя „${d.ingest.folder}“`)
          : null)),
    h('div', { class: 'ov-head-actions' },
      uploadFolderButton('btn ghost'),
      h('a', { class: 'btn primary', href: '#/asistent' }, icon('plus'), 'Нов пост')));
}

// Агентите и денят — един ред, равни части.
function status(a) {
  const slots = [];
  for (let n = 0; n < a.max_jobs; n += 1) {
    const job = a.running[n];
    const member = job ? teamMember(job.stage) : null;
    slots.push(job
      ? h('button', { type: 'button', class: 'ov-agent busy', onclick: () => drawer.open(job.post_id), title: member ? member.text : job.stage },
        h('span', { class: 'ov-agent-top' }, h('small', null, `Агент ${n + 1}`), h('small', { class: 'ov-agent-time' }, fmtTime(job.elapsed))),
        h('b', null, job.title),
        h('span', { class: 'ov-agent-role' }, member ? h('i', null, member.role) : null, member ? member.doing : job.stage))
      : h('div', { class: 'ov-agent' },
        h('span', { class: 'ov-agent-top' }, h('small', null, `Агент ${n + 1}`)),
        h('span', { class: 'ov-agent-free' }, 'свободен')));
  }
  const metric = (value, label, cls) => h('div', { class: `ov-metric${cls ? ` ${cls}` : ''}` }, h('b', null, value), h('small', null, label));
  return h('section', { class: 'ov-status', 'aria-label': 'Агентите сега' },
    h('div', { class: 'ov-status-agents' },
      h('div', { class: 'ov-status-title' },
        h('b', null, 'Агентите'),
        h('button', { type: 'button', class: `link-btn ov-team-toggle${O.teamOpen ? ' on' : ''}`, onclick: () => { O.teamOpen = !O.teamOpen; render(); } },
          icon('help'), O.teamOpen ? 'Скрий' : 'Кой какво прави')),
      h('div', { class: 'ov-slots' }, slots)),
    h('div', { class: 'ov-metrics' },
      metric(String(a.queued.length), 'на опашка'),
      metric(String(a.done_today), 'готови днес', 'ok'),
      metric(String(a.failed_today), 'неуспешни днес', a.failed_today ? 'bad' : ''),
      metric(a.avg_seconds ? fmtTime(a.avg_seconds) : '—', 'средно на пост')));
}

function team() {
  return h('section', { class: 'ov-team', 'aria-label': 'Кой какво прави' },
    h('p', null, 'Всеки агент е екип, който минава поста през тези стъпки:'),
    h('ol', null, TEAM.filter((m) => m.role !== 'Писател').map((m, i) => h('li', null,
      h('span', { class: 'ov-team-n' }, String(i + 1)),
      h('div', null, h('b', null, m.role), h('small', null, m.time), h('span', null, m.text))))));
}

// Качвания/графици, които още се пишат → към таблото им (#/b/<id>) с подробностите.
function uploads(d) {
  const live = (store.batches || []).filter((b) => b.active > 0);
  if (!live.length) return null;
  const avg = d.agents.avg_seconds;
  return h('section', { class: 'ov-uploads', 'aria-label': 'Качвания в ход' }, live.map((b) => {
    const eta = avg ? Math.max(1, Math.round((Math.ceil(b.active / Math.max(1, maxJobs())) * avg) / 60)) : null;
    const fill = h('i');
    fill.style.width = `${Math.round((b.done / Math.max(1, b.count)) * 100)}%`;
    return h('a', { class: 'ov-upload', href: `#/b/${b.id}` },
      icon('folder'),
      h('span', { class: 'ov-upload-text' },
        h('b', null, b.name || 'График'),
        h('small', null, `${b.done} от ${b.count} готови · ${b.active} в процес${eta ? ` · остават ~${eta} мин` : ''}`)),
      h('span', { class: 'ov-upload-bar' }, fill),
      h('b', { class: 'ov-upload-go' }, 'Табло', icon('chevR')));
  }));
}

function waiting(d) {
  const items = d.folder_waiting;
  return h('section', { class: 'ov-wait' },
    icon('folder'),
    h('div', null,
      h('b', null, `В папката ${items.length === 1 ? 'чака 1 пост' : `чакат ${items.length} поста`}, още невзети`),
      h('small', null, items.slice(0, 4).map((i) => i.title).join(' · ') + (items.length > 4 ? ' …' : ''))),
    h('button', { type: 'button', class: 'btn primary small', onclick: () => ingest(items.map((i) => i.path)) }, `Вземи ${items.length === 1 ? 'го' : 'всички'}`));
}

// ------------------------------------------------------------------ пътят: стъпки + списък

function defaultStage(d) {
  if (O.picked && O.stage && d.lanes[O.stage]) return O.stage;
  if (d.lanes.review.length) return 'review';
  if (d.lanes.writing.length) return 'writing';
  if (d.lanes.inbox.length) return 'inbox';
  return 'ready';
}

function pipeline(d) {
  const current = defaultStage(d);
  const steps = h('nav', { class: 'ov-steps', role: 'tablist', 'aria-label': 'Пътят на постовете' }, STAGES.map(([key, label, hint]) => {
    const n = d.lanes[key].length;
    return h('button', {
      type: 'button', role: 'tab', 'aria-selected': String(key === current),
      class: `ov-step step-${key}${key === current ? ' current' : ''}${!n ? ' empty' : ''}`,
      onclick: () => { O.stage = key; O.picked = true; storage('ov-stage', key); render(); },
    }, h('b', null, String(n)), h('span', null, label), h('small', null, hint));
  }));
  const cards = d.lanes[current];
  const extra = [];
  if (current === 'ready') {
    const approvable = cards.filter((c) => c.scheduled_for && !c.approved);
    if (approvable.length) {
      extra.push(h('button', { type: 'button', class: 'btn primary small', onclick: () => approveAll(approvable) },
        icon('check'), `Одобри ${approvable.length === 1 ? 'го' : `всички (${approvable.length})`}`));
    }
  }
  const [, label] = STAGES.find(([key]) => key === current);
  return h('section', { class: 'ov-path' },
    steps,
    h('div', { class: 'ov-list-wrap' },
      h('header', { class: 'ov-list-head' },
        h('h2', null, label, h('span', { class: 'ov-count' }, String(cards.length))),
        h('span', { class: 'grow' }),
        extra),
      cards.length
        ? h('ul', { class: 'ov-list' }, cards.map(row))
        : h('p', { class: 'ov-empty' }, emptyText(current))));
}

function emptyText(key) {
  return {
    inbox: 'Няма постове, които чакат бриф.',
    writing: 'Никой не пише в момента.',
    review: 'Нищо не чака вашия преглед.',
    ready: 'Още няма готови постове.',
    published: 'Нищо публикувано в последните дни.',
  }[key];
}

function row(c) {
  const job = c.job;
  const member = job ? teamMember(job.stage) : null;
  const serious = c.issues.filter((i) => i.level !== 'info');
  const top = serious[0] || c.issues[0];
  let state = null;
  if (job) {
    const fill = h('i');
    fill.style.width = `${Math.max(6, Math.min(96, (job.step / job.total) * 100))}%`;
    state = h('span', { class: 'ov-row-progress' },
      h('span', { class: 'ov-bar' }, fill),
      h('small', null, `${member ? member.role : STEP_SHORT[STEP_FOR[Math.min(job.step, STEP_FOR.length - 1)]]} · ${fmtTime(job.elapsed)}`));
  } else if (c.queue_position) {
    state = h('small', { class: 'ov-row-note' }, `Чака свободен агент · №${c.queue_position}`);
  } else if (top) {
    state = h('small', { class: `ov-row-note lvl-${top.level}` }, serious.length ? serious.map((i) => i.text).join(' · ') : top.text);
  }
  return h('li', { class: `ov-row${c.approved ? ' approved' : ''}${serious.length ? ` lvl-${serious[0].level}` : ''}` },
    h('button', { type: 'button', class: 'ov-row-main', onclick: () => drawer.open(c.id) },
      cover(c),
      h('span', { class: 'ov-row-when' },
        c.scheduled_for ? [h('b', null, fmtDay(c.scheduled_for)), h('small', null, c.scheduled_time || '')] : h('small', null, 'без дата')),
      h('span', { class: 'ov-row-text' },
        h('b', null, c.title),
        state)),
    h('span', { class: 'ov-row-side' },
      c.approved ? h('span', { class: 'ov-badge ok' }, icon('check'), 'одобрен') : null,
      c.source === 'folder' ? h('span', { class: 'ov-badge' }, 'от папката') : null,
      fixButton(c, serious[0])));
}

// Едно действие за всяка причина — най-краткият път до „оправено".
function fixButton(c, issue) {
  const open = h('button', { type: 'button', class: 'btn ghost small', onclick: () => drawer.open(c.id) }, 'Отвори');
  if (!issue) return [open];
  const fix = {
    failed: ['Опитай пак', () => retry(c)],
    time: ['Поправи датите', () => revise(c, `Смени думите за време, които зависят от деня на четене (днес, утре, вчера, тази седмица и т.н.), с конкретни дати. Постът излиза на ${c.scheduled_for || 'насрочената дата'}. Не пипай нищо друго.`)],
    source: ['Пиши наново', () => rewrite(c)],
    stale: ['Одобри пак', () => approve(c)],
    due: ['Публикувах го', () => markPublished(c)],
  }[issue.kind];
  return fix ? [h('button', { type: 'button', class: 'btn primary small', onclick: fix[1] }, fix[0]), open] : [open];
}

function cover(c) {
  if (!c.cover) return h('span', { class: 'ov-cover none', 'aria-hidden': 'true' }, icon('pen'));
  return h('img', { class: 'ov-cover', src: photoUrlFor(c.id, c.cover, 's'), alt: '', loading: 'lazy' });
}

// ------------------------------------------------------------------ действия

async function act(fn, done) {
  try {
    await fn();
    if (done) toast(done);
  } catch (e) {
    toast(e.message, 'err');
  }
  loadPosts();
  loadInfo();
  load();
}

function revise(c, instruction) {
  return act(() => api('POST', `/api/posts/${enc(c.id)}/revise`, { instruction }), 'Агентът поправя датите…');
}

function rewrite(c) {
  return act(() => api('POST', `/api/posts/${enc(c.id)}/rewrite-source`, {}), 'Пиша наново по новия текст от папката…');
}

async function retry(c) {
  return act(async () => {
    const post = await api('GET', `/api/posts/${enc(c.id)}`);
    if (!post.brief) throw new Error('Постът няма бриф — отворете го и напишете какво се е случило.');
    await api('POST', `/api/posts/${enc(c.id)}/generate`, { brief: post.brief, message: 'Опитай пак.' });
  }, 'Пиша наново…');
}

function markPublished(c) {
  return act(() => api('POST', `/api/posts/${enc(c.id)}/published`, {}), 'Отбелязан като публикуван.');
}

function approve(c) {
  return act(() => api('PUT', `/api/posts/${enc(c.id)}/schedule`, { approved: true }), 'Одобрено.');
}

async function approveAll(cards) {
  const lines = cards.slice(0, 6).map((c) => `• ${fmtDay(c.scheduled_for)} ${c.scheduled_time || ''} — ${c.title}`).join('\n');
  const ok = await ask({
    title: `Одобрявате ли ${plural(cards.length, 'пост', 'поста')}?`,
    text: `Проверени са и нямат предупреждения. Всеки излиза в своя час с текста, който е сега.\n${lines}${cards.length > 6 ? '\n…' : ''}`,
    ok: 'Одобри',
  });
  if (!ok) return;
  await act(async () => {
    for (const c of cards) await api('PUT', `/api/posts/${enc(c.id)}/schedule`, { approved: true });
  }, `Одобрих ${plural(cards.length, 'пост', 'поста')}.`);
}

function ingest(paths) {
  return act(() => api('POST', '/api/ingest', { paths }), `Взех ${plural(paths.length, 'пост', 'поста')} — агентът пише.`);
}

