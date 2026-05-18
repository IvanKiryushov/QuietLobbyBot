import asyncio
import logging
from aiogram import Router, F, Bot
from aiogram.types import ChatMemberUpdated, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.exceptions import TelegramAPIError

from database import register_chat, deactivate_chat

logger = logging.getLogger(__name__)
chat_router = Router()

@chat_router.my_chat_member()
async def bot_member_updated(event: ChatMemberUpdated):
    """Отслеживает добавление и удаление бота из чатов для ведения базы."""
    if event.new_chat_member.status in ["member", "administrator"]:
        await register_chat(event.chat.id)
        logger.info(f"[DB] Бот добавлен или права обновлены в чате {event.chat.id}")
    elif event.new_chat_member.status in ["kicked", "left"]:
        await deactivate_chat(event.chat.id)
        logger.info(f"[DB] Бот удален из чата {event.chat.id}")

@chat_router.message(Command(commands=["settings"]), F.chat.type.in_(["group", "supergroup"]))
async def handle_settings_command(message: Message, bot: Bot):
    """Показывает кнопку для настройки бота в ЛС и автоматически регистрирует чат."""
    await register_chat(message.chat.id)
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
    
    await asyncio.sleep(15)
    try:
        await msg.delete()
    except TelegramAPIError:
        pass
