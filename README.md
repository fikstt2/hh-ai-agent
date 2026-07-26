# Безопасный персональный HH Assistant

## 1. Назначение проекта

Проект ищет вакансии на HH.ru, фильтрует их, просит локальную Ollama оценить
соответствие профилю, генерирует сопроводительное письмо и отправляет превью в
Telegram.

По умолчанию используется `APP_MODE=dry_run`: финальная кнопка отклика
технически недоступна. Реальный отклик возможен только при одновременном
выполнении трёх условий:

1. `APP_MODE=approval`;
2. `ENABLE_REAL_APPLY=true`;
3. владелец с ID `TG_USER_ID` нажал `Откликнуться` именно у этой вакансии, пока
   30-минутное подтверждение не устарело.

Массового автоматического режима нет.

## 2. Архитектура

| Компонент | Ответственность |
| --- | --- |
| `config.py` | Проверка `.env` и локального `profile.yaml` |
| `browser_backend.py` | Экспериментальный CloakBrowser или резервный Playwright |
| `hh_client.py` | Поиск, чтение страниц, сообщения HH и физическая отправка |
| `ai_analyzer.py` | Строгий JSON-анализ и письмо только из локального профиля |
| `database.py` | SQLite-статусы, TTL, лимиты и атомарные переходы |
| `approval.py` | Единственный разрешённый инициатор реального отклика |
| `tg_bot.py` | Команды, превью и индивидуальные inline-кнопки |
| `main.py` | Безопасная координация цикла |

Перед физическим нажатием отправки `hh_client.py` повторно вызывает approval
guard. Тот в одной SQLite-транзакции проверяет режим, feature flag, Telegram ID,
TTL, письмо, статус, одноразовый permit и дневной лимит, затем переводит
`approved` в `applying`. Поэтому прямой вызов низкоуровневого метода без
валидного разрешения блокируется до создания страницы браузера.
Завершить такую попытку может только тот же permit. После финального клика
статус `applied` записывается только после явного success-сигнала HH; иначе
вакансия остаётся терминальной ошибкой без автоповтора.
Непосредственно перед этим кликом SQLite ещё раз проверяет TTL и дневную ёмкость.

Browser adapter не знает о подтверждениях. `CloakBrowserBackend` использует один
persistent profile; `PlaywrightBrowserBackend` оставлен как явно выбираемый
резерв. Автоматического переключения между ними нет.

## 3. Установка на macOS ARM64

Поддерживаемая база — Python 3.13. На тестовой машине macOS ARM64 полный набор
закреплённых зависимостей также установился и импортировался с Python 3.14.5, но
PyPI-классификаторы CloakBrowser 0.5.2 пока перечисляют версии только до 3.13.

```bash
brew install python@3.13
"$(brew --prefix python@3.13)/bin/python3.13" -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
python3 -m pip install -r requirements-dev.txt
```

Для резервного Playwright backend установите Chromium:

```bash
python3 -m playwright install chromium
```

CloakBrowser закреплён как `cloakbrowser==0.5.2`. Первый запуск скачивает
бинарник в `~/.cloakbrowser`, а не в репозиторий. Предварительная установка:

```bash
python3 -m cloakbrowser install
```

На некоторых Mac Gatekeeper блокирует первый запуск ad-hoc signed Chromium. В
Finder откройте скачанный `Chromium.app` через правый клик → **Open** → **Open**,
после чего повторите команду. Не отключайте Gatekeeper целиком.

Проверить backend на нейтральной странице, не открывая HH.ru:

```bash
python3 -m scripts.browser_smoke --backend cloakbrowser --no-headless --profile-dir .browser-smoke-profile
```

Ожидаются `status=200` и `title=Example Domain`. Smoke-test ручной и не входит в
`pytest`.

## 4. Настройка Ollama

Установите официальное приложение Ollama для macOS, запустите его и загрузите
модель, совпадающую с `OLLAMA_MODEL`:

```bash
ollama pull llama3
```

Если приложение не запустило локальный сервер автоматически:

```bash
ollama serve
```

Проверка API:

```bash
curl http://localhost:11434/api/tags
```

Официальные инструкции: [Ollama for macOS](https://docs.ollama.com/macos) и
[CLI reference](https://docs.ollama.com/cli).

## 5. Создание Telegram-бота

1. Откройте официальный `@BotFather`.
2. Выполните `/newbot` и сохраните выданный токен как пароль.
3. Узнайте свой числовой Telegram user ID. Если используете стороннего бота для
   этого, учитывайте его политику приватности.
4. Откройте созданного бота и нажмите **Start**: Telegram-бот не может первым
   начать диалог с пользователем.

Токен хранится только в `.env`. Не вставляйте его в issue, commit, лог или
скриншот. Официальное руководство: [Telegram BotFather tutorial](https://core.telegram.org/bots/tutorial).

## 6. Заполнение профиля

Создайте локальные файлы:

```bash
cp .env.example .env
cp profile.example.yaml profile.yaml
```

`profile.yaml` уже исключён из Git. Обязательные поля:

- `candidate.name` — ваше имя для письма;
- `candidate.desired_positions` — непустой список желаемых ролей;
- `candidate.experience_summary` — только подтверждаемый опыт;
- `hh.resume_name` — точное название резюме на HH.ru;
- `hh.search_queries` — непустой список запросов.

Остальные поля кандидата заполняйте только реальными фактами:
`location`, `education`, `technologies`, `projects`, `github_url`,
`salary_expectation`, `work_format`, `excluded_positions` и
`additional_information`.

В секции `hh` доступны `areas` с ID регионов и `experience_filters` со значениями
фильтра HH. В `cover_letter` задаются `language`, `max_length` и `style`.

Проверка без браузера и внешних запросов:

```bash
python3 main.py --check-config
```

При ошибке программа выводит одно сообщение `Configuration error:` без
traceback.

## 7. Первый вход на HH.ru

Оставьте в `.env`:

```ini
APP_MODE=dry_run
ENABLE_REAL_APPLY=false
BROWSER_BACKEND=cloakbrowser
BROWSER_HEADLESS=false
BROWSER_PROFILE_DIR=.browser-profile
```

Запустите:

```bash
python3 main.py
```

В открытом окне вручную войдите в HH.ru, вернитесь в терминал и нажмите Enter.
Cookies, local storage и кэш сохраняются в `.browser-profile`. Не копируйте этот
каталог в Git или облачную папку. При `BROWSER_HEADLESS=true` первичный вход
невозможен и запуск завершится понятной ошибкой.

## 8. Dry-run

Безопасная конфигурация:

```ini
APP_MODE=dry_run
ENABLE_REAL_APPLY=false
```

Запуск:

```bash
python3 main.py --check-config
python3 main.py
```

Бот ищет, читает, фильтрует, вызывает Ollama, сохраняет результаты и отправляет
Telegram-превью без кнопок реального действия. Даже сформированный вручную
прямой вызов sender будет отклонён approval guard.

## 9. Режим подтверждения

Сначала остановите процесс через Ctrl+C. После успешного dry-run измените ровно
две строки:

```ini
APP_MODE=approval
ENABLE_REAL_APPLY=true
```

Затем снова выполните:

```bash
python3 main.py --check-config
python3 main.py
```

Подходящая вакансия получит статус `pending_approval` и кнопки
`Откликнуться`/`Пропустить`. Каждая кнопка привязана к ID вакансии. Подтверждение
одноразовое и действует `APPROVAL_TTL_MINUTES` (по умолчанию 30 минут).

## 10. Проверка перед реальным откликом

Перед включением approval убедитесь, что:

- `/status` показывает ожидаемый режим и user ID принадлежит только вам;
- профиль не содержит чужих или неподтверждённых сведений;
- название резюме совпадает с HH.ru;
- дневной лимит `MAX_APPLICATIONS_PER_DAY` подходит вам;
- письмо в Telegram прочитано полностью;
- ссылка, компания, объяснение и confidence относятся к нужной вакансии;
- в базе нет застрявшего `applying` для этой вакансии.

Нажатие `Пропустить` переводит вакансию в `skipped`. Просроченная карточка
становится `expired`. Автоматического повтора отклика нет.

## 11. Команды Telegram

- `/start` — краткая справка;
- `/status` — режим, состояние, обработанные вакансии и отклики сегодня;
- `/pause` — остановить новый поиск, сохранив процесс и Telegram polling;
- `/resume` — продолжить поиск;
- `/pending` — показать ожидающие решения;
- `/stats` — статистика SQLite по статусам;
- `/cancel` — отменить текущий ручной ввод CAPTCHA.

Команды и callback-кнопки доступны только `TG_USER_ID`. Остальные пользователи
получают нейтральный отказ без ID, токена и конфигурации.

## 12. Резервное копирование базы

Полностью остановите приложение (`Ctrl+C`), затем используйте штатный
SQLite. `/pause` недостаточно: Telegram-callback и отклик могут ещё записывать данные.

```bash
mkdir -p backups
sqlite3 agent.db ".backup 'backups/agent-backup.db'"
sqlite3 backups/agent-backup.db "PRAGMA integrity_check;"
```

Ожидаемый результат второй команды — `ok`. Храните backup как персональные
данные и не добавляйте его в Git.

Старая таблица `applied_jobs` сохраняется без изменений. Она не импортируется в
новую статистику: прежняя версия записывала туда реальные отклики, отклонения и
пропуски без различия.

## 13. Сброс авторизованной сессии

Остановите приложение и переместите профиль в резервную папку:

```bash
mv .browser-profile .browser-profile.backup
```

При следующем запуске с `BROWSER_HEADLESS=false` будет создан чистый профиль и
потребуется новый ручной вход. Старую папку удаляйте только после проверки новой
сессии; она содержит cookies и должна храниться приватно.

## 14. Типичные ошибки

- `Configuration error` — заполните перечисленные поля `.env`/`profile.yaml`.
- `CloakBrowser failed to start` — проверьте Gatekeeper и
  `python3 -m cloakbrowser info`; для резервного backend явно установите
  `BROWSER_BACKEND=playwright` и Chromium.
- `HH.ru login is required` — включите headed mode и войдите вручную.
- `Invalid model response` — проверьте модель и Ollama; вакансия безопасно
  отклоняется, а не считается подходящей.
- `page_structure_changed` — изменились селекторы HH; вакансия помечается
  ошибкой, цикл продолжает работу.
- `captcha_detected` — решите CAPTCHA вручную до тайм-аута или отмените; число
  попыток ограничено.
- `application_blocked` — проверьте режим, flag, TTL, владельца, статус, письмо
  и дневной лимит.
- `applying` после аварийного завершения — не повторяйте отклик автоматически;
  проверьте его наличие вручную на HH.ru и исправьте статус только после этого.

Логи ротируются в `agent.log`. Токены, cookies, profile/storage state и полный
`.env` туда не записываются.

## 15. Ограничения проекта

- Автоматизация может нарушать правила HH.ru; ответственность за использование
  и состояние аккаунта несёт пользователь.
- CloakBrowser — экспериментальный backend, уменьшающий некоторые сигналы
  автоматизации. Он не гарантирует отсутствие обнаружения или CAPTCHA.
- Проект не использует proxy, GeoIP, ротацию IP, внешние CAPTCHA-сервисы или
  автоматический fallback.
- Селекторы и сценарий формы HH.ru могут измениться. Реальный отклик в тестах не
  выполняется, поэтому live selector success не подтверждён.
- Редактирование письма в Telegram пока не реализовано.
- После process crash статус `applying` намеренно остаётся fail-closed.
- Сервис рассчитан на одного владельца и одну локальную SQLite-базу.
- Пользователь или локальный процесс с правом записи в SQLite может изменить её
  состояние; модель угроз защищает от случайного submit, а не от скомпрометированной
  машины.

## Разработка

Unit-тесты не обращаются к HH.ru, Telegram или Ollama:

```bash
python3 -m compileall .
pytest -q
```

Пошаговая установка для начинающего пользователя находится в
[`SETUP_CHECKLIST.md`](SETUP_CHECKLIST.md).
