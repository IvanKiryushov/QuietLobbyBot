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
    resolve_join_request, add_approved_join, remove_pending_verification,
    get_and_clear_cached_message, add_pending_verification
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

async def parse_welcome_message(text: str) -> tuple[str, InlineKeyboardMarkup | None]:
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
    
    # Добавляем обязательную кнопку суперадмина в самое начало (чтобы была сверху), если она настроена в БД
    from database import get_global_setting
    sa_text = await get_global_setting("sa_button_text")
    sa_url = await get_global_setting("sa_button_url")
    if sa_text and sa_url:
        if sa_url.startswith("t.me/"):
            sa_url = "https://" + sa_url
        buttons.insert(0, [InlineKeyboardButton(text=sa_text, url=sa_url)])
        
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
            join_buttons_enabled = settings.get('join_buttons_enabled', 1) if settings else 1
            
            # Если это режим "Только оповещение" (join_buttons_enabled == 0), то одобряют вручную в Telegram.
            # Впускаем пользователя в чат без резервной капчи.
            # Если это "Заявка с подтверждением" (join_buttons_enabled == 1), то проверяем одобрение ботом или админом в Telegram-клиенте.
            approved_by_bot = await check_and_consume_approved_join(chat_id, user_id)
            
            approved_by_telegram = False
            req = await get_join_request(chat_id, user_id)
            if req and req.get('status') == 'pending':
                approved_by_telegram = True
                # Переводим статус заявки в approved в БД
                await resolve_join_request(chat_id, user_id, 'approved', 0, 'Telegram Admin')
                # Синхронизируем карточки админов
                asyncio.create_task(sync_join_request_messages(bot, chat_id, user_id, 'approved', 'Telegram Admin'))
                logger.info(f"[TELEGRAM APPROVE DETECTED] Пользователь {user_id} зашел по одобрению заявки в Telegram-клиенте. Пропускаем без капчи.")
            
            is_approved = (join_buttons_enabled == 0) or approved_by_bot or approved_by_telegram
            
            if is_approved:
                welcome_msg = settings.get("welcome_message") if settings else None
                if welcome_msg:
                    user_name_esc = html.escape(member.first_name)
                    user_mention = f'<a href="tg://user?id={user_id}">{user_name_esc}</a>'
                    formatted_welcome = welcome_msg.replace("{name}", user_name_esc).replace("{mention}", user_mention)
                    
                    welcome_text, welcome_markup = await parse_welcome_message(formatted_welcome)
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

                # Пользователь зашел напрямую в обход ЛС бота в режиме "Заявка с подтверждением"!
                # Не делаем continue — запускаем для него резервную капчу (Уровень 1),
                # но предварительно отправляем админам кнопки подтверждения/отклонения в ЛС!
                logger.warning(f"[ОБХОД ЗАЯВОК] Пользователь {user_id} зашел в обход ручного одобрения в чат {chat_id}. Выдаем капчу.")
                
                # Создаем заявку в БД со статусом pending
                user_username = member.username or ""
                await create_join_request(chat_id, user_id, member.first_name, user_username, message.chat.title or "Группа")
                
                user_name_esc = html.escape(member.first_name)
                user_username_esc = f" (@{html.escape(user_username)})" if user_username else ""
                chat_name_esc = html.escape(message.chat.title or str(chat_id))
                
                text = (
                    f"⚠️ <b>Вход в обход заявок!</b>\n\n"
                    f"<b>Чат:</b> {chat_name_esc}\n"
                    f"<b>Пользователь:</b> <a href='tg://user?id={user_id}'>{user_name_esc}</a>{user_username_esc}\n"
                    f"<b>ID:</b> <code>{user_id}</code>\n\n"
                    f"Пользователь зашел напрямую. Бот временно ограничил его и выдал резервную капчу в группе."
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
                                await add_join_request_message(chat_id, user_id, admin.user.id, msg.message_id)
                            except TelegramAPIError as e:
                                logger.error(f"Не удалось отправить кнопки админу {admin.user.id}: {e}")
                except TelegramAPIError as e:
                    logger.error(f"Не удалось получить список админов для отправки кнопок: {e}")

        is_soft_mute = settings.get('is_soft_mute', 0) if settings else 0
        
        if is_soft_mute == 0:
            try:
                await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=get_permissions(is_muted=True))
                logger.info(f"[MUTE HARD] Пользователь {user_id} временно ограничен в чате {chat_id}.")
            except TelegramAPIError as e:
                logger.error(f"Не удалось наложить MUTE на {user_id}: {e}")
                continue
        else:
            await add_pending_verification(chat_id, user_id)
            logger.info(f"[MUTE SOFT] Пользователь {user_id} добавлен в список ожидающих верификации в {chat_id}. Приветствие в группе пропускаем.")
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
        logger.info(f"[LEFT MEMBER] Успешно удалено системное сообщение о выходе пользователя {message.left_chat_member.id} из чата {message.chat.id}")
    except TelegramAPIError as e:
        logger.error(f"[LEFT MEMBER ERROR] Не удалось удалить системное сообщение о выходе из группы {message.chat.id}: {e}")


@captcha_router.message(Command(commands=["start"]), F.chat.type == "private")
async def handle_start_private(message: Message, bot: Bot):
    args = message.text.split()
    if len(args) != 2 or not args[1].startswith("verify_"):
        # /start без параметра — общее приветствие. Используем язык клиента пользователя.
        user_lang = get_user_language(message.from_user.language_code)
        await message.answer(
            TRANSLATIONS[user_lang]["bot_greeting"],
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
                status_str = str(member.status).split('.')[-1].lower()
                if status_str in ['administrator', 'creator', 'owner']:
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
        logger.info(f"[LANG DEBUG] user_id={user_id}, raw_lang_code={message.from_user.language_code}, resolved_lang={lang}")
            
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
        user_lang = get_user_language(message.from_user.language_code)
        await message.answer(TRANSLATIONS[user_lang]["invalid_verify_link"])


@captcha_router.callback_query(F.data.startswith("c_clk:"))
async def handle_captcha_click(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    if len(parts) != 5:
        await callback.answer(TRANSLATIONS["en"]["cb_keyboard_error"], show_alert=True)
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
            try:
                await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=get_permissions(is_muted=False))
                logger.info(f"[UNMUTE] Пользователь {user_id} размучен в чате {chat_id}.")
            except TelegramAPIError as e:
                logger.error(f"Не удалось размутить пользователя {user_id} в чате {chat_id}: {e}")

            # Удаляем из списка ожидающих верификации (мягкий мьют)
            await remove_pending_verification(chat_id, user_id)
            
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
            await callback.answer(TRANSLATIONS[lang]["cb_success"])
            
            # Извлекаем и отправляем кэшированное сообщение, если оно есть
            cached_html = await get_and_clear_cached_message(chat_id, user_id)
            if cached_html:
                retrieve_prefix = TRANSLATIONS[lang].get("soft_mute_retrieve", "Ваше сохраненное сообщение:")
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"ℹ️ <b>{retrieve_prefix}</b>\n\n<code>{cached_html}</code>",
                        parse_mode="HTML"
                    )
                    logger.info(f"[SOFT MUTE RETRIEVE] Отправлено кэшированное сообщение пользователю {user_id}.")
                except TelegramAPIError as e:
                    logger.error(f"Не удалось отправить кэшированное сообщение пользователю {user_id}: {e}")

            # Отправка кастомного приветствия, если оно настроено в БД
            settings = await get_chat_settings(chat_id)
            welcome_msg = settings.get("welcome_message")
            if welcome_msg:
                user_name = html.escape(callback.from_user.first_name)
                user_mention = f'<a href="tg://user?id={user_id}">{user_name}</a>'
                formatted_welcome = welcome_msg.replace("{name}", user_name).replace("{mention}", user_mention)
                
                welcome_text, welcome_markup = await parse_welcome_message(formatted_welcome)
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
            await callback.answer(TRANSLATIONS[lang]["cb_wrong"], show_alert=True)
            await kick_user_and_clean(bot, chat_id, user_id)

    except TelegramAPIError as e:
        logger.error(f"Ошибка API при клике по капче: {e}")
        await callback.answer(TRANSLATIONS[lang]["cb_error"], show_alert=True)

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
    
    logger.info(f"[JOIN REQUEST DETECTED] Запрос на вступление от {user_id} в чат {chat_id}")
    
    settings = await get_chat_settings(chat_id)
    strictness = settings.get('captcha_strictness', 1) if settings else 1
    
    if strictness != 0:
        logger.info(f"[JOIN REQUEST] Автоматически одобряем запрос от {user_id} в чат {chat_id} (strictness={strictness} - режим капчи).")
        try:
            await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        except TelegramAPIError as e:
            logger.error(f"Не удалось автоматически одобрить заявку в Telegram: {e}")
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
    
    join_buttons_enabled = settings.get('join_buttons_enabled', 1) if settings else 1
    logger.info(f"[JOIN REQUEST] Режим заявок: join_buttons_enabled={join_buttons_enabled}, strictness={strictness}")
    
    if join_buttons_enabled:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_join:{chat_id}:{user_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_join:{chat_id}:{user_id}")
            ]
        ])
    else:
        markup = None
        logger.info(f"[JOIN REQUEST NOTIFY] Режим 'Только оповещение' — отправляем уведомления без кнопок")
    
    try:
        admins = await bot.get_chat_administrators(chat_id)
        logger.info(f"[JOIN REQUEST] Получено {len(admins)} администраторов для чата {chat_id}")
        sent_count = 0
        for admin in admins:
            if not admin.user.is_bot:
                try:
                    msg = await bot.send_message(
                        chat_id=admin.user.id,
                        text=text,
                        reply_markup=markup,
                        parse_mode="HTML"
                    )
                    sent_count += 1
                    logger.info(f"[JOIN REQUEST] Оповещение отправлено админу {admin.user.id} (msg_id={msg.message_id})")
                    # Регистрируем отправленное сообщение для синхронизации, только если есть кнопки
                    if markup:
                        await add_join_request_message(chat_id, user_id, admin.user.id, msg.message_id)
                except TelegramAPIError as e:
                    logger.error(f"Не удалось отправить уведомление о заявке админу {admin.user.id}: {e}")
        logger.info(f"[JOIN REQUEST] Итого отправлено {sent_count} оповещений для заявки пользователя {user_id}")
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
        try:
            await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        except TelegramAPIError as e:
            err_msg = str(e).lower()
            if "user_already_participant" in err_msg or "chat_join_request_not_found" in err_msg:
                logger.info(f"[APPROVE DIRECT] Пользователь {user_id} уже в чате {chat_id}, пропускаем approve_chat_join_request.")
            else:
                raise e
        
        # 4. Добавляем в кэш одобрений в БД
        await add_approved_join(chat_id, user_id)
        
        # Размучиваем пользователя в чате (на случай если он зашел напрямую и получил резервную капчу)
        try:
            await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=get_permissions(is_muted=False))
            logger.info(f"[UNMUTE BY ADMIN] Пользователь {user_id} размучен администратором в чате {chat_id}.")
        except TelegramAPIError as e:
            logger.error(f"Не удалось размутить пользователя {user_id} в чате {chat_id}: {e}")
            
        # Удаляем из списка ожидающих верификации (мягкий мьют)
        await remove_pending_verification(chat_id, user_id)
        
        # Удаляем сообщение с резервной капчей из группы, если оно было создано
        session = verification_sessions.pop((chat_id, user_id), None)
        if session:
            session["is_completed"] = True
            if session.get("message_id"):
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=session["message_id"])
                except TelegramAPIError:
                    pass
                    
        # Отправляем кастомное приветствие в группу, если оно настроено в БД
        settings = await get_chat_settings(chat_id)
        welcome_msg = settings.get("welcome_message") if settings else None
        if welcome_msg:
            try:
                chat = await bot.get_chat(chat_id)
                chat_title = chat.title or "нашего чата"
            except TelegramAPIError:
                chat_title = "нашего чата"
                
            user_info = None
            try:
                user_info = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            except TelegramAPIError:
                pass
                
            user_name = html.escape(user_info.user.first_name) if user_info else "Участник"
            user_mention = f'<a href="tg://user?id={user_id}">{user_name}</a>'
            formatted_welcome = welcome_msg.replace("{name}", user_name).replace("{mention}", user_mention)
            welcome_text, welcome_markup = await parse_welcome_message(formatted_welcome)
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=welcome_text,
                    reply_markup=welcome_markup,
                    parse_mode="HTML"
                )
            except TelegramAPIError as e:
                logger.error(f"Не удалось отправить приветствие при одобрении админом: {e}")
                
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
        try:
            await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        except TelegramAPIError as e:
            err_msg = str(e).lower()
            if "chat_join_request_not_found" in err_msg:
                logger.info(f"[DECLINE DIRECT] Заявка в Telegram не найдена, кикаем пользователя {user_id} из чата {chat_id}.")
                await kick_user_and_clean(bot, chat_id, user_id)
            else:
                raise e
        
        await callback.answer("Заявка отклонена!")
        
        # 4. Фоном обновляем карточки у всех остальных админов
        asyncio.create_task(sync_join_request_messages(bot, chat_id, user_id, 'declined', admin_name))
        
    except TelegramAPIError as e:
        logger.error(f"Не удалось отклонить заявку в Telegram: {e}")
        await callback.answer("Ошибка Telegram API при отклонении.", show_alert=True)

