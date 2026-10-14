/*
 * „Ход на работата" — подробните логове на едно пускане.
 *
 * Събитията идват от работния процес (src/web/worker.py): стъпки, редове от run.log и всяко
 * извикване към модела с време, повторни опити и таймаути. Целта е да се вижда КЪДЕ отива
 * времето — коя стъпка, кой модел, с колко снимки, дали чака отговор или опитва пак.
 */
import { h, fmtTime, plural } from './core.js';

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
const FIRST_STEP = { repick: 'Избира снимки', revise: 'Редактира поста', generate: 'Подготовка' };
const shortModel = (model) => (model || '?').split('/').pop();

export function summarizeRun(events, now, running, kind) {
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

export function timelineView(events, now, running, kind) {
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
    const details = step.lines.slice(1); // първият ред е заглавието на стъпката
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
