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
        "ru": "👋 Бот-модератор QuietLobby. Чтобы запустить бота или пройти верификацию, нажмите «Start» внизу экрана.\n\n🛠️ Разработка ботов, доработка и автоматизация процессов: @kiryvanya",
        "en": "👋 QuietLobby moderation bot. To start the bot or pass verification, click «Start» at the bottom.\n\n🛠️ Custom bots, development & process automation: @kiryvanya",
        "vi": "👋 Bot kiểm duyệt QuietLobby. Để khởi động bot hoặc xác minh, hãy bấm «Start» ở bên dưới.\n\n🛠️ Thiết kế bot, tự động hóa & liên hệ: @kiryvanya"
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

async def set_bot_commands(bot: Bot):
    """Устанавливает подсказки команд для разных областей (scopes)."""
    from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeChat
    
    # 1. Команды в личных сообщениях (для всех пользователей)
    commands_private = [
        BotCommand(command="start", description="Начать работу / Пройти верификацию"),
        BotCommand(command="settings", description="Выбрать группу и открыть настройки")
    ]
    try:
        await bot.set_my_commands(
            commands=commands_private,
            scope=BotCommandScopeAllPrivateChats()
        )
        logger.info("Установлены подсказки команд для ЛС.")
    except Exception as e:
        logger.error(f"Не удалось установить команды для ЛС: {e}")

    # 2. Команды в личных сообщениях для суперадмина
    admin_id_str = os.getenv("ADMIN_ID")
    if admin_id_str:
        try:
            admin_id = int(admin_id_str)
            commands_admin = commands_private + [
                BotCommand(command="chats", description="Список чатов, где добавлен бот"),
                BotCommand(command="logs", description="Просмотр последних логов бота")
            ]
            await bot.set_my_commands(
                commands=commands_admin,
                scope=BotCommandScopeChat(chat_id=admin_id)
            )
            logger.info("Установлены подсказки команд для суперадмина.")
        except Exception as e:
            logger.error(f"Не удалось установить команды для суперадмина: {e}")

    # 3. Команды в группах (для вызова настройки)
    commands_groups = [
        BotCommand(command="settings", description="Получить ссылку на настройки в ЛС")
    ]
    try:
        await bot.set_my_commands(
            commands=commands_groups,
            scope=BotCommandScopeAllGroupChats()
        )
        logger.info("Установлены подсказки команд для групп.")
    except Exception as e:
        logger.error(f"Не удалось установить команды для групп: {e}")


async def sync_all_chats_admins(bot: Bot):
    """Фоновая синхронизация администраторов для всех активных чатов в БД при старте."""
    from database import get_all_active_chats, migrate_chat_id
    from admin_ui import sync_admins_for_chat
    from aiogram.exceptions import TelegramAPIError
    try:
        chats = await get_all_active_chats()
        if not chats:
            logger.info("Нет активных чатов в БД для синхронизации администраторов.")
            return
            
        logger.info(f"Запуск фоновой синхронизации админов для {len(chats)} чатов...")
        for chat_data in chats:
            chat_id = chat_data["chat_id"]
            
            # Проверяем реальный ID чата в Telegram, чтобы выявить скрытую миграцию
            try:
                chat = await bot.get_chat(chat_id)
                if chat.id != chat_id:
                    logger.info(f"Выявлена скрытая миграция чата при старте: {chat_id} -> {chat.id}")
                    await migrate_chat_id(chat_id, chat.id)
                    chat_id = chat.id
            except TelegramAPIError:
                pass
                
            # Запускаем синхронизацию для каждого чата последовательно с небольшой задержкой, чтобы не превысить лимиты API
            await sync_admins_for_chat(chat_id, bot)
            await asyncio.sleep(0.5)
        logger.info("Фоновая синхронизация администраторов завершена.")
    except Exception as e:
        logger.error(f"Ошибка при фоновой синхронизации администраторов: {e}")

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

    # Установка подсказок команд для бота
    await set_bot_commands(bot)

    # Подключаем роутер с хендлерами
    dp.include_router(admin_router)
    dp.include_router(router)

    # Фоновый запуск синхронизации админов для всех групп
    asyncio.create_task(sync_all_chats_admins(bot))

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
