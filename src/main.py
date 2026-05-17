import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from aiogram import Bot, Dispatcher
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Импортируем наши роутеры (хендлеры)
from handlers import router

# Настройка логирования: в консоль и в файл bot.log с ротацией
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Обработчик для записи логов в файл (до 5 МБ, храним 3 резервные копии)
file_handler = RotatingFileHandler(
    'bot.log',
    maxBytes=5*1024*1024,
    backupCount=3,
    encoding='utf-8'
)
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)

# Обработчик для вывода в консоль терминала
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)
logger = logging.getLogger(__name__)

async def main():
    # Получаем токен из .env
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logger.error("BOT_TOKEN не найден в переменных окружения!")
        return

    # Инициализация бота и диспетчера
    bot = Bot(token=bot_token)
    dp = Dispatcher()

    # Подключаем роутер с хендлерами
    dp.include_router(router)

    # Запускаем пуллинг
    logger.info("Запуск бота-модератора (QuietLobbyBot)...")
    try:
        await dp.start_polling(bot, allowed_updates=["chat_member", "message", "callback_query"])
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")
    finally:
        logger.info("Бот остановлен.")
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user.")
