/*
 * Входна точка на уеб интерфейса (web-ui-3). Без библиотеки и без компилиране — ES модули
 * направо от сървъра (src/web/server.py). Тук е само рутерът: един `hashchange`, който казва
 * на точно един изглед да се покаже, и общите неща (влачене на файлове, Escape, банер).
 *
 *   #/            преглед — целият път с един поглед  overview.js
 *   #/asistent    асистент (нов разговор)       assistant.js
 *   #/a/<id>      разговор с асистента
 *   #/kalendar    календар (…/2026-11)          calendar.js
 *   #/papka       папката на компютъра          library.js
 *   #/signali     сигнали за публикуването       alerts.js
 *   #/nastroiki   настройки + туториал          settings.js, tutorial.js
 *   #/nov         нов пост                      chat.js
 *   #/p/<id>      пост
 *   #/grafik      график от файл/текст          board.js
 *   #/b/<id>      табло на график
 */
import { esc, applyTheme } from './core.js';
import { store, on, loadInfo, loadLists, startPolling, serverOutdated, OUTDATED_TEXT } from './store.js';
import * as sidebar from './sidebar.js';
import * as chat from './chat.js';
import * as assistant from './assistant.js';
import * as calendar from './calendar.js';
import * as library from './library.js';
import * as board from './board.js';
import * as alertsView from './alerts.js';
import * as overview from './overview.js';
import { routeFiles } from './files.js';
import * as drawer from './drawer.js';
import * as settingsView from './settings.js';
import * as tutorial from './tutorial.js';
import * as controls from './controls.js';

const SECTIONS = {
  overview: 'view-overview',
  assistant: 'view-assistant',
  calendar: 'view-calendar',
  library: 'view-library',
  alerts: 'view-alerts',
  settings: 'view-settings',
  post: 'view-post',
  board: 'board',
};

function parse(hash) {
  let m;
  if (!hash || hash === '#' || hash === '#/') return { name: 'overview', id: null };
  if (hash === '#/asistent') return { name: 'assistant', id: null };
  if ((m = hash.match(/^#\/a\/([a-f0-9]{12})$/))) return { name: 'assistant', id: m[1] };
  if (hash === '#/nov') return { name: 'post', id: null };
  if ((m = hash.match(/^#\/p\/(.+)$/))) return { name: 'post', id: decodeURIComponent(m[1]) };
  if ((m = hash.match(/^#\/kalendar(?:\/(\d{4}-\d{2}))?$/))) return { name: 'calendar', id: m[1] || null };
  if (hash === '#/papka') return { name: 'library', id: null };
  if (hash === '#/signali') return { name: 'alerts', id: null };
  if (hash === '#/nastroiki') return { name: 'settings', id: null };
  if (hash === '#/grafik') return { name: 'board', id: null };
  if ((m = hash.match(/^#\/b\/([a-f0-9]{12})$/))) return { name: 'board', id: m[1] };
  return { name: 'overview', id: null };
}

function route() {
  const next = parse(location.hash);
  const prev = store.route.name;
  if (prev !== next.name) {
    if (prev === 'post') chat.hide();
    if (prev === 'board') board.hide();
    if (prev === 'overview') overview.hide();
  }
  drawer.close();
  store.route = { ...next };
  for (const [name, id] of Object.entries(SECTIONS)) document.getElementById(id).hidden = name !== next.name;
  document.body.dataset.view = next.name;
  if (next.name === 'overview') overview.show();
  else if (next.name === 'assistant') assistant.show(next.id);
  else if (next.name === 'post') chat.show(next.id);
  else if (next.name === 'calendar') calendar.show(next.id);
  else if (next.name === 'library') library.show();
  else if (next.name === 'alerts') alertsView.show();
  else if (next.name === 'settings') settingsView.show();
  else board.show(next.id);
  sidebar.render();
  sidebar.close();
}

function bindGlobal() {
  window.addEventListener('hashchange', route);

  // Пуснати снимки/ZIP където и да е → към текущия пост (или нов пост).
  const overlay = document.getElementById('drop-overlay');
  let depth = 0;
  const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
  window.addEventListener('dragenter', (e) => {
    if (!hasFiles(e)) return;
    depth += 1;
    overlay.hidden = false;
  });
  window.addEventListener('dragleave', () => {
    depth = Math.max(0, depth - 1);
    if (!depth) overlay.hidden = true;
  });
  window.addEventListener('dragover', (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener('drop', async (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    overlay.hidden = true;
    const files = await chat.collectFiles(e.dataTransfer);
    routeFiles(files, { text: assistant.takeDraft() });
  });

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (tutorial.isOpen()) return; // туториалът сам хваща Esc
    if (chat.closeLightbox()) return;
    if (drawer.isOpen()) { drawer.close(); return; }
    if (chat.photosOpen()) { chat.closePhotos(); return; }
    sidebar.escape();
  });
  window.addEventListener('beforeunload', (e) => { if (chat.isBusy()) e.preventDefault(); });

  const banner = document.getElementById('config-banner');
  on('info', (info) => {
    const outdated = serverOutdated();
    banner.hidden = !info.config_error && !outdated;
    if (info.config_error) banner.innerHTML = `<b>Агентът не е настроен</b>${esc(info.config_error)}`;
    else if (outdated) banner.innerHTML = `<b>Нужен е рестарт на агента</b>${esc(OUTDATED_TEXT)}`;
  });
}

async function init() {
  applyTheme(); // темата от „Настройки“ — преди първото рисуване
  controls.init(); // собствени падащи менюта, дата и час вместо браузърските
  chat.init();
  calendar.init();
  alertsView.init();
  overview.init();
  sidebar.init();
  bindGlobal();
  await Promise.all([loadInfo(), loadLists()]);
  route();
  startPolling();
  tutorial.maybeAutoStart(); // само първия път (web-ui-5)
}

init();
