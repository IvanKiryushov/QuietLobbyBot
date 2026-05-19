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
from admin_ui import admin_router
from database import init_db

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
# Отключаем спам от внутренних событий aiogram (оставляем только ошибки/предупреждения)
for logger_name in ["aiogram", "aiogram.event", "aiogram.dispatcher"]:
    logging.getLogger(logger_name).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

async def set_bot_descriptions(bot: Bot):
    """Устанавливает лаконичное описание бота для разных языков."""
    descriptions = {
        "ru": "👋 Бот-модератор QuietLobby. Чтобы запустить бота или пройти верификацию, нажмите «Start» внизу экрана.",
        "en": "👋 QuietLobby moderation bot. To start the bot or pass verification, click «Start» at the bottom.",
        "vi": "👋 Bot kiểm duyệt QuietLobby. Để khởi động bot hoặc xác minh, hãy bấm «Start» ở bên dưới."
    }
    
    # Глобальный дефолт (английский)
    try:
        await bot.set_my_description(description=descriptions["en"])
        logger.info("Установлено глобальное описание бота по умолчанию.")
    except Exception as e:
        logger.error(f"Не удалось установить глобальное описание бота: {e}")

    for lang, desc in descriptions.items():
        try:
            await bot.set_my_description(description=desc, language_code=lang)
            logger.info(f"Установлено описание бота на языке: {lang.upper()}")
        except Exception as e:
            logger.error(f"Не удалось установить описание бота на языке {lang.upper()}: {e}")

async def main():
    # Получаем токен из .env
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logger.error("BOT_TOKEN не найден в переменных окружения!")
        return

    # Инициализация базы данных
    await init_db()

    # Инициализация бота и диспетчера
    bot = Bot(token=bot_token)
    dp = Dispatcher()

    # Установка приветственных описаний бота
    await set_bot_descriptions(bot)

    # Подключаем роутер с хендлерами
    dp.include_router(admin_router)
    dp.include_router(router)

    # Запускаем пуллинг
    logger.info("Запуск бота-модератора (QuietLobbyBot)...")
    try:
        await dp.start_polling(bot, allowed_updates=["chat_member", "my_chat_member", "message", "callback_query", "chat_join_request"])
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
