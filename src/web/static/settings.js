/*
 * „Настройки" (#/nastroiki, web-ui-5) — всичко универсално на едно място.
 *
 *   Туториал  — пусни пак: целия или една глава; кога е минат/пропуснат
 *   Вид       — тема: като системата / светла / тъмна (в този браузър)
 *   Папка     — коя е, „Взимай новите папки сами“
 *   Публикуване, Сигнали, Агентите и скорост — какво е включено; сменя се в .env
 *
 * Нещата от .env се показват, не се сменят оттук: тези настройки са на сървъра и искат рестарт
 * (start_web.bat) — по-добре честно „сменя се там“, отколкото бутон, който не прави нищо.
 */
import { api, h, icon, toast, ask, applyTheme, storage, TEAM } from './core.js';
import { store, loadInfo } from './store.js';
import * as tutorial from './tutorial.js';

let root = null;
const S = { data: null };

export async function show() {
  root = document.getElementById('view-settings');
  document.title = 'Настройки — LinkedIn агент';
  if (!S.data) root.replaceChildren(h('div', { class: 'view-pad' }, h('div', { class: 'loading' }, h('span', { class: 'spin' }), 'Зареждам…')));
  await load();
}

async function load() {
  try {
    S.data = await api('GET', '/api/settings');
  } catch (e) {
    // Стар процес на сървъра (пуснат преди web-ui-5) не знае /api/settings — кажи какво да се прави,
    // вместо вечно „Зареждам…“. Туториалът работи и без него.
    if (!root || store.route.name !== 'settings') return;
    root.replaceChildren(h('div', { class: 'view-pad st' },
      h('header', { class: 'view-head' }, h('p', { class: 'hero-kicker' }, 'Настройки'), h('h1', null, 'Сървърът е по-стара версия')),
      h('div', { class: 'st-card st-tour' },
        h('p', null, 'Агентът работи с по-стара версия и тази страница още не се отваря. Затворете черния прозорец на агента (start_web.bat) и го пуснете отново — всичко останало продължава да работи.'),
        h('div', { class: 'st-actions' }, h('button', { type: 'button', class: 'btn primary', onclick: () => tutorial.start(0) }, icon('chevR'), 'Пусни туториала')))));
    return;
  }
  render();
}

function render() {
  if (!root || store.route.name !== 'settings' || !S.data) return;
  const d = S.data;
  root.replaceChildren(h('div', { class: 'view-pad st' },
    h('header', { class: 'view-head' },
      h('p', { class: 'hero-kicker' }, 'Настройки'),
      h('h1', null, 'Всичко универсално на едно място'),
      h('p', null, 'Каквото се сменя оттук, важи веднага. Каквото е в .env, се сменя там и иска рестарт на start_web.bat.')),
    h('div', { class: 'st-grid' },
      tourCard(d.tutorial),
      themeCard(),
      folderCard(d),
      publishCard(d),
      alertsCard(d),
      agentsCard(d))));
}

function card(cls, iconName, title, ...body) {
  return h('section', { class: `st-card ${cls}` }, h('h2', null, icon(iconName), title), ...body);
}

function envNote(text) {
  return h('p', { class: 'st-env' }, icon('file'), text);
}

function tourCard(t) {
  const status = t.done
    ? `${t.state === 'finished' ? 'Минат' : 'Пропуснат'}${t.at > 1e9 ? ` на ${new Date(t.at * 1000).toLocaleDateString('bg-BG')}` : ''} — няма да се пуска сам.`
    : 'Още не е минат — ще се пусне сам при следващото отваряне.';
  return card('st-tour', 'help', 'Туториал',
    h('p', null, 'Разходка из всички модули: какво прави всеки, как се ползва и къде е. С „Напред“/„Назад“ и „Пропусни“.'),
    h('p', { class: 'muted' }, status),
    h('div', { class: 'st-actions' },
      h('button', { type: 'button', class: 'btn primary', onclick: () => tutorial.start(0) }, icon('chevR'), 'Пусни туториала'),
      t.done ? h('button', {
        type: 'button', class: 'btn ghost small', title: 'Следващото отваряне на агента го пуска сам — напр. за нова колежка',
        onclick: async () => { try { await tutorial.reset(); toast('Ще се пусне сам при следващото отваряне.'); load(); } catch (e) { toast(e.message, 'err'); } },
      }, 'Покажи го пак при отваряне') : null),
    h('p', { class: 'st-label' }, 'Само една глава:'),
    h('div', { class: 'st-chips' }, tutorial.CHAPTERS.map((name) => h('button', {
      type: 'button', class: 'tour-chip', onclick: () => tutorial.startChapter(name),
    }, name))));
}

function themeCard() {
  const current = storage('theme') || 'auto';
  const options = [['auto', 'Като системата'], ['light', 'Светла'], ['dark', 'Тъмна']];
  return card('st-theme', 'grid', 'Вид',
    h('p', null, 'Тема на интерфейса — само в този браузър.'),
    h('div', { class: 'seg', role: 'radiogroup', 'aria-label': 'Тема' }, options.map(([key, label]) => h('button', {
      type: 'button', role: 'radio', 'aria-checked': String(current === key), class: current === key ? 'on' : '',
      onclick: () => { applyTheme(key); render(); },
    }, label))));
}

function folderCard(d) {
  const toggle = h('input', { type: 'checkbox', checked: d.auto_ingest || null, disabled: d.folder ? null : true });
  toggle.addEventListener('change', () => setAuto(toggle.checked));
  return card('st-folder', 'folder', 'Папка',
    d.folder ? h('p', { class: 'path' }, d.folder) : h('p', { class: 'muted' }, 'Още няма избрана папка.'),
    h('div', { class: 'st-actions' }, h('a', { class: 'btn ghost small', href: '#/papka' }, d.folder ? 'Смени папката' : 'Избери папка')),
    h('label', { class: 'st-check' }, toggle,
      h('span', null, h('b', null, 'Взимай новите папки сами'),
        h('small', null, 'Всяка НОВА подпапка със снимки и текст става пост — насрочва се и се пише. Публикуването пак чака одобрение.'))));
}

async function setAuto(on) {
  if (on) {
    const ok = await ask({
      title: 'Агентът да взима новите папки сам?',
      text: 'Всяка НОВА подпапка със снимки и текст става пост: насрочва се по „Кога:“ или датата в името и веднага се пише. Старите папки остават за ръчно „Вземи“.',
      ok: 'Включи',
    });
    if (!ok) { render(); return; }
  }
  try {
    await api('PUT', '/api/ingest', { auto: on });
    toast(on ? 'Включено — следя папката.' : 'Изключено.');
  } catch (e) {
    toast(e.message, 'err');
  }
  load();
}

function publishCard(d) {
  const linkedin = d.publish_mode === 'linkedin';
  return card('st-publish', 'send', 'Публикуване',
    h('p', null, h('b', null, linkedin ? 'Режим „linkedin“ — ' : 'Режим „чернова“ — '),
      linkedin
        ? 'агентът публикува сам одобрените постове в часа им (не по-късно от 6 ч.).'
        : 'нищо не отива в LinkedIn само. В часа агентът напомня; копирате текста и натискате „Публикувах го“.'),
    d.publish_problem ? h('p', { class: 'st-warn' }, d.publish_problem) : null,
    envNote('PUBLISH_MODE в .env (draft / linkedin). „linkedin“ иска одобрен API достъп.'));
}

function alertsCard(d) {
  const ch = d.alert_channels || {};
  const list = [ch.webhook ? `Teams/Slack (${ch.webhook_host || 'webhook'})` : null, ch.email ? 'имейл' : null].filter(Boolean);
  return card('st-alerts', 'bell', 'Сигнали',
    h('p', null, list.length ? `Освен в „Сигнали“, идват и в: ${list.join(', ')}.` : 'Само в „Сигнали“ тук — без Teams/Slack или имейл.'),
    h('div', { class: 'st-actions' },
      h('button', { type: 'button', class: 'btn ghost small', onclick: testAlert }, 'Изпрати пробен сигнал')),
    envNote('ALERT_WEBHOOK_URL и ALERT_EMAIL_* в .env.'));
}

async function testAlert() {
  try {
    const r = await api('POST', '/api/alerts/test', {});
    toast(r.sending ? 'Пробният сигнал е изпратен — проверете Teams/Slack/имейла.' : 'Пробният сигнал е в „Сигнали“ (външни канали не са настроени).');
    loadInfo();
  } catch (e) {
    toast(e.message, 'err');
  }
}

function agentsCard(d) {
  const speed = d.speed || {};
  const fast = (speed.no_thinking || []).map((r) => ({ planner: 'Планер', critic: 'Критик', media: 'Снимки', writer: 'Писател', consolidator: 'Консолидатор', judge: 'Съдия' }[r] || r));
  const mode = (store.info && (store.info.modes || []).find((m) => m.key === d.default_mode)) || null;
  return card('st-agents', 'spark', 'Агентите и скорост',
    h('ul', { class: 'st-list' },
      h('li', null, h('b', null, `${d.max_jobs} агента`), ' пишат едновременно (WEB_MAX_JOBS).'),
      h('li', null, h('b', null, 'Дължина по подразбиране: '), mode ? mode.label : d.default_mode),
      h('li', null, h('b', null, 'Бързо, без „мислене“: '), fast.length ? fast.join(', ') : 'никоя роля', ' (NO_THINKING_ROLES).'),
      h('li', null, h('b', null, 'Самопоправка след проверката: '), speed.final_autofix === false ? 'изключена' : 'включена', ' (FINAL_AUTOFIX).')),
    h('details', { class: 'st-team' }, h('summary', null, 'Кой какво прави в агента'),
      h('ol', { class: 'tour-team' }, TEAM.filter((m) => m.role !== 'Писател').map((m) => h('li', null,
        h('b', null, m.role), h('small', null, m.time), h('span', null, m.text))))),
    envNote('Сменя се в .env; после рестарт на start_web.bat.'));
}
