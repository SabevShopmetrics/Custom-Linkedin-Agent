/*
 * Сигнали (#/signali) — проблеми с публикуването в LinkedIn (src/web/alerts.py).
 *
 * Отворените стоят най-горе, докато някой не натисне „Готово“ (или постът не се публикува —
 * тогава агентът ги затваря сам). Нов сигнал → известие в интерфейса и, ако е разрешено,
 * системно известие от браузъра. Навън (Teams/Slack/имейл) праща сървърът — по .env.
 */
import { api, h, icon, toast, ask, enc, storage, fmtDay } from './core.js';
import { store, on, loadInfo } from './store.js';

const LEVEL = { error: 'Грешка', warn: 'Внимание', action: 'За действие' };
let root = null;
let items = [];
let showResolved = false;

export async function show() {
  root = document.getElementById('view-alerts');
  document.title = 'Сигнали — LinkedIn агент';
  await load();
}

async function load() {
  try {
    items = (await api('GET', '/api/alerts')).alerts;
  } catch (e) {
    toast(e.message, 'err');
    return;
  }
  render();
}

function when(t) {
  const d = new Date(t * 1000);
  const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  return `${fmtDay(iso)} · ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function render() {
  if (!root || store.route.name !== 'alerts') return;
  const open = items.filter((a) => !a.resolved);
  const closed = items.filter((a) => a.resolved);
  const info = store.info || {};
  const ch = info.alert_channels || {};
  const perm = typeof Notification === 'undefined' ? 'unsupported' : Notification.permission;

  const channels = h('section', { class: 'al-channels' },
    h('h2', null, 'Къде пристигат'),
    h('ul', null,
      h('li', { class: 'on' }, icon('check'), 'Тук, в интерфейса — винаги'),
      h('li', { class: perm === 'granted' ? 'on' : '' }, icon(perm === 'granted' ? 'check' : 'close'), 'Известие от браузъра',
        perm === 'default' ? h('button', { type: 'button', class: 'link-btn', onclick: askPermission }, 'Разреши') : null,
        perm === 'denied' ? h('small', null, ' — забранено в настройките на браузъра') : null),
      h('li', { class: ch.webhook ? 'on' : '' }, icon(ch.webhook ? 'check' : 'close'),
        ch.webhook ? `Teams/Slack (${ch.webhook_host})` : 'Teams/Slack — не е настроено (ALERT_WEBHOOK_URL в .env)'),
      h('li', { class: ch.email ? 'on' : '' }, icon(ch.email ? 'check' : 'close'),
        ch.email ? `Имейл до ${ch.email_to}` : 'Имейл — не е настроен (ALERT_EMAIL_TO и ALERT_SMTP_* в .env)')),
    h('p', { class: 'muted' },
      info.publish_mode === 'linkedin'
        ? 'Режим LinkedIn: агентът публикува одобрените постове в часа им и вдига сигнал, ако нещо не мине.'
        : 'Режим „чернова“: нищо не излиза само — в часа на одобрен пост идва напомняне да го публикувате ръчно.'),
    h('button', { type: 'button', class: 'btn ghost small', onclick: sendTest }, 'Изпрати тестов сигнал'));

  const list = open.length
    ? h('div', { class: 'al-list' }, open.map(card))
    : h('div', { class: 'empty-card al-empty' }, icon('check', 'big'), h('b', null, 'Няма отворени сигнали'),
      h('p', null, 'Ако публикуване не мине, час бъде пропуснат или настройките за LinkedIn не са готови — ще го видите тук.'));

  root.replaceChildren(h('div', { class: 'view-pad' },
    h('header', { class: 'view-head row' },
      h('div', null, h('h1', null, 'Сигнали'),
        h('p', null, open.length ? `${open.length} ${open.length === 1 ? 'отворен' : 'отворени'}` : 'Всичко е наред.')),
      h('div', { class: 'head-actions' }, h('button', { type: 'button', class: 'btn ghost small', onclick: load }, '↻ Опресни'))),
    h('div', { class: 'al-layout' },
      h('div', null, list,
        closed.length ? h('details', { class: 'al-closed', open: showResolved, ontoggle: (e) => { showResolved = e.target.open; } },
          h('summary', null, `Решени · ${closed.length}`),
          h('div', { class: 'al-list' }, closed.slice(0, 50).map(card))) : null),
      channels)));
}

function card(a) {
  const actions = [];
  if (a.post_id) actions.push(h('a', { class: 'btn ghost small', href: `#/p/${enc(a.post_id)}` }, 'Отвори поста'));
  if (!a.resolved && a.post_id && a.kind === 'publish_failed' && (store.info || {}).publish_mode === 'linkedin') {
    actions.push(h('button', { type: 'button', class: 'btn primary small', onclick: () => retry(a) }, 'Опитай пак'));
  }
  if (!a.resolved) actions.push(h('button', { type: 'button', class: 'btn ghost small', onclick: () => resolve(a) }, 'Готово'));
  const delivery = Object.entries(a.delivery || {}).map(([k, v]) => `${k === 'webhook' ? 'Teams/Slack' : k === 'email' ? 'имейл' : k}: ${v}`);
  return h('article', { class: `al-card ${a.level}${a.resolved ? ' resolved' : ''}` },
    h('div', { class: 'al-top' },
      h('span', { class: 'al-level' }, LEVEL[a.level] || a.level),
      h('b', null, a.title),
      a.count > 1 ? h('span', { class: 'al-count' }, `×${a.count}`) : null,
      h('span', { class: 'grow' }),
      h('small', null, when(a.last || a.t))),
    a.post_title ? h('p', { class: 'al-post' }, a.post_title) : null,
    h('p', { class: 'al-msg' }, a.message),
    delivery.length ? h('small', { class: 'al-delivery' }, delivery.join(' · ')) : null,
    a.resolved ? h('small', { class: 'al-delivery' }, `Решен от ${a.resolved_by || '—'} · ${when(a.resolved)}`) : null,
    actions.length ? h('div', { class: 'al-actions' }, actions) : null);
}

async function resolve(a) {
  try {
    await api('POST', `/api/alerts/${enc(a.id)}/resolve`, {});
  } catch (e) {
    toast(e.message, 'err');
  }
  await Promise.all([load(), loadInfo()]);
}

async function retry(a) {
  const ok = await ask({ title: 'Да опитам ли пак?', text: 'Постът излиза в LinkedIn страницата на клъстера веднага и публично.', ok: 'Публикувай' });
  if (!ok) return;
  try {
    await api('POST', `/api/posts/${enc(a.post_id)}/publish`, {});
    toast('Публикуван в LinkedIn ✓');
  } catch (e) {
    toast(e.message, 'err');
  }
  await Promise.all([load(), loadInfo()]);
}

async function sendTest() {
  try {
    const r = await api('POST', '/api/alerts/test', {});
    toast(r.sending ? 'Тестовият сигнал е изпратен — проверете Teams/Slack/имейла.' : 'Тестовият сигнал е тук. Навън не е настроен канал.');
  } catch (e) {
    toast(e.message, 'err');
  }
  setTimeout(() => { load(); loadInfo(); }, 1500);
}

async function askPermission() {
  try { await Notification.requestPermission(); } catch (e) { /* стар браузър */ }
  render();
}

// Нов сигнал, докато гледате друго → известие (веднъж на сигнал).
export function init() {
  let lastKey = '';
  on('info', (info) => {
    const latest = info.alerts && info.alerts.latest;
    // Прерисуване само при промяна — иначе бутоните „подскачат" на всеки 4 s.
    const key = `${(info.alerts || {}).open}|${latest ? latest.id : ''}`;
    if (key !== lastKey) {
      lastKey = key;
      if (store.route.name === 'alerts') load();
    }
    if (!latest || storage('alert-seen') === latest.id) return;
    const first = storage('alert-seen') === null;
    storage('alert-seen', latest.id);
    if (first) return; // при първо отваряне не „крещим" за стари сигнали
    toast(`${latest.title}${latest.message ? ` — ${latest.message}` : ''}`, latest.level === 'error' ? 'err' : undefined);
    if (typeof Notification !== 'undefined' && Notification.permission === 'granted' && document.hidden) {
      try { new Notification(`LinkedIn агент: ${latest.title}`, { body: latest.message || '' }); } catch (e) { /* без известие */ }
    }
  });
}
