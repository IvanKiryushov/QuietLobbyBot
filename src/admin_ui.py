import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.exceptions import TelegramAPIError

from database import get_chat_settings, update_chat_setting

logger = logging.getLogger(__name__)
admin_router = Router()

async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором или создателем чата."""
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status in ['administrator', 'creator']:
            return True
        return False
    except TelegramAPIError as e:
        logger.error(f"Ошибка при проверке прав пользователя {user_id} в чате {chat_id}: {e}")
        return False

def generate_settings_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    """Генерирует клавиатуру настроек для конкретного чата."""
    lang = settings.get('language', 'en')
    strictness = settings.get('captcha_strictness', 1)
    
    lang_text = f"Язык: {'🇷🇺 RU' if lang == 'ru' else '🇻🇳 VI' if lang == 'vi' else '🇬🇧 EN'}"
    strictness_text = f"Строгость: {strictness}"
    
    buttons = [
        [InlineKeyboardButton(text=lang_text, callback_data=f"set_lang:{chat_id}:{lang}")],
        [InlineKeyboardButton(text=strictness_text, callback_data=f"set_strict:{chat_id}:{strictness}")],
        [InlineKeyboardButton(text="Закрыть", callback_data="set_close")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def open_settings_panel(message: Message, bot: Bot, chat_id: int):
    """Открывает панель настроек чата, если есть права."""
    user_id = message.from_user.id
    if not await is_chat_admin(bot, chat_id, user_id):
        await message.answer("У вас нет прав администратора в этом чате или бот не добавлен в этот чат.")
        return
        
    try:
        chat = await bot.get_chat(chat_id)
        chat_name = chat.title or str(chat_id)
    except TelegramAPIError:
        chat_name = str(chat_id)
        
    settings = await get_chat_settings(chat_id)
    if not settings:
        await message.answer(f"Чат <b>{chat_name}</b> еще не зарегистрирован в базе бота.", parse_mode="HTML")
        return
        
    text = f"⚙️ <b>Настройки для чата:</b> {chat_name}\n\nВыберите параметр для изменения:"
    markup = generate_settings_keyboard(chat_id, settings)
    
    await message.answer(text, reply_markup=markup, parse_mode="HTML")

@admin_router.message(F.chat.type == "private", F.forward_from_chat)
async def handle_forwarded_message(message: Message, bot: Bot):
    """Обрабатывает пересланные из публичного чата сообщения для открытия настроек."""
    chat_id = message.forward_from_chat.id
    if message.forward_from_chat.type in ["group", "supergroup"]:
        await open_settings_panel(message, bot, chat_id)

@admin_router.message(Command(commands=["start"]), F.chat.type == "private")
async def handle_start_settings(message: Message, bot: Bot):
    """Обрабатывает DeepLink старт вида /start set_-100123..."""
    args = message.text.split()
    # Пропускаем обычный start и старт верификации (он в handlers.py)
    if len(args) == 2 and args[1].startswith("set_"):
        raw_chat_id = args[1].split("_")[1]
        try:
            # Обрабатываем замену m на минус, если используется (как в верификации)
            if raw_chat_id.startswith("m"):
                chat_id = int("-" + raw_chat_id[1:])
            else:
                chat_id = int(raw_chat_id)
            await open_settings_panel(message, bot, chat_id)
        except ValueError:
            await message.answer("Неверный формат ссылки настроек.")

@admin_router.callback_query(F.data.startswith("set_lang:"))
async def change_language_callback(callback: CallbackQuery, bot: Bot):
    _, chat_id_str, current_lang = callback.data.split(":")
    chat_id = int(chat_id_str)
    
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
        
    # Цикл переключения языка
    langs = ['ru', 'en', 'vi']
    next_lang = langs[(langs.index(current_lang) + 1) % len(langs)]
    
    await update_chat_setting(chat_id, 'language', next_lang)
    
    settings = await get_chat_settings(chat_id)
    markup = generate_settings_keyboard(chat_id, settings)
    
    await callback.message.edit_reply_markup(reply_markup=markup)
    await callback.answer(f"Язык изменен на {next_lang.upper()}")

@admin_router.callback_query(F.data.startswith("set_strict:"))
async def change_strictness_callback(callback: CallbackQuery, bot: Bot):
    _, chat_id_str, current_strict = callback.data.split(":")
    chat_id = int(chat_id_str)
    
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
        
    strictness = int(current_strict)
    # Переключение строгости (1 -> 2 -> 3 -> 1)
    next_strict = strictness + 1 if strictness < 3 else 1
    
    await update_chat_setting(chat_id, 'captcha_strictness', next_strict)
    
    settings = await get_chat_settings(chat_id)
    markup = generate_settings_keyboard(chat_id, settings)
    
    await callback.message.edit_reply_markup(reply_markup=markup)
    await callback.answer(f"Строгость изменена на {next_strict}")

@admin_router.callback_query(F.data == "set_close")
async def close_settings_callback(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer("Настройки закрыты.")
