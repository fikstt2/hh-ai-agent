import aiohttp
from config import OLLAMA_URL, OLLAMA_MODEL, MY_RESUME_SUMMARY

async def generate_cover_letter(vacancy_title: str, vacancy_description: str) -> str:
    prompt = f"""
Напиши сопроводительное письмо для отклика на вакансию.
Мой профиль:
{MY_RESUME_SUMMARY}

Вакансия: {vacancy_title}
Описание: {vacancy_description}

КРИТИЧЕСКИЕ ПРАВИЛА (СТРОГО СОБЛЮДАТЬ):
1. ПИСАТЬ СТРОГО ТОЛЬКО НА РУССКОМ ЯЗЫКЕ! Никакого английского текста.
2. Пиши развернуто, структурировано (3-4 абзаца).
3. Стиль: живой, профессиональный, уверенный.
4. Включай в письмо перечисление моего стека технологий, упоминание высшего образования и опыта работы с ИИ из моего профиля.
5. Обязательно упомяни мой пет-проект VisionForge и ВСЕГДА вставляй ссылку на мой GitHub: https://github.com/fikstt2
6. Никаких подписей в начале письма! Только в самом конце.
7. Подпись строго: "Евгений". Никаких "С уважением".
8. ВЫВОДИ ТОЛЬКО ТЕКСТ ПИСЬМА БЕЗ КАВЫЧЕК. Твой ответ копируется автоматически! Строго запрещены любые вводные фразы (например, "Here is a sample...", "Вот письмо:"). Ни слова, кроме самого письма.

Пример хорошего письма:
Привет!

Заинтересовала вакансия {vacancy_title}. Я программист с опытом разработки на Python, C, C++. Интересуюсь backend-разработкой, фулстек-задачами и Computer Vision. Готов решать сложные задачи и быстро обучаюсь.

Я владею инструментами ИИ и могу сам быстро обучить себя чему угодно. Имею опыт обучения моделей компьютерного зрения для задач детекции и классификации. Высшее образование по направлению "Информатика и вычислительная техника".

Мой стек: Python, C++, C, Docker, SQL, FastAPI, PyQt, HTML, JS, TensorFlow, PyTorch, PostgreSQL, Linux.

Отдельно хочу упомянуть свой пет-проект VisionForge — это фулстек-решение для компьютерного зрения, которое я реализовал на Python и PyQt5 (код тут: https://github.com/fikstt2). Я готов проходить тестовые задания и собеседования.

Буду рад пообщаться подробнее!

Евгений
"""
    
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(OLLAMA_URL, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    text = data.get("response", "").strip()
                    # Жесткая очистка от частых галлюцинаций LLM
                    text = text.replace('"', '').replace("'", "")
                    if "Here is" in text or "Here's" in text:
                        text = text.split("\n\n", 1)[-1]
                    if "Note:" in text:
                        text = text.split("Note:")[0].strip()
                    return text.strip()
    except Exception as e:
        print(f"Ошибка при обращении к Ollama (письмо): {e}")
        return "Здравствуйте! Прошу рассмотреть мое резюме на эту вакансию. Буду рад обсудить детали на собеседовании."

async def is_vacancy_suitable(vacancy_title: str, vacancy_description: str) -> bool:
    prompt = f"""
Твоя задача — оценить, подходит ли вакансия под мои критерии поиска.
Мои требования и профиль (внимательно учти желаемую зарплату, локацию и стек технологий):
{MY_RESUME_SUMMARY}

Также мне СТРОГО НЕ подходят (отклоняй сразу, отвечая NO):
- Вакансии уровня Senior (Сеньор), Lead или Архитектор.
- Вакансии, где требуется опыт работы более 3 лет (у меня от 1 до 3 лет опыта).
- Вакансии из других сфер: менеджеры, аналитики, HR, маркетологи, дизайнеры, преподаватели, риелторы, продавцы, слесари, инженеры по эксплуатации и техподдержка.
- Любые вакансии, которые НЕ связаны напрямую с написанием кода и разработкой ПО (Backend, Fullstack, C++, Python, Computer Vision). Если вакансия не про программирование — сразу пиши NO.

Вакансия:
Название: {vacancy_title}
Описание: {vacancy_description}

Если вакансия подходит под мои критерии, ответь ТОЛЬКО одним словом: YES.
Если не подходит, ответь ТОЛЬКО одним словом: NO.
"""
    
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(OLLAMA_URL, json=payload, timeout=30) as response:
                response.raise_for_status()
                data = await response.json()
                answer = data.get("response", "").strip().upper()
                return "YES" in answer
    except Exception as e:
        print(f"Ошибка при обращении к Ollama (анализ): {e}")
        return False
