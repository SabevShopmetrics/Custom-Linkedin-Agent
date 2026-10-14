/*
 * Къде отиват файловете, които човекът пусне или избере с „+" — където и да е в интерфейса.
 *
 *   график (.xlsx .csv .tsv .txt .md .json) → преглед на графика (#/grafik), после „Напиши N поста"
 *   снимки и ZIP                            → текущия пост, или нов пост (с написания текст)
 *
 * Едно място за решението — лентата на асистента, „+" в чата и пускането върху прозореца
 * ползват една и съща функция.
 */
import { storage, toast } from './core.js';
import { store } from './store.js';
import * as chat from './chat.js';
import * as board from './board.js';
import * as inbox from './inbox.js';

const SCHEDULE = /\.(xlsx|csv|tsv|txt|md|json)$/i;
export const ACCEPT = 'image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp,.jfif,.zip,application/zip,.xlsx,.csv,.tsv,.txt,.md,.json';

const nextTask = () => new Promise((resolve) => setTimeout(resolve, 0));

/** `text` — каквото човекът вече е написал (напр. в лентата на асистента); отива в новия пост. */
export async function routeFiles(list, { text = '' } = {}) {
  const files = Array.from(list || []).filter((f) => f.size > 0);
  if (!files.length) return;
  // Плъзната папка с подпапки (снимки + текст) → всяка подпапка е пост (web-ui-5, inbox.js).
  if (inbox.groupFolders(files)) {
    await inbox.importFiles(files);
    return;
  }
  const schedules = files.filter((f) => SCHEDULE.test(f.name));
  const media = files.filter((f) => !SCHEDULE.test(f.name));
  if (schedules.length > 1) toast(`Чета само първия график — „${schedules[0].name}“.`);

  if (media.length) {
    if (store.route.name !== 'post') {
      if (text.trim()) storage('draft:new', text);
      location.hash = '#/nov';
      await nextTask(); // изгледът „Нов пост" се показва, преди да се създаде постът
    }
    await chat.handleFiles(media);
  }
  if (schedules.length) {
    if (!media.length) location.hash = '#/grafik';
    await board.importFile(schedules[0]);
    if (media.length) toast(`Графикът „${schedules[0].name}“ е прочетен — вижте „Графици“ → „Качи график“.`);
  }
}
