/*
 * Избор на режим (Кадър / Фокус / Панорама) с обяснение под него (web-ui-3).
 *
 * Нищо не е зашито тук: описанието идва от src/pipeline/modes.py (best_for), диапазонът от
 * думи — от modes.py или направо от knowledge/voice/tone.md, а „при вас" — от реалните
 * постове в този режим (src/web/insights.py). Промяна в който и да е източник се вижда
 * при следващото опресняване на /api/info, без да се пипа браузърният код.
 */
import { h } from './core.js';
import { modeList } from './store.js';

/** Визуална скала за дължината: колко от 3000-те знака на LinkedIn заема режимът. */
function lengthBar(mode) {
  const max = 650; // най-дългият режим — горният край на скалата
  const bar = h('span', { class: 'mode-scale', 'aria-hidden': 'true' }, h('i'));
  const low = mode.words_min || 0;
  const high = mode.words_max || max;
  bar.firstChild.style.left = `${(low / max) * 100}%`;
  bar.firstChild.style.width = `${Math.max(6, ((high - low) / max) * 100)}%`;
  return bar;
}

function describe(mode) {
  const own = mode.used
    ? `При вас: ${mode.used} ${mode.used === 1 ? 'пост' : 'поста'}, средно ${mode.avg_words} думи.`
    : 'Още нямате пост в този режим.';
  return h('div', { class: 'mode-desc' },
    h('div', { class: 'mode-desc-top' },
      h('b', null, mode.label), h('span', { class: 'mode-words' }, mode.hint), lengthBar(mode)),
    h('p', null, mode.best_for || ''),
    h('p', { class: 'mode-own' }, own,
      mode.sample ? h('span', { class: 'mode-sample' }, ` Последният: „${mode.sample}“`) : null));
}

/**
 * Сегментиран избор + описание на избрания режим. `compact` — само бутоните и един ред
 * (за полето за писане); пълното описание се показва при задържане/фокус върху бутон.
 */
export function modePicker(current, onPick, { compact = false } = {}) {
  const modes = modeList();
  const wrap = h('div', { class: `mode-picker${compact ? ' compact' : ''}` });
  const seg = h('div', { class: 'seg small', role: 'radiogroup', 'aria-label': 'Дължина на поста' });
  const line = h('div', { class: 'mode-line', 'aria-live': 'polite' });
  const show = (key) => {
    const mode = modes.find((m) => m.key === key);
    if (!mode) { line.replaceChildren(); return; }
    line.replaceChildren(compact
      ? h('span', null, h('b', null, mode.hint), ` — ${mode.best_for || ''}`)
      : describe(mode));
  };
  for (const mode of modes) {
    seg.appendChild(h('button', {
      type: 'button', role: 'radio', 'aria-checked': String(mode.key === current),
      onclick: () => onPick(mode.key),
      onmouseenter: () => show(mode.key),
      onmouseleave: () => show(current),
      onfocus: () => show(mode.key),
      onblur: () => show(current),
    }, mode.label));
  }
  show(current);
  wrap.append(seg, line);
  return wrap;
}

export { describe as describeMode };
