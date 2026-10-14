/*
 * „Качи папка" (web-ui-5): папка от компютъра → постове, без да се избира „Папка" на сървъра.
 *
 *   - Бутонът „Качи папка" отваря обикновения прозорец на Windows за избор на папка.
 *   - Плъзната папка навсякъде в прозореца върви по същия път.
 *   - Всяка подпапка със снимки и/или текст = един пост. Датата, часът, заглавието и режимът се
 *     четат на сървъра по същите правила като пост-папките (име „2026-10-30 11.00 Заглавие" или
 *     „Кога:" в текста). Преди създаване — списък за потвърждение.
 *   - Подпапка без текст → пост във „Входящи", чака бриф. Без дата → „без дата".
 */
import { api, h, icon, toast, ask, enc, plural, fmtDay } from './core.js';
import { loadPosts, loadInfo, loadBatches, serverOutdated, OUTDATED_TEXT } from './store.js';

const IMAGE = /\.(jpe?g|png|webp|jfif)$/i;
const TEXT = /\.(txt|md)$/i;
const TEXT_HINT = /бриф|текст|пост|описание|инфо|brief|text|post|caption|info/i;
const MAX_PHOTOS = 12;

const relPath = (f) => f.relPath || f.webkitRelativePath || '';

/**
 * Пост-папките в списък файлове (с относителни пътища). null — ако това не е папка с постове
 * (напр. пуснати само снимки) — тогава файловете отиват по стария път.
 */
export function groupFolders(files) {
  const withPath = files.filter((f) => relPath(f).includes('/'));
  if (!withPath.length) return null;
  const groups = new Map();
  for (const file of withPath) {
    const parts = relPath(file).split('/');
    const dir = parts.slice(0, -1).join('/');
    if (!groups.has(dir)) groups.set(dir, { dir, name: parts[parts.length - 2], images: [], texts: [] });
    const g = groups.get(dir);
    if (IMAGE.test(file.name)) g.images.push(file);
    else if (TEXT.test(file.name)) g.texts.push(file);
  }
  const posts = [...groups.values()].filter((g) => g.images.length);
  if (!posts.length) return null;
  // Само снимки в една папка, без текст → не е „папка с постове", а снимки за текущия пост.
  if (posts.length === 1 && !posts[0].texts.length) return null;
  for (const g of posts) {
    g.texts.sort((a, b) => Number(!TEXT_HINT.test(a.name)) - Number(!TEXT_HINT.test(b.name)) || a.name.localeCompare(b.name));
    g.text = g.texts[0] || null;
    g.images.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  }
  return posts.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
}

// Notepad / Word „Запиши като .txt" — UTF-8 или windows-1251.
async function readText(file) {
  const buffer = await file.arrayBuffer();
  const utf8 = new TextDecoder('utf-8').decode(buffer);
  if (!utf8.includes('�')) return utf8.replace(/^﻿/, '').slice(0, 4000);
  try { return new TextDecoder('windows-1251').decode(buffer).slice(0, 4000); } catch (e) { return utf8.slice(0, 4000); }
}

// ------------------------------------------------------------------ бутонът „Качи папка"

/** Бутон, който отваря прозореца на Windows за избор на папка. */
export function uploadFolderButton(cls = 'btn ghost') {
  const input = h('input', { type: 'file', multiple: true, webkitdirectory: true, directory: true, class: 'offscreen', tabindex: '-1', 'aria-hidden': 'true' });
  input.addEventListener('change', () => {
    const files = Array.from(input.files || []);
    input.value = '';
    if (files.length) importFiles(files);
  });
  const button = h('button', { type: 'button', class: cls, title: 'Изберете папка от компютъра — всяка подпапка става пост', onclick: () => input.click() },
    icon('up'), 'Качи папка');
  return h('span', { class: 'inbox-btn' }, button, input);
}

/** true — ако файловете бяха папка с постове и са поети оттук. */
export async function importFiles(files) {
  const groups = groupFolders(files);
  if (!groups) {
    if (files.some((f) => relPath(f))) toast('В папката няма снимки. Сложете снимките на всяко събитие в отделна подпапка.', 'err');
    return false;
  }
  if (serverOutdated()) {
    toast(OUTDATED_TEXT, 'warn');
    return true;
  }
  const rows = await Promise.all(groups.map(async (g) => ({ ...g, body: g.text ? await readText(g.text) : '' })));
  if (!(await confirmRows(rows))) return true;
  await create(rows);
  return true;
}

// ------------------------------------------------------------------ потвърждение и създаване

function confirmRows(rows) {
  const withText = rows.filter((r) => r.body.trim()).length;
  const list = h('ol', { class: 'inbox-list' }, rows.map((r) => {
    // Само за показване — истинското разчитане е на сървъра (flow.py), по същите правила.
    const named = r.name.match(/^(\d{4}-\d{2}-\d{2})(?:[\s_-]+(\d{1,2})[.:h](\d{2}))?[\s_–-]*(.*)$/);
    const header = r.body.match(/^\s*(?:кога|дата)\s*[:：]\s*(.+)$/im);
    const when = header ? header[1].trim()
      : named ? `${fmtDay(named[1])}${named[2] ? ` · ${named[2].padStart(2, '0')}:${named[3]}` : ''}` : 'без дата';
    return h('li', null,
      h('span', { class: 'inbox-when' }, when),
      h('b', null, (named && named[4]) || r.name),
      h('small', null, `${plural(Math.min(r.images.length, MAX_PHOTOS), 'снимка', 'снимки')}${r.images.length > MAX_PHOTOS ? ` (от ${r.images.length})` : ''}`,
        r.body.trim() ? ' · текст ✓' : ' · без текст — ще чака бриф'));
  }));
  return ask({
    title: `Намерих ${plural(rows.length, 'пост', 'поста')}`,
    text: `${withText} с текст ще се напишат веднага${withText < rows.length ? `, ${rows.length - withText} без текст чакат бриф във „Входящи“` : ''}. Датата и часът — от името на подпапката или „Кога:“ в текста. Публикуването чака вашето одобрение.`,
    body: list,
    ok: `Създай ${plural(rows.length, 'пост', 'поста')}`,
  });
}

async function create(rows) {
  const progress = h('div', { class: 'inbox-progress', role: 'status' });
  const bar = h('i');
  const label = h('span', null, 'Започвам…');
  progress.append(h('b', null, icon('up'), 'Качвам папката'), label, h('span', { class: 'inbox-bar' }, bar));
  document.body.append(progress);
  let done = 0;
  let created = 0;
  const failed = [];
  const past = [];
  let stop = null;
  // Едно качване = едно табло (#/b/<id>) с подробностите за всички постове от папката.
  let batch = null;
  try {
    batch = await api('POST', '/api/inbox/batch', { name: (rows[0].dir || '').split('/')[0] || 'Качена папка' });
  } catch (e) {
    stop = e;
  }
  for (const r of (stop ? [] : rows)) {
    label.textContent = `${done + 1} от ${rows.length} · ${r.name}`;
    let post = null;
    try {
      post = await api('POST', '/api/inbox', { name: r.name, text: r.body, batch: batch.id });
    } catch (e) {
      // Първият пост не мина — едва ли ще минат останалите (стар сървър, няма връзка): спираме веднага.
      if (!created) { stop = e; break; }
      failed.push(r.name);
      done += 1;
      continue;
    }
    created += 1;
    try {
      for (const file of r.images.slice(0, MAX_PHOTOS)) {
        await fetch(`/api/posts/${enc(post.id)}/upload?name=${enc(file.name)}`, {
          method: 'POST', body: file, headers: { 'X-Requested-With': 'linkedin-agent', 'Content-Type': 'application/octet-stream' },
        }).then((res) => { if (!res.ok) throw new Error(`снимка ${file.name} не се качи`); });
      }
      await api('POST', `/api/inbox/${enc(post.id)}/start`, {});
      if (post.past) past.push(post.title);
    } catch (e) {
      failed.push(r.name); // постът е създаден, но снимка или пускането не минаха — вижда се на „Преглед“
    }
    done += 1;
    bar.style.width = `${Math.round((done / rows.length) * 100)}%`;
  }
  progress.remove();
  loadPosts();
  loadInfo();
  loadBatches();
  if (stop) {
    toast(serverOutdated() || /адрес/i.test(stop.message) ? OUTDATED_TEXT : `Не успях да създам постовете: ${stop.message} Нищо не е създадено — опитайте пак.`, 'err');
    return;
  }
  location.hash = `#/b/${batch.id}`;
  const names = (list) => list.slice(0, 3).map((n) => `„${n}“`).join(', ') + (list.length > 3 ? ` и още ${list.length - 3}` : '');
  if (!failed.length) toast(`Създадох ${plural(created, 'пост', 'поста')} — агентите вече пишат. Това е таблото на папката; намирате го и в „Графици“.`);
  else toast(`Създадох ${created} от ${rows.length}. Не минаха докрай: ${names(failed)} — отворете ги на „Преглед“ или опитайте пак само с тях.`, 'warn');
  if (past.length) toast(`${plural(past.length, 'пост е', 'поста са')} с минала дата и остават без дата: ${names(past)}. Насрочете ги от „Календар“.`, 'warn');
}
