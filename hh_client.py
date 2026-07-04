import os
import asyncio
import random
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
import database
from ai_analyzer import is_vacancy_suitable, generate_cover_letter
from config import SEARCH_QUERIES

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

class HHClient:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def start(self):
        self.playwright = await async_playwright().start()
        # Запуск в headless=False для того, чтобы в первый раз пользователь мог войти (ввести смс/пароль),
        # либо полностью headless, если state.json существует.
        headless = os.path.exists(STATE_FILE)
        self.browser = await self.playwright.chromium.launch(headless=headless)
        
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        if os.path.exists(STATE_FILE):
            self.context = await self.browser.new_context(storage_state=STATE_FILE, user_agent=user_agent)
        else:
            self.context = await self.browser.new_context(user_agent=user_agent)
        
        self.page = await self.context.new_page()
        await Stealth().apply_stealth_async(self.page)

    async def login_if_needed(self):
        print("Переходим на HH.ru для проверки авторизации...")
        await self.page.goto("https://hh.ru/")
        await asyncio.sleep(3)
        
        # Ждем, пока страница реально прогрузится, чтобы не ловить "пустой" экран
        await self.page.wait_for_load_state('networkidle')
        await asyncio.sleep(2)
        
        # Ищем любую ссылку или кнопку с текстом "Войти"
        login_link = self.page.locator('a:has-text("Войти")')
        login_button = self.page.locator('button:has-text("Войти")')
        
        if not await login_link.count() and not await login_button.count():
            print("Уже авторизованы (кнопка 'Войти' не найдена).")
            return True

        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            print("❌ Файл сессии (state.json) недействителен. Я его удалил.")
            print("Пожалуйста, перезапустите скрипт (python main.py), чтобы открылось окно браузера для входа.")
            return False

        print("=========================================")
        print("❗ НУЖНА АВТОРИЗАЦИЯ ❗")
        print("1. В открывшемся браузере войдите в свой аккаунт HH.ru.")
        print("2. Дождитесь, пока загрузится ваш профиль.")
        print("3. ВЕРНИТЕСЬ В ЭТО ОКНО КОНСОЛИ И НАЖМИТЕ КЛАВИШУ ENTER.")
        print("=========================================")
        
        try:
            # Ожидаем нажатия Enter (в отдельном потоке, чтобы не блокировать асинхронность)
            await asyncio.to_thread(input, "👉 Нажмите ENTER здесь, когда войдете в аккаунт: ")
            
            print("⏳ Сохраняем сессию...")
            await asyncio.sleep(2) # На всякий случай даем странице загрузиться
            await self.context.storage_state(path=STATE_FILE)
            print("✅ Авторизация успешна, состояние сохранено!")
            return True
        except Exception as e:
            print(f"❌ Произошла ошибка при сохранении авторизации: {e}")
            return False

    async def search_and_apply(self, send_notification_func):
        print("Начинаем поиск вакансий...")
        for query in SEARCH_QUERIES:
            print(f"\n======================================")
            print(f"🔍 Поиск по запросу: {query}")
            print(f"======================================")
            
            # Два режима поиска: сначала Питер (все графики), потом РФ (только удаленка)
            search_configs = [
                {"name": "Санкт-Петербург (любой график)", "params": "&area=2"},
                {"name": "Вся Россия (только удаленка)", "params": "&area=113&schedule=remote"}
            ]
            
            for config in search_configs:
                print(f"📍 Режим: {config['name']}")
                url = f"https://hh.ru/search/vacancy?text={query}&order_by=publication_time&experience=noExperience&experience=between1And3{config['params']}"
                await self.page.goto(url)
                await asyncio.sleep(3)
                page_num = 1
                while True:
                    print(f"📄 Парсим страницу {page_num} по запросу '{query}' ({config['name']})...")
                    vacancies = await self.page.locator('a[data-qa="serp-item__title"]').all()
                
                    # Собираем ссылки заранее, чтобы избежать ошибки Detached Node при долгом парсинге
                    links_to_process = []
                    for v in vacancies:
                        href = await v.get_attribute("href")
                        title = await v.inner_text()
                        if href:
                            links_to_process.append((title, href))
                        
                    for title, href in links_to_process:
                        # Парсим ID вакансии из URL (https://hh.ru/vacancy/123456?...)
                        job_id = None
                        if "vacancy/" in href:
                            job_id = href.split("vacancy/")[1].split("?")[0]
                    
                        if not job_id or database.is_job_applied(job_id):
                            # print(f"Пропускаем (уже обработано): {title}") # Раскомментировать, если нужно видеть все пропуски
                            continue
                    
                        print(f"👁️ Открываем вакансию: {title}")
                        page = await self.context.new_page()
                        await Stealth().apply_stealth_async(page)
                        try:
                            await page.goto(href)
                            await asyncio.sleep(2)
                        
                            desc_loc = page.locator('div[data-qa="vacancy-description"]')
                            # Если описания нет - возможно капча. Запускаем цикл решения.
                            while not await desc_loc.is_visible():
                                print(f"⚠️ Описание не найдено. Возможно, вылезла капча: {title}")
                                import tg_bot
                                import random
                                
                                try:
                                    # Делаем скриншот видимой области (без full_page, чтобы не триггерить ресайз окна)
                                    await page.screenshot(path="captcha.png")
                                    await tg_bot.send_captcha_request("captcha.png", f"🚨 <b>Подозрение на капчу!</b>\nБот застрял на вакансии <i>{title}</i>.\n\nПожалуйста, введите текст с картинки прямо в этот чат (если там два слова, введите через пробел):")
                                    
                                    print("Ожидаем ввод капчи из Telegram...")
                                    # Ожидание снятия блокировки (когда юзер введет текст)
                                    await tg_bot.captcha_event.wait()
                                    
                                    # Вводим текст
                                    solution = tg_bot.captcha_solution
                                    print(f"Вводим решение: {solution}")
                                    
                                    input_field = page.locator('input[type="text"]').first
                                    if await input_field.is_visible():
                                        await input_field.click()
                                        await asyncio.sleep(random.uniform(0.5, 1.2))
                                        
                                        for char in solution:
                                            if char == " ":
                                                await asyncio.sleep(random.uniform(0.6, 1.5)) # Медленный пробел между словами
                                            await input_field.type(char, delay=random.randint(150, 400)) # Человечный ввод
                                            
                                        await asyncio.sleep(random.uniform(1.0, 2.5))
                                        await input_field.press('Enter')
                                        await asyncio.sleep(5) # Ждем прогрузки после ввода
                                    else:
                                        # Если поля ввода нет (возможно это галочка Cloudflare или вы уже решили её в другом браузере)
                                        # Просто обновляем страницу, чтобы проверить, не снят ли бан по IP
                                        print("Поле ввода не найдено. Обновляем страницу...")
                                        await page.reload()
                                        await asyncio.sleep(4)
                                    
                                    # Проверяем, появилось ли описание
                                    desc_loc = page.locator('div[data-qa="vacancy-description"]')
                                    if await desc_loc.is_visible():
                                        try:
                                            await send_notification_func("✅ Капча успешно пройдена! Бот продолжает работу.")
                                        except:
                                            pass
                                        print("✅ Капча пройдена!")
                                        break # Выходим из цикла решения капчи
                                    else:
                                        try:
                                            await send_notification_func("❌ Капча решена неверно (или появилась новая). Пробуем еще раз!")
                                        except:
                                            pass
                                        print("❌ Капча не пройдена. Повторная попытка...")
                                        # Цикл while начнется заново: сделает новый скриншот и попросит ввод
                                        
                                except Exception as e:
                                    print(f"Ошибка при обработке капчи: {e}")
                                    break # В случае системной ошибки выходим, чтобы не зациклиться
                            description = await desc_loc.inner_text()

                            # Базовый жесткий фильтр по названию, чтобы не пускать ИИ на очевидные сеньорские позиции, стажировки или неайтишные профессии
                            title_lower = title.lower()
                            stop_words = [
                                "senior", "сеньор", "lead", "лид", "architect", "архитектор", "руководитель", "главный", 
                                "стажер", "intern", "trainee", "стажировка", "менеджер", "manager", "дизайнер", "designer", 
                                "hr", "аналитик", "analyst", "преподаватель", "педагог", "маркетолог", "продаж", "1с", "1c",
                                "слесарь", "диспетчер", "ассистент", "риелтор", "учитель"
                            ]
                            if any(word in title_lower for word in stop_words):
                                print(f"⏩ Пропускаем (Неподходящий грейд/профессия): {title}")
                                continue
                            
                            # Анализ ИИ
                            if await is_vacancy_suitable(title, description):
                                print(f"✨ Вакансия подходит: {title}")
                            
                                cover_letter = await generate_cover_letter(title, description)
                            
                                # Пробуем откликнуться
                                apply_btn = page.locator('a[data-qa="vacancy-response-link-top"]').first
                                if await apply_btn.is_visible():
                                    # Имитируем поведение человека перед откликом
                                    await page.mouse.move(random.randint(100, 700), random.randint(100, 500))
                                    await page.mouse.wheel(0, random.randint(200, 600))
                                    await asyncio.sleep(random.uniform(0.8, 1.5))
                                    await page.mouse.wheel(0, random.randint(-200, 100))
                                    await asyncio.sleep(random.uniform(0.5, 1.0))
                                    
                                    await apply_btn.click()
                                    # Даем время на открытие попапа ИЛИ загрузку новой страницы отклика
                                    await asyncio.sleep(3)
                                
                                    # Шаг 0: Выбор нужного резюме (если их несколько)
                                    try:
                                        from config import TARGET_RESUME_NAME
                                        if TARGET_RESUME_NAME:
                                            resume_dropdown = page.locator('[data-qa*="resume-select"], [data-qa*="resume-selector"], [data-qa="vacancy-response-resume-selector"]').first
                                            if await resume_dropdown.is_visible():
                                                await resume_dropdown.click()
                                                await asyncio.sleep(1)
                                                # Кликаем по нужному резюме из выпадающего списка
                                                target_resume_btn = page.locator(f'text="{TARGET_RESUME_NAME}"').first
                                                if await target_resume_btn.is_visible():
                                                    await target_resume_btn.click()
                                                    await asyncio.sleep(1)
                                    except Exception as e:
                                        print(f"⚠️ Ошибка при выборе резюме: {e}")
                                
                                    # Шаг 1: Ищем кнопку "Написать/Добавить сопроводительное" (если поле изначально скрыто)
                                    toggle_btn = page.locator('[data-qa*="letter-toggle"]').or_(
                                        page.locator('text="Написать сопроводительное"')
                                    ).or_(
                                        page.locator('text="Добавить сопроводительное"')
                                    ).first
                                    if await toggle_btn.is_visible():
                                        try:
                                            await toggle_btn.click()
                                            await asyncio.sleep(1)
                                        except:
                                            pass
                                
                                    # Шаг 2: Ищем ЛЮБОЕ многострочное поле (textarea) и ждем его появления (до 3 сек)
                                    letter_sent = False
                                    try:
                                        letter_textarea = page.locator('textarea').first
                                        await letter_textarea.wait_for(state="visible", timeout=3000)
                                        await letter_textarea.fill(cover_letter)
                                        letter_sent = True
                                    except:
                                        print(f"⚠️ Не удалось найти видимое поле (textarea) для письма: {title}")
                                    
                                    # Шаг 3: Отправка отклика (ищем любую видимую кнопку отправки)
                                    submit_btn = page.locator('button[data-qa*="vacancy-response-submit"]:visible').first
                                    if await submit_btn.is_visible():
                                        await submit_btn.click() # РЕАЛЬНЫЙ ОТКЛИК
                                        await asyncio.sleep(2)
                                    
                                        database.add_applied_job(job_id, title, href)
                                    
                                        import html
                                        safe_cover_letter = html.escape(cover_letter)
                                    
                                        if letter_sent:
                                            await send_notification_func(f"✅ Успешный отклик: <a href='{href}'>{title}</a>\n\n<b>Письмо:</b>\n<i>{safe_cover_letter}</i>")
                                        else:
                                            await send_notification_func(f"✅ Отклик без письма: <a href='{href}'>{title}</a>\n\n<i>(Работодатель отключил возможность отправки писем для этой вакансии)</i>")
                                        print(f"✅ Отклик отправлен: {title}")
                                else:
                                    print(f"Кнопка отклика не найдена (возможно, уже откликались): {title}")
                                    database.add_applied_job(job_id, title, href)
                            else:
                                print(f"❌ ИИ отклонил: {title}")
                                database.add_applied_job(job_id, title, href) # Добавляем, чтобы больше не анализировать
                            
                        except Exception as e:
                            print(f"Ошибка при обработке вакансии {title}: {e}")
                        finally:
                            await page.close()
                    
                    # После того как все вакансии на странице обработаны, проверяем кнопку "Дальше"
                    next_btn = self.page.locator('a[data-qa="pager-next"]')
                    if await next_btn.count() > 0 and await next_btn.is_visible():
                        print("➡️ Переходим на следующую страницу...")
                        await next_btn.click()
                        await asyncio.sleep(4)
                        page_num += 1
                    else:
                        print("🛑 Больше страниц нет, переходим к следующему запросу.")
                        break

    async def check_chats(self, send_notification_func):
        print("Проверка новых сообщений в чатах HH...")
        await self.page.goto("https://hh.ru/applicant/negotiations")
        await asyncio.sleep(3)
        
        # Находим список откликов с бейджем непрочитанных сообщений (надежный поиск через filter(has=...))
        chat_cards = await self.page.locator('div[data-qa="negotiations-item"]').filter(has=self.page.locator('span[data-qa="negotiations-item-badge"]')).all()
        
        for chat_card in chat_cards:
            
            title_loc = chat_card.locator('a[data-qa="negotiations-item-vacancy-link"]')
            title = await title_loc.inner_text() if await title_loc.is_visible() else "Неизвестно"
            
            # Переходим в чат
            chat_link = await title_loc.get_attribute("href")
            if chat_link:
                chat_page = await self.context.new_page()
                await Stealth().apply_stealth_async(chat_page)
                await chat_page.goto(f"https://hh.ru{chat_link}")
                await asyncio.sleep(3)
                
                # Получаем последнее сообщение
                messages = await chat_page.locator('div[data-qa="chat-message-text"]').all()
                if messages:
                    last_msg = await messages[-1].inner_text()
                    msg_id = f"{chat_link}_{len(messages)}" # Примитивный ID
                    
                    if not database.is_message_processed(msg_id):
                        database.add_processed_message(msg_id, chat_link, last_msg)
                        await send_notification_func(f"🔔 <b>Новое сообщение от работодателя!</b>\nВакансия: {title}\n\n<i>{last_msg}</i>\n<a href='https://hh.ru{chat_link}'>Перейти к чату</a>")
                
                await chat_page.close()

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
