# Checklist безопасного запуска на macOS ARM64

Выполняйте пункты сверху вниз. Не включайте реальные отклики, пока dry-run не
проработал без ошибок.

## Подготовка

- [ ] Откройте Terminal.
- [ ] Перейдите в папку проекта через `cd`.
- [ ] Установите Homebrew, если команда `brew --version` не работает.
- [ ] Установите Python 3.13:

```bash
brew install python@3.13
```

- [ ] Создайте отдельное Python-окружение:

```bash
"$(brew --prefix python@3.13)/bin/python3.13" -m venv .venv
source .venv/bin/activate
```

- [ ] Убедитесь, что используется локальный Python:

```bash
python3 --version
python3 -m pip --version
```

- [ ] Установите закреплённые зависимости:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
python3 -m pip install -r requirements-dev.txt
```

## CloakBrowser и резервный Playwright

- [ ] Скачайте free binary CloakBrowser:

```bash
python3 -m cloakbrowser install
```

- [ ] Если macOS заблокировал Chromium, найдите `Chromium.app` в
  `~/.cloakbrowser`, откройте через правый клик → **Open** и подтвердите запуск.
  Не отключайте Gatekeeper глобально.
- [ ] Запустите только нейтральный ручной smoke-test:

```bash
python3 -m scripts.browser_smoke --backend cloakbrowser --no-headless --profile-dir .browser-smoke-profile
```

- [ ] Проверьте строки `status=200` и `title=Example Domain`.
- [ ] Если нужен резервный Playwright, установите его Chromium:

```bash
python3 -m playwright install chromium
```

- [ ] Для Playwright меняйте `BROWSER_BACKEND` вручную. Автоматического fallback
  нет.

## Ollama

- [ ] Установите Ollama с официальной страницы для macOS.
- [ ] Откройте приложение Ollama.
- [ ] Загрузите модель из `.env.example`:

```bash
ollama pull llama3
```

- [ ] Проверьте локальный API:

```bash
curl http://localhost:11434/api/tags
```

## Telegram

- [ ] Откройте официальный `@BotFather`.
- [ ] Выполните `/newbot`.
- [ ] Сохраните токен приватно; не вставляйте его в Git, issue или сообщения.
- [ ] Получите свой числовой Telegram user ID.
- [ ] Откройте созданного бота и нажмите **Start**.

## Локальная конфигурация

- [ ] Создайте файлы из шаблонов:

```bash
cp .env.example .env
cp profile.example.yaml profile.yaml
```

- [ ] Откройте `.env` в текстовом редакторе и заполните:

  - `TG_BOT_TOKEN`;
  - `TG_USER_ID`;
  - `OLLAMA_URL`;
  - `OLLAMA_MODEL`;
  - при необходимости интервалы и лимиты.

- [ ] Для первого запуска оставьте:

```ini
APP_MODE=dry_run
ENABLE_REAL_APPLY=false
BROWSER_BACKEND=cloakbrowser
BROWSER_HEADLESS=false
BROWSER_PROFILE_DIR=.browser-profile
```

- [ ] Откройте `profile.yaml` и обязательно заполните:

  - `candidate.name`;
  - хотя бы один элемент `candidate.desired_positions`;
  - `candidate.experience_summary`;
  - `hh.resume_name` точно как на HH.ru;
  - хотя бы один элемент `hh.search_queries`.

- [ ] Заполните только правдивыми данными остальные поля:
  `candidate.location`, `education`, `technologies`, `projects`, `github_url`,
  `salary_expectation`, `work_format`, `excluded_positions`,
  `additional_information`, а также `hh.areas`, `experience_filters` и
  `cover_letter`.

- [ ] Проверьте, что локальные файлы игнорируются Git:

```bash
git check-ignore .env profile.yaml .browser-profile state.json agent.db
```

## Безопасная проверка

- [ ] Проверьте конфигурацию без сети:

```bash
python3 main.py --check-config
```

- [ ] Запустите unit-тесты:

```bash
pytest -q
```

- [ ] Проверьте синтаксис:

```bash
python3 -m compileall .
```

## Первый вход и dry-run

- [ ] Запустите приложение:

```bash
python3 main.py
```

- [ ] В открытом CloakBrowser вручную войдите в HH.ru.
- [ ] Вернитесь в Terminal и нажмите Enter.
- [ ] В Telegram выполните `/status`; режим должен быть `dry_run`.
- [ ] Дождитесь хотя бы одного превью. У него не должно быть кнопки
  `Откликнуться`.
- [ ] Проверьте `/stats`, `/pause`, затем `/resume`.
- [ ] Остановите приложение через Ctrl+C.

## Резервная копия

- [ ] Создайте backup SQLite:

```bash
mkdir -p backups
sqlite3 agent.db ".backup 'backups/agent-backup.db'"
sqlite3 backups/agent-backup.db "PRAGMA integrity_check;"
```

- [ ] Убедитесь, что результат — `ok`.
- [ ] Не добавляйте backup в Git и храните его как персональные данные.

## Переход к индивидуальному подтверждению

- [ ] Прочитайте ограничения в README.
- [ ] Убедитесь, что dry-run не вызывал `application_failed` из-за селекторов.
- [ ] Уменьшите `MAX_APPLICATIONS_PER_DAY`, если пяти откликов много.
- [ ] Измените только:

```ini
APP_MODE=approval
ENABLE_REAL_APPLY=true
```

- [ ] Снова проверьте конфигурацию:

```bash
python3 main.py --check-config
```

- [ ] Запустите `python3 main.py`.
- [ ] Для каждой карточки отдельно прочитайте ссылку, компанию, reason,
  confidence и письмо.
- [ ] Нажимайте `Откликнуться` только осознанно; `Пропустить` безопасно завершает
  обработку карточки.
- [ ] Помните: подтверждение одноразовое, истекает через 30 минут и не может быть
  принято от другого Telegram ID.

## Если что-то пошло не так

- [ ] Выполните `/pause`.
- [ ] Посмотрите `agent.log`, не публикуя его целиком.
- [ ] Проверьте `/pending` и `/stats`.
- [ ] Если статус остался `applying`, сначала вручную проверьте HH.ru. Не
  запускайте повторный отклик автоматически.
- [ ] Для нового входа остановите приложение и сохраните старый профиль:

```bash
mv .browser-profile .browser-profile.backup
```

- [ ] Запустите снова с `BROWSER_HEADLESS=false`.
