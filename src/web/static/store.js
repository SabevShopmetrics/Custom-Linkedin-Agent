/*
 * Споделеното състояние (web-ui-3): една заявка към /api/info, един списък с постове,
 * графици и разговори — за всички изгледи. Преди всеки скрипт питаше сървъра поотделно
 * (на 4 s и на 15 s) и държеше свое копие; сега изгледите се абонират за промени.
 */
import { api } from './core.js';

export const store = {
  info: null,
  posts: [],
  batches: [],
  threads: [],
  route: { name: 'assistant', id: null },
};

const listeners = new Map(); // събитие → Set(функции)

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
  return () => listeners.get(event).delete(fn);
}

export function emit(event, payload) {
  for (const fn of listeners.get(event) || []) {
    try { fn(payload); } catch (e) { console.error(e); } // eslint-disable-line no-console
  }
}

/**
 * Страниците се зареждат наново от диска, но сървърът (Python) — само при рестарт на start_web.bat.
 * Стар процес не връща `tutorial` в /api/info (добавено с web-ui-5) → новите действия няма как да
 * минат. Тогава интерфейсът го казва с човешки думи, вместо „Няма такъв адрес".
 */
// Същото като UI_VERSION в src/web/server.py — тест ги сверява.
export const UI_VERSION = '2026-10-14.6';
export const serverOutdated = () => Boolean(store.info) && store.info.ui_version !== UI_VERSION;
export const OUTDATED_TEXT = 'Агентът е обновен, но прозорецът му още работи със старата версия. Затворете черния прозорец на агента (start_web.bat) и го пуснете отново — после опитайте пак.';

export const runningJobs = () => (store.info && store.info.jobs) || [];
export const maxJobs = () => (store.info && store.info.max_jobs) || 3;
export const modeList = () => (store.info && store.info.modes) || [];

export async function loadInfo() {
  try {
    store.info = await api('GET', '/api/info');
  } catch (e) {
    return;
  }
  emit('info', store.info);
}

export async function loadPosts() {
  try {
    store.posts = (await api('GET', '/api/posts')).posts;
  } catch (e) {
    return;
  }
  emit('posts', store.posts);
}

export async function loadBatches() {
  try {
    store.batches = (await api('GET', '/api/batches')).batches;
  } catch (e) {
    return;
  }
  emit('batches', store.batches);
}

export async function loadThreads() {
  try {
    store.threads = (await api('GET', '/api/assistant')).threads;
  } catch (e) {
    return;
  }
  emit('threads', store.threads);
}

export const loadLists = () => Promise.all([loadPosts(), loadBatches(), loadThreads()]);

export function startPolling() {
  setInterval(() => { if (!document.hidden) loadInfo(); }, 4000);
  setInterval(() => { if (!document.hidden) { loadBatches(); } }, 8000);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    loadInfo();
    loadLists();
    emit('visible');
  });
}
