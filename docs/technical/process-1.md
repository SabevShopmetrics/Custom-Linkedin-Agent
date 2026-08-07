# Технически процес v1 — LinkedIn агент на ИКТ Клъстер – Варна

**Реализира:** [research-plan-3-strategies.md](../research-plans/research-plan-3-strategies.md) (Стратегия 1) + [research-plan-6-strategy1-buildout.md](../research-plans/research-plan-6-strategy1-buildout.md)
**Език:** Python 3.12+
**Обхват на документа:** само техническия процес — модули, поток на данните, схема, endpoint-и, разгръщане. Бизнес обосновката е в референтните планове по-горе, не се повтаря тук.

---

## 1. Технологичен стек

| Слой | Избор | Защо (кратко, детайли в План 3/6) |
|---|---|---|
| Език | Python 3.12+ | Официален Playwright, зряла агентна екосистема, път към LoRA |
| Уеб слой | FastAPI | Панел за одобрение + вътрешни endpoint-и |
| База данни | SQLite + SQLAlchemy | Обемът не изисква сървърна БД |
| Планировчик | APScheduler | Cron тригери + събитийни тригери |
| Локален модел | Ollama (HTTP API) | Сървиращ слой за избрания модел от бейк-офа (Фаза 1.5) |
| Рендер на изображения | Playwright (headless Chromium) | HTML/SVG шаблон → PNG |
| Тестове | pytest | Особено policy/autonomy модулите |
| Контейнеризация | Docker + docker-compose | Един под, вътрешна мрежа localhost между компонентите |
| Версиониране на знанието | git (отделен репо или subtree за `knowledge/`) | Проследимост на промените в гласа/правилата |

---

## 2. Структура на кодовата база

```
linkedin-agent/
├── knowledge/                       # версионирано в git, вижте План 3 Част III
│   ├── identity/  voice/  corpus/  members/  visual/  calendar/  strategy/
│   └── strategy/proposals/          # НОВО — опашка от "сънуване" (План 6, Част III)
│
├── src/
│   ├── core/
│   │   ├── db.py                    # SQLAlchemy engine, session factory
│   │   ├── models.py                # ORM модели — виж §4
│   │   ├── config.py                # env-базирана конфигурация
│   │   └── audit.py                 # единен одитен журнал — всичко минава оттук
│   │
│   ├── llm/
│   │   ├── base.py                  # интерфейс LLMProvider
│   │   ├── ollama.py                # локален модел, по подразбиране
│   │   ├── openai_compat.py         # резервен адаптер за API модел (информативно, План 6 §V)
│   │   └── eval/                    # бейк-оф харнес — виж §7
│   │
│   ├── collectors/
│   │   ├── site_news.py
│   │   ├── calendar.py
│   │   ├── pillars.py
│   │   └── members.py
│   │
│   ├── input/                       # НОВО (План 6, Част II)
│   │   └── brief.py                 # ръчен бриф → нормализиран Source обект
│   │
│   ├── writer/
│   │   ├── generator.py             # извиква llm/, произвежда варианти
│   │   ├── retriever.py             # подбор на примери от corpus/
│   │   ├── factcheck.py             # разграничава стилов/фактически пропуск (общо за collectors и brief)
│   │   └── templates/
│   │
│   ├── visual/
│   │   ├── renderer.py              # Playwright рендер
│   │   └── templates/
│   │
│   ├── policy/
│   │   ├── rules.py                 # детерминистични проверки
│   │   ├── classifier.py            # LLM класификатор (малък модел)
│   │   ├── autonomy.py              # 3 практични нива (План 6 §IV) + протоколи за промяна
│   │   └── guards.py                # kill switch, тавани, circuit breaker
│   │
│   ├── approval/
│   │   ├── api.py                   # FastAPI рутери
│   │   ├── ui/                      # панел: опашка + избирач на ниво (План 6 §I)
│   │   ├── notify.py                # имейл с подписан линк
│   │   └── tokens.py
│   │
│   ├── reflect/                     # НОВО (План 6, Част III — "сънуване")
│   │   ├── trigger.py                # задейства се при нови метрики, не на таймер
│   │   ├── score.py                  # оценка спрямо рубрика
│   │   ├── propose.py                # записва в knowledge/strategy/proposals/
│   │   └── apply.py                  # прилага само след одобрение
│   │
│   ├── publisher/
│   │   ├── base.py
│   │   ├── linkedin.py
│   │   └── draft.py                  # Фаза 0/2 преди LinkedIn одобрение
│   │
│   ├── analytics/
│   │   ├── collector.py              # сваля метрики +7 дни
│   │   └── report.py                 # месечен отчет
│   │
│   └── scheduler.py                  # регистрира всички cron/event тригери
│
├── tests/
│   ├── test_policy.py                # най-висок приоритет за покритие
│   ├── test_autonomy.py
│   ├── test_factcheck.py
│   └── ...
│
├── docker-compose.yml
├── pyproject.toml
└── Makefile
```

---

## 3. Поток на данните — жизнен цикъл на един пост

```
Вход (един от три канала, влизат еднакво в опашката):
  A) collectors/*  — автоматично откритие
  B) input/brief.py — ръчен бриф от панела
  C) reflect/propose.py — прието предложение от "сънуването" (рядко, само за промяна в стратегията, не директно пост)
        │
        ▼
[status: NEW] запис в БД
        │
        ▼
writer/factcheck.py — за B: разделя стилов/фактически пропуск
  ├─ стилов пропуск → допълва от knowledge/voice, knowledge/templates
  └─ фактически пропуск → липсва в knowledge/ → [status: NEEDS_INPUT], въпрос към подателя
        │
        ▼
writer/retriever.py — избира 5–8 релевантни примера от corpus/
        │
        ▼
writer/generator.py — 2–3 варианта чрез llm/ollama.py
        │
        ▼
visual/renderer.py — изображение по шаблон
        │
        ▼
policy/rules.py → policy/classifier.py — детерминистични + LLM проверка
        │
        ▼
policy/autonomy.py — чете текущото ниво за типа съдържание
        │
   ┌────┼────────────────┐
   ▼    ▼                ▼
 Одобрение          Възражение          Автономно
[PENDING_APPROVAL]  [SCHEDULED_VETO]    директно към publisher
   │  извести          │ извести,
   │  чака "да"        │ чака вето 48ч
   └────────┬───────────┘
            ▼
   publisher/linkedin.py — публикува, пази URN
            │
            ▼
   [PUBLISHED] +7 дни → analytics/collector.py сваля метрики
            │
            ▼
   [MEASURED] → reflect/trigger.py се задейства (не на таймер)
            │
            ▼
   reflect/score.py + propose.py → knowledge/strategy/proposals/{id}.md
            │
            ▼
   Одобрение в панела → reflect/apply.py пренаписва knowledge/strategy/*
                          (никога автоматично, План 6 §III)
```

Всеки преход по веригата се записва в `core/audit.py` — тип на прехода, timestamp, кой (система/човек), версия на промпта и на knowledge/ commit-а.

---

## 4. Схема на базата данни (основни таблици)

| Таблица | Ключови колони | Бележка |
|---|---|---|
| `sources` | id, type (`collector`/`brief`/`reflect`), payload_json, created_at | Единен вход независимо от канала |
| `posts` | id, source_id, status, content_type, text, image_path, variant_group_id | `status` следва жизнения цикъл от §3 |
| `autonomy_levels` | content_type (PK), level, updated_by, updated_at | Чете се от `policy/autonomy.py`; историята на промените е в `audit_log`, не тук |
| `approvals` | post_id, approver, decision, decided_at, veto_deadline | Покрива и „Одобрение", и „Възражение" |
| `metrics` | post_id, impressions, reactions, comments, shares, clicks, measured_at | Пълни се от `analytics/collector.py` |
| `proposals` | id, category, diff_before, diff_after, rationale, confidence, status | Опашката от „сънуването"; `status`: pending/approved/rejected |
| `audit_log` | id, entity_type, entity_id, action, actor, before, after, at | Единен, неделим журнал — всичко минава оттук, включително promo/demote на ниво |
| `members` | зареден от `knowledge/members/members.yaml`, кеширан в БД за бързо търсене | Синхронизира се при промяна на YAML файла |

---

## 5. Вътрешни API endpoint-и (FastAPI, `approval/api.py`)

| Метод | Път | Действие |
|---|---|---|
| `GET` | `/queue` | Постове, чакащи одобрение или вето |
| `POST` | `/posts/{id}/approve` | Одобрява PENDING_APPROVAL пост |
| `POST` | `/posts/{id}/veto` | Спира SCHEDULED_VETO пост в прозореца |
| `POST` | `/brief` | Приема ръчен бриф (Част II, План 6) — тяло: `{text: str}` |
| `GET` | `/autonomy` | Текущи нива по тип съдържание |
| `PUT` | `/autonomy/{content_type}` | Смяна на ниво — избирачът от панела (Част I, План 6) |
| `GET` | `/proposals` | Чакащи предложения от „сънуването" |
| `POST` | `/proposals/{id}/approve` | Прилага предложение в `knowledge/strategy/` |
| `POST` | `/proposals/{id}/reject` | Архивира без прилагане |
| `GET` | `/healthz` | Здравна проверка — за kill switch / мониторинг |

Достъп: през подписан линк (имейл) или VPN тунел до сървъра — виж План 2/3 за избора на канал; не се преповтаря тук, само отбелязва, че панелът трябва да е достъпен извън офис мрежата.

---

## 6. Планиране на задачите (`scheduler.py`)

| Тригер | Тип | Честота |
|---|---|---|
| Collectors (сайт, календар, рубрики) | cron | всяка сутрин, 06:00 |
| Проверка на изтекли вета | cron | на всеки час |
| Свалени метрики → `reflect/trigger.py` | **събитийно**, не cron | при пристигане на нов запис в `metrics` |
| Лек преглед | cron | седмично |
| Задълбочен месечен отчет | cron | 1-во число |
| Здравна проверка + kill switch | cron | на всеки 6 часа |
| Проверка на изтичащ OAuth токен | cron | ежедневно, аларма при <30 дни |

Събитийното задействане на reflect е съзнателно — виж обосновката в План 6 §III защо не е денонощен цикъл.

---

## 7. Бейк-оф на модела (`llm/eval/`)

Малък самостоятелен harness, отделен от продукционния път:

```
llm/eval/
├── dataset.py       # зарежда 20-те задачи от вашия корпус (План 3, Фаза 1.5)
├── run.py           # пуска всеки кандидат модел през dataset
└── score.py         # сляпо оценяване по 6-те критерия от План 3
```

Извежда таблица с резултати; изборът на модел за продукция се записва като конфигурационна стойност (`config.py: LLM_MODEL_NAME`), не се хардкодва в кода.

---

## 8. Разгръщане

```
docker-compose.yml
├── app          — FastAPI + scheduler в един процес (Strategy 1 = единна система)
├── ollama       — сървиращ слой за модела
└── (volume) knowledge/, data.db, logs/
```

Един под, без нужда от оркестрация между отделни сървиси — съответства на решението „Единна система" (План 3/6). Бекъп: ежедневен snapshot на `data.db` + `knowledge/` (последното вече е в git, така че основният бекъп е самият repo).

---

## 9. Тестова стратегия

Приоритет по риск, не по покритие:

1. `policy/autonomy.py`, `policy/rules.py` — грешка тук значи публикуван пост, който е трябвало да чака одобрение. Пълно unit покритие, включително всички гранични случаи от протоколите за повишаване/понижаване (План 3, Част V).
2. `writer/factcheck.py` — стилов срещу фактически пропуск, особено за бриф входа.
3. `reflect/propose.py` / `apply.py` — гарантира, че `apply` никога не се вика без одобрен статус на предложението.
4. Останалото — стандартно unit + интеграционно покритие, по-нисък приоритет.

---

## Какво остава извън обхвата на този документ

Бизнес обосновка, ценообразуване, регулаторни изисквания (AI Act, GDPR), пътна карта по фази — всичко това е в План 3 и План 6. Този документ се актуализира само когато техническият процес се промени; решенията „защо" остават в референтните планове.
