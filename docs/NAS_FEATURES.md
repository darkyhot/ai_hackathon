# Ветка `nas_features`: все доработки и как слить их с `main`

Документ для человека или нейросети, которая будет объединять ветки. Здесь — что сделано, где лежит,
какие контракты нельзя сломать, где ждать конфликтов и как проверить результат.

- База ветки: `main` на коммите `f81c53b` («feat: add seller simulation and advertisement sources»).
  На 29.10.2026 `main` после этого не менялся — ветка сливается в него без конфликтов (fast-forward).
  Если к моменту слияния в `main` появятся чужие изменения, ниже — карта конфликтных мест.
- Автор доработок — Никита (GitHub `alndis`). Объём: ~60 файлов, +6 000 строк, 97 тестов (в `main` было 18).
- Связанные документы: [ARCHITECTURE.md](ARCHITECTURE.md) (схема и API), [ERD.md](ERD.md) (база),
  [AGENT_PROMPT.md](AGENT_PROMPT.md) (единый промпт агента), [NAS_PLAN.md](NAS_PLAN.md) (план и итоги первых дней).

## Принципы, которые держит вся ветка

1. **Числа считает код, LLM только разговаривает.** Ставки, платежи, справедливая цена, рамки торга, уверенность
   фактов — в Python. Модель разбирает текст и формулирует реплики; если она недоступна — правила и сценарии.
2. **Ничего не выдумываем.** Нет данных — агент так и говорит: «не проверено», «сохранённый срез», «не удалось проверить».
3. **Агент не падает из-за внешнего мира.** Нет Циана — срез; нет модели — правила; нет базы — работает без записи
   (`repo.record` глотает ошибки базы и пишет в лог).
4. **Персональные данные продавцов не храним** (ни телефонов, ни имён). Ключи — только в `.env`.

---

## 1. Что сделано: по темам

### 1.1 Ипотечные программы и семья
- **Файлы:** `backend/agent/mortgage.py` (новый), изменения в `agent/tools.py`, `agent/finance.py`, `agent/orchestrator.py`,
  `frontend/js/views.js` (блок «Ипотечные программы»), `frontend/js/app.js` (форма «Семья и маткапитал»).
- Семейная ипотека по условиям с 01.10.2026: ребёнок до 7 лет, шкала ставки и лимита по числу детей (регионы / Москва и СПб),
  вторичка только в городах с малым строительством (`secondary_allowed`), рыночная ставка Сбера от 15,5% (+2 п.п. при взносе < 30%).
- Маткапитал идёт во взнос и пересчитывает план накоплений.
- Объяснение «подходит / не подходит и почему» и разница платежа в рублях.
- Ключевые функции: `mortgage.programs(price, city, family, housing, is_secondary)`, `listing_payment(listing, profile)`
  (платёж на каждой карточке), `max_family_price(city, family)`, `normalize_family(raw)`.
- Подтверждение `family` в конвейере: `data: {children, youngest_age, matcap}`.
- Тесты: `tests/test_mortgage.py`.

### 1.2 Единый системный промпт и «спросить агента»
- `docs/AGENT_PROMPT.md` — единый промпт (роль, тон, сценарии, финансы, ипотека, продавцы, запреты).
- `backend/agent/qa.py` (новый): вопрос в любой момент («что такое СБР?», «сколько я переплачу?»). Системный промпт берётся
  из AGENT_PROMPT.md, контекст — числа текущей сессии (`build_context`). Без LLM — ответы правилами по частым темам.
- `Session.receive_text` различает: ответ на уточнение → вопрос (`qa.is_question`) → пожелание к подборке → заметка.
- Тесты: `tests/test_qa_demo.py`.

### 1.3 Тип жилья и новостройки
- Агент не додумывает «вторичку» молча: понимает «новостройка / вторичка» из запроса; если семейная проходит только
  в новостройке — спрашивает «где искать» (подтверждение `housing`: `secondary | new | both`).
- Циан: поиск новостроек (`object_type=2`), поля ЖК, срок сдачи, застройщик, тип договора (ДДУ / уступка),
  роль продавца (застройщик / переуступка). `sources/cian.py: normalize(..., new=True)`, `_new_fields`.
- Звонок в отдел продаж застройщика (`seller_sim.build_new`), план сделки с ДДУ и эскроу или уступкой через СБР.
- Тесты: `tests/test_newbuild_security.py`.

### 1.4 Демо-режим
- `DEMO=1`: объявления из настоящего среза Циана в репозитории (`backend/data/demo/cian/`: Казань вторичка и новостройки,
  Москва; 2 комнаты), без сети. Сценарий проходит все шаги. С ключом LLM разговор ведёт модель.
- Бейдж «Демо-режим» в шапке (`/api/health → demo`).

### 1.5 Только Циан
- Модули и данные Домклика, Авито и Яндекса удалены (были в коммитах `9941501…d183a30`, остались в истории git).
- Механизм нескольких площадок оставлен: `sources/__init__.py: REGISTRY`, параллельный опрос, склейка дублей
  `sources/dedup.py` (тест `tests/test_dedup.py`). Новая площадка — модуль с `search`, `load_snapshot`, `supported`,
  `check_listing_page`, `SOURCE`, `SOURCE_NAME` и строка в `REGISTRY`.

### 1.6 105 городов Циана
- `sources/base.py`: `CIAN_REGIONS` — 105 городов с кодами регионов (7 проверены на живом Циане, остальные — из справочника
  cianparser, совпадающего с проверенными). `normalize_city` понимает падежи («в Калуге», «в Перми», «в Ростове-на-Дону»)
  и не путает слова («читаю» ≠ Чита).
- `sources/cian.py`: `REGIONS` строится из `CIAN_REGIONS`; поддомены известны для 7 городов, для остальных — `www`.
- **Редирект на поддомен города:** Циан отвечает 301 на `www.cian.ru/cat.php?region=…` и переводит на `kaluga.cian.ru`.
  `_fetch_page` идёт за редиректом (только `*.cian.ru`, не капча), запоминает поддомен в
  `backend/data/cache/cian/subdomains.json` и строит адрес поиска сам (`redirect_target`, `_learn_subdomain`).
- `/api/health → featured_cities` (кнопки), `cities` (все). Неизвестный город — агент переспрашивает.
- Тесты: `tests/test_cities.py`.

### 1.7 База данных
- `backend/db/` (новый пакет): `models.py` (20+ таблиц), `repo.py` (все запросы), `seed.py` (каталог признаков),
  `__init__.py` (`configure`, `session`, простая миграция «добавить недостающие колонки»).
- SQLAlchemy 2.0. По умолчанию SQLite `backend/data/app.db` (в git не попадает), PostgreSQL — через `DATABASE_URL`.
- Таблицы и связи — [ERD.md](ERD.md). Главные: `listings` (+ `listing_photos`, `listing_price_history`, `listing_features`),
  `users`, `visitors`, `auth_sessions`, `events`, `agent_sessions`, `chat_events`, `calls`, `call_facts`,
  `security_checks`, `tracked_searches`, `crawl_runs`, `deals` (заглушка), `finance_profiles` (заглушка).
- Объявление: колонки для поиска + полная копия в `listings.data` (формат агента из `sources/base.py`).
- **Признаки квартир** — «квартира → признак → значение» (`features` + `listing_features`), у значения источник
  и уверенность. Поставщики — `backend/features/`: `cian.py` (готов), `maps.py` и `registry.py` (заглушки для команды
  персонализации: реализовать `compute(listing) -> list[Value]`).
- Поиск: `sources.collect` сначала берёт свежую выдачу из базы (`LISTINGS_FRESH_HOURS`, 24 ч), иначе — живой Циан
  с записью в базу (`repo.upsert_listings`, история цены, фото, признаки) и постановкой поиска в ежедневный обход.

### 1.8 Ежедневный обход объявлений
- `backend/workers/refresh.py`: `python -m backend.workers.refresh --once | --daily 04:00`, или `REFRESH_AT=04:00`
  внутри сервера, или `POST /api/admin/refresh`.
- Правило: обходит `tracked_searches`; новые — добавить, цена изменилась — история; пропавшее из **полного** обхода —
  открыть страницу объявления: снято → `removed`, живо → остаётся; не открылось → пропуск, после `REMOVE_AFTER_MISSES` — `removed`.
  Неполный обход (капча, сеть) пропусков не считает. Каждый обход — строка `crawl_runs`.
- `backend/workers/photos.py`: копии фото на диск (`STORE_PHOTOS=true`), только с `cdn-cian.ru`, не больше 10 МБ.

### 1.9 Проверка, что объявление ещё в продаже
- `cian.check_listing_page(url)` → `(True | False | None, пояснение)`: 404/410, редирект со страницы, плашка
  «снято с публикации», статус снятого в данных страницы. Только `https://*.cian.ru` (`is_cian_url`).
- `sources.verify(items)` — перед показом каждой карточки подборки и перед звонком (`orchestrator.alive_only`).
  Снятые убираются и заменяются следующими по рейтингу; на карточке — «✓ Объявление активно · проверено в HH:MM»
  (`card.page`). Повторная проверка — не чаще `LISTING_CHECK_TTL_MIN`; ожидание подборки — до `LISTING_CHECK_BUDGET_S`.

### 1.10 Пользователи и cookie
- `backend/auth.py`: cookie `vid` всем посетителям (middleware), регистрация и вход по почте (scrypt), cookie `sid`
  (в базе — SHA-256 токена), 5 неверных паролей → пауза 10 минут, роль admin по `ADMIN_EMAILS`, Сбер ID — заглушка (501).
- До регистрации всё пишется на посетителя; при входе `repo.claim_visitor` переносит события, диалоги, отметки, звонки в аккаунт.
- Фронтенд: `frontend/js/account.js` (баннер о cookie, вход/регистрация, кабинет), `frontend/js/modal.js`.
- Журнал действий — `events` (через `Session.log`): подтверждения, сообщения, поиск, открытые продукты, снятые объявления.

### 1.11 Речевая аналитика звонка
- `backend/agent/call_analytics.py`: факты продавца с цитатой и уверенностью (никогда не 1). Ниже — за оговорки
  («вроде», «не помню»), расхождение с объявлением, смену ответа. Устная цена — «средняя» до задатка.
  У каждого факта — чем подтвердить. Без LLM, правилами.
- Сохранение: `calls`, `call_facts`, лучший факт — в `listing_features` с `source = seller_call`.
- Событие `call_facts` → бейджи уверенности в блоке «Что агент узнал» (`frontend/js/call.js`, `security.js: factRow`).

### 1.12 Проверка безопасности (сейчас скрыта)
- `backend/agent/security.py`: по объявлению (с доказательствами) + проверки по реестрам до сделки со ссылками
  «проверить самому» (ФССП, Федресурс, Росреестр, наш.дом.рф, реестр доверенностей, Госуслуги). Окно по клику —
  `frontend/js/security.js`, API `GET /api/listing/{id}/security`.
- **Скрыто 29.10:** `SECURITY_STEP=false` (по умолчанию). Конвейер не показывает шаг 5 и не пишет `security_checks`;
  в степпере 8 шагов (нумерация на экране сквозная, внутренние номера шагов прежние: звонок — 6, итог — 9).
  Код и тесты на месте; включить — `SECURITY_STEP=true`.

### 1.13 История диалогов (как в чат-ботах) — 29.10
- Кнопка «Диалоги» в чате: список разговоров (новые сверху), «Новый диалог». Любой диалог можно открыть и продолжить.
  Ссылка на диалог — `/#chat-<id>`; кабинет тоже открывает диалоги.
- **Лента событий:** `Session.push` нумерует каждое событие (`seq`), хранит в памяти и пишет в `chat_events`.
  Решение клиента — событие `resolved {kind, approved, label}` (подпись кнопки приходит в `data._label`).
- **Открыть:** `GET /api/chat/{id}/stream?after=N` — история (`replay: true`, фронтенд рисует её без пауз и голоса),
  затем `replay_end {live, seq, title}`, затем живые события, если агент работает.
- **Закрытая вкладка не останавливает агента** (раньше задача отменялась при обрыве SSE). Агент ждёт решения клиента.
- **Продолжить завершённый диалог / после перезапуска сервера:** `POST /api/chat/{id}/message {text}` →
  `orchestrator.continue_session`: вопрос → `answer_only` (ответ + событие `idle`); иначе — новый запуск конвейера
  в той же ленте с профилем из `agent_sessions.profile` (`Session.base_profile`), агент говорит «Продолжаю этот диалог…».
- **Доступ:** браузер, где начат диалог, и пользователь, к которому он привязан после входа (с любого устройства).
  Чужой id — 404 (`main._owns`, `repo.can_open_chat`).
- Тесты: `tests/test_chats.py`; сценарий в браузере — 17 проверок (см. раздел 5).

### 1.14 LLM: GigaChat и DeepSeek
- GigaChat: `api.giga.chat/v1`, токен — `ngw.devices.sberbank.ru:9443/api/v2/oauth`, обновляется сам; `GET /models`;
  проверка ключа — `python -m backend.llm.check --chat`. Ключ ещё не получен.
- DeepSeek: ключ без «sk-» дополняется сам; если модель не ответила (401 и т. п.) — сообщение в чате и работа по правилам.

### 1.15 Безопасность (исправлено при проверке 28.10)
- Сессию агента нельзя вести по чужому id: все `/api/session/{sid}/*` и `/api/chat/{sid}/*` проверяют владельца.
- Агент открывает только https-страницы и фото Циана (защита от SSRF через данные).
- Путь сохранения фото очищается от лишних символов.

---

## 2. Контракты, которые нельзя сломать при слиянии

### События SSE (сервер → браузер)
Каждое событие — `{"type": …, …, "seq": N}`; при повторе истории ещё `"replay": true`.

| type | Поля | Кто шлёт |
| --- | --- | --- |
| `provider` | provider, model, degraded, reason | orchestrator |
| `message` | role (`user` / `agent` / `note`), text | orchestrator, qa |
| `step` | step (1–9), title, status (`active` / `done` / `pending`) | orchestrator |
| `result` | step, kind (`profile`, `finance`, `search`, `selection`, `legal`, `viewing`, `deal`, `summary`), data | orchestrator |
| `confirm` | kind (`clarify`, `red_lines`, `family`, `finance`, `payment`, `housing`, `finance_choice`, `selection`, `call`, `price`, `object`, `sign`), title, text, … | `Session.ask` |
| `resolved` | kind, approved, label | `Session.resolve` |
| `call_start`, `call_phase`, `turn`, `price`, `guard`, `await_seller`, `call_end` | см. `agent/negotiation.py` | звонок |
| `call_facts` | listing_id, facts[], summary | после звонка |
| `product_opened` | product, amount | финплан |
| `done` | status (`completed`, `cancelled`, `saving`, `empty`, `paused`, `exhausted`) | конец запуска |
| `idle` | — | ответ в завершённом диалоге |
| `error` | text | сбой конвейера |
| `replay_end` | live, seq, title | только поток `/api/chat/{id}/stream` |

### HTTP API
Полный список — [ARCHITECTURE.md → API](ARCHITECTURE.md#api) и README. Новые в ветке: `/api/auth/*`, `/api/me`, `/api/me/history`,
`/api/consent`, `/api/chats`, `/api/chat/{id}/stream`, `/api/chat/{id}/message`, `/api/listing/{id}/security`,
`/api/admin/stats`, `/api/admin/refresh`. Старый `/api/session/{id}/stream` оставлен (то же, что новый поток).
`ConfirmRequest.data` может содержать `_label` (подпись кнопки для истории).

### Формат объявления
`sources/base.py` (док-строка): `id` (`cian-<число>`), `source`, `url`, `city`, `district`, `okrug`, `address`, `rooms`, `area`,
`kitchen`, `floor`, `floors`, `year`, `condition`, `material`, `price`, `days_on_market`, `published_at`, `metro`, `photo`, `photos`,
`description`, `rosreestr_checked`, `is_apartments`, `is_auction`, `seller_role`, `lat`, `lng`, `is_new`
(+ для новостроек `complex`, `deadline`, `finished`, `developer`, `sale_type`). Агент добавляет `fair`, `score`, `pay`, `page`, `missing`.

### Настройки (.env)
Новые в ветке: `DEMO`, `SECURITY_STEP`, `SOURCES`, `DATABASE_URL`, `LISTINGS_FRESH_HOURS`, `REFRESH_AT`, `REFRESH_PAGES`,
`REMOVE_AFTER_MISSES`, `LISTING_CHECK_TTL_MIN`, `LISTING_CHECK_BUDGET_S`, `STORE_PHOTOS`, `PHOTOS_DIR`, `AUTH_SESSION_DAYS`,
`ADMIN_EMAILS`, `COOKIE_SECURE`, `GIGACHAT_AUTH_URL`, `GIGACHAT_BASE_URL`. Шаблон с пояснениями — `.env.example`.
Зависимость добавлена одна: `sqlalchemy>=2.0`.

---

## 3. Карта файлов

**Новые** (конфликтов с `main` быть не может):
`backend/auth.py`, `backend/db/*`, `backend/features/*`, `backend/workers/*`, `backend/llm/check.py`,
`backend/agent/{mortgage,qa,security,call_analytics}.py`, `backend/sources/dedup.py`,
`backend/data/demo/cian/*.json`, `frontend/js/{account,modal,security}.js`,
`docs/{AGENT_PROMPT,ARCHITECTURE,ERD,NAS_PLAN,NAS_FEATURES}.md`,
`tests/{test_mortgage,test_qa_demo,test_newbuild_security,test_dedup,test_db_auth,test_cities,test_chats}.py`.

**Изменённые файлы `main`** — здесь будут конфликты, если `main` тоже их менял:

| Файл | Что изменено в ветке | Как сливать |
| --- | --- | --- |
| `backend/agent/orchestrator.py` | `Session`: `push`/`emit` с `seq`, `history`, `subscribers`, `base_profile`, `mode`, `visitor_id`/`user_id`, `log`, `resolve` с `resolved`; `continue_session`, `answer_only`, `alive_only`; конвейер: семья и программы, `housing`, база, проверка страниц, скрытый шаг 5, речевая аналитика, запись в базу | Брать версию ветки целиком и переносить в неё чужие изменения шагов. Все события только через `s.emit`/`s.push` (не `s.events.put`), иначе они не попадут в историю диалога |
| `backend/main.py` | middleware `vid`, startup `db.configure()` и `REFRESH_AT`, эндпоинты из раздела 2, проверка владельца сессии, `health.features` | Объединить списки эндпоинтов; новые чужие эндпоинты сессии — через `_session(sid, request)` |
| `backend/agent/tools.py` | разбор семьи / типа жилья / оплаты, сценарии ипотеки по программе, `missing_info` для новостроек, `legal_check` → `security.dossier` | Слияние по функциям |
| `backend/agent/finance.py` | маткапитал во взносе | По функциям |
| `backend/agent/negotiation.py`, `seller_sim.py`, `prompts.py` | новостройки, извлечение фактов, промпты | По функциям |
| `backend/sources/__init__.py`, `base.py`, `cian.py` | реестр площадок, база, `verify`, 105 городов, новостройки, редирект, проверка страниц, `fetch_live` | Версия ветки; формат объявления — раздел 2 |
| `backend/config.py` | новые настройки (раздел 2), пустые значения в `.env` не ломают запуск | Объединить поля |
| `backend/llm/gigachat.py`, `deepseek.py` | адрес API, `models()`, приставка `sk-` | По функциям |
| `frontend/js/app.js` | история диалогов (`openChat`, `continueChat`, `openStream`, `refreshChats`, повтор без пауз), `resolved`/`idle`/`replay_end`, скрытый шаг 5, форма семьи, `_label` | Версия ветки; чужие обработчики событий добавлять в `handle` с учётом `ev.replay` (без пауз и голоса) |
| `frontend/js/views.js` | программы ипотеки, новостройки на карточках, отметка «объявление активно», окно проверки, приветствие с городами | По функциям |
| `frontend/js/call.js`, `api.js`, `index.html`, `styles.css` | факты с уверенностью, новые методы API, кнопка «Диалоги», аккаунт, стили в конце файла | `styles.css` — новые блоки дописаны в конец, конфликты маловероятны |
| `README.md`, `.env.example`, `.gitignore`, `requirements.txt` | описание, настройки, `backend/data/*.db` и `photos/` в `.gitignore`, `sqlalchemy` | Объединить |

---

## 4. Порядок слияния

```bash
git fetch origin
git checkout main && git pull
git merge --no-ff origin/nas_features      # сейчас — без конфликтов
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests   # 97 тестов, без сети
```

Если `main` успел измениться и есть конфликты:
1. Новые файлы ветки — принять как есть.
2. `orchestrator.py`, `app.js`, `sources/cian.py` — взять версию ветки, вручную перенести чужие изменения.
3. Остальные файлы — слить по функциям (таблица выше).
4. Проверить инварианты: все события конвейера — через `s.emit`/`s.push`; новые подтверждения — через `s.ask`
   (тогда `resolved` и история работают сами); новые запросы к внешним сайтам — через проверку адреса
   (как `cian.is_cian_url`).
5. Прогнать тесты и сценарии ниже.

---

## 5. Как проверить после слияния

Демо без сети (с ключом DeepSeek в `.env` разговор ведёт модель; без него — `LLM_PROVIDER=mock`):
```bash
DEMO=1 .venv/bin/uvicorn backend.main:app --port 8000
```

| Сценарий | Ожидание |
| --- | --- |
| «Ищу 2-комнатную в Казани до 9 млн, ипотека, на взнос ещё копим, сын 4 года, есть маткапитал» | Семейная на вторичку в Казани не проходит — объяснение; маткапитал во взнос; «новостройки и вторичка»; подборка с платежом на карточках; все шаги до итога |
| Посреди подборки: «что такое СБР?» | Ответ, подборка ждёт дальше |
| «Двушка в Москве до 20 млн, наличными» | Все шаги до итога; в степпере 8 шагов, «Проверки» нет |
| Звонок «продавец — я» | Агент ведёт разговор по ответам из поля ввода |
| «Новый запрос», затем «Диалоги» → старый диалог | История на месте; если агент ждал решения — можно продолжить с того же места |
| Перезагрузка страницы со ссылкой `/#chat-…` | Диалог открывается |
| В завершённом диалоге: «сколько я переплачу?», затем «а если трёшку?» | Ответ без нового поиска; затем «Продолжаю этот диалог…» и поиск трёхкомнатных в том же городе |
| Перезапуск сервера и открытие старого диалога | История из базы, можно продолжить |
| Регистрация после поиска | Поиск в кабинете и в «Диалогах»; после входа с другого устройства — тоже |
| Живой режим (без `DEMO`): «двушка в Калуге до 6 млн» | В логе `Циан: город Калуга живёт на kaluga.cian.ru`; карточки с фото и «✓ Объявление активно» |

Ограничения, известные на 29.10: живой Циан проверен на 7 городах (остальные 98 — по справочнику кодов);
признаки снятого объявления и редирект на поддомен проверены на смоделированных ответах; реестры, телефония,
Сбер ID — заглушки; задачи агента в памяти сервера не имеют таймаута (ожидающий решения диалог живёт до перезапуска,
после перезапуска продолжается из базы новым запуском).
