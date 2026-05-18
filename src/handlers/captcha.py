import asyncio
import aiohttp
import logging
import time
import html
from aiogram import Router, F, Bot
from aiogram.types import ChatPermissions, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.exceptions import TelegramAPIError

from keyboards import generate_emoji_captcha, TRANSLATIONS
from database import get_chat_settings, register_chat

logger = logging.getLogger(__name__)
captcha_router = Router()

# Хранилище временных данных: (chat_id, user_id) -> message_id временного сообщения в группе
group_prompts = {}

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
                    if data.get("ok") and data.get("result", {}).get("offender"):
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

# --- TASKS ---

async def verification_timeout_task(chat_id: int, user_id: int, bot: Bot, lang: str):
    """Фоновый таймер: если пользователь не прошел капчу, он кикается."""
    await asyncio.sleep(VERIFICATION_TIMEOUT)
    if (chat_id, user_id) in group_prompts:
        logger.info(f"[ТАЙМАУТ] Время вышло для {user_id} в {chat_id}")
        await kick_user_and_clean(bot, chat_id, user_id)

# --- HANDLERS ---

@captcha_router.message(F.new_chat_members)
async def handle_new_member(message: Message, bot: Bot):
    chat_id = message.chat.id
    chat_name = html.escape(message.chat.title or "нашего чата")
    
    # Автоподхват группы в БД при вступлении нового пользователя
    await register_chat(chat_id)
    
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
        
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=TRANSLATIONS[lang]["group_greet"].format(name=user_name, chat_name=chat_name),
                reply_markup=markup,
                parse_mode="HTML",
                disable_notification=True
            )
            group_prompts[(chat_id, user_id)] = msg.message_id
            asyncio.create_task(verification_timeout_task(chat_id, user_id, bot, lang))
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
            await message.answer("Заявка на проверку не найдена или время верификации истекло.")
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
        await callback.answer("Это не твоя кнопка!", show_alert=True)
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
            
        else:
            await callback.message.edit_text(TRANSLATIONS[lang]["wrong"], reply_markup=None)
            await callback.answer("Неверно!", show_alert=True)
            await kick_user_and_clean(bot, chat_id, user_id)

    except TelegramAPIError as e:
        logger.error(f"Ошибка API при клике по капче: {e}")
        await callback.answer("Произошла ошибка, попробуйте еще раз.", show_alert=True)
