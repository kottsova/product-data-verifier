# Product Data Verifier — Project Roadmap

## Цель проекта

Создать Product Data Verifier — систему, которая по бренду/модели товара:

1. определяет точный товар;
2. находит релевантные источники;
3. извлекает характеристики из HTML, structured data, PDF/manual;
4. нормализует атрибуты;
5. сопоставляет их со схемой категории;
6. выполняет targeted search по пробелам;
7. разрешает конфликты и оценивает достоверность;
8. формирует FinalProductProfile с provenance/evidence;
9. отдаёт результат пользователю через Telegram-бота.

Ключевой принцип: не выдумывать отсутствующие данные и не повышать authority без доказательств.

## Текущий pipeline

`Product input → identity → discovery → relevance/ranking → fetch → extraction → category detection → schema → mapping → gap detection → targeted search → validation/conflict resolution → FinalProductProfile → JSON/CSV/Telegram`

# Завершённые этапы

## Stage 1–7 — Core Pipeline
Статус: DONE

Реализованы базовые компоненты:
- product identity;
- discovery;
- fetch/extraction;
- category detection;
- schema;
- mapping;
- gap handling / targeted search;
- validation / conflict resolution;
- FinalProductProfile;
- JSON/CSV export.

## Stage 8 — Application Orchestration
Статус: DONE

Собран единый workflow:
`ProductWorkflowRequest → ... → FinalProductProfile`

Commit:
`aeb428f Implement Stage 8 application orchestration`

## Stage 8.1 — Discovery Provider Resilience
Статус: DONE

Добавлено:
- fallback search providers;
- continuation exact-model search after blocked bootstrap;
- provider provenance;
- отсутствие authority promotion через fallback.

## Stage 8.2 — Discovery Relevance Guardrails
Статус: DONE

Добавлено:
- exact / likely_variant / weak / reject;
- wrong-model filtering;
- brand-only filtering;
- person/sports/wiki/forum filtering;
- accessory/replacement-part filtering;
- fetch guard.

## Stage 8.3 — Coverage Gap Diagnosis
Статус: DONE

Добавлены diagnostics:
- attribute coverage;
- loss classification;
- цепочка source → selected → fetched → extracted → mapped → targeted → validated → final profile.

Commit Stage 8.1–8.3:
`43583f1 Implement discovery resilience and coverage diagnostics`

## Stage 8.4 — Structured / Dynamic Extraction
Статус: DONE

Добавлено generic extraction из:
- structured HTML;
- data-value;
- JSON-LD;
- embedded JSON;
- application state.

HONOR X8d:
- category: smartphone/high;
- coverage вырос до ~43–47%.

## Stage 8.5 — Playwright Lifecycle
Статус: DONE

Исправлен lifecycle conflict между discovery и fetch.

Commit Stage 8.4–8.5:
`6639a32 Improve structured extraction and Playwright lifecycle`

## Stage 8.6 — Multilingual Mapping
Статус: DONE

Добавлено:
- multilingual aliases;
- Unicode/case normalization;
- ё→е;
- unit suffix normalization;
- numbered-label fallback;
- safe alias resolution.

Commit:
`013508b Improve multilingual schema mapping`

## Stage 8.7 — PDF / Manual Specification Extraction
Статус: DONE

Добавлено:
- `pdf_spec` extraction;
- page/section provenance;
- technical-block detection;
- noise filtering;
- Cyrillic units;
- scoped capacity preservation.

Commit:
`0fb8051 Improve PDF specification extraction`

## Stage 8.8 — Compound Model / Targeted Source Selection
Статус: DONE

Исправлено:
- compound model matching;
- exact model ranking;
- targeted selection для моделей вида `G12 Pro HHR32A`.

Dreame coverage вырос примерно:
`30.8% → 53.8%`

Commit:
`496d4a1 Improve targeted source selection for exact product identity`

# Текущий quality baseline

Live coverage плавает из-за сети и блокировок источников.

| Product | Category | Coverage |
|---|---|---:|
| Bosch PUE611BB5E | cooktop | ~61–69% |
| HONOR X8d | smartphone | ~43–47% |
| Janome Sakura 95 | sewing_machine | обычно ~50%+ |
| Gressel GAF-1825 | air_fryer | ~20% |
| Dreame G12 Pro HHR32A | wet_dry_vacuum | ~54% |

Coverage не является единственной метрикой качества: отдельно учитывать Confirmed / Unresolved / Conflict и authority источников.

# Следующие этапы

## Stage 9 — Quality Gate & Result Policy
Статус: NEXT

Цель: зафиксировать, когда профиль можно считать достаточным для выдачи пользователю.

Нужно определить:
- minimum coverage;
- minimum confirmed fields;
- допустимую долю unresolved;
- handling conflicts;
- minimum source quality;
- critical attributes per category;
- итоговый profile quality status.

Предлагаемые статусы:
- `verified`
- `partial`
- `insufficient`
- `conflicted`

Acceptance:
- deterministic policy;
- отдельные unit tests;
- policy не меняет сами факты;
- profile получает агрегированный quality verdict.

## Stage 10 — Stable Application Service Boundary

Цель: отделить Telegram/UI от внутренних модулей pipeline.

Нужно:
- `ProductVerificationService`;
- стабильный request/response contract;
- progress states;
- typed errors;
- timeout/cancellation boundary;
- конфигурация market/max_sources/targeted_search;
- serializable service response.

Acceptance:
- UI не импортирует внутренние Stage напрямую;
- service можно вызвать из CLI, Telegram и будущего API;
- deterministic integration tests.

## Stage 11 — Cache & Persistence

Цель: не прогонять один и тот же товар заново без необходимости.

Минимальный MVP:
- SQLite.

Хранить:
- normalized product identity;
- FinalProductProfile;
- source provenance;
- timestamps;
- quality verdict;
- workflow version.

Политика:
- TTL;
- force refresh;
- stale result;
- version invalidation.

Acceptance:
- повторный запрос может обслуживаться из cache;
- provenance не теряется;
- force-refresh работает.

## Stage 12 — Telegram Bot MVP

Цель: первый пользовательский интерфейс.

Flow:
1. пользователь отправляет бренд + модель;
2. бот подтверждает запуск;
3. workflow выполняется;
4. бот возвращает результат.

Ответ должен показывать:
- товар;
- категорию;
- основные характеристики;
- quality status;
- Confirmed / Unresolved;
- conflicts;
- источники;
- coverage;
- предупреждение при недостаточных данных.

Нужно:
- `/start`
- `/help`
- обычный текстовый запрос;
- обработка ошибок;
- форматирование длинных профилей.

Acceptance:
- 5 regression-товаров проходят через Telegram end-to-end;
- бот не падает на blocked/unknown;
- сообщение укладывается в лимиты Telegram.

## Stage 13 — Telegram UX & Async Job Handling

Цель: нормальный UX для долгих workflow.

Нужно:
- progress/status messages;
- background job execution;
- timeout handling;
- cancellation;
- duplicate request suppression;
- retry;
- result pagination / sections.

Пример состояний:
`Ищу источники → Проверяю модель → Извлекаю характеристики → Проверяю данные → Готово`

Acceptance:
- Telegram handler не блокируется на несколько минут;
- повторный запрос не запускает бессмысленный duplicate workflow;
- ошибки объясняются пользователю.

## Stage 14 — Configuration & Secrets

Цель: убрать настройки из кода.

Нужно:
- environment config;
- Telegram bot token;
- provider configuration;
- timeouts;
- source limits;
- cache path;
- logging level.

Правила:
- secrets не хранятся в Git;
- `.env.example`;
- startup validation.

Acceptance:
- fresh clone можно настроить без правки Python-кода.

## Stage 15 — Observability & Operations

Нужно:
- structured logs;
- workflow ID;
- latency;
- provider/fetch failure counts;
- coverage metrics;
- quality verdict metrics;
- exception logging.

Опционально:
- simple admin diagnostics;
- health check.

Acceptance:
можно понять, почему конкретный запрос завершился poorly, без ручного дебага всего pipeline.

## Stage 16 — Deployment

MVP deployment:
- один persistent service;
- Telegram polling или webhook;
- SQLite volume;
- restart policy.

Варианты:
- VPS;
- Render/Fly/Railway/аналогичный Python hosting;
- Docker.

Нужно:
- Dockerfile;
- startup command;
- persistent storage;
- health/restart.

Acceptance:
бот работает независимо от локального ПК.

## Stage 17 — Final MVP Regression

Финальный набор:
- Bosch PUE611BB5E
- HONOR X8d
- Janome Sakura 95
- Gressel GAF-1825
- Dreame G12 Pro HHR32A
- дополнительные товары вне исходной пятёрки.

Проверяем:
- identity;
- discovery;
- extraction;
- mapping;
- validation;
- quality verdict;
- cache;
- Telegram UX;
- failure modes.

Обязательно:
- wrong model;
- unknown product;
- blocked discovery;
- fetch blocked;
- conflicting sources;
- low coverage;
- repeated request/cache hit.

MVP считается готовым после PASS финальной regression matrix.

# После MVP

## Stage 18 — Category Expansion

Добавлять новые категории через:
- schema;
- aliases;
- critical fields;
- regression products.

Без переписывания pipeline.

## Stage 19 — Provider Hardening

При необходимости:
- более стабильные search APIs;
- provider scoring;
- retries/backoff;
- provider health;
- paid provider option.

## Stage 20 — Product/API Expansion

Возможные направления:
- REST API;
- batch verification;
- CSV upload;
- web UI;
- marketplace integrations;
- comparison mode;
- export to catalog/PIM.

# Инженерные правила проекта

1. Не добавлять product-specific hardcode.
2. Не выдумывать значения.
3. Не повышать authority без evidence.
4. Wrong-model источник не должен влиять на профиль.
5. Unknown attributes сохраняются, а не теряются.
6. Provenance обязателен.
7. Каждый Stage отвечает только за свою задачу.
8. Live-network нестабильность отделять от deterministic regressions.
9. После каждого Stage: tests → live regression → `git diff --check` → review → commit → push.
10. `.vscode/`, credentials, tokens, secrets не коммитить.

# Рабочий процесс AI

Перед каждым новым промптом для coding-agent указывать:
- **Модель**
- **Режим / effort**

Текущий default для сложных Stage:
- Claude Sonnet 5 — High
- Codex — второй coding/review контур по необходимости.

# Ближайший шаг

**Stage 9 — Quality Gate & Result Policy**
