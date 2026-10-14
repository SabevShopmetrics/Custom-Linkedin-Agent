/*
 * Лявата колона (web-ui-3): навигация между изгледите и списъците — разговори с асистента,
 * постове (с търсене) и графици. Един собственик за цялата колона — преди чатът и таблото
 * я рисуваха поотделно и маркираха различни „текущи" редове.
 */
import { api, h, icon, toast, ask, enc, postState, storage } from './core.js';
import { store, on, loadPosts, loadInfo, runningJobs } from './store.js';
import { deleteThread, newThread } from './assistant.js';
import { forget } from './chat.js';

const NAV = [
  { name: 'overview', href: '#/', label: 'Преглед', icon: 'grid' },
  { name: 'assistant', href: '#/asistent', label: 'Асистент', icon: 'spark' },
  { name: 'calendar', href: '#/kalendar', label: 'Календар', icon: 'calendar' },
  { name: 'library', href: '#/papka', label: 'Папка', icon: 'folder' },
  { name: 'alerts', href: '#/signali', label: 'Сигнали', icon: 'bell' },
  { name: 'settings', href: '#/nastroiki', label: 'Настройки', icon: 'gear' },
];

const SB = { query: '', renaming: null, menu: null, tab: storage('side-tab') || 'posts' };
const el = {};

export function init() {
  el.side = document.getElementById('sidebar');
  el.nav = document.getElementById('side-nav');
  el.lists = document.getElementById('side-lists');
  el.busy = document.getElementById('busy-chip');
  el.busyText = document.getElementById('busy-text');
  el.scrim = document.getElementById('scrim');
  el.search = document.getElementById('side-search');
  el.search.addEventListener('input', () => { SB.query = el.search.value.trim().toLowerCase(); renderLists(); });
  el.busy.addEventListener('click', () => {
    const other = runningJobs().find((j) => j.post_id !== store.route.id);
    if (other) location.hash = `#/p/${enc(other.post_id)}`;
  });
  document.getElementById('side-open').addEventListener('click', open);
  document.getElementById('side-close').addEventListener('click', close);
  el.scrim.addEventListener('click', close);
  document.addEventListener('click', (e) => { if (SB.menu && !SB.menu.contains(e.target)) closeMenu(); });
  el.lists.addEventListener('scroll', closeMenu);
  window.addEventListener('resize', closeMenu);
  on('posts', renderLists);
  on('threads', renderLists);
  on('batches', renderLists);
  on('info', onInfo);
  render();
}

export function render() {
  renderNav();
  renderLists();
}

function renderNav() {
  const route = store.route.name;
  const active = route === 'board' ? null : route;
  const summary = (store.info && store.info.alerts) || { open: 0, errors: 0 };
  el.nav.replaceChildren(...NAV.map((item) => h('a', {
    class: `nav-item${active === item.name ? ' current' : ''}`,
    href: item.href,
    'aria-current': active === item.name ? 'page' : null,
    onclick: item.name === 'assistant' ? (e) => { e.preventDefault(); newThread(); } : null,
  }, icon(item.icon), h('span', null, item.label),
  item.name === 'alerts' && summary.open
    ? h('span', { class: `nav-badge${summary.errors ? ' err' : ''}`, 'aria-label': `${summary.open} отворени` }, String(summary.open))
    : null)));
}

function onInfo(info) {
  renderNav();
  const jobs = info.jobs || [];
  const others = jobs.filter((j) => j.post_id !== store.route.id);
  el.busy.hidden = !others.length;
  if (others.length) {
    el.busyText.textContent = others.length === 1 ? `Пише още 1 пост · ${Math.floor(others[0].elapsed / 60)}:${String(Math.round(others[0].elapsed) % 60).padStart(2, '0')}` : `Пише още ${others.length} поста`;
  }
  // Пост, който е приключил във фонов режим (докато гледате друго) — кратко известие.
  const ids = new Set(jobs.map((j) => j.post_id));
  const before = SB.prevRunning;
  SB.prevRunning = ids;
  if (before) {
    for (const id of before) {
      if (!ids.has(id) && id !== store.route.id) {
        const p = store.posts.find((x) => x.id === id);
        toast(`Агентът приключи: ${p ? p.title : id}`);
      }
    }
    if (before.size !== ids.size || [...ids].some((id) => !before.has(id))) loadPosts();
  }
  renderLists();
}

function tabButton(key, label, count) {
  return h('button', {
    type: 'button', role: 'tab', class: 'side-tab', 'aria-selected': String(SB.tab === key),
    onclick: () => { SB.tab = key; storage('side-tab', key); renderLists(); },
  }, label, count ? h('small', null, String(count)) : null);
}

function renderLists() {
  if (!el.lists) return;
  const nodes = [];
  nodes.push(h('div', { class: 'side-tabs', role: 'tablist' },
    tabButton('posts', 'Постове', store.posts.length),
    tabButton('threads', 'Разговори', store.threads.length),
    tabButton('batches', 'Графици', store.batches.length)));
  el.search.hidden = SB.tab !== 'posts';
  if (SB.tab === 'posts') nodes.push(...postRows());
  else if (SB.tab === 'threads') nodes.push(...threadRows());
  else nodes.push(...batchRows());
  el.lists.replaceChildren(...nodes);
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

function postRows() {
  const running = new Set(runningJobs().map((j) => j.post_id));
  const list = SB.query ? store.posts.filter((p) => p.title.toLowerCase().includes(SB.query)) : store.posts;
  if (!list.length) {
    return [h('div', { class: 'chat-empty' }, SB.query ? 'Нищо не съвпада.' : 'Още няма постове.')];
  }
  const nodes = [];
  let group = null;
  for (const p of list) {
    const g = dayGroup(p.id);
    if (g !== group) {
      group = g;
      nodes.push(h('div', { class: 'chat-group' }, g));
    }
    const state = postState(p, running.has(p.id));
    const current = store.route.name === 'post' && store.route.id === p.id;
    if (SB.renaming === p.id) {
      nodes.push(renameRow(p, state));
      continue;
    }
    nodes.push(h('div', { class: `chat-row${current ? ' current' : ''}` },
      h('a', { class: 'chat-item', href: `#/p/${enc(p.id)}`, title: `${p.title} — ${state.label}` },
        h('span', { class: `s st-${state.key}` }), h('span', { class: 't' }, p.title),
        p.scheduled_for ? h('small', { class: 'chat-date' }, p.scheduled_for.slice(5).split('-').reverse().join('.')) : null),
      h('button', {
        type: 'button', class: 'chat-more', title: 'Още', 'aria-label': `Още за „${p.title}“`, 'aria-haspopup': 'menu',
        html: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="5" r="1.6" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="12" cy="19" r="1.6" fill="currentColor" stroke="none"/></svg>',
        onclick: (e) => { e.stopPropagation(); e.preventDefault(); openMenu(p, e.currentTarget); },
      })));
  }
  return nodes;
}

function renameRow(p, state) {
  const input = h('input', { type: 'text', class: 'chat-rename', value: p.title, 'aria-label': 'Ново име' });
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    SB.renaming = null;
    if (save && input.value.trim() !== p.title) {
      try {
        await api('PUT', `/api/posts/${enc(p.id)}`, { title: input.value });
      } catch (e) {
        toast(e.message, 'err');
      }
      await loadPosts();
    } else {
      renderLists();
    }
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
  setTimeout(() => { input.focus(); input.select(); }, 0);
  return h('div', { class: 'chat-row current' }, h('span', { class: `s st-${state.key}` }), input);
}

function threadRows() {
  if (!store.threads.length) {
    return [h('div', { class: 'chat-empty' }, 'Още няма разговори. Попитайте асистента нещо.')];
  }
  return store.threads.map((t) => h('div', { class: `chat-row${store.route.name === 'assistant' && store.route.id === t.id ? ' current' : ''}` },
    h('a', { class: 'chat-item', href: `#/a/${t.id}`, title: t.title }, icon('chat', 'row-ico'), h('span', { class: 't' }, t.title)),
    h('button', {
      type: 'button', class: 'chat-more', title: 'Изтрий', 'aria-label': `Изтрий „${t.title}“`,
      onclick: (e) => { e.preventDefault(); deleteThread(t); },
    }, icon('trash'))));
}

function batchRows() {
  const nodes = [h('a', { class: 'side-link', href: '#/grafik' }, icon('up'), 'Качи график от файл или текст')];
  if (!store.batches.length) {
    nodes.push(h('div', { class: 'chat-empty' }, 'Още няма графици. Помолете асистента за план или прочетете график от „Папка“.'));
    return nodes;
  }
  for (const b of store.batches.slice(0, 20)) {
    const bar = h('span', { class: 'mini-bar' }, h('span'));
    bar.firstChild.style.width = `${b.count ? (b.done / b.count) * 100 : 0}%`;
    nodes.push(h('div', { class: `chat-row${store.route.name === 'board' && store.route.id === b.id ? ' current' : ''}` },
      h('a', { class: 'chat-item batch', href: `#/b/${b.id}`, title: b.name },
        h('span', { class: `s ${b.active ? 'st-running' : b.done === b.count ? 'st-ready' : 'st-draft'}` }),
        h('span', { class: 't' }, b.name),
        h('small', { class: 'chat-date' }, `${b.done}/${b.count}`),
        bar)));
  }
  return nodes;
}

// ------------------------------------------------------------------ меню „⋮"

function closeMenu() {
  if (SB.menu) {
    SB.menu.remove();
    SB.menu = null;
  }
}

export function menuOpen() {
  return !!SB.menu || !!SB.renaming;
}

export function escape() {
  if (SB.menu) { closeMenu(); return true; }
  if (SB.renaming) { SB.renaming = null; renderLists(); return true; }
  if (el.side.classList.contains('open')) { close(); return true; }
  return false;
}

function openMenu(p, anchor) {
  const reopen = SB.menu && SB.menu.dataset.post === p.id;
  closeMenu();
  if (reopen) return;
  SB.menu = h('div', { class: 'row-menu', role: 'menu', 'data-post': p.id },
    h('button', { type: 'button', role: 'menuitem', onclick: () => { closeMenu(); SB.renaming = p.id; renderLists(); } }, icon('pen'), h('span', null, 'Преименувай')),
    h('button', { type: 'button', role: 'menuitem', class: 'danger', onclick: () => { closeMenu(); deletePost(p); } }, icon('trash'), h('span', null, 'Изтрий')));
  document.body.appendChild(SB.menu);
  const box = anchor.getBoundingClientRect();
  const menu = SB.menu.getBoundingClientRect();
  const top = box.bottom + menu.height + 8 > window.innerHeight ? box.top - menu.height - 4 : box.bottom + 4;
  SB.menu.style.top = `${Math.max(8, top)}px`;
  SB.menu.style.left = `${Math.max(8, Math.min(box.right - menu.width, window.innerWidth - menu.width - 8))}px`;
  SB.menu.querySelector('button').focus();
}

async function deletePost(p) {
  const busy = runningJobs().some((j) => j.post_id === p.id);
  const ok = await ask({
    title: busy ? `Агентът още пише „${p.title}“` : `Да изтрия ли „${p.title}“?`,
    text: `${busy ? 'Ще го спра и ще изтрия поста. ' : ''}Постът отива в кошчето (папка posts/_kosh) — оттам може да се върне ръчно.`,
    ok: busy ? 'Спри и изтрий' : 'Изтрий',
    danger: true,
  });
  if (!ok) return;
  try {
    await api('DELETE', `/api/posts/${enc(p.id)}`);
  } catch (e) {
    toast(e.message, 'err');
    return;
  }
  forget(p.id);
  toast('Постът е преместен в кошчето.');
  await Promise.all([loadPosts(), loadInfo()]);
}

export function open() {
  el.side.classList.add('open');
  el.scrim.hidden = false;
}

export function close() {
  el.side.classList.remove('open');
  el.scrim.hidden = true;
}
