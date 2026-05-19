import asyncio
import logging
from aiogram import Router, F, Bot
from aiogram.types import ChatMemberUpdated, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.exceptions import TelegramAPIError

from database import register_chat, deactivate_chat, migrate_chat_id
from admin_ui import sync_admins_for_chat

logger = logging.getLogger(__name__)
chat_router = Router()

@chat_router.my_chat_member()
async def bot_member_updated(event: ChatMemberUpdated, bot: Bot):
    """Отслеживает добавление и удаление бота из чатов для ведения базы."""
    if event.new_chat_member.status in ["member", "administrator"]:
        chat_title = event.chat.title or "Группа"
        await register_chat(event.chat.id, chat_title)
        logger.info(f"[DB] Бот добавлен или права обновлены в чате {event.chat.id} ({chat_title})")
        # Синхронизируем админов группы в БД
        await sync_admins_for_chat(event.chat.id, bot)
    elif event.new_chat_member.status in ["kicked", "left"]:
        await deactivate_chat(event.chat.id)
        logger.info(f"[DB] Бот удален из чата {event.chat.id}")

@chat_router.message(Command(commands=["settings"]), F.chat.type.in_(["group", "supergroup"]))
async def handle_settings_command(message: Message, bot: Bot):
    """Показывает кнопку для настройки бота в ЛС и автоматически регистрирует чат."""
    chat_title = message.chat.title or "Группа"
    await register_chat(message.chat.id, chat_title)
    await sync_admins_for_chat(message.chat.id, bot)
    
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

@chat_router.chat_member()
async def user_member_updated(event: ChatMemberUpdated, bot: Bot):
    """Отслеживает вход, выход и изменение прав участников в чате."""
    chat_id = event.chat.id
    user_id = event.new_chat_member.user.id
    
    # Игнорируем ботов
    if event.new_chat_member.user.is_bot:
        return

    # 1. Проверяем изменение прав администратора
    is_old_admin = event.old_chat_member.status in ["administrator", "creator"]
    is_new_admin = event.new_chat_member.status in ["administrator", "creator"]
    
    if is_old_admin != is_new_admin:
        # Изменился статус администратора, синхронизируем список админов чата
        logger.info(f"Статус админа пользователя {user_id} изменился в чате {chat_id}. Запуск синхронизации...")
        await sync_admins_for_chat(chat_id, bot)
        
    # 2. Проверяем вход/выход участников для фиксации в БД (для будущего сбора фидбека)
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    
    # Пользователь зашел в группу (стал member)
    is_joining = (
        new_status == "member" and 
        old_status not in ["member", "administrator", "creator"]
    )
    # Пользователь покинул группу (сам или кикнут)
    is_leaving = (
        new_status in ["left", "kicked"] and 
        old_status in ["member", "administrator", "creator"]
    )
    
    if is_joining:
        from database import record_member_join
        await record_member_join(chat_id, user_id)
        logger.info(f"Пользователь {user_id} вошел в чат {chat_id}. Записано в group_members.")
    elif is_leaving:
        from database import record_member_leave
        is_kick = (new_status == "kicked")
        await record_member_leave(chat_id, user_id, is_kick=is_kick)
        logger.info(f"Пользователь {user_id} покинул чат {chat_id} (kick={is_kick}). Записано в group_members.")

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

