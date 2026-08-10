# LinkedIn Agent — ИКТ Клъстер Варна

Скелет на агент, който превръща бриф + снимки в готов LinkedIn пост на български.
**Първа стъпка** от проекта — виж [CLAUDE.md](CLAUDE.md),
[docs/prompts/agent-bootstrap-prompt.md](docs/prompts/agent-bootstrap-prompt.md),
[docs/prompts/followup-prompt-2-knowledge-critic.md](docs/prompts/followup-prompt-2-knowledge-critic.md) и
[docs/prompts/followup-prompt-3-gdpr-warnings-and-logging.md](docs/prompts/followup-prompt-3-gdpr-warnings-and-logging.md)
за пълния контекст и решенията, довели до тази структура. Публикуването в LinkedIn **не е част**
от тази фаза.

## Какво прави

Човек описва повод в `briefing.md` (или го подава директно с `--brief "текст"`, без нужда от
файл) и слага снимки в `photos/`. Агентът минава през шест стъпки — Планер → Писател +
Media Picker (паралелно) → Критик → Ревизия (до 2 опита, при нужда) → Консолидатор — и оставя
готов текст в `output/post.md`, готов за ръчен преглед.

Всяка стъпка пише файл на диска. Няма база данни — състоянието е файловата система, което дава
възобновяемост: ако конвейерът спре по средата, следващото пускане продължава оттам, без да
преизчислява вече свършеното. Всяко пускане пише и хронологичен лог в `posts/<име>/run.log`.

Критикът прави три отделни проверки срещу `knowledge/` — факти (срещу брифа + `facts.yaml` +
`members.yaml`), тон (срещу `voice/`) и правила (срещу `rules/`). Нарушение на тон/факти е повод
за до 2 автоматични опита за пренаписване (Писателят получава директно препоръката на Критика);
нарушение на `rules/forbidden.md` е твърд отказ — спира директно за човешки преглед, без опит за
пренаписване. GDPR находки (споменаване на организация без потвърдено съгласие, снимка с вероятно
непълнолетни, лично име извън брифа) са вградени в кода на Критика, но **не спират конвейера** —
инструментът е за вътрешния маркетинг екип, не за клиенти, и се показват само като предупреждения
(в `critique.md`, `output/meta.json`, `run.log` и в конзолата).

## Инсталация

```bash
python -m venv .venv
.venv/Scripts/activate     # Windows; на Linux/Mac: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env       # после попълнете OPENWEBUI_API_KEY и моделите
```

## Употреба

```bash
# провери връзката с Open WebUI и че всички модели отговарят
python -m src.cli check

# покажи кои knowledge/ файлове липсват/са празни и кои полета във facts.yaml са null
python -m src.cli check-knowledge

# създай нова папка за пост от шаблона
python -m src.cli new 2026-07-praktiki
# → редактирайте posts/2026-07-praktiki/briefing.md, сложете снимки в photos/

# генерирай поста
python -m src.cli generate posts/2026-07-praktiki

# преизчисли всички стъпки наново, независимо кои файлове вече съществуват
python -m src.cli generate posts/2026-07-praktiki --force
```

## Структура

```
knowledge/
  identity/      # about.md, history.md (проза) + facts.yaml (машинно проверими факти)
  voice/         # tone.md, formats.md, vocabulary.md
  rules/         # forbidden.md — твърд отказ; gdpr.md — предупреждение, не спира; legal.md
  strategy/      # goals.md, audiences.md, pillars.md
  members/       # members.yaml — mention_allowed: true е единственото разрешение за споменаване
  examples/      # good/ (еталонни постове) и bad/ (отхвърлени + причина)
posts/<име>/     # един пост = една папка; briefing.md + photos/ на вход,
                 # plan.json / post_i.md / media.json / critique.md / post_ii.md
                 # / output/ на изход
src/
  config.py      # чете .env
  errors.py      # всички грешки — всяка носи ясно съобщение какво да направи човек
  cli.py         # generate / new / check / check-knowledge
  logging_utils.py  # setup_run_logger() — пише posts/<име>/run.log
  llm/           # HTTP клиент към Open WebUI, роля→модел, валидиране на JSON изход
  brand/         # зарежда knowledge/ (loader.py) + одит за check-knowledge
  pipeline/      # шестте стъпки + оркестраторът (runner.py)
tests/
```

## Модели

Мапингът роля → модел е изцяло в `.env` (виж `.env.example`), не е хардкоднат в кода.
Виж таблицата в [docs/prompts/agent-bootstrap-prompt.md](docs/prompts/agent-bootstrap-prompt.md) за
кой модел за коя роля и защо. Точните идентификатори на моделите се потвърждават в Open WebUI
преди първо реално пускане — стойностите в `.env.example` са ориентировъчни.

## Обработка на грешки

Всяка грешка в `src/errors.py` носи `human_message` — изречение, което казва какво точно да
провери или направи човекът, не суров HTTP код или traceback. Частичен резултат никога не се
трие — ако конвейерът спре на стъпка 4, файловете от стъпки 1–3 остават на диска.

## Тестове

```bash
pytest
```

Фокусът на тестовете е върху обработката на грешки: retry/fallback логиката на LLM клиента,
поправящия retry при невалиден JSON, автоматичното свиване на снимки, skip-логиката за
възобновяемост, зареждането на knowledge/ (включително null-семантиката на facts.yaml), и
трите присъди на Критика (ok / needs_revision / hard_reject) — включително че GDPR находки
остават предупреждения, докато rules/forbidden.md остава твърд отказ, и че код-нивото
enforcement коригира verdict в двете посоки (не само ескалира, но и понижава, ако моделът
самò е сгрешил присъдата). Няма реални мрежови извиквания в тестовете.

## Състояние на knowledge/

`voice/tone.md`, `voice/formats.md`, `identity/facts.yaml` (секция `programs`), `members/members.yaml`
и `examples/good/` вече са попълнени с реални данни, изведени от 12 действителни поста и
публичния списък членове на клъстера — виж
[docs/prompts/followup-prompt-2-knowledge-critic.md](docs/prompts/followup-prompt-2-knowledge-critic.md),
раздел 8. Всичко останало (`identity/about.md`, `identity/history.md`, `voice/vocabulary.md`,
`rules/*`, `strategy/*`, `examples/bad/`, повечето полета в `facts.yaml`) е още с `TODO` маркер —
`python -m src.cli check-knowledge` показва точно какво липсва.

## Съзнателно извън обхвата (засега)

Публикуване в LinkedIn, уеб интерфейс, база данни, автоматично откриване на теми, Nextcloud
интеграция, генериране на изображения, cron/планировчик. Виж
[docs/prompts/agent-bootstrap-prompt.md](docs/prompts/agent-bootstrap-prompt.md) за пълния списък.
