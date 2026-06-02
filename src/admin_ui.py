import os
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.exceptions import TelegramAPIError

from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from database import get_chat_settings, update_chat_setting

logger = logging.getLogger(__name__)
admin_router = Router()

class AdminSettings(StatesGroup):
    waiting_for_welcome = State()
    waiting_timeout_input = State()
    waiting_timeout_confirm = State()
    waiting_for_sa_text = State()
    waiting_for_sa_url = State()



# Системный ID, используемый Telegram для отправки сообщений от имени анонимных администраторов групп (@GroupAnonymousBot)
TELEGRAM_ANONYMOUS_BOT_ID = 1087968824

# Лимит таймаута капчи (минуты), 0 в БД = без лимита
TIMEOUT_MIN_MINUTES = 1
TIMEOUT_MAX_MINUTES = 1440  # 24 часа


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором или создателем чата."""
    if user_id == TELEGRAM_ANONYMOUS_BOT_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        status_str = str(member.status).split('.')[-1].lower()
        if status_str in ['administrator', 'creator', 'owner']:
            return True
        return False
    except TelegramAPIError as e:
        logger.error(f"Ошибка при проверке прав пользователя {user_id} в чате {chat_id}: {e}")
        return False

async def sync_admins_for_chat(chat_id: int, bot: Bot):
    """Синхронизирует список администраторов группы с базой данных."""
    try:
        admins = await bot.get_chat_administrators(chat_id)
        admin_ids = [admin.user.id for admin in admins if not admin.user.is_bot]
        from database import set_chat_admins
        await set_chat_admins(chat_id, admin_ids)
        logger.info(f"Синхронизировано {len(admin_ids)} администраторов для чата {chat_id}")
    except TelegramAPIError as e:
        logger.error(f"Не удалось синхронизировать администраторов для чата {chat_id}: {e}")
        # Если чат не найден или бот кикнут/заблокирован, помечаем чат как неактивный
        err_msg = str(e).lower()
        if any(keyword in err_msg for keyword in ["chat not found", "kicked", "forbidden", "not member", "deactivated"]):
            logger.info(f"Деактивируем чат {chat_id} в БД из-за ошибки Telegram API: {e}")
            from database import deactivate_chat
            await deactivate_chat(chat_id)


def get_entry_mode_text(chat_name: str) -> str:
    return (
        f"🚪 <b>Настройки режима входа:</b> {chat_name}\n\n"
        "Укажите, как новые участники будут вступать в группу:\n\n"
        "• <b>Капча в ЛС</b> — новые участники должны пройти проверку в ЛС бота. "
        "Доступно два режима ограничения:\n"
        "   • <b>мягкое</b> — перехват сообщений в чате до верификации. Бот удаляет первое сообщение пользователя и предлагает пройти капчу. При этом сохраняет в памяти текст, который пользователь писал, и возвращает его после успешного прохождения капчи.\n"
        "   • <b>жесткое</b> — ограничение на отправку любых сообщений в Telegram до прохождения капчи.\n"
        "• <b>Заявка с подтверждением</b> — новые участники подают заявку, админы одобряют её кнопками в ЛС.\n"
        "• <b>Только оповещение</b> — новые участники подают заявку, бот присылает простое оповещение админам."
    )

def generate_settings_keyboard(chat_id: int, settings: dict, show_back: bool = False) -> InlineKeyboardMarkup:
    """Генерирует Главное меню настроек чата."""
    back_button = (
        InlineKeyboardButton(text="⬅️ К списку групп", callback_data="adm_back")
        if show_back
        else InlineKeyboardButton(text="Закрыть", callback_data="set_close")
    )
    
    buttons = [
        [InlineKeyboardButton(text="Язык чата", callback_data=f"sub_lang:{chat_id}")],
        [InlineKeyboardButton(text="Режим входа", callback_data=f"sub_entry:{chat_id}")],
        [InlineKeyboardButton(text="Антимат", callback_data=f"sub_antiswear:{chat_id}")],
        [back_button]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def generate_language_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    """Генерирует меню выбора языка с флагами."""
    lang = settings.get('language', 'en')
    
    btn_ru = InlineKeyboardButton(text=f"{'✅ ' if lang == 'ru' else ''}Русский 🇷🇺", callback_data=f"set_lang:{chat_id}:ru")
    btn_en = InlineKeyboardButton(text=f"{'✅ ' if lang == 'en' else ''}English 🇬🇧", callback_data=f"set_lang:{chat_id}:en")
    btn_vi = InlineKeyboardButton(text=f"{'✅ ' if lang == 'vi' else ''}Tiếng Việt 🇻🇳", callback_data=f"set_lang:{chat_id}:vi")
    
    buttons = [
        [btn_ru],
        [btn_en],
        [btn_vi],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data=f"back_main:{chat_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def generate_entry_mode_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    """Генерирует подменю режима входа."""
    strictness = settings.get('captcha_strictness', 1)
    join_buttons_enabled = settings.get('join_buttons_enabled', 1)
    is_soft_mute = settings.get('is_soft_mute', 0)
    welcome = settings.get('welcome_message')
    timeout_mins = settings.get('verification_timeout', 0)
    if timeout_mins is None:
        timeout_mins = 0
        
    mode_captcha = strictness == 1
    mode_approve = (strictness == 0) and (join_buttons_enabled == 1)
    mode_notify = (strictness == 0) and (join_buttons_enabled == 0)
    
    btn_captcha = InlineKeyboardButton(text=f"{'✅ ' if mode_captcha else ''}Капча в ЛС", callback_data=f"set_mode:{chat_id}:captcha")
    btn_approve = InlineKeyboardButton(text=f"{'✅ ' if mode_approve else ''}Заявка с подтверждением", callback_data=f"set_mode:{chat_id}:approve")
    btn_notify = InlineKeyboardButton(text=f"{'✅ ' if mode_notify else ''}Только оповещение", callback_data=f"set_mode:{chat_id}:notify")
    
    buttons = [
        [btn_captcha]
    ]
    
    if mode_captcha:
        btn_soft_mute = InlineKeyboardButton(text=f"{'✅ ' if is_soft_mute == 1 else ''}Мягкое ограничение", callback_data=f"set_mute_type:{chat_id}:soft")
        btn_hard_mute = InlineKeyboardButton(text=f"{'✅ ' if is_soft_mute == 0 else ''}Жесткое ограничение", callback_data=f"set_mute_type:{chat_id}:hard")
        buttons.append([btn_soft_mute, btn_hard_mute])
        
    buttons.append([btn_approve])
    buttons.append([btn_notify])
    
    if mode_captcha:
        timeout_text = "Таймаут: Без лимита" if timeout_mins == 0 else f"Таймаут: {timeout_mins} мин"
        btn_timeout = InlineKeyboardButton(text=timeout_text, callback_data=f"timeout_start:{chat_id}")
        
        welcome_text = "Приветствие: Настроено" if welcome else "Приветствие: Выкл"
        btn_welcome = InlineKeyboardButton(text=welcome_text, callback_data=f"set_welcome:{chat_id}")
        
        buttons.append([btn_timeout])
        buttons.append([btn_welcome])
        if welcome:
            buttons.append([InlineKeyboardButton(text="Посмотреть настроенное приветствие", callback_data=f"show_welcome_preview:{chat_id}")])
    elif mode_approve:
        welcome_text = "Приветствие: Настроено" if welcome else "Приветствие: Выкл"
        btn_welcome = InlineKeyboardButton(text=welcome_text, callback_data=f"set_welcome:{chat_id}")
        buttons.append([btn_welcome])
        if welcome:
            buttons.append([InlineKeyboardButton(text="Посмотреть настроенное приветствие", callback_data=f"show_welcome_preview:{chat_id}")])
        
    buttons.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data=f"back_main:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def generate_antiswear_keyboard(chat_id: int, settings: dict) -> InlineKeyboardMarkup:
    """Генерирует подменю антимата."""
    anti_swear_enabled = settings.get('anti_swear_enabled', 0)
    max_swear_warnings = settings.get('max_swear_warnings', 3)
    
    anti_swear_text = f"Антимат: {'Вкл' if anti_swear_enabled else 'Выкл'}"
    btn_toggle = InlineKeyboardButton(text=anti_swear_text, callback_data=f"set_antiswear:{chat_id}")
    
    warnings_text = f"Лимит предупреждений: {max_swear_warnings}"
    btn_warnings = InlineKeyboardButton(text=warnings_text, callback_data=f"cycle_warnings:{chat_id}")
    
    buttons = [
        [btn_toggle],
        [btn_warnings],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data=f"back_main:{chat_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def open_settings_panel(event: Message | CallbackQuery, bot: Bot, chat_id: int, show_back: bool = False, state: FSMContext = None):
    """Открывает панель настроек чата, если есть права (отправляя новое или редактируя старое сообщение)."""
    user_id = event.from_user.id
    is_callback = isinstance(event, CallbackQuery)
    
    # Сбрасываем любые незавершенные состояния FSM при открытии/смене настроек чата
    if state:
        current_state = await state.get_state()
        if current_state:
            await state.clear()
            logger.info(f"Сброшено состояние FSM ({current_state}) для пользователя {user_id} при открытии настроек чата {chat_id}")
            
    if not await is_chat_admin(bot, chat_id, user_id):
        error_msg = "У вас нет прав администратора в этом чате или бот не добавлен в этот чат."
        if is_callback:
            await event.answer(error_msg, show_alert=True)
        else:
            await event.answer(error_msg)
        return
        
    try:
        chat = await bot.get_chat(chat_id)
        chat_name = chat.title or str(chat_id)
    except TelegramAPIError:
        chat_name = str(chat_id)
        
    settings = await get_chat_settings(chat_id)
    if not settings:
        error_msg = f"Чат <b>{chat_name}</b> еще не зарегистрирован в базе бота."
        if is_callback:
            await event.answer("Чат не зарегистрирован в БД.", show_alert=True)
        else:
            await event.answer(error_msg, parse_mode="HTML")
        return
        
    text = f"⚙️ <b>Настройки для чата:</b> {chat_name}\n\nВыберите параметр для изменения:"
    markup = generate_settings_keyboard(chat_id, settings, show_back=show_back)
    
    if is_callback:
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup, parse_mode="HTML")

@admin_router.message(F.chat.type == "private", F.forward_from_chat)
async def handle_forwarded_message(message: Message, bot: Bot, state: FSMContext):
    """Обрабатывает пересланные из публичного чата сообщения для открытия настроек."""
    chat_id = message.forward_from_chat.id
    if message.forward_from_chat.type in ["group", "supergroup"]:
        await open_settings_panel(message, bot, chat_id, state=state)

@admin_router.message(Command(commands=["start"]), F.chat.type == "private", F.text.startswith("/start set_"))
async def handle_start_settings(message: Message, bot: Bot, state: FSMContext):
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
            await open_settings_panel(message, bot, chat_id, state=state)
        except ValueError:
            await message.answer("Неверный формат ссылки настроек.")

async def show_admin_chats(event: Message | CallbackQuery, bot: Bot):
    """Отображает список групп в ЛС, где пользователь является администратором."""
    user_id = event.from_user.id
    is_callback = isinstance(event, CallbackQuery)
    
    from database import get_admin_chats
    try:
        chats = await get_admin_chats(user_id)
    except Exception as e:
        error_text = f"⚠️ Ошибка базы данных: {e}"
        if is_callback:
            await event.answer(error_text, show_alert=True)
        else:
            await event.answer(error_text)
        return

    admin_id_env = os.getenv("ADMIN_ID")
    is_sa = False
    if admin_id_env:
        try:
            if int(admin_id_env) == user_id:
                is_sa = True
        except ValueError:
            pass

    buttons = []
    for chat_data in chats:
        chat_id = chat_data["chat_id"]
        title = chat_data.get("title") or f"Чат {chat_id}"
        buttons.append([InlineKeyboardButton(text=f"⚙️ {title}", callback_data=f"adm_set:{chat_id}")])
    
    if is_sa:
        buttons.append([InlineKeyboardButton(text="👑 Обязательная кнопка", callback_data="sa_button_settings")])
        
    if not chats and not is_sa:
        text = "💬 <b>У вас нет чатов для настройки.</b>\n\nВы должны быть администратором в чатах, куда добавлен этот бот."
        if is_callback:
            await event.message.edit_text(text, parse_mode="HTML")
            await event.answer()
        else:
            await event.answer(text, parse_mode="HTML")
        return

    # Кнопка закрытия меню
    buttons.append([InlineKeyboardButton(text="❌ Закрыть меню", callback_data="set_close")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    text = "⚙️ <b>Панель управления QuietLobbyBot</b>\n\nВыберите группу для изменения настроек:"
    if is_callback:
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup, parse_mode="HTML")


@admin_router.message(Command(commands=["settings"]), F.chat.type == "private")
async def handle_settings_private(message: Message, bot: Bot):
    """Показывает список администрируемых чатов в ЛС по команде /settings."""
    await show_admin_chats(message, bot)

@admin_router.callback_query(F.data.startswith("adm_set:"))
async def handle_admin_set_callback(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """Открывает настройки конкретного чата из списка в ЛС."""
    chat_id = int(callback.data.split(":")[1])
    # Передаем show_back=True, чтобы кнопка «⬅️ К списку групп» была видна
    await open_settings_panel(callback, bot, chat_id, show_back=True, state=state)

@admin_router.callback_query(F.data == "adm_back")
async def handle_admin_back_callback(callback: CallbackQuery, bot: Bot):
    """Возвращает к списку чатов."""
    await show_admin_chats(callback, bot)

@admin_router.callback_query(F.data.startswith("sub_lang:"))
async def sub_language_callback(callback: CallbackQuery, bot: Bot):
    chat_id = int(callback.data.split(":")[1])
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
    settings = await get_chat_settings(chat_id)
    markup = generate_language_keyboard(chat_id, settings)
    await callback.message.edit_text(
        "🌍 <b>Настройка языка чата</b>\n\nВыберите основной язык общения для системных сообщений бота в группе:",
        reply_markup=markup,
        parse_mode="HTML"
    )
    await callback.answer()

@admin_router.callback_query(F.data.startswith("sub_entry:"))
async def sub_entry_callback(callback: CallbackQuery, bot: Bot):
    chat_id = int(callback.data.split(":")[1])
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
    try:
        chat = await bot.get_chat(chat_id)
        chat_name = chat.title or str(chat_id)
    except TelegramAPIError:
        chat_name = str(chat_id)
    settings = await get_chat_settings(chat_id)
    markup = generate_entry_mode_keyboard(chat_id, settings)
    text = get_entry_mode_text(chat_name)
    await callback.message.edit_text(
        text,
        reply_markup=markup,
        parse_mode="HTML"
    )
    await callback.answer()

@admin_router.callback_query(F.data.startswith("sub_antiswear:"))
async def sub_antiswear_callback(callback: CallbackQuery, bot: Bot):
    chat_id = int(callback.data.split(":")[1])
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
    settings = await get_chat_settings(chat_id)
    markup = generate_antiswear_keyboard(chat_id, settings)
    await callback.message.edit_text(
        "🤬 <b>Настройки антимата</b>\n\nВключите автоматическую фильтрацию нецензурной лексики:",
        reply_markup=markup,
        parse_mode="HTML"
    )
    await callback.answer()

@admin_router.callback_query(F.data.startswith("back_main:"))
async def back_main_callback(callback: CallbackQuery, bot: Bot):
    chat_id = int(callback.data.split(":")[1])
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
    try:
        chat = await bot.get_chat(chat_id)
        chat_name = chat.title or str(chat_id)
    except TelegramAPIError:
        chat_name = str(chat_id)
    settings = await get_chat_settings(chat_id)
    markup = generate_settings_keyboard(chat_id, settings, show_back=True)
    await callback.message.edit_text(
        f"⚙️ <b>Настройки для чата:</b> {chat_name}\n\nВыберите параметр для изменения:",
        reply_markup=markup,
        parse_mode="HTML"
    )
    await callback.answer()

@admin_router.callback_query(F.data.startswith("set_lang:"))
async def change_language_callback(callback: CallbackQuery, bot: Bot):
    _, chat_id_str, selected_lang = callback.data.split(":")
    chat_id = int(chat_id_str)
    
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
        
    await update_chat_setting(chat_id, 'language', selected_lang)
    
    settings = await get_chat_settings(chat_id)
    markup = generate_language_keyboard(chat_id, settings)
    
    try:
        await callback.message.edit_text(
            "🌍 <b>Настройка языка чата</b>\n\nВыберите основной язык общения для системных сообщений бота в группе:",
            reply_markup=markup,
            parse_mode="HTML"
        )
    except TelegramAPIError:
        pass
    await callback.answer(f"Язык изменен на {selected_lang.upper()}")

@admin_router.callback_query(F.data.startswith("set_mode:"))
async def change_mode_callback(callback: CallbackQuery, bot: Bot):
    _, chat_id_str, selected_mode = callback.data.split(":")
    chat_id = int(chat_id_str)
    
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
        
    if selected_mode == 'captcha':
        await update_chat_setting(chat_id, 'captcha_strictness', 1)
    elif selected_mode == 'approve':
        await update_chat_setting(chat_id, 'captcha_strictness', 0)
        await update_chat_setting(chat_id, 'join_buttons_enabled', 1)
    elif selected_mode == 'notify':
        await update_chat_setting(chat_id, 'captcha_strictness', 0)
        await update_chat_setting(chat_id, 'join_buttons_enabled', 0)
        
    try:
        chat = await bot.get_chat(chat_id)
        chat_name = chat.title or str(chat_id)
    except TelegramAPIError:
        chat_name = str(chat_id)
        
    settings = await get_chat_settings(chat_id)
    markup = generate_entry_mode_keyboard(chat_id, settings)
    
    text = get_entry_mode_text(chat_name)
    
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramAPIError:
        pass
        
    mode_text = {
        'captcha': 'Капча в ЛС',
        'approve': 'Заявка с подтверждением',
        'notify': 'Только оповещение'
    }.get(selected_mode, selected_mode)
    
    if selected_mode in ['approve', 'notify']:
        # Проверяем, есть ли у бота право can_invite_users (необходимо для получения заявок)
        bot_has_invite_right = False
        try:
            bot_info = await bot.get_me()
            bot_member = await bot.get_chat_member(chat_id=chat_id, user_id=bot_info.id)
            if hasattr(bot_member, 'can_invite_users') and bot_member.can_invite_users:
                bot_has_invite_right = True
        except TelegramAPIError:
            pass
        
        if not bot_has_invite_right:
            await callback.answer(
                "⚠️ Внимание!\n\n"
                "У бота нет права «Добавлять участников» (Invite Users).\n"
                "Без этого права бот НЕ будет получать заявки на вступление от Telegram!\n\n"
                "Зайдите в: Настройки группы → Администраторы → Бот → включите «Add Members / Invite Users via link».",
                show_alert=True
            )
        else:
            await callback.answer(
                "⚠️ Важно!\nДля работы режимов заявок обязательно включите «Заявки на вступление» в настройках группы в Telegram!",
                show_alert=True
            )
    else:
        await callback.answer(f"Выбран режим: {mode_text}")

@admin_router.callback_query(F.data.startswith("set_mute_type:"))
async def set_mute_callback(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    chat_id = int(parts[1])
    mute_type = parts[2]  # 'soft' или 'hard'
    
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
        
    new_mute = 1 if mute_type == 'soft' else 0
    await update_chat_setting(chat_id, 'is_soft_mute', new_mute)
    
    try:
        chat = await bot.get_chat(chat_id)
        chat_name = chat.title or str(chat_id)
    except TelegramAPIError:
        chat_name = str(chat_id)
        
    settings = await get_chat_settings(chat_id)
    markup = generate_entry_mode_keyboard(chat_id, settings)
    
    text = get_entry_mode_text(chat_name)
    
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramAPIError:
        pass
        
    mute_text = "Мягкий" if new_mute else "Жесткий"
    await callback.answer(f"Тип ограничения изменен на {mute_text}")

async def _restore_settings_message(callback: CallbackQuery, bot: Bot, chat_id: int, state: FSMContext):
    """Возвращает сообщение к главному экрану настроек (после отмены / сохранения)."""
    await state.clear()
    try:
        chat = await bot.get_chat(chat_id)
        chat_name = chat.title or str(chat_id)
    except TelegramAPIError:
        chat_name = str(chat_id)
    settings = await get_chat_settings(chat_id)
    markup = generate_settings_keyboard(chat_id, settings, show_back=True)
    await callback.message.edit_text(
        f"⚙️ <b>Настройки для чата:</b> {chat_name}\n\nВыберите параметр для изменения:",
        reply_markup=markup,
        parse_mode="HTML",
    )

async def _restore_entry_menu_message(event: Message | CallbackQuery, bot: Bot, chat_id: int, state: FSMContext):
    """Возвращает к подменю режима входа."""
    try:
        chat = await bot.get_chat(chat_id)
        chat_name = chat.title or str(chat_id)
    except TelegramAPIError:
        chat_name = str(chat_id)
    settings = await get_chat_settings(chat_id)
    markup = generate_entry_mode_keyboard(chat_id, settings)
    
    text = get_entry_mode_text(chat_name)
    
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        data = await state.get_data()
        settings_msg_id = data.get("settings_msg_id")
        if settings_msg_id:
            try:
                await bot.edit_message_text(
                    chat_id=event.from_user.id,
                    message_id=settings_msg_id,
                    text=text,
                    reply_markup=markup,
                    parse_mode="HTML"
                )
                await state.clear()
                return
            except TelegramAPIError:
                pass
        await bot.send_message(chat_id=event.from_user.id, text=text, reply_markup=markup, parse_mode="HTML")
    await state.clear()


@admin_router.callback_query(F.data.startswith("timeout_start:"))
async def timeout_start_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Открывает сценарий смены таймаута: инструкция и ввод числа (без записи в БД до подтверждения)."""
    _, chat_id_str = callback.data.split(":", 1)
    chat_id = int(chat_id_str)

    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return

    await state.set_state(AdminSettings.waiting_timeout_input)
    await state.update_data(
        settings_chat_id=chat_id,
        settings_msg_id=callback.message.message_id,
    )

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Без лимита (снять таймаут)", callback_data=f"timeout_clear:{chat_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"timeout_cancel:{chat_id}")],
        ]
    )

    await callback.message.edit_text(
        "⏳ <b>Лимит времени на прохождение капчи</b>\n\n"
        "Сейчас новичок должен успеть нажать кнопку в группе и пройти проверку в ЛС за заданное время, "
        "иначе он будет удалён из чата.\n\n"
        f"📝 <b>Введи одно целое число</b> — сколько <b>минут</b> даётся на прохождение (от "
        f"<code>{TIMEOUT_MIN_MINUTES}</code> до <code>{TIMEOUT_MAX_MINUTES}</code>).\n"
        "Отправь отдельным сообщением, например: <code>10</code>\n\n"
        "После ввода появятся кнопки <b>Сохранить</b> или <b>Отменить</b> — в базу попадёт только сохранённое значение.\n\n"
        "Или нажми «Без лимита», чтобы убрать ограничение по времени.",
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("timeout_cancel:"))
async def timeout_cancel_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Отмена сценария таймаута с первого экрана — без изменений в БД."""
    _, chat_id_str = callback.data.split(":", 1)
    chat_id = int(chat_id_str)
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
    await callback.answer("Отменено.")
    await callback.message.answer("❌ Изменение таймаута отменено.")
    await _restore_entry_menu_message(callback, bot, chat_id, state)


@admin_router.callback_query(F.data.startswith("timeout_clear:"))
async def timeout_clear_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Снять лимит (0 в БД) — одна явная запись, без цикла."""
    _, chat_id_str = callback.data.split(":", 1)
    chat_id = int(chat_id_str)
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
    await update_chat_setting(chat_id, "verification_timeout", 0)
    await callback.answer("Лимит снят.")
    await callback.message.answer("✅ Лимит времени снят.")
    await _restore_entry_menu_message(callback, bot, chat_id, state)


@admin_router.callback_query(F.data.startswith("timeout_save:"))
async def timeout_save_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Подтверждение введённого значения — запись в БД один раз."""
    _, chat_id_str = callback.data.split(":", 1)
    chat_id = int(chat_id_str)
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return

    data = await state.get_data()
    pending = data.get("pending_timeout_minutes")
    if pending is None:
        await callback.answer("Нет данных для сохранения. Начни сначала.", show_alert=True)
        await callback.message.answer("⚠️ Ошибка сохранения таймаута.")
        await _restore_entry_menu_message(callback, bot, chat_id, state)
        return

    await update_chat_setting(chat_id, "verification_timeout", int(pending))
    await callback.answer(f"Сохранено: {pending} мин.")
    await callback.message.answer(f"✅ Сохранено: {pending} мин.")
    await _restore_entry_menu_message(callback, bot, chat_id, state)


@admin_router.callback_query(F.data.startswith("timeout_discard:"))
async def timeout_discard_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Отмена после ввода числа — в БД не пишем."""
    _, chat_id_str = callback.data.split(":", 1)
    chat_id = int(chat_id_str)
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
    await callback.answer("Изменения не сохранены.")
    await callback.message.answer("❌ Изменения не сохранены.")
    await _restore_entry_menu_message(callback, bot, chat_id, state)


@admin_router.message(
    AdminSettings.waiting_timeout_input,
    F.chat.type == "private",
    F.text,
    ~F.text.startswith("/"),
)
async def process_timeout_input(message: Message, state: FSMContext, bot: Bot):
    """Принимает число минут, показывает подтверждение с кнопками Сохранить / Отменить."""
    data = await state.get_data()
    chat_id = data.get("settings_chat_id")
    settings_msg_id = data.get("settings_msg_id")
    if not chat_id or not settings_msg_id:
        await state.clear()
        return

    if not await is_chat_admin(bot, chat_id, message.from_user.id):
        await state.clear()
        return

    raw = (message.text or "").strip()
    try:
        minutes = int(raw)
    except ValueError:
        await message.answer(
            f"Нужно целое число минут от {TIMEOUT_MIN_MINUTES} до {TIMEOUT_MAX_MINUTES}. Попробуй ещё раз."
        )
        return

    if minutes < TIMEOUT_MIN_MINUTES or minutes > TIMEOUT_MAX_MINUTES:
        await message.answer(
            f"Число должно быть от {TIMEOUT_MIN_MINUTES} до {TIMEOUT_MAX_MINUTES} (минут). Попробуй ещё раз."
        )
        return

    await state.update_data(pending_timeout_minutes=minutes)
    await state.set_state(AdminSettings.waiting_timeout_confirm)

    try:
        await message.delete()
    except TelegramAPIError:
        pass

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Сохранить", callback_data=f"timeout_save:{chat_id}"),
                InlineKeyboardButton(text="Отменить", callback_data=f"timeout_discard:{chat_id}"),
            ]
        ]
    )

    try:
        await bot.edit_message_text(
            chat_id=message.from_user.id,
            message_id=settings_msg_id,
            text=(
                f"⏳ Ты указал: <b>{minutes}</b> мин. на прохождение капчи.\n\n"
                "Нажми <b>Сохранить</b>, чтобы записать в настройки чата, или <b>Отменить</b> — без изменений в базе."
            ),
            reply_markup=markup,
            parse_mode="HTML",
        )
    except TelegramAPIError:
        await message.answer(
            f"Указано: {minutes} мин. Сохранить?",
            reply_markup=markup,
            parse_mode="HTML",
        )


@admin_router.message(
    AdminSettings.waiting_timeout_confirm,
    F.chat.type == "private",
    F.text,
    ~F.text.startswith("/"),
)
async def process_timeout_confirm_guard(message: Message, bot: Bot):
    """В фазе подтверждения текст не принимаем — только кнопки."""
    await message.answer("Сейчас нужно нажать «Сохранить» или «Отменить» под сообщением с настройкой.")

@admin_router.callback_query(F.data == "timeout_cancel")
async def cancel_timeout_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Изменение таймаута отменено.")
    await callback.answer()

# --- ОБРАБОТЧИКИ АНТИМАТА ---

@admin_router.callback_query(F.data.startswith("set_antiswear:"))
async def toggle_antiswear_callback(callback: CallbackQuery, bot: Bot):
    _, chat_id_str = callback.data.split(":")
    chat_id = int(chat_id_str)
    
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
        
    settings = await get_chat_settings(chat_id)
    current_state = settings.get('anti_swear_enabled', 0)
    new_state = 0 if current_state else 1
    
    await update_chat_setting(chat_id, 'anti_swear_enabled', new_state)
    logger.info(f"Админ {callback.from_user.id} изменил антимат в чате {chat_id} на {new_state}")
    
    settings['anti_swear_enabled'] = new_state
    markup = generate_antiswear_keyboard(chat_id, settings)
    
    text = "🤬 <b>Настройки антимата</b>\n\nВключите автоматическую фильтрацию нецензурной лексики:"
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramAPIError:
        pass
    await callback.answer(f"Антимат {'включен' if new_state else 'выключен'}")

@admin_router.callback_query(F.data.startswith("cycle_warnings:"))
async def cycle_warnings_callback(callback: CallbackQuery, bot: Bot):
    _, chat_id_str = callback.data.split(":")
    chat_id = int(chat_id_str)
    
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
        
    settings = await get_chat_settings(chat_id)
    current_warnings = settings.get('max_swear_warnings', 3)
    
    # Циклическое переключение: 1 -> 2 -> 3 -> 4 -> 5 -> 1
    new_warnings = current_warnings + 1 if current_warnings < 5 else 1
    
    await update_chat_setting(chat_id, 'max_swear_warnings', new_warnings)
    logger.info(f"Админ {callback.from_user.id} изменил лимит предупреждений в чате {chat_id} на {new_warnings}")
    
    settings['max_swear_warnings'] = new_warnings
    markup = generate_antiswear_keyboard(chat_id, settings)
    
    text = "🤬 <b>Настройки антимата</b>\n\nВключите автоматическую фильтрацию нецензурной лексики:"
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramAPIError:
        pass
    await callback.answer(f"Лимит предупреждений изменен на {new_warnings}")

@admin_router.callback_query(F.data == "set_close")
async def close_settings_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer("Настройки закрыты.")

@admin_router.callback_query(F.data.startswith("set_welcome:"))
async def set_welcome_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    _, chat_id_str = callback.data.split(":")
    chat_id = int(chat_id_str)
    
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
        
    await state.update_data(settings_chat_id=chat_id, settings_msg_id=callback.message.message_id)
    await state.set_state(AdminSettings.waiting_for_welcome)
    
    settings = await get_chat_settings(chat_id)
    welcome = settings.get('welcome_message')
    
    buttons = []
    if welcome:
        buttons.append([InlineKeyboardButton(text="Отключить приветствие", callback_data=f"del_welcome:{chat_id}")])
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data=f"cancel_welcome:{chat_id}")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text(
        "💬 <b>Настройка приветственного сообщения</b>\n\n"
        "Отправь мне текст, который бот пришлет в чат после успешного прохождения капчи.\n\n"
        "ℹ️ Ты можешь использовать:\n"
        "• <code>{name}</code> — имя пользователя\n"
        "• <code>{mention}</code> — кликабельное упоминание пользователя\n\n"
        "🌟 <b>Добавление инлайн-кнопок:</b>\n"
        "Ты можешь добавить одну или несколько кнопок-ссылок к своему приветствию! Для этого просто пиши каждую кнопку с новой строки в формате:\n"
        "<code>Текст кнопки | ссылка</code>\n"
        "<i>Пример: Наш канал | t.me/my_channel</i>\n\n"
        "Для управления настройкой используй инлайн-кнопки ниже:",
        reply_markup=markup,
        parse_mode="HTML"
    )
    await callback.answer()

@admin_router.callback_query(F.data.startswith("cancel_welcome:"))
async def cancel_welcome_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    _, chat_id_str = callback.data.split(":")
    chat_id = int(chat_id_str)
    
    await callback.answer("Изменение отменено.")
    await callback.message.answer("❌ Изменение отменено.")
    await _restore_entry_menu_message(callback, bot, chat_id, state)

@admin_router.callback_query(F.data.startswith("del_welcome:"))
async def del_welcome_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    _, chat_id_str = callback.data.split(":")
    chat_id = int(chat_id_str)
    
    await update_chat_setting(chat_id, 'welcome_message', None)
    await callback.answer("Приветствие отключено.", show_alert=True)
    await callback.message.answer("✅ Приветствие отключено.")
    await _restore_entry_menu_message(callback, bot, chat_id, state)

@admin_router.message(AdminSettings.waiting_for_welcome)
async def process_welcome_message(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    chat_id = data.get("settings_chat_id")
    
    if not chat_id:
        await state.clear()
        return
        
    # Сохраняем новое приветствие
    new_welcome = message.text
    await update_chat_setting(chat_id, 'welcome_message', new_welcome)
    
    # Пытаемся удалить отправленное пользователем сообщение для чистоты
    try:
        await message.delete()
    except TelegramAPIError:
        pass
        
    await bot.send_message(chat_id=message.from_user.id, text="✅ Приветствие успешно сохранено!")
    await _restore_entry_menu_message(message, bot, chat_id, state)

@admin_router.callback_query(F.data.startswith("show_welcome_preview:"))
async def show_welcome_preview_callback(callback: CallbackQuery, bot: Bot):
    import html
    from handlers.captcha import parse_welcome_message
    
    chat_id = int(callback.data.split(":")[1])
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
        
    settings = await get_chat_settings(chat_id)
    welcome_msg = settings.get("welcome_message") if settings else None
    
    if not welcome_msg:
        await callback.answer("Приветствие не настроено в этом чате.", show_alert=True)
        return
        
    user_name = html.escape(callback.from_user.first_name)
    user_mention = f'<a href="tg://user?id={callback.from_user.id}">{user_name}</a>'
    formatted_welcome = welcome_msg.replace("{name}", user_name).replace("{mention}", user_mention)
    
    welcome_text, welcome_markup = await parse_welcome_message(formatted_welcome)
    
    preview_header = "👀 <b>Пример приветственного сообщения:</b>\n\n"
    
    try:
        await callback.message.answer(
            text=f"{preview_header}{welcome_text}",
            reply_markup=welcome_markup,
            parse_mode="HTML"
        )
        await callback.answer("Пример приветствия отправлен.")
    except TelegramAPIError as e:
        logger.error(f"Не удалось отправить превью приветствия администратору {callback.from_user.id}: {e}")
        await callback.answer("Ошибка при отправке превью сообщения.", show_alert=True)


async def show_sa_button_settings_menu(message_or_callback: Message | CallbackQuery):
    """Вспомогательная функция для отображения меню управления обязательной кнопкой."""
    import html
    from database import get_global_setting
    
    sa_text = await get_global_setting("sa_button_text")
    sa_url = await get_global_setting("sa_button_url")
    
    text = (
        "👑 <b>Обязательная кнопка суперадмина</b>\n\n"
        "Эта кнопка автоматически добавляется в конец всех приветственных сообщений в группах. "
        "Обычные администраторы чатов не видят её в меню настройки приветствия и не могут её удалить.\n\n"
        f"📝 <b>Текст кнопки:</b> {html.escape(sa_text) if sa_text else '<i>Не настроен</i>'}\n"
        f"🔗 <b>Ссылка кнопки:</b> {html.escape(sa_url) if sa_url else '<i>Не настроена</i>'}\n\n"
        "Выберите действие:"
    )
    
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data="sa_edit_text")],
        [InlineKeyboardButton(text="🔗 Изменить ссылку", callback_data="sa_edit_url")],
        [InlineKeyboardButton(text="🗑️ Удалить кнопку", callback_data="sa_delete_button")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="adm_back")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await message_or_callback.answer(text, reply_markup=markup, parse_mode="HTML")


@admin_router.callback_query(F.data == "sa_button_settings")
async def handle_sa_button_settings(callback: CallbackQuery, bot: Bot):
    # Проверяем права суперадмина
    admin_id_env = os.getenv("ADMIN_ID")
    if not admin_id_env or int(admin_id_env) != callback.from_user.id:
        await callback.answer("У вас нет прав!", show_alert=True)
        return
    await show_sa_button_settings_menu(callback)
    await callback.answer()


@admin_router.callback_query(F.data == "sa_edit_text")
async def handle_sa_edit_text(callback: CallbackQuery, state: FSMContext):
    admin_id_env = os.getenv("ADMIN_ID")
    if not admin_id_env or int(admin_id_env) != callback.from_user.id:
        await callback.answer("У вас нет прав!", show_alert=True)
        return
        
    await state.set_state(AdminSettings.waiting_for_sa_text)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="sa_button_settings")]
    ])
    await callback.message.edit_text(
        "📝 Отправьте мне новый текст для обязательной кнопки (максимум 50 символов):",
        reply_markup=markup
    )
    await callback.answer()


@admin_router.message(AdminSettings.waiting_for_sa_text)
async def process_sa_text(message: Message, state: FSMContext, bot: Bot):
    import html
    admin_id_env = os.getenv("ADMIN_ID")
    if not admin_id_env or int(admin_id_env) != message.from_user.id:
        return
        
    new_text = message.text.strip()
    if len(new_text) > 50:
        await message.answer("Текст кнопки слишком длинный (максимум 50 символов). Попробуйте еще раз:")
        return
        
    from database import set_global_setting
    await set_global_setting("sa_button_text", new_text)
    await state.clear()
    
    await message.answer(f"✅ Текст кнопки успешно изменен на: <b>{html.escape(new_text)}</b>", parse_mode="HTML")
    await show_sa_button_settings_menu(message)


@admin_router.callback_query(F.data == "sa_edit_url")
async def handle_sa_edit_url(callback: CallbackQuery, state: FSMContext):
    admin_id_env = os.getenv("ADMIN_ID")
    if not admin_id_env or int(admin_id_env) != callback.from_user.id:
        await callback.answer("У вас нет прав!", show_alert=True)
        return
        
    await state.set_state(AdminSettings.waiting_for_sa_url)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="sa_button_settings")]
    ])
    await callback.message.edit_text(
        "🔗 Отправьте мне новую ссылку для обязательной кнопки (должна начинаться с http://, https:// или t.me/):",
        reply_markup=markup
    )
    await callback.answer()


@admin_router.message(AdminSettings.waiting_for_sa_url)
async def process_sa_url(message: Message, state: FSMContext, bot: Bot):
    import html
    admin_id_env = os.getenv("ADMIN_ID")
    if not admin_id_env or int(admin_id_env) != message.from_user.id:
        return
        
    new_url = message.text.strip()
    if not (new_url.startswith("http://") or new_url.startswith("https://") or new_url.startswith("t.me/")):
        await message.answer("Неверный формат ссылки. Ссылка должна начинаться с http://, https:// или t.me/. Попробуйте еще раз:")
        return
        
    from database import set_global_setting
    await set_global_setting("sa_button_url", new_url)
    await state.clear()
    
    await message.answer(f"✅ Ссылка кнопки успешно изменена на: <code>{html.escape(new_url)}</code>", parse_mode="HTML")
    await show_sa_button_settings_menu(message)


@admin_router.callback_query(F.data == "sa_delete_button")
async def handle_sa_delete_button(callback: CallbackQuery):
    admin_id_env = os.getenv("ADMIN_ID")
    if not admin_id_env or int(admin_id_env) != callback.from_user.id:
        await callback.answer("У вас нет прав!", show_alert=True)
        return
        
    from database import set_global_setting
    await set_global_setting("sa_button_text", None)
    await set_global_setting("sa_button_url", None)
    
    await callback.answer("Обязательная кнопка удалена!", show_alert=True)
    await show_sa_button_settings_menu(callback)



