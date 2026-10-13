'use strict';
/*
 * График → много постове наведнъж (docs/technical/web-ui-2.md).
 *
 *   #/grafik    — откъде е графикът: папка на компютъра, файл (.xlsx/.csv/.txt/.json) или поставен текст
 *   #/b/<id>    — таблото: по една карта на пост, агентите пишат паралелно, всичко се вижда на живо
 *
 * Папка на компютъра на човека (File System Access API, Chrome/Edge): сървърът НЯМА достъп до
 * компютъра — браузърът чете графика и снимките от избраната папка, качва ги към сървъра, а
 * готовите постове записва обратно в подпапка „LinkedIn постове“ (.md файлове, които човекът
 * редактира където си иска; „Прочети редакциите“ ги връща в агента). Запис има само по https
 * или localhost; иначе папката е само за четене и резултатът се сваля като ZIP.
 *
 * Отделен файл от app.js нарочно: чатът не се пипа, тук е само новото.
 */
(() => {
  const enc = encodeURIComponent;
  const HEADERS = { 'X-Requested-With': 'linkedin-agent' };
  const OUT_DIR = 'LinkedIn постове';
  const SCHEDULE_EXT = /\.(csv|tsv|xlsx|txt|md|json)$/i;
  const IMAGE_EXT = /\.(jpe?g|png|webp|jfif|heic|heif|zip)$/i;
  const STEP_LABELS = ['Бриф', 'Чернова', 'Проверка', 'Подобрение', 'Финал'];
  const STEP_FOR = [0, 0, 1, 2, 3, 3, 4];
  const MONTHS = ['яну', 'фев', 'мар', 'апр', 'май', 'юни', 'юли', 'авг', 'сеп', 'окт', 'ное', 'дек'];
  const MODE_NAMES = { kadar: 'Кадър', fokus: 'Фокус', panorama: 'Панорама' };
  const STATE_NAMES = {
    running: 'пише', queued: 'чака ред', done: 'готов', error: 'грешка', cancelled: 'спрян', idle: 'не е пуснат',
    missing: 'изтрит', uploading: 'качва снимки',
  };
  const canPickFolder = typeof window.showDirectoryPicker === 'function' && window.isSecureContext;

  const B = {
    info: null,
    batches: [],
    batchId: null,
    status: null,
    timer: null,
    folder: null, // { name, handle?, writable, files: Map, dirs: Set }
    parsed: null, // { rows, warnings, source }
    candidates: [],
    uploads: {}, // post_id → { done, total }
    autoSave: true,
    savedSig: {},
    busy: false,
  };

  const $ = (id) => document.getElementById(id);
  const board = $('board');
  const batchList = $('batch-list');

  // ------------------------------------------------------------------ помощници

  async function api(method, url, body, raw) {
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
    if (!res.ok) throw new Error((data && data.error) || `Грешка ${res.status}`);
    return data;
  }

  function h(tag, attrs, ...children) {
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

  function toast(message, kind) {
    const box = $('toasts');
    const node = h('div', { class: `toast${kind === 'err' ? ' err' : ''}` }, message);
    box.appendChild(node);
    setTimeout(() => node.remove(), kind === 'err' ? 7000 : 3200);
  }

  function storage(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch (e) { /* частен режим */ }
    return null;
  }

  const fmtTime = (s) => `${Math.floor(Math.max(0, s) / 60)}:${String(Math.round(Math.max(0, s)) % 60).padStart(2, '0')}`;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const thumbUrl = (postId, name) => `/api/posts/${enc(postId)}/photos/${enc(name)}?size=s`;

  function dateParts(iso) {
    const m = (iso || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!m) return null;
    return { day: +m[3], month: MONTHS[+m[2] - 1], year: +m[1] };
  }

  // ------------------------------------------------------------------ папка на компютъра (IndexedDB пази достъпа)

  function idb(mode, fn) {
    return new Promise((resolve) => {
      let request;
      try { request = indexedDB.open('linkedin-agent', 1); } catch (e) { resolve(null); return; }
      request.onupgradeneeded = () => request.result.createObjectStore('folders');
      request.onerror = () => resolve(null);
      request.onsuccess = () => {
        try {
          const tx = request.result.transaction('folders', mode);
          const op = fn(tx.objectStore('folders'));
          tx.oncomplete = () => resolve(op ? op.result : null);
          tx.onerror = () => resolve(null);
        } catch (e) { resolve(null); }
      };
    });
  }
  const rememberFolder = (key, handle) => idb('readwrite', (s) => s.put(handle, key));
  const recallFolder = (key) => idb('readonly', (s) => s.get(key));

  async function indexHandle(root) {
    const files = new Map();
    const dirs = new Set();
    let count = 0;
    async function walk(dir, prefix, depth) {
      for await (const [name, entry] of dir.entries()) {
        if (count > 5000) return;
        if (name.startsWith('.') || name === OUT_DIR) continue;
        const path = prefix ? `${prefix}/${name}` : name;
        count += 1;
        if (entry.kind === 'directory') {
          dirs.add(path.toLowerCase());
          if (depth < 4) await walk(entry, path, depth + 1);
        } else {
          files.set(path.toLowerCase(), { path, name, get: () => entry.getFile() });
        }
      }
    }
    await walk(root, '', 0);
    return { files, dirs };
  }

  function indexFileList(list) {
    const files = new Map();
    const dirs = new Set();
    let root = '';
    for (const file of list) {
      const parts = (file.webkitRelativePath || file.name).split('/');
      root = parts.length > 1 ? parts[0] : root;
      const rel = parts.slice(parts.length > 1 ? 1 : 0);
      if (rel.some((p) => p.startsWith('.')) || rel[0] === OUT_DIR) continue;
      for (let i = 1; i < rel.length; i += 1) dirs.add(rel.slice(0, i).join('/').toLowerCase());
      const path = rel.join('/');
      files.set(path.toLowerCase(), { path, name: file.name, get: async () => file });
    }
    return { files, dirs, root };
  }

  async function pickFolder() {
    if (!canPickFolder) {
      $('bd-folder-input').click();
      return;
    }
    let handle;
    try {
      handle = await window.showDirectoryPicker({ id: 'linkedin-agent', mode: 'readwrite' });
    } catch (e) {
      if (e && e.name !== 'AbortError') toast('Браузърът не даде достъп до папката.', 'err');
      return;
    }
    await useHandle(handle);
    rememberFolder('last', handle);
  }

  async function useHandle(handle) {
    B.busy = true;
    renderImport();
    try {
      const { files, dirs } = await indexHandle(handle);
      let writable = false;
      try { writable = (await handle.queryPermission({ mode: 'readwrite' })) === 'granted'; } catch (e) { /* стар браузър */ }
      B.folder = { name: handle.name, handle, writable, files, dirs };
      await afterFolder();
    } finally {
      B.busy = false;
      renderImport();
    }
  }

  async function afterFolder() {
    const f = B.folder;
    B.candidates = [...f.files.values()]
      .filter((x) => SCHEDULE_EXT.test(x.name) && x.path.split('/').length <= 2 && !/^readme/i.test(x.name))
      .map((x) => {
        let score = 0;
        if (/граф|grafik|schedule|план|plan|пост|post|календар|calendar/i.test(x.name)) score += 10;
        if (/\.(xlsx|csv|tsv)$/i.test(x.name)) score += 3;
        if (x.path.split('/').length === 1) score += 2;
        return { ...x, score };
      })
      .sort((a, b) => b.score - a.score);
    if (B.candidates.length) await readSchedule(B.candidates[0]);
    else toast(`В „${f.name}“ няма файл с график — качете файл или поставете текст. Снимките ще търся в тази папка.`);
  }

  async function readSchedule(candidate) {
    const file = await candidate.get();
    await parseFile(candidate.path, file);
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

  function imagesUnder(prefix) {
    const p = prefix ? `${prefix}/` : '';
    return [...B.folder.files.entries()]
      .filter(([key, x]) => key.startsWith(p) && IMAGE_EXT.test(x.name))
      .map(([, x]) => x);
  }

  // Колона „Снимки": подпапка или имена на файлове. Празна → подпапка, която започва с датата на реда.
  function resolvePhotos(row) {
    if (!B.folder) return { files: [], missing: row.photos.slice(), guessed: false };
    const found = [];
    const missing = [];
    let guessed = false;
    const add = (list) => list.forEach((x) => { if (!found.includes(x)) found.push(x); });
    const basenameDirs = (base) => [...B.folder.dirs].filter((d) => d.split('/').pop() === base);
    for (const raw of row.photos) {
      const norm = raw.replace(/\\/g, '/').replace(/^\.?\//, '').replace(/\/+$/, '').toLowerCase();
      if (!norm) continue;
      if (B.folder.dirs.has(norm)) { add(imagesUnder(norm)); continue; }
      if (B.folder.files.has(norm)) { add([B.folder.files.get(norm)]); continue; }
      const byDir = basenameDirs(norm.split('/').pop());
      if (byDir.length === 1) { add(imagesUnder(byDir[0])); continue; }
      const byName = [...B.folder.files.values()].filter((x) => x.name.toLowerCase() === norm.split('/').pop());
      if (byName.length === 1) { add(byName); continue; }
      missing.push(raw);
    }
    if (!row.photos.length && row.date) {
      const [y, m, d] = row.date.split('-');
      const keys = [row.date, `${d}.${m}.${y}`, `${d}.${m}`, `${+d}.${+m}`];
      const match = [...B.folder.dirs].filter((dir) => {
        const base = dir.split('/').pop();
        return keys.some((k) => base.startsWith(k));
      });
      if (match.length === 1) { add(imagesUnder(match[0])); guessed = true; }
    }
    return { files: found.slice(0, 24), missing, guessed, over: Math.max(0, found.length - 24) };
  }

  // ------------------------------------------------------------------ изглед: откъде е графикът

  function renderImport() {
    if (!B.active || B.batchId) return;
    const info = B.info || {};
    const maxJobs = info.max_jobs || 3;
    const busy = B.busy;

    const folderCard = h('div', { class: 'bd-source' },
      h('div', { class: 'bd-source-icon', html: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/></svg>' }),
      h('h3', null, 'Папка на компютъра'),
      h('p', null, 'Изберете папката с графика и снимките. Готовите постове се записват обратно в нея — в „', OUT_DIR, '“.'),
      B.folder
        ? h('p', { class: 'bd-ok' }, `✓ „${B.folder.name}“ · ${plural(B.folder.files.size, 'файл', 'файла')}`,
          B.folder.writable ? ' · запис ✓' : ' · само четене (резултатът — като ZIP)')
        : null,
      canPickFolder ? null : h('p', { class: 'bd-hint' },
        'Този браузър/адрес позволява само четене на папка. За запис: Chrome или Edge през https:// или localhost.'),
      h('button', { type: 'button', class: 'btn primary', disabled: busy, onclick: pickFolder },
        B.folder ? 'Друга папка' : 'Избери папка'));

    const fileCard = h('div', { class: 'bd-source' },
      h('div', { class: 'bd-source-icon', html: '<svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Zm0 0v5h5M9 13h6M9 17h6"/></svg>' }),
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
      h('div', { class: 'bd-source-icon', html: '<svg viewBox="0 0 24 24"><path d="M9 4h6v3H9zM7 5H5v16h14V5h-2M8 12h8M8 16h5"/></svg>' }),
      h('h3', null, 'Постави текст'),
      h('p', null, 'По един пост на абзац (или разделени с „---“). Дата в началото — по желание.'),
      area,
      h('button', { type: 'button', class: 'btn ghost', disabled: busy, onclick: () => {
        if (!area.value.trim()) { toast('Поставете поне един бриф.', 'err'); return; }
        parseFile('grafik.txt', new Blob([area.value], { type: 'text/plain' }));
      } }, 'Прочети'));

    const nodes = [
      h('header', { class: 'bd-hero' },
        h('h1', null, 'Цял график наведнъж'),
        h('p', null, `Всеки ред става пост. Агентът пише до ${maxJobs} паралелно — останалите чакат ред и тръгват сами. `,
          'Всеки пост после се отваря в чата за промени, както обикновено.')),
      h('div', { class: 'bd-sources' }, folderCard, fileCard, pasteCard),
      h('details', { class: 'bd-format' },
        h('summary', null, 'Как да изглежда графикът?'),
        h('div', null,
          h('p', null, 'Таблица с колони (заглавията на български или английски, редът няма значение):'),
          h('ul', null,
            h('li', null, h('b', null, 'Бриф'), ' (или Тема/Текст) — какво се е случило. Единствената задължителна.'),
            h('li', null, h('b', null, 'Дата'), ' — 20.10.2026, 2026-10-20 или „20 октомври“.'),
            h('li', null, h('b', null, 'Режим'), ' — кадър, фокус или панорама.'),
            h('li', null, h('b', null, 'Снимки'), ' — подпапка в избраната папка или имена на файлове, разделени със запетая. ',
              'Празно → подпапка, която започва с датата (напр. „2026-10-20 среща“).')),
          h('button', { type: 'button', class: 'link-btn', onclick: downloadExample }, '⬇ Свали примерен график (CSV за Excel)'))),
    ];

    if (B.folder && B.candidates.length > 1) {
      const select = h('select', { class: 'bd-select', onchange: (e) => readSchedule(B.candidates[+e.target.value]) },
        B.candidates.map((c, i) => h('option', { value: String(i), selected: B.parsed && B.parsed.source === c.path }, c.path)));
      nodes.push(h('div', { class: 'bd-pick' }, h('span', null, 'Файл с графика в папката:'), select));
    }
    if (busy) nodes.push(h('div', { class: 'bd-loading' }, h('span', { class: 'bd-spin' }), 'Чета…'));
    if (B.parsed) nodes.push(renderPreview());
    board.replaceChildren(h('div', { class: 'bd-wrap' }, nodes));
  }

  function renderPreview() {
    const parsed = B.parsed;
    const chosen = parsed.rows.filter((r) => r.on);
    const defaultMode = storage('bd-mode') || (B.info && B.info.default_mode) || 'fokus';
    const modeSeg = h('div', { class: 'seg small', role: 'radiogroup', 'aria-label': 'Дължина по подразбиране' },
      Object.entries(MODE_NAMES).map(([key, name]) => h('button', {
        type: 'button', role: 'radio', 'aria-checked': String(defaultMode === key),
        onclick: () => { storage('bd-mode', key); renderImport(); },
      }, name)));

    const rows = parsed.rows.map((row) => {
      const photos = resolvePhotos(row);
      const brief = h('textarea', { class: 'bd-brief', rows: '2', 'aria-label': 'Бриф' });
      brief.value = row.brief;
      brief.addEventListener('input', () => { row.brief = brief.value; });
      const dateInput = h('input', { type: 'date', class: 'bd-date-input', value: row.date || '', 'aria-label': 'Дата' });
      dateInput.addEventListener('change', () => { row.date = dateInput.value || null; renderImport(); });
      const modeSelect = h('select', { class: 'bd-select small', 'aria-label': 'Режим', onchange: (e) => { row.mode = e.target.value || null; } },
        h('option', { value: '' }, '—'),
        Object.entries(MODE_NAMES).map(([key, name]) => h('option', { value: key, selected: row.mode === key }, name)));
      let photoNote;
      if (photos.files.length) {
        photoNote = h('span', { class: 'bd-photo ok', title: photos.files.map((f) => f.path).join('\n') },
          `📷 ${photos.files.length}${photos.guessed ? ' (по датата)' : ''}${photos.over ? ` · +${photos.over} пропуснати` : ''}`);
      } else if (row.photos.length && !B.folder) {
        photoNote = h('span', { class: 'bd-photo warn' }, '📷 изберете папка');
      } else {
        photoNote = h('span', { class: 'bd-photo' }, 'без снимки');
      }
      const missing = photos.missing.length ? h('span', { class: 'bd-photo warn', title: photos.missing.join('\n') }, `⚠ няма: ${photos.missing.slice(0, 2).join(', ')}${photos.missing.length > 2 ? '…' : ''}`) : null;
      const check = h('input', { type: 'checkbox', checked: row.on, 'aria-label': 'Включи реда' });
      check.addEventListener('change', () => { row.on = check.checked; renderImport(); });
      return h('div', { class: `bd-row${row.on ? '' : ' off'}` },
        check, dateInput, brief, h('div', { class: 'bd-row-side' }, modeSelect, photoNote, missing));
    });

    const name = h('input', { type: 'text', class: 'bd-name', value: (parsed.source || 'График').replace(/\.[^.]+$/, '').split('/').pop(), 'aria-label': 'Име на графика' });
    return h('section', { class: 'bd-preview' },
      h('header', { class: 'bd-preview-head' },
        h('h2', null, `${plural(parsed.rows.length, 'пост', 'поста')} в „${parsed.source}“`),
        h('span', { class: 'grow' }),
        h('span', { class: 'bd-muted' }, 'Дължина по подразбиране:'), modeSeg),
      parsed.warnings.length ? h('ul', { class: 'bd-warnings' }, parsed.warnings.map((w) => h('li', null, w))) : null,
      h('div', { class: 'bd-rows' }, rows),
      h('footer', { class: 'bd-go' },
        name,
        h('span', { class: 'grow' }),
        h('button', { type: 'button', class: 'btn primary big', disabled: !chosen.length || B.busy, onclick: () => launch(name.value, defaultMode) },
          `▶ Пусни ${plural(chosen.length, 'пост', 'поста')}`)));
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

  // ------------------------------------------------------------------ пускане

  async function launch(name, defaultMode) {
    const rows = B.parsed.rows.filter((r) => r.on && r.brief.trim());
    if (!rows.length) return;
    B.busy = true;
    renderImport();
    let status;
    try {
      status = await api('POST', '/api/batches', {
        name, source: B.parsed.source, mode: defaultMode, start: false,
        rows: rows.map((r) => ({ row: r.row, brief: r.brief, date: r.date, mode: r.mode, title: r.title, photos: r.photos })),
      });
    } catch (e) {
      B.busy = false;
      renderImport();
      toast(e.message, 'err');
      return;
    }
    B.busy = false;
    const folder = B.folder;
    if (folder && folder.handle) rememberFolder(`batch:${status.id}`, folder.handle);
    const work = rows.map((row, i) => ({ row, item: status.items[i], photos: resolvePhotos(row).files }));
    B.parsed = null;
    B.folderFor = { id: status.id, folder };
    location.hash = `#/b/${status.id}`;

    // Без снимки — тръгват веднага. Със снимки — всеки пост тръгва, щом неговите снимки се качат.
    const now = work.filter((w) => !w.photos.length).map((w) => w.item.post_id);
    if (now.length) api('POST', `/api/batches/${status.id}/start`, { post_ids: now }).catch((e) => toast(e.message, 'err'));
    const withPhotos = work.filter((w) => w.photos.length);
    withPhotos.forEach((w) => { B.uploads[w.item.post_id] = { done: 0, total: w.photos.length }; });
    let next = 0;
    const lane = async () => {
      while (next < withPhotos.length) {
        const w = withPhotos[next];
        next += 1;
        for (const photo of w.photos) {
          try {
            const file = await photo.get();
            await api('POST', `/api/posts/${enc(w.item.post_id)}/upload?name=${enc(file.name)}`, undefined, file);
          } catch (e) {
            toast(`${photo.name}: ${e.message}`, 'err');
          }
          B.uploads[w.item.post_id].done += 1;
          if (B.batchId === status.id) renderBoard();
        }
        delete B.uploads[w.item.post_id];
        try {
          await api('POST', `/api/batches/${status.id}/start`, { post_ids: [w.item.post_id] });
        } catch (e) {
          toast(e.message, 'err');
        }
        if (B.batchId === status.id) poll();
      }
    };
    await Promise.all([lane(), lane(), lane()]);
  }

  // ------------------------------------------------------------------ табло

  async function openBoard(id) {
    B.batchId = id;
    B.status = null;
    board.replaceChildren(h('div', { class: 'bd-wrap' }, h('div', { class: 'bd-loading' }, h('span', { class: 'bd-spin' }), 'Зареждам графика…')));
    if (B.folderFor && B.folderFor.id === id) {
      B.boardFolder = B.folderFor.folder;
    } else {
      B.boardFolder = null;
      const handle = await recallFolder(`batch:${id}`);
      if (handle) B.boardFolder = { name: handle.name, handle, writable: false, needsPermission: true };
    }
    await poll();
  }

  async function poll() {
    clearTimeout(B.timer);
    const id = B.batchId;
    if (!id || !B.active) return;
    try {
      B.status = await api('GET', `/api/batches/${enc(id)}`);
    } catch (e) {
      board.replaceChildren(h('div', { class: 'bd-wrap' }, h('div', { class: 'bd-empty' }, e.message)));
      return;
    }
    if (B.batchId !== id) return;
    renderBoard();
    autoSave();
    const live = B.status.items.some((i) => i.state === 'running' || i.state === 'queued') || Object.keys(B.uploads).length;
    B.timer = setTimeout(poll, live ? 1500 : 6000);
  }

  function stateOf(item) {
    if (B.uploads[item.post_id]) return 'uploading';
    return item.state;
  }

  function renderBoard() {
    const st = B.status;
    if (!st || !B.active) return;
    const items = st.items;
    const counts = { done: 0, running: 0, queued: 0, error: 0, idle: 0, uploading: 0 };
    items.forEach((i) => {
      const s = stateOf(i);
      const key = s === 'cancelled' ? 'error' : s === 'missing' ? 'idle' : s;
      counts[key] = (counts[key] || 0) + 1;
    });
    const total = items.length || 1;
    const bar = h('div', { class: 'bd-bar', role: 'img', 'aria-label': `${counts.done} от ${items.length} готови` },
      ['done', 'running', 'uploading', 'queued', 'error'].map((key) => {
        const seg = h('span', { class: `bd-bar-${key}` });
        seg.style.width = `${(counts[key] / total) * 100}%`;
        return seg;
      }));

    const running = items.filter((i) => i.state === 'running');
    const slots = [];
    for (let n = 0; n < st.max_jobs; n += 1) {
      const item = running[n];
      if (item) {
        const step = item.job ? STEP_FOR[Math.min(item.job.step, 6)] : 0;
        slots.push(h('button', { type: 'button', class: 'bd-slot busy', onclick: () => scrollToCard(item.post_id) },
          h('span', { class: 'bd-slot-n' }, `Агент ${n + 1}`),
          h('b', null, item.title),
          h('small', null, `${STEP_LABELS[step]} · ${fmtTime(item.job ? item.job.elapsed : 0)}`)));
      } else {
        slots.push(h('div', { class: 'bd-slot' },
          h('span', { class: 'bd-slot-n' }, `Агент ${n + 1}`),
          h('small', null, n < st.running_total ? 'зает с друг пост' : 'свободен')));
      }
    }

    const leftovers = items.filter((i) => ['idle', 'error', 'cancelled'].includes(i.state) && !B.uploads[i.post_id]).length;
    const active = counts.running + counts.queued + counts.uploading;
    const folder = B.boardFolder;
    const actions = [];
    if (folder) {
      if (folder.needsPermission || !folder.writable) {
        actions.push(h('button', { type: 'button', class: 'btn ghost small', onclick: reconnectFolder },
          folder.handle ? `📁 Свържи „${folder.name}“` : `📁 „${folder.name}“ (само четене)`));
      } else {
        const toggle = h('input', { type: 'checkbox', checked: B.autoSave });
        toggle.addEventListener('change', () => { B.autoSave = toggle.checked; if (B.autoSave) autoSave(); });
        actions.push(h('label', { class: 'bd-auto' }, toggle, `Записвай в „${folder.name}/${OUT_DIR}“`));
        actions.push(h('button', { type: 'button', class: 'btn ghost small', onclick: () => saveToFolder(true) }, '💾 Запиши сега'));
        actions.push(h('button', { type: 'button', class: 'btn ghost small', onclick: readEdits }, '↺ Прочети редакциите'));
      }
    } else if (canPickFolder) {
      actions.push(h('button', { type: 'button', class: 'btn ghost small', onclick: connectFolder }, '📁 Записвай в папка'));
    }
    actions.push(h('a', { class: 'btn ghost small', href: `/api/batches/${enc(st.id)}/export.zip`, download: '' }, '⬇ ZIP'));
    if (leftovers) actions.push(h('button', { type: 'button', class: 'btn primary small', onclick: () => startItems(null) }, `▶ Пусни ${leftovers} непуснати`));
    if (active) actions.push(h('button', { type: 'button', class: 'btn ghost small danger', onclick: stopAll }, '■ Спри всички'));

    const sorted = items.slice().sort((a, b) => (a.date || '9999').localeCompare(b.date || '9999') || a.index - b.index);
    const timeline = h('div', { class: 'bd-timeline', role: 'list' }, sorted.map((item) => {
      const p = dateParts(item.date);
      return h('button', { type: 'button', role: 'listitem', class: `bd-tl ${stateOf(item)}`, title: `${item.title} — ${STATE_NAMES[stateOf(item)] || ''}`, onclick: () => scrollToCard(item.post_id) },
        h('span', { class: 'bd-tl-dot' }),
        h('span', null, p ? `${p.day} ${p.month}` : `№${item.index}`));
    }));

    const head = h('header', { class: 'bd-head' },
      h('div', { class: 'bd-title' },
        h('a', { class: 'bd-back', href: '#/grafik' }, '← Нов график'),
        h('h1', null, st.name || 'График'),
        h('p', { class: 'bd-muted' }, `${plural(items.length, 'пост', 'поста')}${st.source ? ` · от „${st.source}“` : ''}`)),
      h('div', { class: 'bd-actions' }, actions));

    const summary = h('section', { class: 'bd-summary' },
      h('div', { class: 'bd-stats' },
        h('span', { class: 'bd-big' }, `${counts.done}`, h('small', null, ` / ${items.length} готови`)),
        stat('running', counts.running, 'пишат'),
        counts.uploading ? stat('uploading', counts.uploading, 'качват снимки') : null,
        stat('queued', counts.queued, 'чакат ред'),
        counts.error ? stat('error', counts.error, 'с грешка') : null,
        counts.idle ? stat('idle', counts.idle, 'непуснати') : null),
      bar,
      h('div', { class: 'bd-slots' }, slots));

    const grid = h('section', { class: 'bd-grid' }, sorted.map(card));
    const scroll = board.querySelector('.bd-wrap');
    const top = scroll ? scroll.scrollTop : 0;
    board.replaceChildren(h('div', { class: 'bd-wrap' }, head, summary, timeline, grid));
    board.querySelector('.bd-wrap').scrollTop = top;
    document.title = `${counts.done}/${items.length} · ${st.name} — LinkedIn агент`;
  }

  function stat(kind, n, label) {
    return h('span', { class: `bd-stat ${kind}` }, h('i'), h('b', null, String(n)), ` ${label}`);
  }

  function card(item) {
    const state = stateOf(item);
    const p = dateParts(item.date);
    const step = item.job ? STEP_FOR[Math.min(item.job.step, 6)] : state === 'done' ? 5 : -1;
    const steps = h('ol', { class: 'bd-steps', 'aria-label': 'Етапи' }, STEP_LABELS.map((label, i) => h('li', {
      class: i < step || state === 'done' ? 'on' : i === step && state === 'running' ? 'now' : '',
      title: label,
    }, h('span', null, label))));

    let line;
    if (state === 'running') line = `${item.job ? item.job.stage : 'Пише…'} · ${fmtTime(item.job ? item.job.elapsed : 0)}`;
    else if (state === 'uploading') line = `Качвам снимки ${B.uploads[item.post_id].done} от ${B.uploads[item.post_id].total}…`;
    else if (state === 'queued') line = `Чака свободен агент${item.queue_position ? ` · №${item.queue_position} на опашката` : ''}`;
    else if (state === 'done') line = `${item.status_label || 'Готов'} · ${plural(item.words, 'дума', 'думи')}${item.notes ? ` · ⚠ ${item.notes}` : ''}${item.seconds ? ` · ${fmtTime(item.seconds)}` : ''}`;
    else if (state === 'error' || state === 'cancelled') line = item.error || 'Не завърши.';
    else line = 'Не е пуснат.';

    const text = state === 'running' ? item.draft : item.snippet;
    const photos = (item.photos_in_post.length ? item.photos_in_post : item.photos_included).slice(0, 4);
    const buttons = [h('a', { class: 'btn ghost small', href: `#/p/${enc(item.post_id)}` }, 'Отвори')];
    if (state === 'running' && item.job) buttons.push(h('button', { type: 'button', class: 'btn ghost small', onclick: () => cancelJob(item.job.id) }, '■ Спри'));
    if (['idle', 'error', 'cancelled'].includes(state)) buttons.push(h('button', { type: 'button', class: 'btn primary small', onclick: () => startItems([item.post_id]) }, '▶ Пусни'));
    if (state === 'done') {
      buttons.push(h('button', { type: 'button', class: 'btn ghost small', onclick: (e) => copy(item, e.currentTarget) }, 'Копирай'));
      buttons.push(h('button', { type: 'button', class: `btn small ${item.published ? 'ghost' : 'primary'}`, onclick: () => publish(item) }, item.published ? '✓ Публикуван' : 'Публикувай'));
      buttons.push(h('button', { type: 'button', class: 'link-btn', onclick: () => rewrite(item) }, '↻ Наново'));
    }

    return h('article', { class: `bd-card ${state}${item.insufficient_brief ? ' thin' : ''}`, id: `bd-${item.post_id}` },
      h('div', { class: 'bd-day' },
        p ? [h('b', null, String(p.day)), h('span', null, p.month), h('small', null, item.weekday || '')] : [h('b', null, `№${item.index}`), h('small', null, 'без дата')]),
      h('div', { class: 'bd-body' },
        h('div', { class: 'bd-card-head' },
          h('h3', null, item.title),
          h('span', { class: `bd-chip ${state}` }, state === 'done' && item.published ? 'публикуван' : STATE_NAMES[state] || state)),
        steps,
        h('p', { class: `bd-line${state === 'error' ? ' err' : ''}` }, line),
        text ? h('p', { class: `bd-snippet${state === 'running' ? ' draft' : ''}` }, text) : h('p', { class: 'bd-snippet brief' }, item.brief),
        photos.length ? h('div', { class: 'bd-thumbs' }, photos.map((name) => h('img', { src: thumbUrl(item.post_id, name), alt: '', loading: 'lazy' })),
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
    if (!confirm(`Да напиша ли наново „${item.title}“? Сегашният текст се пази като предишен вариант.`)) return;
    startItems([item.post_id]);
  }

  async function stopAll() {
    if (!confirm('Да спра ли всички постове от графика, които пишат или чакат? Готовите остават.')) return;
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
      await navigator.clipboard.writeText(text);
      button.textContent = '✓ Копирано';
    } catch (e) {
      toast('Браузърът не позволи копиране — отворете поста и копирайте оттам.', 'err');
    }
  }

  async function publish(item) {
    const info = B.info || {};
    if (info.publish_mode === 'linkedin') {
      if (info.publish_problem) { toast(info.publish_problem, 'err'); return; }
      if (!confirm(`Публикувам „${item.title}“ в LinkedIn страницата на клъстера — веднага и публично. Продължаваме?`)) return;
    } else if (!confirm('Режим „чернова“: нищо няма да се изпрати към LinkedIn. Ще запиша как би изглеждала заявката (output/linkedin-request.json). Продължаваме?')) {
      return;
    }
    try {
      const r = await api('POST', `/api/posts/${enc(item.post_id)}/publish`, {});
      toast(r.mode === 'linkedin' ? 'Публикуван в LinkedIn ✓' : 'Заявката е записана (чернова) — нищо не е изпратено.');
    } catch (e) {
      toast(e.message, 'err');
    }
    poll();
  }

  // ------------------------------------------------------------------ запис в папката на човека

  async function connectFolder() {
    try {
      const handle = await window.showDirectoryPicker({ id: 'linkedin-agent', mode: 'readwrite' });
      B.boardFolder = { name: handle.name, handle, writable: true };
      rememberFolder(`batch:${B.batchId}`, handle);
      renderBoard();
      saveToFolder(false);
    } catch (e) {
      if (e && e.name !== 'AbortError') toast('Браузърът не даде достъп до папката.', 'err');
    }
  }

  async function reconnectFolder() {
    const folder = B.boardFolder;
    if (!folder.handle) { toast('Тази папка е само за четене в този браузър — ползвайте „⬇ ZIP“.'); return; }
    try {
      const state = await folder.handle.requestPermission({ mode: 'readwrite' });
      folder.writable = state === 'granted';
      folder.needsPermission = !folder.writable;
      renderBoard();
      if (folder.writable) saveToFolder(false);
    } catch (e) {
      toast('Браузърът не даде достъп до папката.', 'err');
    }
  }

  function signature(items) {
    return items.filter((i) => i.state === 'done').map((i) => `${i.post_id}:${i.words}:${i.edited}:${i.snippet.length}`).join('|');
  }

  async function autoSave() {
    const folder = B.boardFolder;
    if (!B.autoSave || !folder || !folder.writable || !B.status) return;
    const sig = signature(B.status.items);
    if (!sig || B.savedSig[B.batchId] === sig) return;
    B.savedSig[B.batchId] = sig;
    await saveToFolder(false);
  }

  async function saveToFolder(force) {
    const folder = B.boardFolder;
    if (!folder || !folder.writable) return;
    let full;
    try {
      full = await api('GET', `/api/batches/${enc(B.batchId)}?full=1`);
    } catch (e) {
      toast(e.message, 'err');
      return;
    }
    let dir;
    try {
      dir = await folder.handle.getDirectoryHandle(OUT_DIR, { create: true });
    } catch (e) {
      folder.writable = false;
      folder.needsPermission = true;
      renderBoard();
      toast('Нямам право да пиша в папката — натиснете „Свържи“.', 'err');
      return;
    }
    let written = 0;
    const conflicts = [];
    for (const item of full.items) {
      if (!item.text) continue;
      const key = `bw:${B.batchId}:${item.post_id}`;
      const last = storage(key);
      if (last === item.text && !force) continue;
      let existing = null;
      try {
        const fh = await dir.getFileHandle(item.file_name);
        existing = (await (await fh.getFile()).text()).trim();
      } catch (e) { /* още няма такъв файл */ }
      const editedByHuman = existing !== null && existing !== item.text.trim() && existing !== (last || '').trim();
      if (editedByHuman) {
        conflicts.push(item.file_name);
        continue;
      }
      try {
        const fh = await dir.getFileHandle(item.file_name, { create: true });
        const w = await fh.createWritable();
        await w.write(`${item.text}\n`);
        await w.close();
        storage(key, item.text);
        written += 1;
      } catch (e) {
        toast(`${item.file_name}: ${e.message}`, 'err');
      }
    }
    try {
      const res = await fetch(`/api/batches/${enc(B.batchId)}/result.csv`, { headers: HEADERS });
      const fh = await dir.getFileHandle('график-резултат.csv', { create: true });
      const w = await fh.createWritable();
      await w.write(await res.blob());
      await w.close();
    } catch (e) { /* обобщението е удобство, не е критично */ }
    if (written) toast(`Записах ${plural(written, 'пост', 'поста')} в „${folder.name}/${OUT_DIR}“.`);
    if (conflicts.length) {
      toast(`${plural(conflicts.length, 'файл е редактиран', 'файла са редактирани')} в папката — не ги презаписах. „↺ Прочети редакциите“ ги връща в агента.`, 'err');
    }
  }

  async function readEdits() {
    const folder = B.boardFolder;
    if (!folder || !folder.writable) return;
    let dir;
    let full;
    try {
      dir = await folder.handle.getDirectoryHandle(OUT_DIR);
      full = await api('GET', `/api/batches/${enc(B.batchId)}?full=1`);
    } catch (e) {
      toast(`В „${folder.name}“ още няма папка „${OUT_DIR}“.`, 'err');
      return;
    }
    let changed = 0;
    for (const item of full.items) {
      if (!item.text) continue;
      let text;
      try {
        const fh = await dir.getFileHandle(item.file_name);
        text = (await (await fh.getFile()).text()).trim();
      } catch (e) {
        continue;
      }
      if (!text || text === item.text.trim()) continue;
      try {
        await api('PUT', `/api/posts/${enc(item.post_id)}/text`, { text });
        storage(`bw:${B.batchId}:${item.post_id}`, text);
        changed += 1;
      } catch (e) {
        toast(`${item.file_name}: ${e.message}`, 'err');
      }
    }
    toast(changed ? `Прочетох ${plural(changed, 'редакция', 'редакции')} от папката.` : 'Няма нови редакции в папката.');
    poll();
  }

  // ------------------------------------------------------------------ история вляво

  async function loadBatches() {
    try {
      B.batches = (await api('GET', '/api/batches')).batches;
    } catch (e) {
      return;
    }
    if (!B.batches.length) {
      batchList.replaceChildren();
      return;
    }
    batchList.replaceChildren(
      h('div', { class: 'chat-group' }, 'Графици'),
      ...B.batches.slice(0, 8).map((b) => {
        const bar = h('span', { class: 'bd-mini' }, h('span'));
        bar.firstChild.style.width = `${b.count ? (b.done / b.count) * 100 : 0}%`;
        return h('div', { class: `chat-row${b.id === B.batchId && B.active ? ' current' : ''}` },
          h('button', { type: 'button', class: 'chat-item', title: b.name, onclick: () => { location.hash = `#/b/${b.id}`; } },
            h('span', { class: `s ${b.active ? 'running' : b.done === b.count ? 'ready' : ''}` }),
            h('span', { class: 't' }, b.name),
            h('small', { class: 'bd-count' }, `${b.done}/${b.count}`),
            bar));
      }));
  }

  // ------------------------------------------------------------------ навигация

  async function loadInfo() {
    try { B.info = await api('GET', '/api/info'); } catch (e) { /* app.js показва грешката */ }
  }

  function route() {
    const hash = location.hash;
    const match = hash.match(/^#\/b\/([a-f0-9]{12})$/);
    const active = hash === '#/grafik' || !!match;
    B.active = active;
    document.body.classList.toggle('board-mode', active);
    board.hidden = !active;
    if (!active) {
      clearTimeout(B.timer);
      B.batchId = null;
      loadBatches();
      return;
    }
    document.getElementById('scrim').hidden = true;
    document.getElementById('sidebar').classList.remove('open');
    if (match) {
      if (match[1] !== B.batchId) openBoard(match[1]);
    } else {
      clearTimeout(B.timer);
      B.batchId = null;
      document.title = 'График — LinkedIn агент';
      renderImport();
    }
    loadBatches();
  }

  function init() {
    $('grafik-btn').addEventListener('click', () => { location.hash = '#/grafik'; });
    $('new-btn').addEventListener('click', () => { if (B.active) location.hash = '#/'; });
    $('bd-folder-input').addEventListener('change', async (e) => {
      const list = Array.from(e.target.files || []);
      e.target.value = '';
      if (!list.length) return;
      const { files, dirs, root } = indexFileList(list);
      B.folder = { name: root || 'папка', writable: false, files, dirs };
      await afterFolder();
      renderImport();
    });
    window.addEventListener('hashchange', route);
    loadInfo().then(route);
    setInterval(() => { if (!document.hidden) loadBatches(); }, 5000);
    setInterval(() => { if (B.active) loadInfo(); }, 15000);
  }

  init();
})();
