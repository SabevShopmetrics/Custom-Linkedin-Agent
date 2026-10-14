/*
 * Бърза поправка на пост от календара (web-ui-3) — панел отстрани (на телефон: на цял екран).
 *
 * Всичко, което трябва, ако нещо е сгрешено или направено без да искате, без да напускате
 * календара: дата/час, одобрение, текст на ръка, „помоли агента" (свободно или бързи бутони),
 * снимки, бележките на агента, махни от календара, изтрий. Пълният чат е на „Отвори в чата".
 */
import { api, h, icon, toast, ask, enc, plural, words, fmtDay, todayIso, photoUrlFor, LIMIT_CHARS, postState } from './core.js';
import { loadPosts, loadInfo } from './store.js';

const QUICK = ['По-кратко', 'По-дълго', 'По-официално', 'По-топло и лично'];
const D = { id: null, post: null, el: null, poll: null, dirty: false };

export function isOpen() {
  return !!D.el;
}

export function close() {
  clearInterval(D.poll);
  if (D.el) {
    D.el.remove();
    D.el = null;
  }
  D.id = null;
}

export async function open(postId) {
  close();
  D.id = postId;
  D.dirty = false;
  D.el = h('div', { class: 'drawer-wrap', onclick: (e) => { if (e.target === D.el) tryClose(); } },
    h('aside', { class: 'drawer', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Поправка на пост' },
      h('div', { class: 'loading' }, h('span', { class: 'spin' }), 'Зареждам…')));
  document.body.appendChild(D.el);
  await reload();
}

async function tryClose() {
  if (D.dirty && !(await ask({ title: 'Да затворя ли без запис?', text: 'Промените по текста ще се загубят.', ok: 'Затвори', danger: true }))) return;
  close();
}

async function reload() {
  try {
    D.post = await api('GET', `/api/posts/${enc(D.id)}`);
  } catch (e) {
    toast(e.message, 'err');
    close();
    return;
  }
  render();
  const job = D.post.job;
  if (job && job.state === 'running') watch(job.id);
}

function watch(jobId) {
  clearInterval(D.poll);
  D.poll = setInterval(async () => {
    let data;
    try { data = await api('GET', `/api/jobs/${enc(jobId)}?since=999999`); } catch (e) { return; }
    if (!D.el) { clearInterval(D.poll); return; }
    const stage = D.el.querySelector('.dr-running span:last-child');
    if (stage && data.job.state === 'running') stage.textContent = data.job.stage;
    if (data.job.state !== 'running') {
      clearInterval(D.poll);
      await reload();
      loadPosts();
      if (data.job.state === 'done') toast('Агентът поправи поста.');
    }
  }, 1500);
}

function render() {
  if (!D.el) return;
  const p = D.post;
  const post = p.post;
  const sch = p.schedule || {};
  const running = p.job && p.job.state === 'running';
  const state = postState({ ...sch, status: post && post.status, published: post && post.published }, running);
  const box = D.el.querySelector('.drawer');

  const head = h('header', { class: 'dr-head' },
    h('div', null,
      h('span', { class: `dr-state st-${state.key}` }, state.label),
      h('h2', null, p.title || '(без заглавие)')),
    h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Затвори', onclick: tryClose }, icon('close')));

  // Кога
  const date = h('input', { type: 'date', value: sch.scheduled_for || '', min: todayIso(), 'aria-label': 'Дата' });
  const time = h('input', { type: 'time', value: sch.scheduled_time || '10:00', step: '300', 'aria-label': 'Час' });
  const saveWhen = async (extra = {}) => {
    try {
      await api('PUT', `/api/posts/${enc(D.id)}/schedule`, { date: date.value, time: time.value, ...extra });
      await reload();
      loadPosts();
    } catch (e) { toast(e.message, 'err'); }
  };
  date.addEventListener('change', () => saveWhen());
  time.addEventListener('change', () => { if (date.value) saveWhen(); });
  const when = h('section', { class: 'dr-sec' },
    h('h3', null, 'Кога излиза'),
    h('div', { class: 'dr-when' }, date, time,
      sch.scheduled_for ? h('span', { class: 'muted' }, fmtDay(sch.scheduled_for, { long: true })) : null),
    sch.time_warning ? h('p', { class: 'dr-warn' }, `⚠ ${sch.time_warning}`) : null,
    sch.approval_stale ? h('p', { class: 'dr-warn' }, 'Текстът е променен след одобрението — одобрете пак.') : null,
    post && !post.published ? h('div', { class: 'dr-row' },
      sch.approved
        ? h('button', { type: 'button', class: 'btn ghost small', onclick: () => saveWhen({ approved: false }) }, 'Отмени одобрението')
        : h('button', { type: 'button', class: 'btn primary small', disabled: !sch.scheduled_for || running, onclick: () => saveWhen({ approved: true }) }, '✓ Одобри за този час'),
      sch.scheduled_for ? h('button', { type: 'button', class: 'btn ghost small', onclick: unschedule }, 'Махни от календара') : null) : null);

  // Текст
  let body;
  if (running) {
    body = h('div', { class: 'dr-running' }, h('span', { class: 'spinner small' }), h('span', null, p.job.stage || 'Агентът пише…'));
  } else if (!post) {
    body = h('div', { class: 'dr-empty' },
      h('p', null, sch.needs_brief ? 'Празно място — още няма текст.' : 'Още няма текст.'),
      sch.idea ? h('p', { class: 'muted' }, `Идея: ${sch.idea}`) : null,
      briefBox());
  } else {
    const area = h('textarea', { class: 'dr-text', rows: '12', 'aria-label': 'Текст на поста' });
    area.value = post.text;
    const counter = h('small', { class: 'muted' });
    const save = h('button', { type: 'button', class: 'btn primary small', disabled: true }, 'Запази текста');
    const count = () => {
      counter.textContent = `${words(area.value)} думи · ${area.value.length} / ${LIMIT_CHARS} знака`;
      D.dirty = area.value.trim() !== post.text.trim();
      save.disabled = !D.dirty;
    };
    area.addEventListener('input', count);
    save.addEventListener('click', async () => {
      try {
        await api('PUT', `/api/posts/${enc(D.id)}/text`, { text: area.value });
        D.dirty = false;
        toast(sch.approved ? 'Запазено. Одобрението отпадна — одобрете новия текст.' : 'Запазено.');
        await reload();
        loadPosts();
      } catch (e) { toast(e.message, 'err'); }
    });
    count();
    body = h('div', null, area, h('div', { class: 'dr-row' }, counter, h('span', { class: 'grow' }), save));
  }
  const textSec = h('section', { class: 'dr-sec' }, h('h3', null, 'Текст'), body);

  // Помоли агента
  const askSec = post && !running ? h('section', { class: 'dr-sec' },
    h('h3', null, 'Помоли агента'),
    h('div', { class: 'dr-chips' }, QUICK.map((q) => h('button', { type: 'button', class: 'chip', onclick: () => revise(q) }, q))),
    instructionBox()) : null;

  // Снимки и бележки
  const names = (p.in_post && p.in_post.length ? p.in_post : (p.photos || []).filter((x) => x.included).map((x) => x.name)).slice(0, 8);
  const photos = names.length ? h('section', { class: 'dr-sec' },
    h('h3', null, `Снимки · ${plural(names.length, 'в поста', 'в поста')}`),
    h('div', { class: 'dr-photos' }, names.map((n) => h('img', { src: photoUrlFor(D.id, n, 's'), alt: n, loading: 'lazy' })))) : null;
  const notes = post && post.notes.length ? h('section', { class: 'dr-sec' },
    h('h3', null, 'Бележки на агента'),
    h('ul', { class: 'dr-notes' }, post.notes.map((n) => h('li', null, n)))) : null;

  const foot = h('footer', { class: 'dr-foot' },
    h('a', { class: 'btn ghost small', href: `#/p/${enc(D.id)}`, onclick: () => close() }, icon('chat'), 'Отвори в чата'),
    h('span', { class: 'grow' }),
    h('button', { type: 'button', class: 'btn ghost small danger', onclick: remove }, icon('trash'), 'Изтрий'));

  box.replaceChildren(head, h('div', { class: 'dr-body' }, when, textSec, askSec, photos, notes), foot);
}

function instructionBox() {
  const input = h('input', { type: 'text', placeholder: 'Напр. „добави, че е 10-то издание“ или „махни второто изречение“', 'aria-label': 'Какво да промени агентът' });
  const go = h('button', { type: 'button', class: 'btn primary small' }, 'Поправи');
  const submit = () => { if (input.value.trim()) revise(input.value.trim()); };
  go.addEventListener('click', submit);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
  return h('div', { class: 'dr-row' }, input, go);
}

function briefBox() {
  const area = h('textarea', { rows: '3', placeholder: 'Какво се случи? Кой, какво, кога…', 'aria-label': 'Бриф' });
  const go = h('button', { type: 'button', class: 'btn primary small' }, 'Напиши поста');
  go.addEventListener('click', async () => {
    if (!area.value.trim()) return;
    try {
      const r = await api('POST', `/api/posts/${enc(D.id)}/generate`, { brief: area.value.trim(), message: area.value.trim() });
      D.id = r.post_id;
      await reload();
      loadPosts();
      loadInfo();
    } catch (e) { toast(e.message, 'err'); }
  });
  return h('div', { class: 'dr-brief' }, area, go);
}

async function revise(instruction) {
  if (D.dirty && !(await ask({ title: 'Има незапазени промени', text: 'Агентът ще поправя последния ЗАПАЗЕН текст. Продължаваме?', ok: 'Продължи' }))) return;
  try {
    const r = await api('POST', `/api/posts/${enc(D.id)}/revise`, { instruction });
    D.dirty = false;
    await reload();
    watch(r.job.id);
    loadInfo();
  } catch (e) { toast(e.message, 'err'); }
}

async function unschedule() {
  const ok = await ask({ title: 'Да махна ли поста от календара?', text: 'Текстът остава — постът отива в „Без дата“. Одобрението отпада.', ok: 'Махни' });
  if (!ok) return;
  try {
    await api('PUT', `/api/posts/${enc(D.id)}/schedule`, { date: '' });
    await reload();
    loadPosts();
  } catch (e) { toast(e.message, 'err'); }
}

async function remove() {
  const ok = await ask({ title: `Да изтрия ли „${D.post.title}“?`, text: 'Постът отива в кошчето (posts/_kosh) — оттам може да се върне ръчно.', ok: 'Изтрий', danger: true });
  if (!ok) return;
  try {
    await api('DELETE', `/api/posts/${enc(D.id)}`);
    toast('Постът е преместен в кошчето.');
    close();
    loadPosts();
  } catch (e) { toast(e.message, 'err'); }
}
