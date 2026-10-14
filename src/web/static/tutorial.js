/*
 * Туториал (web-ui-5): разходка из всички модули — какво прави всеки, как се ползва и къде е.
 *
 *   - Пуска се сам САМО първия път. Минат или пропуснат → сървърът го помни
 *     (posts/_settings.json → tutorial), за да не спами. Отново: „Настройки“ → „Пусни туториала“,
 *     целият или само една глава.
 *   - „Напред“ / „Назад“ (и стрелките ← →), „Пропусни“ (и Esc). Точките отдолу прескачат към глава.
 *   - Всяка стъпка отваря своя изглед и осветява истинския елемент на екрана. Ако елементът го
 *     няма (празен изглед), картата застава в средата — текстът казва кога се появява.
 *   - Презареждане по средата продължава от същата стъпка (sessionStorage).
 */
import { api, h, icon, toast, storage, TEAM } from './core.js';
import { store } from './store.js';
import * as sidebar from './sidebar.js';

const SESSION_KEY = 'tour-step';
// Резервна памет в браузъра: ако сървърът не запише (стар процес, без връзка), пак да не спамим.
const DONE_KEY = 'tour-done';

// ------------------------------------------------------------------ съдържание

const pathDiagram = () => h('ol', { class: 'tour-path' },
  ['Вход — папка със снимки и текст, или изречение към Асистента',
    'Агентите пишат — екип: Планер, Писател, Критик… (~2,5–4 мин)',
    'Проверка — агентът сам маха недоказаното',
    'Вие одобрявате — с един клик, за точния текст',
    'Публикуване — в часа, който сте избрали'].map((t) => h('li', null, t)));

const teamTable = () => h('ol', { class: 'tour-team' }, TEAM.filter((m) => m.role !== 'Писател').map((m) => h('li', null,
  h('b', null, m.role), h('small', null, m.time), h('span', null, m.text))));

const folderFormat = () => h('div', { class: 'tour-format' },
  h('p', { class: 'tour-ok' }, icon('check'), 'Всичко работи и се приема: само снимки, текст без дата, Excel график, ZIP, Word „Запиши като .txt“. Но най-ефективно — най-малко бележки и поправки, правилен час — е така:'),
  h('pre', null,
    '📁 LinkedIn                     ← папката, избрана в „Папка“\n'
    + ' └ 📁 2026-10-30 11.00 Стажанти в Морски софтуер\n'
    + '     ├ бриф.txt\n'
    + '     ├ 01.jpg  02.jpg  03.jpg  04.jpg'),
  h('p', { class: 'tour-label' }, 'бриф.txt'),
  h('pre', null,
    'Кога: 30.10.2026 11:00\n'
    + 'Режим: фокус\n'
    + 'Заглавие: Стажанти в „Морски софтуер“\n'
    + '---\n'
    + 'Шестима студенти от ТУ – Варна започнаха 3-месечен стаж в „Морски софтуер“\n'
    + 'на 14 октомври 2026. Работят по система за проследяване на контейнери в\n'
    + 'пристанище Варна. Ментор: инж. Петър Иванов, ръководител „Развойна дейност“.\n'
    + 'Повече: https://ictclustervarna.com/...'),
  h('ul', { class: 'tour-tips' },
    h('li', null, h('b', null, 'Една подпапка = един пост. '), 'Име „ГГГГ-ММ-ДД ЧЧ.ММ Заглавие“ — датата, часът и заглавието се четат сами.'),
    h('li', null, h('b', null, 'Един .txt или .md файл до снимките. '), 'Ако са няколко — сложете „бриф“ в името на правилния.'),
    h('li', null, h('b', null, 'Най-отгоре по желание: '), '„Кога:“, „Режим:“ (кадър / фокус / панорама), „Заглавие:“, после „---“. „Кога:“ печели пред името на папката. Час — с двоеточие („11:00“) или „11.00 ч.“'),
    h('li', null, h('b', null, 'Брифът: '), 'кой, какво, кога, къде, числа, имена с длъжност, линк. Конкретни дати — не „вчера“ или „утре“: постът може да излезе седмица по-късно.'),
    h('li', null, h('b', null, 'Снимки: '), '3–8 броя .jpg/.png, без почти еднакви; агентът гледа до 12. Деца — само със съгласие.'),
    h('li', null, h('b', null, 'Имена на хора и фирми — '), 'само ако са съгласни да бъдат споменати.'),
    h('li', null, h('b', null, 'Много постове наведнъж: '), 'файл .xlsx или .csv с колони Дата · Тема (бриф) · Режим · Снимки (име на подпапка) · Заглавие — в главната папка или през „+“ на Асистента.')),
  h('p', { class: 'tour-why' }, 'Защо: агентът не гадае. Каквото липсва, той не измисля — постът става по-кратък и с бележка „допишете“.'));

const briefExample = () => h('div', { class: 'tour-format' },
  h('p', { class: 'tour-label' }, 'Слаб бриф'),
  h('pre', null, 'Днес имахме среща с фирма.'),
  h('p', { class: 'tour-label' }, 'Силен бриф'),
  h('pre', null, 'На 24.09.2026 в ИУ – Варна проведохме шестото „Сверяване на часовника“ —\n180 души. Гост-лектор: проф. Мария Тодорова (ТУ – Варна), ИИ в морската\nиндустрия. Връчихме 11 ваучера по Фонд „Мост към бъдещето“.\nОтчет: https://ictclustervarna.com/news/...'));

const publishFlow = () => h('ul', { class: 'tour-tips' },
  h('li', null, h('b', null, 'Режим „чернова“ (сега): '), 'в часа агентът напомня — копирате текста в LinkedIn и натискате „Публикувах го“.'),
  h('li', null, h('b', null, 'Режим „linkedin“ (след одобрен API достъп): '), 'публикува сам — само одобрени постове и не по-късно от 6 часа след часа.'),
  h('li', null, h('b', null, 'Одобрението важи за точния текст. '), 'Всяка промяна по текста го отменя — одобрявате пак.'));

/**
 * Стъпките. `route` — изгледът; `target` — CSS селектор на елемента, който се осветява (или null);
 * `what` / `how` / `where` — трите въпроса; `extra` — допълнително съдържание (схема, пример).
 */
export const STEPS = [
  { chapter: 'Начало', route: '#/', target: null, title: 'Добре дошли в LinkedIn агента',
    what: 'Агент, който пише постовете за страницата на клъстера — от изречение или папка със снимки до проверен текст, който вие одобрявате.',
    how: 'С „Напред“ и „Назад“ (или стрелките ← →) се движите; точките отдолу прескачат към глава; „Пропусни“ затваря.',
    where: 'Туториалът се пуска сам само сега. После — от „Настройки“ → „Пусни туториала“.',
    extra: pathDiagram },
  { chapter: 'Начало', route: '#/', target: '#side-nav', title: 'Навигация',
    what: 'Петте места на агента и „Настройки“.',
    how: 'Клик за смяна. Числото до „Сигнали“ е броят отворени проблеми.',
    where: 'Лявата колона, най-горе.' },
  { chapter: 'Начало', route: '#/', target: '#side-lists', title: 'Постове, разговори, графици',
    what: 'Историята: всеки пост, всеки разговор с Асистента и всеки график.',
    how: 'Табовете превключват; търсенето е по заглавие. От менюто на реда — преименуване или изтриване (отива в кошче, нищо не се губи).',
    where: 'Лявата колона, под навигацията.' },

  { chapter: 'Преглед', route: '#/', target: '.ov-head', title: 'Преглед — всичко с един поглед',
    what: 'Заглавието казва дали всичко е наред; под него — кой е следващият пост и коя папка следи агентът.',
    how: 'Ако пише „N поста искат вашия поглед“ — те са в стъпката „За преглед“ по-долу.',
    where: '„Преглед“ в навигацията — това е началото.' },
  { chapter: 'Преглед', route: '#/', target: '.ov-head-actions', title: '„Качи папка“ и „Нов пост“',
    what: '„Качи папка“ — изберете папка от компютъра (или я плъзнете в прозореца): всяка подпапка става пост, а качването получава свое табло. „Нов пост“ отваря Асистента.',
    how: 'Агентът да следи папка постоянно и да взима новите подпапки сам — включва се в „Настройки“ → „Папка“.',
    where: 'Горе вдясно на „Преглед“.' },
  { chapter: 'Преглед', route: '#/', target: '.ov-steps', title: 'Пътят: пет стъпки',
    what: 'Входящи → Пише се → За преглед → Готови → Публикувани. Числото е колко поста има във всяка.',
    how: 'Клик върху стъпка показва постовете ѝ отдолу. Сама се отваря тази, която иска човек — „За преглед“, ако има нещо там.',
    where: 'Средата на „Преглед“.' },
  { chapter: 'Преглед', route: '#/', target: '.ov-list-wrap', title: 'Списъкът на стъпката',
    what: 'Широки редове: снимка, дата и час, заглавие и най-важната бележка (червено — спира поста, жълто — проверете, сиво — информация).',
    how: 'Клик върху реда отваря панела за поправка. Вдясно — едно действие („Опитай пак“, „Поправи датите“, „Одобри пак“, „Публикувах го“). В „Готови“ — „Одобри всички“.',
    where: 'Под стъпките.' },

  { chapter: 'Агентите', route: '#/', target: '.ov-status', title: 'Агентите — кой какво пише',
    what: '„Агент 1, 2, 3“ = до 3 поста едновременно. Всеки агент е цял екип, който минава поста през стъпките.',
    how: 'На всеки агент: кой пост пише и кой от екипа работи сега (напр. „Критик · проверява факти“). Клик → постът. До тях: опашка, готови и неуспешни днес, средно време на пост.',
    where: 'Под заглавието на „Преглед“. Колко агента — WEB_MAX_JOBS в .env.' },
  { chapter: 'Агентите', route: '#/', target: '.ov-team-toggle', title: 'Екипът във всеки агент',
    what: 'Кой член на екипа за какво отговаря и колко време отнема:',
    how: 'Целият път е ~2,5–4 минути на пост. Постът е „Готов“ едва след последната проверка.',
    where: 'Същото се отваря с „Кой какво прави“ до заглавието „Агентите“.',
    extra: teamTable },

  { chapter: 'Асистент', route: '#/asistent', target: '#view-assistant .composer', title: 'Асистентът — един вход за всичко',
    what: 'Разговор само за LinkedIn постовете на клъстера.',
    how: '① Опишете събитие с 1–2 изречения → пише поста веднага. ② Поискайте план („2 поста седмично през ноември“) → таблица, която поправяте → „Създай“. ③ „+“ — снимки, ZIP или файл с график.',
    where: '„Асистент“ в навигацията или „Нов пост или план“ на „Преглед“.' },
  { chapter: 'Асистент', route: '#/asistent', target: '#view-assistant .modes-slot', title: 'Дължина: Кадър, Фокус, Панорама',
    what: 'Кадър — 40–90 думи, покана или поздрав. Фокус — повечето постове. Панорама — 400–650 думи, история с много факти (иска богат бриф).',
    how: 'Изберете преди да изпратите. Агентът никога не избира вместо вас.',
    where: 'До полето за писане — тук и във всеки пост.' },
  { chapter: 'Асистент', route: '#/asistent', target: null, title: 'Добър бриф = добър пост',
    what: 'Агентът пише само от това, което му дадете, и не измисля.',
    how: 'Кой, какво, кога, къде, числа, имена с длъжност, линк. Каквото липсва, остава извън поста.',
    where: 'Важи за Асистента, за поста и за текста в папката.',
    extra: briefExample },

  { chapter: 'Пост', route: '#/nov', target: '#composer', title: 'Постът — разговор с агента',
    what: 'Всеки пост е отделен разговор: брифът, текстът, бележките от проверката и „Ход на работата“.',
    how: 'Пишете молби с обикновени думи: „по-кратко“, „махни второто изречение“, „добави линка…“ → лека редакция за ~30 s, без писане отначало.',
    where: 'Клик върху пост от лявата колона, „Преглед“ или „Календар“.' },
  { chapter: 'Пост', route: '#/nov', target: '#album-btn', title: 'Снимките',
    what: 'Снимките на поста — от папката на компютъра, с „+“ или с плъзгане навсякъде в прозореца.',
    how: 'Клик върху снимка я включва/изключва; със ★ решавате кои влизат в самия пост. Агентът гледа до 12.',
    where: 'Горе вдясно в поста — „От папката“.' },

  { chapter: 'Папка', route: '#/papka', target: '#view-library .view-head', title: 'Папката на компютъра',
    what: 'Агентът чете папката сам — без качване. Нищо в нея не се променя.',
    how: 'Два начина: „Качи папка“ — еднократно, от прозореца на Windows или с плъзгане, всяка подпапка става пост веднага. „Избери папка“ — агентът я следи постоянно (за „Взимай новите папки сами“). Подпапка със снимки и текст = пост; файл с график = много постове.',
    where: '„Папка“ в навигацията; „Качи папка“ има и на „Преглед“.' },
  { chapter: 'Папка', route: '#/papka', target: null, title: 'Най-ефективният формат на папката',
    what: '', how: '', where: '', wide: true,
    extra: folderFormat },

  { chapter: 'Календар', route: '#/kalendar', target: '.cal-headbar', title: 'Календар',
    what: 'Всички насрочени постове по месеци.',
    how: '„Месец“ / „Списък“; ← → сменят месеца; „Планирай…“ пита Асистента за план на месеца.',
    where: '„Календар“ в навигацията.' },
  { chapter: 'Календар', route: '#/kalendar', target: '.cal-layout', title: 'Насрочване с плъзгане',
    what: 'Цветът на поста е състоянието му (легендата е вдясно).',
    how: 'Плъзнете пост в ден, за да го насрочите; „+“ в деня — нов пост за този ден; „Без дата“ вдясно — още ненасрочени.',
    where: 'Средата на „Календар“.' },
  { chapter: 'Календар', route: '#/kalendar', target: null, title: 'Панелът за поправка',
    what: 'Отваря се с клик върху пост навсякъде — в „Преглед“, „Календар“, „Иска внимание“.',
    how: 'Ръчна редакция; „Помоли агента“; дата и час; „Одобри“; махни от графика; изтрий (в кошче).',
    where: 'Излиза отдясно, без да напускате изгледа.',
    extra: publishFlow },

  { chapter: 'Сигнали', route: '#/signali', target: '#view-alerts .view-head', title: 'Сигнали',
    what: 'Проблеми, които не бива да пропуснете: неуспешно писане, пропуснат час, риск в готов пост, грешка при публикуване.',
    how: 'Затварят се сами, щом е оправено, или с „Готово“. По желание идват и в Teams/Slack или имейл (.env); пробен сигнал — от „Настройки“.',
    where: '„Сигнали“ в навигацията; числото до него = отворени.' },

  { chapter: 'Настройки', route: '#/nastroiki', target: '.st-tour', title: 'Настройки',
    what: 'Всичко универсално на едно място: тема, папка и автоматично взимане, публикуване, сигнали, агентите и скоростта.',
    how: 'Оттук пускате този туториал пак — целия или само една глава.',
    where: '„Настройки“ — най-долу в навигацията.' },
  { chapter: 'Настройки', route: '#/', target: null, title: 'Готово!',
    what: 'Това е всичко. Няма да се показва повече само.',
    how: 'Започнете с „Нов пост или план“ или пуснете подпапка в папката си.',
    where: 'Ако забравите нещо — „Настройки“ → „Пусни туториала“.' },
];

export const CHAPTERS = [...new Set(STEPS.map((s) => s.chapter))];

// ------------------------------------------------------------------ състояние

const T = { i: -1, layer: null, spot: null, card: null, timer: null, target: null, onKey: null, onResize: null };

export const isOpen = () => T.i >= 0;

/** Пуска се сам само ако още не е минат или пропуснат. */
export function maybeAutoStart() {
  const saved = Number(sessionStorage.getItem(SESSION_KEY));
  const tutorial = store.info && store.info.tutorial;
  if (!store.info) return;
  if (tutorial && tutorial.done) return;
  // Сървър без `tutorial` в /api/info е стар процес (преди web-ui-5) — не може да запомни „минат",
  // затова не пускаме сами; иначе излиза при всяко презареждане (видяно на живо 2026-10-14).
  if (!tutorial) return;
  if (storage(DONE_KEY)) { syncDone(storage(DONE_KEY)); return; }
  start(Number.isInteger(saved) && saved > 0 && saved < STEPS.length ? saved : 0);
}

export function start(index = 0) {
  if (!T.layer) build();
  T.layer.hidden = false;
  document.body.classList.add('touring');
  go(Math.max(0, Math.min(STEPS.length - 1, index)));
}

export function startChapter(chapter) {
  start(Math.max(0, STEPS.findIndex((s) => s.chapter === chapter)));
}

function build() {
  T.spot = h('div', { class: 'tour-spot', 'aria-hidden': 'true' });
  T.card = h('section', { class: 'tour-card', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'tour-title' });
  T.layer = h('div', { class: 'tour', hidden: true }, h('div', { class: 'tour-block' }), T.spot, T.card);
  document.body.append(T.layer);
  T.onKey = (e) => {
    if (!isOpen()) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); skip(); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); next(); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); back(); }
  };
  document.addEventListener('keydown', T.onKey, true);
  T.onResize = () => place();
  window.addEventListener('resize', T.onResize);
}

function go(index) {
  T.i = index;
  try { sessionStorage.setItem(SESSION_KEY, String(index)); } catch (e) { /* частен режим */ }
  const step = STEPS[index];
  if (step.route && location.hash !== step.route && !(step.route === '#/' && (location.hash === '' || location.hash === '#'))) {
    location.hash = step.route;
  }
  renderCard(step);
  T.target = null;
  T.layer.classList.add('waiting');
  waitForTarget(step, Date.now());
}

// Изгледите се рисуват асинхронно (заявка към сървъра) — чакаме елемента до ~2,5 s.
function waitForTarget(step, startedAt) {
  clearTimeout(T.timer);
  if (STEPS[T.i] !== step) return;
  const node = step.target ? findTarget(step.target) : null;
  if (step.target && !node && Date.now() - startedAt < 2500) {
    T.timer = setTimeout(() => waitForTarget(step, startedAt), 120);
    return;
  }
  T.target = node;
  const inSidebar = node && node.closest('#sidebar');
  if (window.matchMedia('(max-width: 860px)').matches) {
    if (inSidebar) sidebar.open(); else sidebar.close();
  }
  if (node) {
    node.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    // scrollIntoView мести и колони със скрит хоризонтален скрол — връщаме лявата колона на място.
    for (const id of ['sidebar', 'side-lists']) { const el = document.getElementById(id); if (el) el.scrollLeft = 0; }
  }
  T.layer.classList.remove('waiting');
  place();
  T.timer = setTimeout(function track() {
    // Изгледите се опресняват сами (Преглед — на 2,5–8 s) и подменят елемента — следим го.
    if (STEPS[T.i] !== step) return;
    if (step.target) T.target = findTarget(step.target);
    place();
    T.timer = setTimeout(track, 400);
  }, 400);
}

function findTarget(selector) {
  const node = document.querySelector(selector);
  if (!node) return null;
  const rect = node.getBoundingClientRect();
  return rect.width && rect.height ? node : null;
}

// ------------------------------------------------------------------ рисуване

function renderCard(step) {
  const index = T.i;
  const last = index === STEPS.length - 1;
  const chapterIndex = CHAPTERS.indexOf(step.chapter);
  const rows = [['Какво', step.what], ['Как', step.how], ['Къде', step.where]].filter(([, text]) => text);
  T.card.classList.toggle('wide', Boolean(step.wide || step.extra === folderFormat));
  T.card.replaceChildren(
    h('header', { class: 'tour-head' },
      h('p', { class: 'tour-kicker' }, `${step.chapter} · стъпка ${index + 1} от ${STEPS.length}`),
      h('h2', { id: 'tour-title' }, step.title),
      h('button', { type: 'button', class: 'icon-btn tour-x', 'aria-label': 'Пропусни туториала', title: 'Пропусни (Esc)', onclick: skip }, icon('close'))),
    h('div', { class: 'tour-body' },
      rows.length ? h('dl', { class: 'tour-rows' }, rows.flatMap(([label, text]) => [h('dt', null, label), h('dd', null, text)])) : null,
      step.extra ? step.extra() : null),
    h('nav', { class: 'tour-chapters', 'aria-label': 'Глави' }, CHAPTERS.map((name, n) => h('button', {
      type: 'button',
      class: `tour-chip${n === chapterIndex ? ' current' : ''}${n < chapterIndex ? ' done' : ''}`,
      title: `Към „${name}“`,
      onclick: () => go(STEPS.findIndex((s) => s.chapter === name)),
    }, name))),
    h('div', { class: 'tour-progress', 'aria-hidden': 'true' }, progressFill(index)),
    h('footer', { class: 'tour-foot' },
      h('button', { type: 'button', class: 'link-btn tour-skip', onclick: skip }, 'Пропусни туториала'),
      h('span', { class: 'grow' }),
      h('button', { type: 'button', class: 'btn ghost small', disabled: index === 0 || null, onclick: back }, icon('chevL'), 'Назад'),
      h('button', { type: 'button', class: 'btn primary small tour-next', onclick: next }, last ? 'Готово' : 'Напред', last ? icon('check') : icon('chevR'))));
  const nextBtn = T.card.querySelector('.tour-next');
  if (nextBtn) setTimeout(() => nextBtn.focus({ preventScroll: true }), 0);
}

function progressFill(index) {
  const fill = h('i');
  fill.style.width = `${((index + 1) / STEPS.length) * 100}%`;
  return fill;
}

function place() {
  if (!isOpen() || !T.card) return;
  const node = T.target;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const narrow = vw < 720;
  T.layer.classList.toggle('no-target', !node);
  T.layer.classList.toggle('narrow', narrow);
  if (node) {
    const r = node.getBoundingClientRect();
    const pad = 6;
    const top = Math.max(4, r.top - pad);
    const left = Math.max(4, r.left - pad);
    T.spot.style.top = `${top}px`;
    T.spot.style.left = `${left}px`;
    T.spot.style.width = `${Math.min(vw - 8, r.right + pad) - left}px`;
    T.spot.style.height = `${Math.min(vh - 8, r.bottom + pad) - top}px`;
  }
  const card = T.card;
  if (narrow || !node) {
    card.style.top = '';
    card.style.left = '';
    return;
  }
  // Карта до елемента: вдясно, вляво, отдолу или отгоре — където има място; иначе в ъгъла.
  const r = node.getBoundingClientRect();
  const cw = card.offsetWidth;
  const ch = card.offsetHeight;
  const gap = 16;
  let left;
  let top;
  if (vw - r.right >= cw + gap * 2) { left = r.right + gap; top = r.top; }
  else if (r.left >= cw + gap * 2) { left = r.left - cw - gap; top = r.top; }
  else if (vh - r.bottom >= ch + gap * 2) { top = r.bottom + gap; left = r.left; }
  else if (r.top >= ch + gap * 2) { top = r.top - ch - gap; left = r.left; }
  else { left = vw - cw - gap; top = vh - ch - gap; }
  card.style.left = `${Math.max(gap, Math.min(vw - cw - gap, left))}px`;
  card.style.top = `${Math.max(gap, Math.min(vh - ch - gap, top))}px`;
}

// ------------------------------------------------------------------ движение и край

function next() {
  if (T.i >= STEPS.length - 1) {
    finish('finished');
    toast('Готово! Туториалът е в „Настройки“, ако ви потрябва пак.');
    return;
  }
  go(T.i + 1);
}

function back() {
  if (T.i > 0) go(T.i - 1);
}

function skip() {
  finish('skipped');
  toast('Затворено. Ако ви потрябва пак — „Настройки“ → „Пусни туториала“.');
}

async function finish(state) {
  const step = T.i;
  close();
  storage(DONE_KEY, state);
  try {
    const tutorial = await api('PUT', '/api/settings/tutorial', { state, step });
    if (store.info) store.info.tutorial = tutorial;
  } catch (e) {
    // Без съобщение: браузърът вече го помни (DONE_KEY) и няма да го пусне пак сам, а сървърът
    // ще го научи при следващото отваряне (syncDone). Човекът няма какво да направи с тази грешка.
  }
}

// Браузърът помни „минат", а сървърът — не (напр. записът не мина) → кажи му сега.
function syncDone(state) {
  api('PUT', '/api/settings/tutorial', { state: state === 'skipped' ? 'skipped' : 'finished' })
    .then((tutorial) => { if (store.info) store.info.tutorial = tutorial; })
    .catch(() => {});
}

/** „Нулирай" от Настройки — следващото отваряне го пуска пак сам. */
export async function reset() {
  storage(DONE_KEY, null);
  const tutorial = await api('PUT', '/api/settings/tutorial', { state: 'reset' });
  if (store.info) store.info.tutorial = tutorial;
  return tutorial;
}

export function close() {
  clearTimeout(T.timer);
  T.i = -1;
  T.target = null;
  try { sessionStorage.removeItem(SESSION_KEY); } catch (e) { /* частен режим */ }
  if (T.layer) T.layer.hidden = true;
  document.body.classList.remove('touring');
}
