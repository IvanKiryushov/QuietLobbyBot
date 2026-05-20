import asyncio
import aiohttp
import logging
import time
import html
from aiogram import Router, F, Bot
from aiogram.types import ChatPermissions, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatJoinRequest
from aiogram.filters import Command
from aiogram.exceptions import TelegramAPIError

from keyboards import generate_emoji_captcha, TRANSLATIONS
from database import (
    get_chat_settings, register_chat, check_and_consume_approved_join,
    create_join_request, add_join_request_message, get_join_request,
    resolve_join_request, add_approved_join
)

logger = logging.getLogger(__name__)
captcha_router = Router()

# Хранилище сессий верификации: (chat_id, user_id) -> dict с метаданными
# dict: { "message_id": int, "started_at": float, "is_completed": bool }
verification_sessions = {}

# Таймаут верификации в секундах (180 секунд = 3 минуты)
VERIFICATION_TIMEOUT = 180

# --- HELPER FUNCTIONS ---

async def is_global_spammer(user_id: int) -> bool:
    """Проверяет ID пользователя в базе спамеров CAS (Combot Anti-Spam)."""
    url = f"https://api.cas.chat/check?user_id={user_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=3.0) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("ok"):
                        logger.warning(f"[🛡️ CAS] Обнаружен известный спамер: {user_id}")
                        return True
    except Exception as e:
        logger.error(f"Ошибка при запросе к CAS API: {e}")
    return False

async def kick_user_and_clean(bot: Bot, chat_id: int, user_id: int):
    """Кикает пользователя (бан + разбан) и удаляет временное сообщение в группе."""
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
        logger.info(f"[KICK] Пользователь {user_id} кикнут из чата {chat_id}.")
    except TelegramAPIError as e:
        logger.error(f"Не удалось кикнуть пользователя {user_id}: {e}")
        
    session = verification_sessions.pop((chat_id, user_id), None)
    if session and session.get("message_id"):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=session["message_id"])
        except TelegramAPIError:
            pass

def get_user_language(language_code: str | None) -> str:
    """Определяет язык пользователя по его Telegram-клиенту для ЛС."""
    code = (language_code or "en").lower()
    if code.startswith("ru"): return "ru"
    if code.startswith("vi"): return "vi"
    return "en"

async def resolve_language(chat_id: int, fallback_lang_code: str) -> str:
    """Определяет язык чата из БД или fallback пользователя."""
    settings = await get_chat_settings(chat_id)
    if settings and settings.get('language'):
        return settings['language']
        
    code = (fallback_lang_code or "en").lower()
    if code.startswith("ru"): return "ru"
    if code.startswith("vi"): return "vi"
    return "en"

def get_permissions(is_muted: bool) -> ChatPermissions:
    """Возвращает ChatPermissions для режима MUTE или UNMUTE."""
    s = not is_muted
    return ChatPermissions(
        can_send_messages=s, can_send_audios=s, can_send_documents=s,
        can_send_photos=s, can_send_videos=s, can_send_video_notes=s,
        can_send_voice_notes=s, can_send_polls=s, can_send_other_messages=s,
        can_add_web_page_previews=s
    )

async def get_chat_invite_url(bot: Bot, chat_id: int) -> str:
    """Пытается получить ссылку на чат."""
    try:
        chat = await bot.get_chat(chat_id)
        if chat.username:
            return f"https://t.me/{chat.username}"
        elif chat.invite_link:
            return chat.invite_link
    except TelegramAPIError:
        pass
    clean_id = str(chat_id).replace("-100", "").replace("-", "")
    return f"https://t.me/c/{clean_id}"

def parse_welcome_message(text: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Разбирает текст приветствия на собственно сообщение и инлайн-кнопки в формате 'Текст | ссылка'."""
    lines = text.split("\n")
    message_lines = []
    buttons = []
    
    for line in lines:
        if "|" in line:
            parts = line.split("|")
            if len(parts) == 2:
                btn_text = parts[0].strip()
                btn_url = parts[1].strip()
                if btn_url.startswith("http://") or btn_url.startswith("https://") or btn_url.startswith("t.me/"):
                    if btn_url.startswith("t.me/"):
                        btn_url = "https://" + btn_url
                    buttons.append([InlineKeyboardButton(text=btn_text, url=btn_url)])
                    continue
        message_lines.append(line)
        
    formatted_text = "\n".join(message_lines).strip()
    markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return formatted_text, markup

# --- TASKS ---


async def verification_timeout_task(chat_id: int, user_id: int, message_id: int, bot: Bot, lang: str, timeout_mins: int):
    """Фоновый таймер: если пользователь не прошел капчу, он кикается."""
    timeout_val = timeout_mins if timeout_mins > 0 else 3
    await asyncio.sleep(timeout_val * 60)
    session = verification_sessions.get((chat_id, user_id))
    if session and session.get("message_id") == message_id and not session.get("is_completed", False):
        logger.info(f"[ТАЙМАУТ] Время вышло для {user_id} в {chat_id} (лимит {timeout_val} мин, сообщение {message_id})")
        await kick_user_and_clean(bot, chat_id, user_id)

# --- HANDLERS ---

@captcha_router.message(F.new_chat_members)
async def handle_new_member(message: Message, bot: Bot):
    chat_id = message.chat.id
    chat_name = html.escape(message.chat.title or "нашего чата")
    
    # Автоподхват группы в БД при вступлении нового пользователя
    await register_chat(chat_id, message.chat.title or "Группа")
    
    try:
        await message.delete()
    except TelegramAPIError:
        pass

    for member in message.new_chat_members:
        if member.is_bot:
            continue
            
        user_id = member.id
        user_name = html.escape(member.first_name)
        
        if await is_global_spammer(user_id):
            await kick_user_and_clean(bot, chat_id, user_id)
            continue
            
        settings = await get_chat_settings(chat_id)
        strictness = settings.get('captcha_strictness', 1) if settings else 1
        timeout_mins = settings.get('verification_timeout', 0) if settings else 0
        if timeout_mins is None:
            timeout_mins = 0
        
        if strictness == 0:
            # Уровень 0: Ручное одобрение.
            # Проверяем, был ли пользователь одобрен через бота
            is_approved = await check_and_consume_approved_join(chat_id, user_id)
            
            if is_approved:
                welcome_msg = settings.get("welcome_message") if settings else None
                if welcome_msg:
                    user_name_esc = html.escape(member.first_name)
                    user_mention = f'<a href="tg://user?id={user_id}">{user_name_esc}</a>'
                    formatted_welcome = welcome_msg.replace("{name}", user_name_esc).replace("{mention}", user_mention)
                    
                    welcome_text, welcome_markup = parse_welcome_message(formatted_welcome)
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=welcome_text,
                            reply_markup=welcome_markup,
                            parse_mode="HTML"
                        )
                    except TelegramAPIError as e:
                        logger.error(f"Не удалось отправить кастомное приветствие: {e}")
                continue
            else:
                # Пользователь зашел напрямую в обход ЛС бота!
                # Не делаем continue — запускаем для него резервную капчу (Уровень 1)!
                logger.warning(f"[ОБХОД ЗАЯВОК] Пользователь {user_id} зашел в обход ручного одобрения в чат {chat_id}. Выдаем капчу.")

        try:
            await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=get_permissions(is_muted=True))
            logger.info(f"[MUTE] Пользователь {user_id} временно ограничен в чате {chat_id}.")
        except TelegramAPIError as e:
            logger.error(f"Не удалось наложить MUTE на {user_id}: {e}")
            continue

        # Для сообщений в общей группе используем строго язык настроек чата из БД
        chat_lang = settings.get('language', 'en') if settings else 'en'
        if chat_lang not in ['ru', 'en', 'vi']:
            chat_lang = 'en'
        lang = chat_lang
        
        bot_info = await bot.get_me()
        clean_chat_id = str(chat_id).replace("-", "m")
        verify_url = f"https://t.me/{bot_info.username}?start=verify_{clean_chat_id}"
        
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=TRANSLATIONS[lang]["btn_verify"], url=verify_url, style="success")]
        ])
        
        if timeout_mins > 0:
            time_limit_text = TRANSLATIONS[lang]["time_limit_info"].format(timeout_min=timeout_mins)
        else:
            time_limit_text = ""
            
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=TRANSLATIONS[lang]["group_greet"].format(
                    name=user_name,
                    chat_name=chat_name,
                    time_limit_info=time_limit_text
                ),
                reply_markup=markup,
                parse_mode="HTML",
                disable_notification=True
            )
            verification_sessions[(chat_id, user_id)] = {
                "message_id": msg.message_id,
                "started_at": time.time(),
                "is_completed": False
            }
            if timeout_mins > 0:
                asyncio.create_task(verification_timeout_task(chat_id, user_id, msg.message_id, bot, lang, timeout_mins))
        except TelegramAPIError as e:
            logger.error(f"Не удалось отправить приветственное сообщение: {e}")

@captcha_router.message(F.left_chat_member)
async def handle_left_chat_member(message: Message):
    try:
        await message.delete()
    except TelegramAPIError:
        pass

@captcha_router.message(Command(commands=["start"]), F.chat.type == "private")
async def handle_start_private(message: Message, bot: Bot):
    args = message.text.split()
    if len(args) != 2 or not args[1].startswith("verify_"):
        await message.answer(
            "👋 Привет! Я QuietLobbyBot — бот-модератор.\nЯ помогаю защищать публичные группы от спамеров.\n\n"
            "⚙️ <b>Разработка и автоматизация ботов:</b> @bimivan",
            parse_mode="HTML"
        )
        return

    try:
        raw_chat_id = args[1].split("_")[1]
        chat_id = int("-" + raw_chat_id[1:]) if raw_chat_id.startswith("m") else int(raw_chat_id)
        user_id = message.from_user.id
        
        session = verification_sessions.get((chat_id, user_id))
        if not session or session.get("is_completed", False):
            is_admin = False
            try:
                member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                if member.status in ['administrator', 'creator']:
                    is_admin = True
            except TelegramAPIError:
                pass

            lang = get_user_language(message.from_user.language_code)
            if is_admin:
                await message.answer(TRANSLATIONS[lang]["admin_verification_info"])
            else:
                await message.answer(TRANSLATIONS[lang]["not_your_verification"])
            return
            
        lang = get_user_language(message.from_user.language_code)
            
        try:
            chat = await bot.get_chat(chat_id)
            chat_name = chat.title or "группы"
        except TelegramAPIError:
            chat_name = "группы"

        target_word, markup = generate_emoji_captcha(chat_id, user_id, lang)
        
        text = TRANSLATIONS[lang]["greet"].format(
            name=html.escape(message.from_user.first_name),
            chat_name=html.escape(chat_name),
            target_word=html.escape(target_word.upper())
        )
        await message.answer(text=text, reply_markup=markup, parse_mode="HTML")
        
    except (IndexError, ValueError) as e:
        logger.error(f"Ошибка при парсинге параметров старта: {e}")
        await message.answer("Неверный формат ссылки верификации.")


@captcha_router.callback_query(F.data.startswith("c_clk:"))
async def handle_captcha_click(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    if len(parts) != 5:
        await callback.answer("Ошибка клавиатуры", show_alert=True)
        return
        
    is_correct = parts[1] == "1"
    chat_id = int(parts[2])
    user_id = int(parts[3])
    sent_timestamp = int(parts[4])
    
    if callback.from_user.id != user_id:
        lang = get_user_language(callback.from_user.language_code)
        await callback.answer(TRANSLATIONS[lang]["not_your_button"], show_alert=True)
        return
        
    reaction_time = time.time() - sent_timestamp
    lang = get_user_language(callback.from_user.language_code)
        
    try:
        if reaction_time < 1.5:
            logger.warning(f"[РОБОТ!] Слишком быстрый клик ({reaction_time:.2f}с) от {user_id}.")
            await callback.message.edit_text(TRANSLATIONS[lang]["too_fast"], reply_markup=None)
            await kick_user_and_clean(bot, chat_id, user_id)
            return
 
        if is_correct:
            await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=get_permissions(is_muted=False))
            
            session = verification_sessions.pop((chat_id, user_id), None)
            if session:
                session["is_completed"] = True
                if session.get("message_id"):
                    try:
                        await bot.delete_message(chat_id=chat_id, message_id=session["message_id"])
                    except TelegramAPIError:
                        pass
                    
            group_url = await get_chat_invite_url(bot, chat_id)
            markup_return = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=TRANSLATIONS[lang]["btn_return"], url=group_url, style="primary")]
            ])
            
            await callback.message.edit_text(TRANSLATIONS[lang]["success"], reply_markup=markup_return, parse_mode="HTML")
            await callback.answer("Успешно!")
            
            # Отправка кастомного приветствия, если оно настроено в БД
            settings = await get_chat_settings(chat_id)
            welcome_msg = settings.get("welcome_message")
            if welcome_msg:
                user_name = html.escape(callback.from_user.first_name)
                user_mention = f'<a href="tg://user?id={user_id}">{user_name}</a>'
                formatted_welcome = welcome_msg.replace("{name}", user_name).replace("{mention}", user_mention)
                
                welcome_text, welcome_markup = parse_welcome_message(formatted_welcome)
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=welcome_text,
                        reply_markup=welcome_markup,
                        parse_mode="HTML"
                    )
                except TelegramAPIError as e:
                    logger.error(f"Не удалось отправить кастомное приветствие в чат {chat_id}: {e}")
            
        else:
            await callback.message.edit_text(TRANSLATIONS[lang]["wrong"], reply_markup=None)
            await callback.answer("Неверно!", show_alert=True)
            await kick_user_and_clean(bot, chat_id, user_id)

    except TelegramAPIError as e:
        logger.error(f"Ошибка API при клике по капче: {e}")
        await callback.answer("Произошла ошибка, попробуйте еще раз.", show_alert=True)

# --- MANUAL APPROVAL HANDLERS (Level 0) ---

async def sync_join_request_messages(bot: Bot, chat_id: int, user_id: int, status: str, resolved_by_name: str):
    """Синхронизирует сообщения-карточки заявок во всех ЛС администраторов."""
    from database import get_join_request_messages, get_join_request
    
    req = await get_join_request(chat_id, user_id)
    if not req:
        return
        
    messages = await get_join_request_messages(chat_id, user_id)
    
    status_symbol = "✅" if status == "approved" else "❌"
    resolved_word = "Одобрено" if status == "approved" else "Отклонено"
    
    user_name = html.escape(req.get("user_name") or "Пользователь")
    user_username = f" (@{req['user_username']})" if req.get("user_username") else ""
    chat_title = html.escape(req.get("chat_title") or str(chat_id))
    
    text = (
        f"🔔 <b>Заявка на вступление обработана!</b>\n\n"
        f"<b>Чат:</b> {chat_title}\n"
        f"<b>Пользователь:</b> <a href='tg://user?id={user_id}'>{user_name}</a>{user_username}\n"
        f"<b>ID:</b> <code>{user_id}</code>\n\n"
        f"{status_symbol} <b>{resolved_word} администратором</b> {html.escape(resolved_by_name)}"
    )
    
    for admin_id, message_id in messages:
        try:
            await bot.edit_message_text(
                chat_id=admin_id,
                message_id=message_id,
                text=text,
                reply_markup=None,
                parse_mode="HTML"
            )
        except TelegramAPIError:
            pass

@captcha_router.chat_join_request()
async def handle_chat_join_request(request: ChatJoinRequest, bot: Bot):
    chat_id = request.chat.id
    user_id = request.from_user.id
    
    settings = await get_chat_settings(chat_id)
    strictness = settings.get('captcha_strictness', 1) if settings else 1
    
    if strictness != 0:
        return
        
    user_name = request.from_user.full_name
    user_username = request.from_user.username or ""
    chat_name = request.chat.title or str(chat_id)
    
    # Создаем заявку в БД
    await create_join_request(chat_id, user_id, user_name, user_username, chat_name)
    
    user_name_esc = html.escape(user_name)
    user_username_esc = f" (@{html.escape(user_username)})" if user_username else ""
    chat_name_esc = html.escape(chat_name)
    
    text = (
        f"🔔 <b>Новая заявка на вступление!</b>\n\n"
        f"<b>Чат:</b> {chat_name_esc}\n"
        f"<b>Пользователь:</b> <a href='tg://user?id={user_id}'>{user_name_esc}</a>{user_username_esc}\n"
        f"<b>ID:</b> <code>{user_id}</code>"
    )
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_join:{chat_id}:{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_join:{chat_id}:{user_id}")
        ]
    ])
    
    try:
        admins = await bot.get_chat_administrators(chat_id)
        for admin in admins:
            if not admin.user.is_bot:
                try:
                    msg = await bot.send_message(
                        chat_id=admin.user.id,
                        text=text,
                        reply_markup=markup,
                        parse_mode="HTML"
                    )
                    # Регистрируем отправленное сообщение для синхронизации
                    await add_join_request_message(chat_id, user_id, admin.user.id, msg.message_id)
                except TelegramAPIError:
                    pass
    except TelegramAPIError as e:
        logger.error(f"Не удалось получить список администраторов чата {chat_id}: {e}")

@captcha_router.callback_query(F.data.startswith("approve_join:"))
async def approve_join_callback(callback: CallbackQuery, bot: Bot):
    _, chat_id_str, user_id_str = callback.data.split(":")
    chat_id = int(chat_id_str)
    user_id = int(user_id_str)
    
    # 1. Проверяем текущее состояние заявки в БД
    req = await get_join_request(chat_id, user_id)
    if not req or req['status'] != 'pending':
        resolved_by = req['resolved_by_name'] if req else "администратором"
        status_text = "одобрена" if req and req['status'] == 'approved' else "отклонена"
        await callback.answer(f"⚠️ Эта заявка уже была {status_text} ({resolved_by})!", show_alert=True)
        if req:
            status_symbol = "✅" if req['status'] == 'approved' else "❌"
            resolved_word = "Одобрено" if req['status'] == 'approved' else "Отклонено"
            user_name_esc = html.escape(req.get("user_name") or "Пользователь")
            user_username_esc = f" (@{html.escape(req['user_username'])})" if req.get("user_username") else ""
            chat_name_esc = html.escape(req.get("chat_title") or str(chat_id))
            try:
                await callback.message.edit_text(
                    text=(
                        f"🔔 <b>Заявка на вступление обработана!</b>\n\n"
                        f"<b>Чат:</b> {chat_name_esc}\n"
                        f"<b>Пользователь:</b> <a href='tg://user?id={user_id}'>{user_name_esc}</a>{user_username_esc}\n"
                        f"<b>ID:</b> <code>{user_id}</code>\n\n"
                        f"{status_symbol} <b>{resolved_word} администратором</b> {html.escape(resolved_by)}"
                    ),
                    reply_markup=None,
                    parse_mode="HTML"
                )
            except TelegramAPIError:
                pass
        return

    admin_name = callback.from_user.full_name
    admin_id = callback.from_user.id
    
    # 2. Атомарно переводим статус в 'approved'
    success = await resolve_join_request(chat_id, user_id, 'approved', admin_id, admin_name)
    if not success:
        req = await get_join_request(chat_id, user_id)
        resolved_by = req['resolved_by_name'] if req else "администратором"
        await callback.answer(f"⚠️ Синхронизация: Заявка уже обработана ({resolved_by})!", show_alert=True)
        return
        
    try:
        # 3. Одобряем запрос в Telegram
        await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        
        # 4. Добавляем в кэш одобрений в БД
        await add_approved_join(chat_id, user_id)
        
        await callback.answer("Заявка одобрена!")
        
        # 5. Фоном обновляем карточки у всех остальных админов
        asyncio.create_task(sync_join_request_messages(bot, chat_id, user_id, 'approved', admin_name))
        
    except TelegramAPIError as e:
        logger.error(f"Не удалось одобрить заявку в Telegram: {e}")
        await callback.answer("Ошибка Telegram API при одобрении.", show_alert=True)

@captcha_router.callback_query(F.data.startswith("decline_join:"))
async def decline_join_callback(callback: CallbackQuery, bot: Bot):
    _, chat_id_str, user_id_str = callback.data.split(":")
    chat_id = int(chat_id_str)
    user_id = int(user_id_str)
    
    # 1. Проверяем текущее состояние заявки в БД
    req = await get_join_request(chat_id, user_id)
    if not req or req['status'] != 'pending':
        resolved_by = req['resolved_by_name'] if req else "администратором"
        status_text = "одобрена" if req and req['status'] == 'approved' else "отклонена"
        await callback.answer(f"⚠️ Эта заявка уже была {status_text} ({resolved_by})!", show_alert=True)
        if req:
            status_symbol = "✅" if req['status'] == 'approved' else "❌"
            resolved_word = "Одобрено" if req['status'] == 'approved' else "Отклонено"
            user_name_esc = html.escape(req.get("user_name") or "Пользователь")
            user_username_esc = f" (@{html.escape(req['user_username'])})" if req.get("user_username") else ""
            chat_name_esc = html.escape(req.get("chat_title") or str(chat_id))
            try:
                await callback.message.edit_text(
                    text=(
                        f"🔔 <b>Заявка на вступление обработана!</b>\n\n"
                        f"<b>Чат:</b> {chat_name_esc}\n"
                        f"<b>Пользователь:</b> <a href='tg://user?id={user_id}'>{user_name_esc}</a>{user_username_esc}\n"
                        f"<b>ID:</b> <code>{user_id}</code>\n\n"
                        f"{status_symbol} <b>{resolved_word} администратором</b> {html.escape(resolved_by)}"
                    ),
                    reply_markup=None,
                    parse_mode="HTML"
                )
            except TelegramAPIError:
                pass
        return

    admin_name = callback.from_user.full_name
    admin_id = callback.from_user.id
    
    # 2. Атомарно переводим статус в 'declined'
    success = await resolve_join_request(chat_id, user_id, 'declined', admin_id, admin_name)
    if not success:
        req = await get_join_request(chat_id, user_id)
        resolved_by = req['resolved_by_name'] if req else "администратором"
        await callback.answer(f"⚠️ Синхронизация: Заявка уже обработана ({resolved_by})!", show_alert=True)
        return
        
    try:
        # 3. Отклоняем запрос в Telegram
        await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        
        await callback.answer("Заявка отклонена!")
        
        # 4. Фоном обновляем карточки у всех остальных админов
        asyncio.create_task(sync_join_request_messages(bot, chat_id, user_id, 'declined', admin_name))
        
    except TelegramAPIError as e:
        logger.error(f"Не удалось отклонить заявку в Telegram: {e}")
        await callback.answer("Ошибка Telegram API при отклонении.", show_alert=True)

