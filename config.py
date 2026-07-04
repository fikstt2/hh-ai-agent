import os
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

# Telegram
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TG_USER_ID = os.getenv("TG_USER_ID", "YOUR_USER_ID_HERE")

# Ollama (локальная модель)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3") # Укажите используемую модель

# HH.ru настройки
# Ключевые слова для поиска (backend, python, c, c++, cv)
SEARCH_QUERIES = [
    "Python backend", 
    "Python разработчик",
    "FastAPI",
    "C++ разработчик", 
    "Программист C++",
    "Фулстек Python", 
    "Computer Vision",
    "Backend Developer",
    "Backend Python"
]
# Название резюме, которое агент должен выбирать при отклике (должно в точности совпадать с тем, что написано на HH)
TARGET_RESUME_NAME = "Backend-разработчик"

# Резюме (для генерации сопроводительного письма)
MY_RESUME_SUMMARY = """
Я программист с опытом разработки на Python, C, C++. 
Интересуюсь backend-разработкой, фулстек-задачами и Computer Vision.
Готов решать сложные задачи и быстро обучаюсь.
Имею очень крутые и сильные проекты на гитхаб https://github.com/fikstt2, самый крутой из них - VisionForge.
Не боюсь рутины, готов учить все что нужно для работы. 
Владею инструментами ИИ и могу сам быстро обучить себя чему угодно.
Ищу удаленную работу, либо работу в офисе в Санкт-Петербурге.
Зарплата от 120 000 руб.
Готов проходить тестовые задания и собеседования. Если в вакансии указано автоматизация и агенты - привожу пример, что откликаюсь через своего бота.
Имею опыт обучения моделей компьютерного зрения для задач детекции и классификации.
Имею высшее образование по направлению "Информатика и вычислительная техника".
Мой стек: Python, C++, C, Docker, SQL, FastAPI, PyQt, HTML, JS, TensorFlow, PyTorch, PostgreSQL, Linux.
"""
