import asyncio
import logging
from aiogram import Router, F, Bot
from aiogram.types import ChatMemberUpdated, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.exceptions import TelegramAPIError

from database import register_chat, deactivate_chat, migrate_chat_id

logger = logging.getLogger(__name__)
chat_router = Router()

@chat_router.my_chat_member()
async def bot_member_updated(event: ChatMemberUpdated):
    """Отслеживает добавление и удаление бота из чатов для ведения базы."""
    if event.new_chat_member.status in ["member", "administrator"]:
        chat_title = event.chat.title or "Группа"
        await register_chat(event.chat.id, chat_title)
        logger.info(f"[DB] Бот добавлен или права обновлены в чате {event.chat.id} ({chat_title})")
    elif event.new_chat_member.status in ["kicked", "left"]:
        await deactivate_chat(event.chat.id)
        logger.info(f"[DB] Бот удален из чата {event.chat.id}")

@chat_router.message(Command(commands=["settings"]), F.chat.type.in_(["group", "supergroup"]))
async def handle_settings_command(message: Message, bot: Bot):
    """Показывает кнопку для настройки бота в ЛС и автоматически регистрирует чат."""
    chat_title = message.chat.title or "Группа"
    await register_chat(message.chat.id, chat_title)
    try:
        await message.delete()
    except TelegramAPIError:
        pass
        
    bot_info = await bot.get_me()
    clean_chat_id = str(message.chat.id).replace("-", "m")
    url = f"https://t.me/{bot_info.username}?start=set_{clean_chat_id}"
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Настроить бота", url=url)]
    ])
    
    msg = await message.answer(
        "Настройка бота производится только администраторами в личных сообщениях.", 
        reply_markup=markup
    )
    
    await asyncio.sleep(5)
    try:
         await msg.delete()
    except TelegramAPIError:
         pass

@chat_router.message(F.migrate_to_chat_id)
async def handle_migrate_to(message: Message):
    old_chat_id = message.chat.id
    new_chat_id = message.migrate_to_chat_id
    logger.info(f"[DB] Чат мигрировал: {old_chat_id} -> {new_chat_id}")
    await migrate_chat_id(old_chat_id, new_chat_id)

@chat_router.message(F.migrate_from_chat_id)
async def handle_migrate_from(message: Message):
    old_chat_id = message.migrate_from_chat_id
    new_chat_id = message.chat.id
    logger.info(f"[DB] Чат мигрировал (обратное): {old_chat_id} -> {new_chat_id}")
    await migrate_chat_id(old_chat_id, new_chat_id)

