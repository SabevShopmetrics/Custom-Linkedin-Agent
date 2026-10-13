'use strict';
/*
 * Уеб интерфейс на LinkedIn агента — чат: история вляво, разговор в средата, поле за писане
 * лепнато отдолу, „+" за снимки/ZIP. Без библиотеки и без стъпка за компилиране.
 * Сървърът е src/web/server.py; състоянието живее там (папката на поста), тук е само изгледът.
 */
(() => {
  const enc = encodeURIComponent;
  const HEADERS = { 'X-Requested-With': 'linkedin-agent' };
  const LIMIT_CHARS = 3000; // таван на LinkedIn за текст на пост
  const QUICK = ['По-кратко', 'По-дълго', 'По-официално', 'По-топло и лично'];
  const EXAMPLES = [
    ['Събитие', 'Вчера проведохме среща на членовете на клъстера във Варна — 30 участници от 15 фирми обсъдиха общите проекти за 2027.'],
    ['Ученици и стажове', 'Ученици от варненски гимназии посетиха три фирми от клъстера и видяха реални проекти отблизо.'],
    ['Покана', 'Каним членовете на клъстера на уъркшоп за ИИ в бизнеса на 20 ноември от 10:00 ч. — записване до 15 ноември.'],
  ];
  const STEP_LABELS = ['Чета брифа', 'Пиша чернова', 'Проверявам', 'Подобрявам', 'Последни поправки'];
  const STEP_FOR = [0, 0, 1, 2, 3, 3, 4]; // стъпка на конвейера (0–6) → етикет

  const S = {
    info: null,
    posts: [],
    postId: null,
    post: null,
    mode: 'fokus',
    jobId: null,
    job: null,
    draft: null,
    editing: false,
    editText: '',
    view: 'desktop',
    pollTimer: null,
    finalTimer: null,
    uploads: [],
    events: [],
    trayNotes: [],
    pendingEl: null,
    lastCount: -1,
  };

  const el = {};
  [
    'sidebar', 'side-close', 'side-open', 'scrim', 'new-btn', 'chat-list', 'busy-chip', 'busy-text', 'chat-title',
    'photos-btn', 'photos-btn-text', 'config-banner', 'thread', 'messages', 'composer', 'tray', 'prompt', 'modes',
    'file-input', 'file-input-2', 'send', 'composer-hint', 'photos-modal', 'photo-count', 'photo-help', 'star-help',
    'photos', 'include-all', 'exclude-all', 'photos-close', 'photos-done', 'drop-overlay', 'lightbox', 'lightbox-img',
    'toasts',
  ].forEach((id) => { el[id.replace(/-([a-z0-9])/g, (_, c) => c.toUpperCase())] = document.getElementById(id); });

  // ------------------------------------------------------------------ помощници

  async function api(method, url, body) {
    const opts = { method, headers: { ...HEADERS } };
    if (body !== undefined) {
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

  function svg(path) {
    return h('span', { html: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${path}"/></svg>` }).firstChild;
  }

  function toast(message, kind) {
    const node = h('div', { class: `toast${kind === 'err' ? ' err' : ''}` }, message);
    el.toasts.appendChild(node);
    setTimeout(() => node.remove(), kind === 'err' ? 7000 : 3200);
  }

  function esc(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function linkify(html) {
    return html.replace(/(https?:\/\/[^\s<]+[^\s<.,;:!?)\]]|www\.[^\s<]+[^\s<.,;:!?)\]])/g, '<span class="link">$1</span>');
  }

  const fmtTime = (s) => `${Math.floor(Math.max(0, s) / 60)}:${String(Math.round(Math.max(0, s)) % 60).padStart(2, '0')}`;
  const words = (text) => (text.trim().match(/\S+/g) || []).length;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const photoUrlFor = (postId, name, size, bytes) => `/api/posts/${enc(postId)}/photos/${enc(name)}?size=${size}&v=${bytes || 0}`;
  const photoUrl = (name, size, bytes) => photoUrlFor(S.postId, name, size, bytes);
  const jobRunningHere = () => !!(S.job && S.job.state === 'running' && S.job.post_id === S.postId);
  const runningJobs = () => (S.info && S.info.jobs) || [];
  // Няколко поста пишат наведнъж, но до тавана на сървъра (WEB_MAX_JOBS).
  const atCapacity = () => !jobRunningHere()
    && runningJobs().filter((j) => j.post_id !== S.postId).length >= ((S.info && S.info.max_jobs) || 3);
  const SEND_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m0 0-6 6m6-6 6 6"/></svg>';
  const VDOTS_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="5" r="1.6" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="12" cy="19" r="1.6" fill="currentColor" stroke="none"/></svg>';
  const STOP_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor" stroke="none"/></svg>';

  function storage(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch (e) { /* частен режим — без памет между презарежданията */ }
    return null;
  }

  // ------------------------------------------------------------------ навигация

  function setHash() {
    const target = S.postId ? `#/p/${enc(S.postId)}` : '#/';
    if (location.hash !== target) location.hash = target;
  }

  function route() {
    if (/^#\/(grafik|b\/)/.test(location.hash)) return; // график и табло — board.js
    const match = location.hash.match(/^#\/p\/(.+)$/);
    const id = match ? decodeURIComponent(match[1]) : null;
    if (id && id === S.postId && S.post) return;
    if (id) openPost(id);
    else if (S.postId || !S.started) newChat();
    S.started = true;
    closeSidebar();
  }

  function resetView() {
    stopPolling();
    clearInterval(S.finalTimer);
    S.post = null;
    S.job = null;
    S.jobId = null;
    S.draft = null;
    S.editing = false;
    S.pendingEl = null;
    S.trayNotes = [];
    S.lastCount = -1;
    S.recall = null;
    S.hist = null;
    closeRowMenu();
  }

  function draftKey() {
    return `draft:${S.postId || 'new'}`;
  }

  function newChat() {
    resetView();
    S.postId = null;
    S.mode = storage('newMode') || (S.info && S.info.default_mode) || 'fokus';
    el.prompt.value = storage(draftKey()) || '';
    setHash();
    renderAll();
    autosize();
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
    if (S.post.job && S.post.job.state === 'running') startJob(S.post.job, false);
    renderAll();
    autosize();
    scrollToBottom();
    watchFinalCheck();
  }

  async function refreshPost() {
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

  // ------------------------------------------------------------------ история вляво

  async function loadPosts() {
    try {
      S.posts = (await api('GET', '/api/posts')).posts;
    } catch (e) {
      return;
    }
    renderSidebar();
  }

  function dayGroup(id) {
    const m = id.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return 'По-рано';
    const day = new Date(+m[1], +m[2] - 1, +m[3]);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const diff = Math.round((today - day) / 86400000);
    if (diff <= 0) return 'Днес';
    if (diff === 1) return 'Вчера';
    if (diff < 7) return 'Последните 7 дни';
    if (diff < 31) return 'Последните 30 дни';
    return 'По-рано';
  }

  function renderSidebar() {
    if (!S.posts.length) {
      el.chatList.replaceChildren(h('div', { class: 'chat-empty' }, 'Още няма постове. Напишете първия отдясно.'));
      return;
    }
    const nodes = [];
    let group = null;
    const running = new Set(runningJobs().map((j) => j.post_id));
    for (const p of S.posts) {
      const g = dayGroup(p.id);
      if (g !== group) {
        group = g;
        nodes.push(h('div', { class: 'chat-group' }, g));
      }
      const busy = running.has(p.id);
      const dot = busy ? 'running' : p.published ? 'published' : p.status || '';
      const label = busy ? 'агентът пише' : p.published ? 'публикуван' : p.status === 'ready' ? 'готов' : p.status ? 'с бележки' : 'чернова';
      if (S.renaming === p.id) {
        const input = h('input', { type: 'text', class: 'chat-rename', value: p.title, 'aria-label': 'Ново име' });
        let done = false;
        const finish = (save) => {
          if (done) return;
          done = true;
          S.renaming = null;
          if (save && input.value.trim() !== p.title) renamePost(p, input.value);
          else renderSidebar();
        };
        input.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') { e.preventDefault(); finish(true); }
          if (e.key === 'Escape') { e.preventDefault(); finish(false); }
        });
        input.addEventListener('blur', () => finish(true));
        nodes.push(h('div', { class: 'chat-row current' }, h('span', { class: `s ${dot}` }), input));
        setTimeout(() => { input.focus(); input.select(); }, 0);
        continue;
      }
      nodes.push(h('div', { class: `chat-row${p.id === S.postId ? ' current' : ''}` },
        h('button', {
          type: 'button',
          class: 'chat-item',
          title: `${p.title} — ${label}`,
          onclick: () => { location.hash = `#/p/${enc(p.id)}`; },
        }, h('span', { class: `s ${dot}` }), h('span', { class: 't' }, p.title)),
        h('button', {
          type: 'button',
          class: 'chat-more',
          title: 'Още',
          'aria-label': `Още за „${p.title}“`,
          'aria-haspopup': 'menu',
          html: VDOTS_ICON,
          onclick: (e) => { e.stopPropagation(); openRowMenu(p, e.currentTarget); },
        })));
    }
    el.chatList.replaceChildren(...nodes);
  }

  // Меню „⋮" на ред от историята: Преименувай / Изтрий.
  let rowMenu = null;

  function closeRowMenu() {
    if (rowMenu) {
      rowMenu.remove();
      rowMenu = null;
    }
  }

  function openRowMenu(p, anchor) {
    const reopen = rowMenu && rowMenu.dataset.post === p.id;
    closeRowMenu();
    if (reopen) return;
    rowMenu = h('div', { class: 'row-menu', role: 'menu', 'data-post': p.id },
      h('button', {
        type: 'button', role: 'menuitem',
        html: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16v4Zm10-14 4 4"/></svg><span>Преименувай</span>',
        onclick: () => { closeRowMenu(); S.renaming = p.id; renderSidebar(); },
      }),
      h('button', {
        type: 'button', role: 'menuitem', class: 'danger',
        html: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg><span>Изтрий</span>',
        onclick: () => { closeRowMenu(); deletePost(p); },
      }));
    document.body.appendChild(rowMenu);
    const box = anchor.getBoundingClientRect();
    const menu = rowMenu.getBoundingClientRect();
    const top = box.bottom + menu.height + 8 > window.innerHeight ? box.top - menu.height - 4 : box.bottom + 4;
    rowMenu.style.top = `${Math.max(8, top)}px`;
    rowMenu.style.left = `${Math.max(8, Math.min(box.right - menu.width, window.innerWidth - menu.width - 8))}px`;
    rowMenu.querySelector('button').focus();
  }

  async function renamePost(p, title) {
    try {
      await api('PUT', `/api/posts/${enc(p.id)}`, { title });
    } catch (e) {
      toast(e.message, 'err');
    }
    await loadPosts();
    if (p.id === S.postId) await refreshPost();
  }

  async function deletePost(p) {
    const busy = runningJobs().some((j) => j.post_id === p.id);
    const question = busy
      ? `Агентът още пише „${p.title}“. Да го спра и да изтрия поста?`
      : `Да изтрия ли „${p.title}“?`;
    if (!confirm(`${question}\n\nПостът отива в кошчето (папка posts/_kosh) — оттам може да се върне ръчно.`)) return;
    try {
      await api('DELETE', `/api/posts/${enc(p.id)}`);
    } catch (e) {
      toast(e.message, 'err');
      return;
    }
    storage(`draft:${p.id}`, null);
    toast('Постът е преместен в кошчето.');
    if (p.id === S.postId) location.hash = '#/';
    await Promise.all([loadPosts(), loadInfo()]);
  }

  function openSidebar() {
    el.sidebar.classList.add('open');
    el.scrim.hidden = false;
  }

  function closeSidebar() {
    el.sidebar.classList.remove('open');
    el.scrim.hidden = true;
  }

  // ------------------------------------------------------------------ нишката

  function scrollToBottom(smooth) {
    requestAnimationFrame(() => {
      el.thread.scrollTo({ top: el.thread.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
    });
  }

  function hero() {
    return h('div', { class: 'hero' },
      h('span', { class: 'brand-mark', 'aria-hidden': 'true' }, 'in'),
      h('h2', null, 'Какво се случи?'),
      h('p', null, 'Опишете повода с едно-две изречения и добавете снимки или ZIP с „+“. Агентът пише поста и показва точно как ще изглежда в LinkedIn.'),
      h('div', { class: 'examples' }, EXAMPLES.map(([title, text]) => h('button', {
        type: 'button',
        class: 'example',
        onclick: () => {
          el.prompt.value = text;
          autosize();
          el.prompt.focus();
          storage(draftKey(), text);
          renderComposer();
        },
      }, h('b', null, title), text))));
  }

  function renderThread() {
    const messages = (S.post && S.post.messages) || [];
    const running = jobRunningHere();
    if (!S.postId || (!messages.length && !running)) {
      el.messages.replaceChildren(hero());
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
    el.messages.replaceChildren(...nodes);
    const count = nodes.length;
    if (count !== S.lastCount) scrollToBottom(S.lastCount > 0);
    S.lastCount = count;
  }

  function userMessage(m) {
    const names = Array.isArray(m.photos) ? m.photos : [];
    const meta = [];
    if (typeof m.photos === 'number' && m.photos) meta.push(`📎 ${plural(m.photos, 'снимка', 'снимки')}`);
    if (m.mode && S.info) {
      const mode = S.info.modes.find((x) => x.key === m.mode);
      if (mode && m.mode !== S.info.default_mode) meta.push(mode.label);
    }
    // Прикачените към съобщението снимки — в самото съобщение, както в чат.
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
    return h('div', { class: 'msg agent' }, h('div', { class: 'avatar', 'aria-hidden': 'true' }, 'in'), h('div', { class: 'msg-body' }, ...content));
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

  // Готовият пост се показва като „преглед“ — същата идея като output/preview.html от терминала:
  // цветна лента със статуса и „Копирай“ отгоре, картата върху фона на лентата в LinkedIn,
  // бележките отдолу. Тук панелът живее в чата и има и действията за следващата стъпка.

  function panel(tone, statusParts, stage, ...rest) {
    return h('section', { class: `preview ${tone}` },
      h('header', { class: 'preview-status' }, statusParts),
      h('div', { class: `preview-stage${S.view === 'mobile' ? ' mobile' : ''}` }, stage),
      ...rest);
  }

  function oldPost(m, version) {
    // По-стар вариант: свит, само текстът — за сравнение и копиране, без действия.
    const node = h('details', { class: 'old-version' },
      h('summary', null, h('span', null, version ? `Вариант ${version}` : 'Предишен вариант'),
        h('small', null, (m.text || '').replace(/\s+/g, ' ').slice(0, 90) + ((m.text || '').length > 90 ? '…' : ''))),
      h('div', { class: 'old-body' },
        h('div', { class: 'old-text' }, m.text || ''),
        h('button', { type: 'button', class: 'btn ghost small', onclick: (e) => copyText(m.text || '', e.currentTarget) }, 'Копирай този вариант')));
    return node;
  }

  function livePost(version) {
    const post = S.post.post;
    const names = S.post.in_post || [];
    const included = S.post.photos.filter((p) => p.included).length;
    const busy = jobRunningHere() || atCapacity();
    const text = S.editing ? S.editText : post.text;

    const tone = post.insufficient_brief ? 'thin' : post.status === 'ready' ? 'ready' : 'notes';
    const icon = tone === 'ready' ? '✓' : '⚠';
    const label = post.insufficient_brief ? 'Брифът не стига за истински пост' : post.status_label;
    const extras = [];
    if (post.notes.length && !post.insufficient_brief) extras.push(plural(post.notes.length, 'бележка', 'бележки'));
    if (post.final_check === 'pending') extras.push('последна проверка тече…');
    if (post.published) extras.push(post.edited ? 'редактиран · публикуван' : 'публикуван');
    else if (post.edited) extras.push('редактиран от вас');
    const status = [
      h('span', { class: 'preview-icon', 'aria-hidden': 'true' }, icon),
      h('span', { class: 'preview-title' }, version ? h('small', null, `Вариант ${version} · `) : null, h('b', null, label),
        extras.length ? h('small', null, ` · ${extras.join(' · ')}`) : null),
      h('span', { class: 'grow' }),
      h('button', { type: 'button', class: 'btn copy small', onclick: (e) => copyText(text, e.currentTarget) },
        svg('M8 8h12v12H8zM4 16V4h12'), h('span', null, 'Копирай текста')),
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

    const rest = [];
    if (post.insufficient_brief) {
      rest.push(h('div', { class: 'preview-notes thin' }, h('b', null, 'Какво да направите'),
        h('p', null, 'Напишете отдолу още подробности — число, имена, резултат. Ще ги добавя към брифа и ще пиша наново.')));
    }
    if (post.notes.length) {
      rest.push(h('div', { class: 'preview-notes' }, h('b', null, '⚠ Проверете преди публикуване:'),
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
      const node = h('details', { class: 'preview-logs done' },
        h('summary', null, h('span', null, title), h('small', null, ' — подробности')),
        timelineView(run.events, run.seconds, false, run.kind));
      rest.push(node);
    }

    const viewSeg = h('div', { class: 'seg small', role: 'radiogroup', 'aria-label': 'Изглед' },
      [['desktop', 'Компютър'], ['mobile', 'Телефон']].map(([key, name]) => h('button', {
        type: 'button', role: 'radio', 'aria-checked': String(S.view === key),
        onclick: () => { S.view = key; storage('view', key); renderThread(); },
      }, name)));
    rest.push(h('footer', { class: 'preview-tools' },
      h('button', { type: 'button', class: 'btn ghost small', onclick: S.editing ? saveEdit : startEdit }, S.editing ? 'Запази' : '✎ Редактирай'),
      h('button', { type: 'button', class: 'btn ghost small', onclick: openPhotos },
        included ? `🖼 Снимки · ${names.length} от ${included}` : '🖼 Добави снимки'),
      h('button', { type: 'button', class: 'btn ghost small', onclick: markPublished }, post.published ? '✓ Публикуван' : '✓ Публикувах го'),
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

  // ------------------------------------------------------------------ „Ход на работата" (подробни логове)
  //
  // Събитията идват от работния процес (src/web/worker.py): стъпки, редове от run.log и всяко
  // извикване към модела с време, повторни опити и таймаути. Целта е да се вижда КЪДЕ отива
  // времето — коя стъпка, кой модел, с колко снимки, дали чака отговор или опитва пак.

  const STEP_NAMES = {
    '[1]': 'Чете брифа и прави план',
    '[1.5]': 'Подрежда фактите',
    '[2]/[3]': 'Пише чернова и избира снимки',
    '[2]': 'Пише чернова',
    '[3]': 'Избира снимки',
    '[4]': 'Проверява факти, тон и правила',
    '[5]': 'Пише подобрен вариант',
    '[6]': 'Обединява двата варианта',
    '[6б]': 'Последни поправки по правилата',
    '[Р]': 'Редактира поста',
  };
  const ROLE_NAMES = {
    planner: 'Планер', writer: 'Писател', media: 'Избор на снимки', critic: 'Критик',
    consolidator: 'Консолидатор', judge: 'Съдия',
  };
  const SLOW_CALL = 120; // сек — над това извикването е „бавно"
  const VERY_SLOW_CALL = 300;
  const SLOW_STEP = 180;
  const shortModel = (model) => (model || '?').split('/').pop();

  const FIRST_STEP = { repick: 'Избира снимки', revise: 'Редактира поста', generate: 'Подготовка' };

  function summarizeRun(events, now, running, kind) {
    const steps = [];
    const calls = {};
    let cur = null;
    const open = (tag, t) => {
      // Работа преди първия етикет (напр. „избери наново" вика модела, преди да логне „[3]")
      // и стъпката със същото име са едно и също — не две реда „Подготовка" + „Избира снимки".
      if (cur && !cur.tag && tag && STEP_NAMES[tag] === cur.name) {
        cur.tag = tag;
        return;
      }
      if (cur && cur.end === null) cur.end = t;
      cur = { tag, name: STEP_NAMES[tag] || tag || FIRST_STEP[kind] || 'Подготовка', start: t, end: null, calls: [], lines: [] };
      steps.push(cur);
    };
    for (const e of events || []) {
      if (e.type === 'step') {
        if (e.tag === '[7]') { // фоновата финална проверка — не е част от чакането
          if (cur && cur.end === null) cur.end = e.t;
          cur = null;
          continue;
        }
        open(e.tag, e.t);
      } else if (e.type === 'call') {
        if (!cur) open('', e.t);
        const call = { ...e, end: null, http: 0, fails: [] };
        calls[e.id] = call;
        cur.calls.push(call);
      } else if (e.type === 'call_end') {
        const call = calls[e.id];
        if (call) Object.assign(call, { end: e.t, dur: e.dur, ok: e.ok, err: e.err, json_retries: e.json_retries || 0, fallback: e.fallback });
      } else if (e.type === 'http') {
        const call = calls[e.id];
        if (call) { call.http = e.n; call.httpModel = e.model; }
      } else if (e.type === 'http_fail') {
        const call = calls[e.id];
        if (call) call.fails.push(e);
      } else if (e.type === 'line' && cur && e.tag === cur.tag) {
        cur.lines.push(e);
      }
    }
    if (cur && cur.end === null && !running) cur.end = now;
    let slowest = null;
    for (const step of steps) {
      for (const call of step.calls) {
        const dur = call.end === null ? now - call.t : call.dur;
        if (!slowest || dur > slowest.dur) {
          slowest = {
            dur,
            inflight: call.end === null,
            label: `${ROLE_NAMES[call.role] || call.role}${call.images ? ` (${plural(call.images, 'снимка', 'снимки')})` : ''}`,
          };
        }
      }
    }
    return { steps, slowest };
  }

  function timelineView(events, now, running, kind) {
    const { steps, slowest } = summarizeRun(events, now, running, kind);
    if (!steps.length) return h('div', { class: 'tl-empty' }, 'Стартирам агента…');
    const rows = steps.map((step) => {
      const live = running && step.end === null;
      const dur = (step.end === null ? now : step.end) - step.start;
      const slow = dur > SLOW_STEP;
      const callRows = step.calls.map((call) => {
        const inflight = call.end === null;
        const cdur = inflight ? now - call.t : call.dur;
        const notes = [];
        if (call.images) notes.push(plural(call.images, 'снимка', 'снимки'));
        if (call.http > 1) notes.push(`опит ${call.http}`);
        if (call.json_retries) notes.push(`поправка на JSON ×${call.json_retries}`);
        if (call.fallback) notes.push(`резервен модел ${shortModel(call.httpModel)}`);
        const level = cdur > VERY_SLOW_CALL ? ' very-slow' : cdur > SLOW_CALL ? ' slow' : '';
        return h('div', { class: `tl-call${level}${inflight && running ? ' live' : ''}${call.ok === false ? ' failed' : ''}` },
          h('span', { class: 'tl-call-name' }, `${ROLE_NAMES[call.role] || call.role} · ${shortModel(call.model)}`),
          notes.length ? h('span', { class: 'tl-call-notes' }, notes.join(' · ')) : null,
          h('span', { class: 'tl-time' }, inflight && running ? `чака отговор ${fmtTime(cdur)}` : fmtTime(cdur)),
          call.fails.length ? h('div', { class: 'tl-fails' }, call.fails.map((f) =>
            h('div', null, `⚠ ${shortModel(f.model)}: ${f.err} след ${fmtTime(f.dur)} — опитвам пак`))) : null,
          call.ok === false ? h('div', { class: 'tl-fails' }, `✗ ${call.err || 'неуспех'}`) : null);
      });
      const details = step.lines.slice(1); // първият ред е заглавието на стъпката — всички останали, със скрол
      return h('li', { class: `tl-step${live ? ' live' : ''}${slow ? ' slow' : ''}` },
        h('div', { class: 'tl-head' },
          live ? h('span', { class: 'spinner small', 'aria-hidden': 'true' }) : h('span', { class: 'tl-ok' }, '✓'),
          h('span', { class: 'tl-name' }, step.name),
          slow ? h('span', { class: 'tl-badge' }, 'бавно') : null,
          h('span', { class: 'tl-time' }, fmtTime(dur))),
        callRows.length ? h('div', { class: 'tl-calls' }, callRows) : null,
        details.length ? h('ul', { class: 'tl-lines' },
          details.map((line) => h('li', { class: line.level === 'warn' ? 'warn' : null }, line.text))) : null);
    });
    const summary = slowest && slowest.dur >= 20
      ? h('div', { class: `tl-summary${slowest.dur > SLOW_CALL ? ' slow' : ''}` },
        `${slowest.inflight && running ? 'Сега се чака' : 'Най-дълго'}: ${slowest.label} — ${fmtTime(slowest.dur)}`)
      : null;
    return h('div', { class: 'timeline' }, summary, h('ol', { class: 'tl-steps' }, rows));
  }

  function logsSection(title, events, now, running, hint, kind) {
    const open = storage('logs') !== 'closed';
    const node = h('details', { class: 'preview-logs', open },
      h('summary', null, h('span', null, title), hint ? h('small', null, hint) : null),
      timelineView(events, now, running, kind));
    node.addEventListener('toggle', () => storage('logs', node.open ? null : 'closed'));
    return node;
  }

  // ------------------------------------------------------------------ ход на работата

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
    // Най-долу → следва новите редове; качил се е нагоре да чете → оставаме там.
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
        scrollToBottom(true);
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
    if (!post) {
      ok = await generate(text, text);
    } else if (post.insufficient_brief) {
      // Тънък бриф: добавката отива към брифа и постът се пише наново (както в терминала).
      ok = await generate(`${S.post.brief}\n\n${text}`, text);
    } else {
      ok = await revise(text);
    }
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
    el.modes.hidden = !!post && !post.insufficient_brief;
    renderModes();
  }

  function updateSendState() {
    el.send.disabled = jobRunningHere()
      ? !!(S.job && S.job.cancel_requested)
      : atCapacity() || S.uploading > 0 || !el.prompt.value.trim();
  }

  function renderModes() {
    const modes = (S.info && S.info.modes) || [];
    el.modes.replaceChildren(...modes.map((m) => h('button', {
      type: 'button', role: 'radio', 'aria-checked': String(m.key === S.mode), title: `${m.label} — ${m.hint}`,
      onclick: () => setMode(m.key),
    }, m.label)));
  }

  async function setMode(key) {
    S.mode = key;
    renderModes();
    if (!S.postId) {
      storage('newMode', key);
      return;
    }
    try { await api('PUT', `/api/posts/${enc(S.postId)}`, { mode: key }); } catch (e) { toast(e.message, 'err'); }
  }

  // ------------------------------------------------------------------ снимки: качване

  async function collectFiles(dataTransfer) {
    // Пусната папка: обхождаме я (Chrome/Edge/Firefox) — иначе браузърът дава само името ѝ.
    const items = Array.from(dataTransfer.items || []);
    const entries = items.map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null)).filter(Boolean);
    if (!entries.length || !entries.some((e) => e.isDirectory)) return Array.from(dataTransfer.files || []);
    const files = [];
    const walk = async (entry) => {
      if (entry.isFile) {
        files.push(await new Promise((ok, fail) => entry.file(ok, fail)));
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

  async function handleFiles(fileList) {
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
    S.uploading = (S.uploading || 0) + batch.length;
    renderTray();
    renderComposer();
    const postId = S.postId;
    const before = S.post && S.post.post ? S.post.in_post.length : null;
    let added = 0;
    for (const u of batch) {
      try {
        const report = await uploadOne(postId, u);
        added += report.added.length + report.parked.length;
        collectNotes(report);
      } catch (e) {
        S.trayNotes.push(`${u.file.name} — ${e.message}`);
      }
      S.uploads = S.uploads.filter((x) => x !== u);
      S.uploading -= 1;
      if (S.postId === postId) await refreshPost();
    }
    renderTray();
    renderComposer();
    if (added && before !== null && S.postId === postId) {
      toast('Снимките са добавени. „↻ Избери снимките наново“ под поста — ако искате агентът да ги прецени.');
    }
  }

  function collectNotes(report) {
    if (report.parked.length) {
      const max = (S.info && S.info.max_agent_photos) || 12;
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
      : [h('div', { class: 'grid-empty' }, 'Добавете снимки или ZIP с бутона отдолу или ги пуснете тук.')]));
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
    const flag = p.possible_minor ? 'дете на снимката?' : p.mismatch ? 'не пасва?' : null;
    if (flag) {
      tile.appendChild(h('span', {
        class: 'flag', html: `⚠<span class="long"> ${esc(flag)}</span>`,
        title: p.mismatch || 'Агентът смята, че може да има непълнолетни — нужно е съгласие на родител.',
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
      toast('Запазено.');
    } catch (e) {
      toast(e.message, 'err');
    }
  }

  async function copyText(text, button) {
    text = (text || '').trim();
    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        ok = true;
      }
    } catch (e) { ok = false; }
    if (!ok) {
      const ta = h('textarea', { readonly: true });
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove();
    }
    if (!ok) {
      toast('Не успях да копирам автоматично — маркирайте текста ръчно.', 'err');
      return;
    }
    if (button) {
      const original = button.innerHTML;
      button.classList.add('done');
      button.textContent = '✓ Копирано';
      setTimeout(() => { button.classList.remove('done'); button.innerHTML = original; }, 1500);
    } else {
      toast('Копирано.');
    }
  }

  async function markPublished() {
    if (!confirm('Отбелязваме поста като публикуван с текущия текст? (помага да мерим колко се поправя преди публикуване)')) return;
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

  function openLightbox(src) {
    el.lightboxImg.src = src;
    el.lightbox.hidden = false;
  }

  // ------------------------------------------------------------------ общо

  async function loadInfo() {
    try {
      S.info = await api('GET', '/api/info');
    } catch (e) {
      return;
    }
    el.configBanner.hidden = !S.info.config_error;
    if (S.info.config_error) el.configBanner.innerHTML = `<b>Агентът не е настроен</b>${esc(S.info.config_error)}`;
    const jobs = runningJobs();
    const others = jobs.filter((j) => j.post_id !== S.postId);
    el.busyChip.hidden = !others.length;
    if (others.length) {
      el.busyText.textContent = others.length === 1
        ? `Пише още 1 пост · ${fmtTime(others[0].elapsed)}`
        : `Пише още ${others.length} поста`;
    }
    const mine = jobs.find((j) => j.post_id === S.postId);
    if (mine && !S.pollTimer) startJob(mine, false);

    // Пост, който е приключил във фонов режим (докато гледате друг) — кратко известие.
    const ids = new Set(jobs.map((j) => j.post_id));
    const before = S.prevRunning;
    S.prevRunning = ids;
    if (before) {
      for (const id of before) {
        if (!ids.has(id) && id !== S.postId) {
          const p = S.posts.find((x) => x.id === id);
          toast(`Агентът приключи: ${p ? p.title : id}`);
        }
      }
    }
    const changed = !before || before.size !== ids.size || [...ids].some((id) => !before.has(id));
    if (changed && before) loadPosts();
    else renderSidebar();
    renderComposer();
  }

  function renderHeader() {
    el.chatTitle.textContent = (S.post && S.post.title) || 'Нов пост';
    const photos = (S.post && S.post.photos) || [];
    el.photosBtn.hidden = !photos.length;
    const included = photos.filter((p) => p.included).length;
    el.photosBtnText.textContent = `${plural(photos.length, 'снимка', 'снимки')}${included !== photos.length ? ` · ${included} включени` : ''}`;
    document.title = S.post && S.post.title ? `${S.post.title} — LinkedIn агент` : 'LinkedIn агент';
  }

  function renderAll() {
    renderHeader();
    renderSidebar();
    renderThread();
    renderPhotos();
    renderComposer();
  }

  function bind() {
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
      input.addEventListener('change', () => {
        handleFiles(input.files);
        input.value = '';
      });
    }
    let depth = 0;
    const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
    window.addEventListener('dragenter', (e) => {
      if (!hasFiles(e)) return;
      depth += 1;
      el.dropOverlay.hidden = false;
    });
    window.addEventListener('dragleave', () => {
      depth = Math.max(0, depth - 1);
      if (!depth) el.dropOverlay.hidden = true;
    });
    window.addEventListener('dragover', (e) => e.preventDefault());
    window.addEventListener('drop', async (e) => {
      e.preventDefault();
      depth = 0;
      el.dropOverlay.hidden = true;
      if (!hasFiles(e)) return;
      handleFiles(await collectFiles(e.dataTransfer));
    });

    el.photosBtn.addEventListener('click', openPhotos);
    el.photosClose.addEventListener('click', closePhotos);
    el.photosDone.addEventListener('click', closePhotos);
    el.photosModal.addEventListener('click', (e) => { if (e.target === el.photosModal) closePhotos(); });
    el.includeAll.addEventListener('click', () => setAllIncluded(true));
    el.excludeAll.addEventListener('click', () => setAllIncluded(false));

    el.newBtn.addEventListener('click', () => {
      if (!S.postId) {
        el.prompt.focus();
        closeSidebar();
        return;
      }
      location.hash = '#/';
      setTimeout(() => el.prompt.focus(), 0);
    });
    el.sideOpen.addEventListener('click', openSidebar);
    el.sideClose.addEventListener('click', closeSidebar);
    el.scrim.addEventListener('click', closeSidebar);
    el.busyChip.addEventListener('click', () => {
      const other = runningJobs().find((j) => j.post_id !== S.postId);
      if (other) location.hash = `#/p/${enc(other.post_id)}`;
    });
    el.lightbox.addEventListener('click', () => { el.lightbox.hidden = true; });
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      if (rowMenu) closeRowMenu();
      else if (S.renaming) { S.renaming = null; renderSidebar(); }
      else if (!el.lightbox.hidden) el.lightbox.hidden = true;
      else if (!el.photosModal.hidden) closePhotos();
      else closeSidebar();
    });
    document.addEventListener('click', (e) => { if (rowMenu && !rowMenu.contains(e.target)) closeRowMenu(); });
    el.chatList.addEventListener('scroll', closeRowMenu);
    window.addEventListener('resize', closeRowMenu);
    window.addEventListener('beforeunload', (e) => { if (S.uploading) e.preventDefault(); });
    window.addEventListener('hashchange', route);
    // Колега може да е качил снимки или пуснал агента от друг компютър — при връщане в
    // раздела показваме прясното състояние (без да пипаме текст, който се редактира).
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState !== 'visible') return;
      if (document.title.startsWith('✓')) renderHeader();
      loadInfo();
      loadPosts();
      if (S.postId && !S.editing && !S.uploading && !S.pollTimer) refreshPost();
    });
  }

  async function init() {
    S.view = storage('view') === 'mobile' ? 'mobile' : 'desktop';
    S.uploading = 0;
    bind();
    await Promise.all([loadInfo(), loadPosts()]);
    route();
    el.prompt.focus();
    setInterval(() => { loadInfo(); }, 4000);
  }

  init();
})();
