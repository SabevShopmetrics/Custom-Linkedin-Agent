/*
 * „Папка" (#/papka) — какво агентът вижда на компютъра (web-ui-3, src/web/library.py).
 *
 * Сървърът работи на същия компютър, затова чете папката сам: албуми (подпапки със снимки)
 * и файлове с график. Нищо не се качва ръчно — човекът посочва папката веднъж, после само
 * избира: „Пост от албума", „Направи постове от графика".
 */
import { api, h, icon, toast, plural, enc, fmtDay, libraryImage, storage } from './core.js';
import { store, loadPosts, loadBatches, modeList } from './store.js';
import { uploadFolderButton } from './inbox.js';

const L = { data: null, canChoose: true, preview: null, busy: false };
let root = null;

export async function show() {
  root = document.getElementById('view-library');
  document.title = 'Папка — LinkedIn агент';
  if (!L.data) root.replaceChildren(h('div', { class: 'view-pad' }, h('div', { class: 'loading' }, h('span', { class: 'spin' }), 'Чета папката…')));
  await load();
}

async function load() {
  try {
    const r = await api('GET', '/api/library');
    L.data = r.library;
    L.canChoose = r.can_choose;
  } catch (e) {
    toast(e.message, 'err');
    return;
  }
  render();
}

export async function getLibrary() {
  if (L.data) return L.data;
  try {
    const r = await api('GET', '/api/library');
    L.data = r.library;
    L.canChoose = r.can_choose;
  } catch (e) { /* без папка */ }
  return L.data;
}

function render() {
  if (!root || store.route.name !== 'library') return;
  const lib = L.data;
  const pad = h('div', { class: 'view-pad' });
  if (!lib) {
    pad.append(
      h('header', { class: 'view-head' },
        h('h1', null, 'Папка на компютъра'),
        h('p', null, 'Посочете папката, в която държите снимките от събития и графиците. Агентът ще я чете сам — без качване. Нищо в нея не се променя.')),
      L.canChoose
        ? h('div', { class: 'empty-card' },
          icon('folder', 'big'),
          h('b', null, 'Още няма избрана папка'),
          h('p', null, 'Подпапка за всяко събитие („2026-10-20 среща“) е най-удобно — агентът познава датата от името.'),
          h('div', { class: 'st-actions' },
            h('button', { type: 'button', class: 'btn primary', onclick: chooseFolder }, 'Избери папка'),
            uploadFolderButton('btn ghost')),
          h('p', { class: 'muted' }, '„Избери папка“ — агентът я следи сам. „Качи папка“ — еднократно: всяка подпапка става пост веднага.'))
        : h('div', { class: 'empty-card' }, h('b', null, 'Папката се избира от компютъра, на който работи агентът.')));
    root.replaceChildren(pad);
    return;
  }

  const head = h('header', { class: 'view-head row' },
    h('div', null,
      h('h1', null, lib.name),
      h('p', { class: 'path' }, lib.folder)),
    h('div', { class: 'head-actions' },
      h('button', { type: 'button', class: 'btn ghost small', onclick: load }, '↻ Опресни'),
      uploadFolderButton('btn ghost small'),
      L.canChoose ? h('button', { type: 'button', class: 'btn ghost small', onclick: chooseFolder }, 'Смени папката') : null));
  pad.append(head);

  // Графици
  if (lib.schedules.length) {
    pad.append(h('section', { class: 'lib-section' },
      h('h2', null, 'Графици в папката'),
      h('div', { class: 'sched-list' }, lib.schedules.slice(0, 6).map((s) => h('div', { class: 'sched-item' },
        icon('file'),
        h('div', { class: 'sched-text' }, h('b', null, s.name), h('small', null, s.path)),
        h('button', { type: 'button', class: 'btn ghost small', disabled: L.busy, onclick: () => readSchedule(s.path) }, 'Прочети'))))));
  }
  if (L.preview) pad.append(schedulePreview());

  // Албуми
  pad.append(h('section', { class: 'lib-section' },
    h('h2', null, lib.albums.length ? `Албуми · ${lib.albums.length}` : 'Албуми'),
    lib.albums.length
      ? h('div', { class: 'album-grid' }, lib.albums.map(albumCard))
      : h('p', { class: 'muted' }, 'Няма подпапки със снимки (.jpg, .png, .webp). Сложете снимките от всяко събитие в отделна подпапка.')));
  root.replaceChildren(pad);
}

function albumCard(album) {
  const used = store.posts.filter((p) => p.album === album.path);
  return h('article', { class: 'album' },
    h('button', { type: 'button', class: 'album-cover', onclick: () => postFromAlbum(album), 'aria-label': `Пост от „${album.name}“` },
      h('img', { src: libraryImage(album.cover), alt: '', loading: 'lazy' }),
      h('span', { class: 'album-count' }, plural(album.count, 'снимка', 'снимки'))),
    h('div', { class: 'album-meta' },
      h('b', { title: album.path || album.name }, album.name),
      h('small', null, album.date ? fmtDay(album.date) : 'без дата в името',
        used.length ? ` · ${plural(used.length, 'пост', 'поста')}` : ''),
      album.brief ? h('p', { class: 'album-brief', title: album.brief_file }, album.brief) : null),
    h('button', { type: 'button', class: 'btn ghost small', onclick: () => postFromAlbum(album) }, used.length ? 'Още един пост' : 'Пост от албума'));
}

async function postFromAlbum(album) {
  try {
    const post = await api('POST', '/api/library/album', { path: album.path, mode: storage('newMode') || undefined });
    // Папка = пост: текстът от папката е готовият бриф — остава само Enter.
    storage(`draft:${post.id}`, post.album_brief || `${album.name}${album.date ? ` (${album.date})` : ''}: `);
    await loadPosts();
    location.hash = `#/p/${enc(post.id)}`;
  } catch (e) {
    toast(e.message, 'err');
  }
}

// ------------------------------------------------------------------ график от папката

async function readSchedule(path) {
  L.busy = true;
  render();
  try {
    const parsed = await api('POST', '/api/library/schedule', { path });
    parsed.rows.forEach((r) => { r.on = true; });
    L.preview = parsed;
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    L.busy = false;
    render();
  }
}

function schedulePreview() {
  const p = L.preview;
  const chosen = p.rows.filter((r) => r.on);
  const modes = modeList();
  const rows = p.rows.map((row) => {
    const check = h('input', { type: 'checkbox', checked: row.on, 'aria-label': 'Включи реда' });
    check.addEventListener('change', () => { row.on = check.checked; render(); });
    const brief = h('textarea', { class: 'pv-brief', rows: '2', 'aria-label': 'Бриф' });
    brief.value = row.brief;
    brief.addEventListener('input', () => { row.brief = brief.value; });
    const date = h('input', { type: 'date', value: row.date || '', 'aria-label': 'Дата' });
    date.addEventListener('change', () => { row.date = date.value || null; });
    const mode = h('select', { 'aria-label': 'Режим', onchange: (e) => { row.mode = e.target.value || null; } },
      h('option', { value: '' }, 'по подразбиране'),
      modes.map((m) => h('option', { value: m.key, selected: row.mode === m.key }, `${m.label} · ${m.hint}`)));
    return h('div', { class: `pv-row${row.on ? '' : ' off'}` },
      check, date, brief,
      h('div', { class: 'pv-side' }, mode,
        h('span', { class: `pv-photos${row.photo_count ? ' ok' : ''}`, title: row.photo_names.join('\n') },
          row.photo_count ? `${plural(row.photo_count, 'снимка', 'снимки')} от папката` : 'без снимки')));
  });
  return h('section', { class: 'pv' },
    h('header', { class: 'pv-head' },
      h('h2', null, `${plural(p.rows.length, 'пост', 'поста')} в „${p.source}“`),
      h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Затвори', onclick: () => { L.preview = null; render(); } }, icon('close'))),
    p.warnings.length ? h('ul', { class: 'pv-warn' }, p.warnings.map((w) => h('li', null, w))) : null,
    h('div', { class: 'pv-rows' }, rows),
    h('footer', { class: 'pv-foot' },
      h('span', { class: 'muted' }, 'Снимките се взимат направо от папката. Всеки пост влиза в календара на датата си.'),
      h('span', { class: 'grow' }),
      h('button', { type: 'button', class: 'btn primary', disabled: !chosen.length || L.busy, onclick: launch },
        `Напиши ${plural(chosen.length, 'пост', 'поста')}`)));
}

async function launch() {
  const p = L.preview;
  const rows = p.rows.filter((r) => r.on && r.brief.trim());
  if (!rows.length) return;
  L.busy = true;
  render();
  try {
    const status = await api('POST', '/api/batches', {
      name: p.source.split('/').pop().replace(/\.[^.]+$/, ''), source: p.source, library: true, start: true,
      rows: rows.map((r) => ({ row: r.row, brief: r.brief, date: r.date, mode: r.mode, title: r.title, photos: r.photos })),
    });
    L.preview = null;
    loadBatches();
    loadPosts();
    location.hash = `#/b/${status.id}`;
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    L.busy = false;
  }
}

// ------------------------------------------------------------------ избор на папка

async function chooseFolder() {
  const folder = await folderDialog();
  if (!folder) return;
  try {
    const r = await api('PUT', '/api/library', { folder });
    L.data = r.library;
    L.preview = null;
    toast(`Агентът вече чете „${r.library.name}“.`);
    render();
  } catch (e) {
    toast(e.message, 'err');
  }
}

/** Диалог за папка — разглежда диска през сървъра (само от този компютър). */
function folderDialog() {
  return new Promise((resolve) => {
    const list = h('div', { class: 'fd-list' });
    const crumbs = h('div', { class: 'fd-path' });
    const shortcuts = h('div', { class: 'fd-shortcuts' });
    let current = null;
    const pick = h('button', { type: 'button', class: 'btn primary' }, 'Избери тази папка');
    const dialog = h('dialog', { class: 'ask fd', 'aria-label': 'Избор на папка' },
      h('h2', null, 'Коя папка да чете агентът?'),
      h('p', { class: 'muted' }, 'Отворете я по-долу или поставете пътя ѝ от Explorer (десен бутон → „Копирай като път“). Ако искате само да качите папка веднъж — затворете и ползвайте „Качи папка“.'),
      shortcuts, crumbs, list,
      h('div', { class: 'ask-foot' }, h('button', { type: 'button', class: 'btn ghost', value: 'no' }, 'Откажи'), pick));
    const done = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    const open = async (path) => {
      let data;
      try {
        data = await api('GET', `/api/library/browse${path ? `?path=${enc(path)}` : ''}`);
      } catch (e) {
        toast(e.message, 'err');
        return;
      }
      current = data.path;
      shortcuts.replaceChildren(...data.shortcuts.map((s) => h('button', { type: 'button', class: 'chip', onclick: () => open(s.path) }, s.label)));
      // Пътят се вижда и може да се постави направо (копиран от Explorer) — Enter го отваря.
      const field = h('input', { type: 'text', class: 'fd-input', value: data.path, spellcheck: 'false', 'aria-label': 'Път до папката' });
      field.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); open(field.value.trim().replace(/^"|"$/g, '')); } });
      crumbs.replaceChildren(
        data.parent ? h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Нагоре', onclick: () => open(data.parent) }, icon('chevL')) : null,
        field,
        h('button', { type: 'button', class: 'btn ghost small', onclick: () => open(field.value.trim().replace(/^"|"$/g, '')) }, 'Отвори'));
      list.replaceChildren(...(data.dirs.length
        ? data.dirs.map((d) => h('button', { type: 'button', class: 'fd-item', onclick: () => open(d.path) }, icon('folder'), h('span', null, d.name)))
        : [h('p', { class: 'muted' }, 'Няма подпапки.')]));
    };
    pick.addEventListener('click', () => done(current));
    dialog.querySelector('[value="no"]').addEventListener('click', () => done(null));
    dialog.addEventListener('cancel', (e) => { e.preventDefault(); done(null); });
    document.body.appendChild(dialog);
    dialog.showModal();
    open(null);
  });
}

/** Избор на албум (за чата): Promise с албума или null. Без папка — води към „Папка". */
export async function pickAlbum() {
  const lib = await getLibrary();
  if (!lib) {
    toast('Първо изберете папка — агентът ще я чете сам.');
    location.hash = '#/papka';
    return null;
  }
  if (!lib.albums.length) {
    toast(`В „${lib.name}“ няма подпапки със снимки.`, 'err');
    return null;
  }
  return new Promise((resolve) => {
    const grid = h('div', { class: 'album-grid picker' });
    const dialog = h('dialog', { class: 'ask wide', 'aria-label': 'Албум от папката' },
      h('h2', null, `Албум от „${lib.name}“`),
      grid,
      h('div', { class: 'ask-foot' }, h('button', { type: 'button', class: 'btn ghost' }, 'Откажи')));
    const done = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    grid.replaceChildren(...lib.albums.map((album) => h('button', { type: 'button', class: 'album-pick', onclick: () => done(album) },
      h('img', { src: libraryImage(album.cover), alt: '', loading: 'lazy' }),
      h('b', null, album.name),
      h('small', null, `${plural(album.count, 'снимка', 'снимки')}${album.date ? ` · ${fmtDay(album.date)}` : ''}`))));
    dialog.querySelector('.ask-foot .btn').addEventListener('click', () => done(null));
    dialog.addEventListener('cancel', (e) => { e.preventDefault(); done(null); });
    document.body.appendChild(dialog);
    dialog.showModal();
  });
}

export function invalidate() {
  L.data = null;
}
