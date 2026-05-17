import asyncio
import logging
import os
from aiogram import Bot, Dispatcher
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Импортируем наши роутеры (хендлеры)
from handlers import router

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
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
        await dp.start_polling(bot, allowed_updates=["chat_join_request", "callback_query"])
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
