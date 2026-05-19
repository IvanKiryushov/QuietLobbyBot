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
from database import get_chat_settings, register_chat

logger = logging.getLogger(__name__)
captcha_router = Router()

# Хранилище временных данных: (chat_id, user_id) -> message_id временного сообщения в группе
group_prompts = {}

# Хранилище одобренных заявок (для прохода без капчи при strictness=0): (chat_id, user_id) -> timestamp
approved_join_requests = {}

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
        
    group_msg_id = group_prompts.pop((chat_id, user_id), None)
    if group_msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=group_msg_id)
        except TelegramAPIError:
            pass

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


async def verification_timeout_task(chat_id: int, user_id: int, bot: Bot, lang: str, timeout_mins: int):
    """Фоновый таймер: если пользователь не прошел капчу, он кикается."""
    timeout_val = timeout_mins if timeout_mins > 0 else 3
    await asyncio.sleep(timeout_val * 60)
    if (chat_id, user_id) in group_prompts:
        logger.info(f"[ТАЙМАУТ] Время вышло для {user_id} в {chat_id} (лимит {timeout_val} мин)")
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
            is_approved = (chat_id, user_id) in approved_join_requests
            
            if is_approved:
                # Пользователь одобрен админом в ЛС бота. Удаляем из временного списка и пропускаем без капчи.
                approved_join_requests.pop((chat_id, user_id), None)
                
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

        lang = await resolve_language(chat_id, member.language_code)
        
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
            group_prompts[(chat_id, user_id)] = msg.message_id
            if timeout_mins > 0:
                asyncio.create_task(verification_timeout_task(chat_id, user_id, bot, lang, timeout_mins))
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
        await message.answer("👋 Привет! Я QuietLobbyBot — бот-модератор.\nЯ помогаю защищать публичные группы от спамеров.")
        return

    try:
        raw_chat_id = args[1].split("_")[1]
        chat_id = int("-" + raw_chat_id[1:]) if raw_chat_id.startswith("m") else int(raw_chat_id)
        user_id = message.from_user.id
        
        if (chat_id, user_id) not in group_prompts:
            is_admin = False
            try:
                member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                if member.status in ['administrator', 'creator']:
                    is_admin = True
            except TelegramAPIError:
                pass

            lang = await resolve_language(chat_id, message.from_user.language_code)
            if is_admin:
                await message.answer(TRANSLATIONS[lang]["admin_verification_info"])
            else:
                await message.answer(TRANSLATIONS[lang]["not_your_verification"])
            return
            
        lang = await resolve_language(chat_id, message.from_user.language_code)
            
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
        lang = await resolve_language(chat_id, callback.from_user.language_code)
        await callback.answer(TRANSLATIONS[lang]["not_your_button"], show_alert=True)
        return
        
    reaction_time = time.time() - sent_timestamp
    lang = await resolve_language(chat_id, callback.from_user.language_code)
        
    try:
        if reaction_time < 1.5:
            logger.warning(f"[РОБОТ!] Слишком быстрый клик ({reaction_time:.2f}с) от {user_id}.")
            await callback.message.edit_text(TRANSLATIONS[lang]["too_fast"], reply_markup=None)
            await kick_user_and_clean(bot, chat_id, user_id)
            return
 
        if is_correct:
            await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=get_permissions(is_muted=False))
            
            group_msg_id = group_prompts.pop((chat_id, user_id), None)
            if group_msg_id:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=group_msg_id)
                except TelegramAPIError:
                    pass
                    
            group_url = await get_chat_invite_url(bot, chat_id)
            markup_return = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=TRANSLATIONS[lang]["btn_return"], url=group_url, style="primary")]
            ])
            
            await callback.message.edit_text(TRANSLATIONS[lang]["success"], reply_markup=markup_return)
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

@captcha_router.chat_join_request()
async def handle_chat_join_request(request: ChatJoinRequest, bot: Bot):
    chat_id = request.chat.id
    user_id = request.from_user.id
    
    settings = await get_chat_settings(chat_id)
    strictness = settings.get('captcha_strictness', 1) if settings else 1
    
    # Если строгость не 0, значит чат не в ручном режиме. Оставляем заявку висеть
    # (или в будущем можем авто-одобрять и слать капчу)
    if strictness != 0:
        return
        
    user_name = html.escape(request.from_user.full_name)
    user_username = f" (@{request.from_user.username})" if request.from_user.username else ""
    chat_name = html.escape(request.chat.title or str(chat_id))
    
    text = (
        f"🔔 <b>Новая заявка на вступление!</b>\n\n"
        f"<b>Чат:</b> {chat_name}\n"
        f"<b>Пользователь:</b> <a href='tg://user?id={user_id}'>{user_name}</a>{user_username}\n"
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
                    await bot.send_message(
                        chat_id=admin.user.id,
                        text=text,
                        reply_markup=markup,
                        parse_mode="HTML"
                    )
                except TelegramAPIError:
                    # Администратор не запускал бота в ЛС, игнорируем
                    pass
    except TelegramAPIError as e:
        logger.error(f"Не удалось получить список администраторов чата {chat_id}: {e}")

@captcha_router.callback_query(F.data.startswith("approve_join:"))
async def approve_join_callback(callback: CallbackQuery, bot: Bot):
    _, chat_id_str, user_id_str = callback.data.split(":")
    chat_id = int(chat_id_str)
    user_id = int(user_id_str)
    
    try:
        await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        
        # Добавляем в список одобренных в памяти
        approved_join_requests[(chat_id, user_id)] = time.time()
        
        # Обновляем сообщение у админа
        await callback.message.edit_text(
            text=f"{callback.message.html_text}\n\n✅ <b>Одобрено администратором</b> {html.escape(callback.from_user.full_name)}",
            reply_markup=None,
            parse_mode="HTML"
        )
        await callback.answer("Заявка одобрена!")
        
    except TelegramAPIError as e:
        logger.error(f"Не удалось одобрить заявку: {e}")
        await callback.answer("Ошибка! Возможно, пользователь уже принят или отозвал заявку.", show_alert=True)

@captcha_router.callback_query(F.data.startswith("decline_join:"))
async def decline_join_callback(callback: CallbackQuery, bot: Bot):
    _, chat_id_str, user_id_str = callback.data.split(":")
    chat_id = int(chat_id_str)
    user_id = int(user_id_str)
    
    try:
        await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        
        # Обновляем сообщение у админа
        await callback.message.edit_text(
            text=f"{callback.message.html_text}\n\n❌ <b>Отклонено администратором</b> {html.escape(callback.from_user.full_name)}",
            reply_markup=None,
            parse_mode="HTML"
        )
        await callback.answer("Заявка отклонена!")
        
    except TelegramAPIError as e:
        logger.error(f"Не удалось отклонить заявку: {e}")
        await callback.answer("Ошибка! Возможно, заявка уже обработана.", show_alert=True)

