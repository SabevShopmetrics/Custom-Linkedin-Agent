/*
 * Общото ядро на интерфейса (web-ui-3): заявки към сървъра, създаване на елементи, икони,
 * известия, диалог за потвърждение, памет в браузъра и форматиране. Всички изгледи (чат,
 * асистент, календар, папка, табло) внасят оттук — никой не държи свое копие.
 */

export const enc = encodeURIComponent;
export const HEADERS = { 'X-Requested-With': 'linkedin-agent' };
export const LIMIT_CHARS = 3000; // таван на LinkedIn за текст на пост
export const STEP_LABELS = ['Чета брифа', 'Пиша чернова', 'Проверявам', 'Подобрявам', 'Последни поправки'];
export const STEP_SHORT = ['Бриф', 'Чернова', 'Проверка', 'Подобрение', 'Финал'];
export const STEP_FOR = [0, 0, 1, 2, 3, 3, 4]; // стъпка на конвейера (0–6) → етикет
export const MONTHS = ['януари', 'февруари', 'март', 'април', 'май', 'юни', 'юли', 'август', 'септември', 'октомври', 'ноември', 'декември'];
export const MONTHS_SHORT = ['яну', 'фев', 'мар', 'апр', 'май', 'юни', 'юли', 'авг', 'сеп', 'окт', 'ное', 'дек'];
export const WEEKDAYS_SHORT = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'нд'];

// ------------------------------------------------------------------ заявки

export async function api(method, url, body, raw) {
  const opts = { method, headers: { ...HEADERS } };
  if (raw !== undefined) {
    opts.headers['Content-Type'] = 'application/octet-stream';
    opts.body = raw;
  } else if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch (e) {
    throw new Error('Няма връзка със сървъра на агента. Работи ли още прозорецът му?');
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* празно тяло */ }
  if (res.status === 404 && data && data.error === 'Няма такъв адрес.') {
    // Непознат адрес = страниците са по-нови от сървъра (не е рестартиран след обновяване).
    throw new Error('Агентът е обновен, но прозорецът му още работи със старата версия. Затворете черния прозорец на агента (start_web.bat) и го пуснете отново.');
  }
  if (!res.ok) throw new Error((data && data.error) || `Грешка ${res.status}`);
  return data;
}

// ------------------------------------------------------------------ елементи

export function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// Иконите на едно място — един щрих, един размер; изгледите ги ползват по име.
export const ICONS = {
  spark: 'M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6',
  calendar: 'M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6Zm0 4h16M9 2v4m6-4v4',
  folder: 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z',
  pen: 'M4 20h4L19 9l-4-4L4 16v4Zm10-14 4 4',
  list: 'M4 6h16M4 12h16M4 18h10',
  close: 'M6 6l12 12M18 6 6 18',
  plus: 'M12 5v14M5 12h14',
  send: 'M12 19V5m0 0-6 6m6-6 6 6',
  copy: 'M8 8h12v12H8zM4 16V4h12',
  photo: 'M4 5h16v14H4zM4 15l4-4 4 4 3-3 5 5',
  clock: 'M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
  check: 'M5 12l5 5L20 7',
  trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  chevL: 'M15 6l-6 6 6 6',
  chevR: 'M9 6l6 6-6 6',
  search: 'M11 18a7 7 0 1 1 0-14 7 7 0 0 1 0 14Zm5-2 4 4',
  file: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Zm0 0v5h5M9 13h6M9 17h6',
  up: 'M12 16V4m0 0-4 4m4-4 4 4M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2',
  chat: 'M4 5h16v11H8l-4 4V5Z',
  grid: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',
  bell: 'M6 16V11a6 6 0 1 1 12 0v5l2 2H4l2-2Zm4 4h4',
  gear: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4-3a7.4 7.4 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7.5 7.5 0 0 0-2-1.2L14.5 3h-4l-.4 2.6a7.5 7.5 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1a7.5 7.5 0 0 0 2 1.2l.4 2.6h4l.4-2.6a7.5 7.5 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2Z',
  alert: 'M12 8v5m0 3.5h.01M10.3 3.9 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z',
  help: 'M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6M12 17h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
};

// ------------------------------------------------------------------ екипът във всеки агент (web-ui-5)
//
// „Агент 1–3" на „Преглед" = до толкова поста едновременно. Всеки агент е екип, който минава
// поста през едни и същи стъпки. Едно място за описанията — ползват ги лентата „Агентите" и
// туториалът. `stage` е етапът, който конвейерът докладва (src/repl.py, _HUMAN_STAGES).
export const TEAM = [
  { role: 'Планер', stage: /Чета брифа/, doing: 'чете брифа', time: '5–15 s',
    text: 'Чете брифа и решава: какъв пост е, какъв тон, стига ли информацията, има ли чувствителна тема.' },
  { role: 'Писател + Снимки', stage: /Пиша чернова/, doing: 'пише първа чернова и избира снимки', time: '20–50 s',
    text: 'Писателят пише първата чернова. Едновременно с него „Снимки“ избира кои снимки влизат, маха дубликатите и отбелязва възможни деца.' },
  { role: 'Критик', stage: /Проверявам факти/, doing: 'проверява факти, тон и правила', time: '20–40 s',
    text: 'Проверява всяко твърдение срещу брифа и знанията за клъстера, тона срещу реалните постове и правилата (GDPR, забранени теми). Не спира поста — дава бележки.' },
  { role: 'Писател + Консолидатор', stage: /Подобрявам/, doing: 'пише втора версия и сглобява', time: '30–60 s',
    text: 'Писателят пише втора версия по бележките на Критика; Консолидаторът сглобява най-доброто от двете.' },
  { role: 'Правила в кода', stage: /Последни поправки/, doing: 'поправя емоджи, дължина, дати', time: '< 1 s',
    text: 'Без модел: емоджи, хаштагове (никога), дължина, непознати линкове, ден от седмицата. Всяка поправка се записва.' },
  { role: 'Критик + самопоправка', stage: /Последна проверка/, doing: 'проверява готовия текст', time: '20–60 s',
    text: 'Критикът поглежда готовия текст още веднъж и агентът сам маха недоказаното, без да губи факт от брифа. Едва тогава постът е „Готов“.' },
  { role: 'Писател', stage: /Редактирам/, doing: 'редактира по молбата ви', time: '~30 s',
    text: 'Лека редакция на готовия пост по ваша молба — без да пише отначало.' },
];

export function teamMember(stage) {
  return TEAM.find((m) => m.stage.test(stage || '')) || null;
}

// ------------------------------------------------------------------ тема (web-ui-5)

export function applyTheme(value) {
  const theme = value === undefined ? storage('theme') : value;
  if (value !== undefined) storage('theme', theme === 'auto' ? null : theme);
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

export function icon(name, cls) {
  const wrap = document.createElement('span');
  wrap.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"${cls ? ` class="${cls}"` : ''}><path d="${ICONS[name] || name}"/></svg>`;
  return wrap.firstChild;
}

// ------------------------------------------------------------------ известия и диалози

// Известие (web-ui-5): карта с икона, заглавие и „×"; лентата отдолу показва кога ще изчезне,
// спира, докато мишката е върху него. kind: undefined (готово), 'info', 'warn', 'err'.
const TOAST_KINDS = {
  ok: { icon: 'check', title: null, ms: 3600 },
  info: { icon: 'help', title: null, ms: 4500 },
  warn: { icon: 'alert', title: 'Обърнете внимание', ms: 7000 },
  err: { icon: 'alert', title: 'Нещо не се получи', ms: 8000 },
};

export function toast(message, kind) {
  const box = document.getElementById('toasts');
  const key = TOAST_KINDS[kind] ? kind : 'ok';
  const spec = TOAST_KINDS[key];
  const timer = h('i', { class: 'toast-timer' });
  timer.style.animationDuration = `${spec.ms}ms`;
  const node = h('div', { class: `toast ${key}`, role: key === 'err' || key === 'warn' ? 'alert' : 'status' },
    h('span', { class: 'toast-icon' }, icon(spec.icon)),
    h('div', { class: 'toast-text' }, spec.title ? h('b', null, spec.title) : null, h('span', null, message)),
    h('button', { type: 'button', class: 'toast-x', 'aria-label': 'Затвори', onclick: () => remove() }, icon('close')),
    timer);
  let left = spec.ms;
  let started = Date.now();
  let handle = null;
  const remove = () => { clearTimeout(handle); node.classList.add('leaving'); setTimeout(() => node.remove(), 180); };
  const arm = () => { started = Date.now(); handle = setTimeout(remove, left); };
  node.addEventListener('mouseenter', () => { clearTimeout(handle); left -= Date.now() - started; node.classList.add('paused'); });
  node.addEventListener('mouseleave', () => { node.classList.remove('paused'); arm(); });
  box.appendChild(node);
  while (box.children.length > 4) box.firstElementChild.remove(); // без купчини
  arm();
}

/**
 * Потвърждение в стила на интерфейса (вместо системното confirm()). Връща Promise<boolean>.
 * `danger` оцветява бутона за действие в червено — за изтриване и спиране.
 */
export function ask({ title, text, body = null, ok = 'Да', cancel = 'Откажи', danger = false }) {
  return new Promise((resolve) => {
    const dialog = h('dialog', { class: `ask${body ? ' wide' : ''}`, 'aria-labelledby': 'ask-title' },
      h('h2', { id: 'ask-title' }, title),
      text ? h('p', null, text) : null,
      body,
      h('div', { class: 'ask-foot' },
        h('button', { type: 'button', class: 'btn ghost', value: 'no' }, cancel),
        h('button', { type: 'button', class: `btn ${danger ? 'danger-fill' : 'primary'}`, value: 'yes' }, ok)));
    let answered = false;
    const finish = (value) => {
      if (answered) return;
      answered = true;
      dialog.close();
      dialog.remove();
      resolve(value);
    };
    dialog.addEventListener('click', (e) => {
      if (e.target === dialog) finish(false);
      const button = e.target.closest('.ask-foot button');
      if (button) finish(button.value === 'yes');
    });
    dialog.addEventListener('cancel', (e) => { e.preventDefault(); finish(false); });
    document.body.appendChild(dialog);
    dialog.showModal();
    dialog.querySelector('.btn:last-child').focus();
  });
}

export async function copyText(text, button) {
  text = (text || '').trim();
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (e) { ok = false; }
  if (!ok) {
    const ta = h('textarea', { readonly: true, class: 'offscreen' });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    ta.remove();
  }
  if (!ok) {
    toast('Не успях да копирам автоматично — маркирайте текста ръчно.', 'err');
    return false;
  }
  if (button) {
    const original = button.innerHTML;
    button.classList.add('done');
    button.textContent = '✓ Копирано';
    setTimeout(() => { button.classList.remove('done'); button.innerHTML = original; }, 1500);
  } else {
    toast('Копирано.');
  }
  return true;
}

// ------------------------------------------------------------------ памет в браузъра

export function storage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch (e) { /* частен режим — без памет между презарежданията */ }
  return null;
}

// ------------------------------------------------------------------ текст и време

export function esc(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function linkify(html) {
  return html.replace(/(https?:\/\/[^\s<]+[^\s<.,;:!?)\]]|www\.[^\s<]+[^\s<.,;:!?)\]])/g, '<span class="link">$1</span>');
}

export const fmtTime = (s) => `${Math.floor(Math.max(0, s) / 60)}:${String(Math.round(Math.max(0, s)) % 60).padStart(2, '0')}`;
export const words = (text) => (text.trim().match(/\S+/g) || []).length;
export const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

export function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function isoOf(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function parseIso(iso) {
  const m = (iso || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
}

/** „чт, 20 ное" — за дати в списъци; „днес"/„утре" когато е близо. */
export function fmtDay(iso, { long = false } = {}) {
  const d = parseIso(iso);
  if (!d) return '';
  const today = parseIso(todayIso());
  const diff = Math.round((d - today) / 86400000);
  const wd = WEEKDAYS_SHORT[(d.getDay() + 6) % 7];
  const label = `${wd}, ${d.getDate()} ${long ? MONTHS[d.getMonth()] : MONTHS_SHORT[d.getMonth()]}`;
  if (diff === 0) return `днес · ${label}`;
  if (diff === 1) return `утре · ${label}`;
  return label;
}

export const photoUrlFor = (postId, name, size, bytes) => `/api/posts/${enc(postId)}/photos/${enc(name)}?size=${size}&v=${bytes || 0}`;
export const libraryImage = (path, size = 's') => `/api/library/image?path=${enc(path)}&size=${size}`;

/** Състояние на пост с една дума — едно и също навсякъде (история, календар, табло). */
export function postState(p, running) {
  if (running) return { key: 'running', label: 'агентът пише' };
  if (p.published) return { key: 'published', label: 'публикуван' };
  if (p.approved) return { key: 'approved', label: 'одобрен' };
  if (p.needs_brief) return { key: 'empty', label: 'чака бриф' };
  if (p.status === 'ready') return { key: 'ready', label: 'готов' };
  if (p.status) return { key: 'ready_with_notes', label: 'с бележки' };
  return { key: 'draft', label: 'чернова' };
}
