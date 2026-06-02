import asyncio
import logging
import html
import time
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
    status_old = str(event.old_chat_member.status).split('.')[-1].lower()
    status_new = str(event.new_chat_member.status).split('.')[-1].lower()
    is_old_admin = status_old in ["administrator", "creator", "owner"]
    is_new_admin = status_new in ["administrator", "creator", "owner"]
    
    if is_old_admin != is_new_admin:
        # Изменился статус администратора, синхронизируем список админов чата
        logger.info(f"Статус админа пользователя {user_id} изменился в чате {chat_id}. Запуск синхронизации...")
        await sync_admins_for_chat(chat_id, bot)
        
    # 2. Проверяем вход/выход участников для фиксации в БД (для будущего сбора фидбека)
    old_status = status_old
    new_status = status_new
    
    # Пользователь зашел в группу (стал member)
    is_joining = (
        new_status == "member" and 
        old_status not in ["member", "administrator", "creator", "owner"]
    )
    # Пользователь покинул группу (сам или кикнут)
    is_leaving = (
        new_status in ["left", "kicked"] and 
        old_status in ["member", "administrator", "creator", "owner"]
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


async def custom_verification_timeout_task(chat_id: int, user_id: int, message_id: int, bot: Bot, remaining_seconds: float):
    """Фоновый таймер для актуализированного сообщения (мягкий мьют)."""
    if remaining_seconds <= 0:
        from handlers.captcha import kick_user_and_clean
        await kick_user_and_clean(bot, chat_id, user_id)
        return
        
    await asyncio.sleep(remaining_seconds)
    from handlers.captcha import verification_sessions, kick_user_and_clean
    session = verification_sessions.get((chat_id, user_id))
    if session and session.get("message_id") == message_id and not session.get("is_completed", False):
        logger.info(f"[ТАЙМАУТ] Время вышло для {user_id} в {chat_id} (мягкий мьют, сообщение {message_id})")
        await kick_user_and_clean(bot, chat_id, user_id)


async def is_unverified_user(message: Message) -> bool:
    """Фильтр для перехвата сообщений только от неверифицированных пользователей."""
    if not message.from_user or message.left_chat_member or message.new_chat_members:
        return False
    from database import is_pending_verification
    return await is_pending_verification(message.chat.id, message.from_user.id)


@chat_router.message(F.chat.type.in_(["group", "supergroup"]), is_unverified_user)
async def intercept_unverified_message(message: Message, bot: Bot):
    chat_id = message.chat.id
    user_id = message.from_user.id

    # Удаляем сообщение неверифицированного
    try:
        await message.delete()
    except TelegramAPIError:
        pass

    # Кэшируем текст сообщения
    from database import cache_user_message
    html_text = message.html_text if message.text else (message.caption or "")
    if html_text:
        await cache_user_message(chat_id, user_id, html_text)
        logger.info(f"[SOFT MUTE CACHE] Сообщение от {user_id} в чате {chat_id} сохранено в кэш.")

    from handlers.captcha import verification_sessions
    session = verification_sessions.get((chat_id, user_id))
    
    # Определяем время старта верификации для сохранения таймаута
    started_at = time.time()
    if session and "started_at" in session:
        started_at = session["started_at"]

    # Удаляем старое приветственное сообщение с кнопкой
    if session and session.get("message_id"):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=session["message_id"])
        except TelegramAPIError:
            pass

    # Получаем настройки чата
    from database import get_chat_settings
    settings = await get_chat_settings(chat_id)
    chat_lang = settings.get('language', 'en') if settings else 'en'
    if chat_lang not in ['ru', 'en', 'vi']:
        chat_lang = 'en'
        
    timeout_mins = settings.get('verification_timeout', 0) if settings else 0
    if timeout_mins is None:
        timeout_mins = 0

    user_name = html.escape(message.from_user.first_name)
    chat_name = html.escape(message.chat.title or "нашего чата")
    
    bot_info = await bot.get_me()
    clean_chat_id = str(chat_id).replace("-", "m")
    verify_url = f"https://t.me/{bot_info.username}?start=verify_{clean_chat_id}"
    
    from keyboards import TRANSLATIONS
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=TRANSLATIONS[chat_lang]["btn_verify"], url=verify_url, style="success")]
    ])
    
    time_limit_text = ""
    if timeout_mins > 0:
        time_limit_text = TRANSLATIONS[chat_lang]["time_limit_info"].format(timeout_min=timeout_mins)

    base_greet = TRANSLATIONS[chat_lang]["group_greet"].format(
        name=user_name,
        chat_name=chat_name,
        time_limit_info=time_limit_text
    )
    warn_text = TRANSLATIONS[chat_lang].get("soft_mute_warn", "")
    
    full_text = f"{base_greet}{warn_text}"
    
    try:
        new_msg = await bot.send_message(
            chat_id=chat_id,
            text=full_text,
            reply_markup=markup,
            parse_mode="HTML",
            disable_notification=True
        )
        
        # Обновляем сессию верификации
        verification_sessions[(chat_id, user_id)] = {
            "message_id": new_msg.message_id,
            "started_at": started_at,
            "is_completed": False
        }
        
        # Если есть таймаут, запускаем отслеживание оставшегося времени
        if timeout_mins > 0:
            elapsed = time.time() - started_at
            remaining = (timeout_mins * 60) - elapsed
            asyncio.create_task(
                custom_verification_timeout_task(chat_id, user_id, new_msg.message_id, bot, remaining)
            )
            
    except TelegramAPIError as e:
        logger.error(f"Не удалось отправить актуализированное приветствие с кнопкой капчи: {e}")

