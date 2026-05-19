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


# Лимит таймаута капчи (минуты), 0 в БД = без лимита
TIMEOUT_MIN_MINUTES = 1
TIMEOUT_MAX_MINUTES = 1440  # 24 часа


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
    welcome = settings.get('welcome_message')
    timeout_mins = settings.get('verification_timeout', 0)
    if timeout_mins is None:
        timeout_mins = 0
    
    lang_text = f"Язык: {'🇷🇺 RU' if lang == 'ru' else '🇻🇳 VI' if lang == 'vi' else '🇬🇧 EN'}"
    strictness_texts = {
        0: "Строгость: 0 - Ручное одобрение",
        1: "Строгость: 1 - Слово + кнопка с эмоджи",
        2: "Строгость: 2 - В разработке",
        3: "Строгость: 3 - В разработке"
    }
    strictness_text = strictness_texts.get(strictness, f"Строгость: {strictness}")
    welcome_text = "👋 Приветствие: Настроено" if welcome else "👋 Приветствие: Выкл"
    timeout_text = "⏳ Таймаут: Без лимита" if timeout_mins == 0 else f"⏳ Таймаут: {timeout_mins} мин"
    
    buttons = [
        [InlineKeyboardButton(text=lang_text, callback_data=f"set_lang:{chat_id}:{lang}")],
        [InlineKeyboardButton(text=strictness_text, callback_data=f"set_strict:{chat_id}:{strictness}")],
        [InlineKeyboardButton(text=welcome_text, callback_data=f"set_welcome:{chat_id}")],
        [InlineKeyboardButton(text=timeout_text, callback_data=f"timeout_start:{chat_id}")],
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

@admin_router.message(Command(commands=["start"]), F.chat.type == "private", F.text.startswith("/start set_"))
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
    # Переключение строгости (0 -> 1 -> 2 -> 3 -> 0)
    next_strict = strictness + 1 if strictness < 3 else 0
    
    await update_chat_setting(chat_id, 'captcha_strictness', next_strict)
    
    settings = await get_chat_settings(chat_id)
    markup = generate_settings_keyboard(chat_id, settings)
    
    await callback.message.edit_reply_markup(reply_markup=markup)
    if next_strict == 0:
        await callback.answer(
            "⚠️ Важно!\nЧтобы этот режим работал, обязательно включите «Заявки на вступление» в настройках вашей группы в Telegram!",
            show_alert=True
        )
    else:
        await callback.answer(f"Строгость изменена на {next_strict}")

async def _restore_settings_message(callback: CallbackQuery, bot: Bot, chat_id: int, state: FSMContext):
    """Возвращает сообщение к главному экрану настроек (после отмены / сохранения)."""
    await state.clear()
    try:
        chat = await bot.get_chat(chat_id)
        chat_name = chat.title or str(chat_id)
    except TelegramAPIError:
        chat_name = str(chat_id)
    settings = await get_chat_settings(chat_id)
    markup = generate_settings_keyboard(chat_id, settings)
    await callback.message.edit_text(
        f"⚙️ <b>Настройки для чата:</b> {chat_name}\n\nВыберите параметр для изменения:",
        reply_markup=markup,
        parse_mode="HTML",
    )


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
            [InlineKeyboardButton(text="♾️ Без лимита (снять таймаут)", callback_data=f"timeout_clear:{chat_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"timeout_cancel:{chat_id}")],
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
    await _restore_settings_message(callback, bot, chat_id, state)


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
    await _restore_settings_message(callback, bot, chat_id, state)


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
        await _restore_settings_message(callback, bot, chat_id, state)
        return

    await update_chat_setting(chat_id, "verification_timeout", int(pending))
    await callback.answer(f"Сохранено: {pending} мин.")
    await _restore_settings_message(callback, bot, chat_id, state)


@admin_router.callback_query(F.data.startswith("timeout_discard:"))
async def timeout_discard_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Отмена после ввода числа — в БД не пишем."""
    _, chat_id_str = callback.data.split(":", 1)
    chat_id = int(chat_id_str)
    if not await is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
    await callback.answer("Изменения не сохранены.")
    await _restore_settings_message(callback, bot, chat_id, state)


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
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"timeout_discard:{chat_id}"),
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
        buttons.append([InlineKeyboardButton(text="🗑️ Отключить приветствие", callback_data=f"del_welcome:{chat_id}")])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_welcome:{chat_id}")])
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
    
    await state.clear()
    await callback.answer("Изменение отменено.")
    
    try:
        chat = await bot.get_chat(chat_id)
        chat_name = chat.title or str(chat_id)
    except TelegramAPIError:
        chat_name = str(chat_id)
        
    settings = await get_chat_settings(chat_id)
    markup = generate_settings_keyboard(chat_id, settings)
    await callback.message.edit_text(
        f"⚙️ <b>Настройки для чата:</b> {chat_name}\n\nВыберите параметр для изменения:",
        reply_markup=markup,
        parse_mode="HTML"
    )

@admin_router.callback_query(F.data.startswith("del_welcome:"))
async def del_welcome_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    _, chat_id_str = callback.data.split(":")
    chat_id = int(chat_id_str)
    
    await update_chat_setting(chat_id, 'welcome_message', None)
    await state.clear()
    await callback.answer("Приветствие отключено.", show_alert=True)
    
    try:
        chat = await bot.get_chat(chat_id)
        chat_name = chat.title or str(chat_id)
    except TelegramAPIError:
        chat_name = str(chat_id)
        
    settings = await get_chat_settings(chat_id)
    markup = generate_settings_keyboard(chat_id, settings)
    await callback.message.edit_text(
        f"⚙️ <b>Настройки для чата:</b> {chat_name}\n\nВыберите параметр для изменения:",
        reply_markup=markup,
        parse_mode="HTML"
    )

@admin_router.message(AdminSettings.waiting_for_welcome)
async def process_welcome_message(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    chat_id = data.get("settings_chat_id")
    settings_msg_id = data.get("settings_msg_id")
    
    if not chat_id:
        await state.clear()
        return
        
    # Сохраняем новое приветствие
    new_welcome = message.text
    await update_chat_setting(chat_id, 'welcome_message', new_welcome)
    await state.clear()
    
    # Пытаемся удалить отправленное пользователем сообщение для чистоты
    try:
        await message.delete()
    except TelegramAPIError:
        pass
        
    try:
        chat = await bot.get_chat(chat_id)
        chat_name = chat.title or str(chat_id)
    except TelegramAPIError:
        chat_name = str(chat_id)
        
    settings = await get_chat_settings(chat_id)
    markup = generate_settings_keyboard(chat_id, settings)
    
    # Редактируем исходное сообщение настроек
    if settings_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=message.from_user.id,
                message_id=settings_msg_id,
                text=f"✅ <b>Приветствие успешно сохранено!</b>\n\n⚙️ <b>Настройки для чата:</b> {chat_name}\n\nВыберите параметр для изменения:",
                reply_markup=markup,
                parse_mode="HTML"
            )
            return
        except TelegramAPIError:
            pass
            
    # Запасной вариант
    await message.answer("✅ Приветственное сообщение сохранено!")
    await message.answer(f"⚙️ <b>Настройки для чата:</b> {chat_name}\n\nВыберите параметр для изменения:", reply_markup=markup, parse_mode="HTML")

