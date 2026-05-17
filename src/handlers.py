import asyncio
import aiohttp
import logging
import time
import os
import html
from aiogram import Router, F, Bot
from aiogram.types import ChatMemberUpdated, ChatPermissions, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, IS_NOT_MEMBER, MEMBER
from aiogram.filters import Command
from aiogram.exceptions import TelegramAPIError
from keyboards import generate_emoji_captcha, TRANSLATIONS

logger = logging.getLogger(__name__)
router = Router()

# Хранилище временных данных: (chat_id, user_id) -> message_id временного сообщения в группе
group_prompts = {}

# Таймаут верификации в секундах (60 секунд = 1 минута)
VERIFICATION_TIMEOUT = 60

async def is_global_spammer(user_id: int) -> bool:
    """
    Проверяет ID пользователя в глобальной базе спамеров CAS (Combot Anti-Spam).
    """
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


async def verification_timeout_task(chat_id: int, user_id: int, bot: Bot, lang: str):
    """
    Фоновый таймер: если пользователь не прошел капчу за VERIFICATION_TIMEOUT секунд,
    он кикается из чата, а временное сообщение удаляется.
    """
    await asyncio.sleep(VERIFICATION_TIMEOUT)
    
    # Если связка (chat_id, user_id) всё еще в хранилище, значит проверка не пройдена
    if (chat_id, user_id) in group_prompts:
        logger.info(f"[ТАЙМАУТ] Время вышло для пользователя {user_id} в чате {chat_id}")
        
        # Удаляем временное сообщение из группы
        group_msg_id = group_prompts.get((chat_id, user_id))
        if group_msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=group_msg_id)
            except TelegramAPIError as e:
                logger.error(f"Не удалось удалить временное сообщение при таймауте: {e}")
            finally:
                group_prompts.pop((chat_id, user_id), None)
                
        # Кикаем пользователя (бан + мгновенный разбан)
        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            logger.info(f"[ТАЙМАУТ] Пользователь {user_id} успешно кикнут из чата {chat_id}.")
        except TelegramAPIError as e:
            logger.error(f"Не удалось кикнуть пользователя {user_id} по таймауту: {e}")


@router.message(F.new_chat_members)
async def handle_new_member(message: Message, bot: Bot):
    """
    Ловит вступление нового участника (включая повторные входы), проверяет по CAS,
    мутит его, удаляет системное сообщение и отправляет капчу.
    """
    chat_id = message.chat.id
    chat_name = html.escape(message.chat.title or "нашего чата")
    
    # Мгновенно удаляем служебное сообщение о входе
    try:
        await message.delete()
        logger.info(f"[ОЧИСТКА] Системное сообщение о входе в чате {chat_id} успешно удалено.")
    except TelegramAPIError as e:
        logger.warning(f"Не удалось удалить системное сообщение о входе: {e}")

    for member in message.new_chat_members:
        if member.is_bot:
            continue
            
        user_id = member.id
        user_name = html.escape(member.first_name)
        
        # 1. Проверяем в глобальной базе спамеров CAS
        if await is_global_spammer(user_id):
            try:
                await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
                logger.info(f"[CAS] Спамер {user_id} обнаружен при входе и забанен.")
            except TelegramAPIError as e:
                logger.error(f"Не удалось забанить спамера {user_id}: {e}")
            continue

        # 2. Накладываем Mute (запрет на отправку любых сообщений)
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=False,
                    can_send_audios=False,
                    can_send_documents=False,
                    can_send_photos=False,
                    can_send_videos=False,
                    can_send_video_notes=False,
                    can_send_voice_notes=False,
                    can_send_polls=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False
                )
            )
            logger.info(f"[MUTE] Пользователь {user_id} временно ограничен в чате {chat_id}.")
        except TelegramAPIError as e:
            logger.error(f"Не удалось наложить MUTE на {user_id}: {e}")
            continue

        # Определение языка пользователя
        raw_lang = member.language_code
        user_lang = raw_lang or "en"
        if user_lang.startswith("ru"):
            lang = "ru"
        elif user_lang.startswith("vi"):
            lang = "vi"
        else:
            lang = "en"

        # 3. Отправляем временное сообщение в группу
        bot_info = await bot.get_me()
        bot_username = bot_info.username
        
        # Создаем кнопку со ссылкой на ЛС бота с параметром start=verify_CHATID
        # Заменяем минус в ID чата на символ 'm' для соответствия правилам Telegram (парсинг параметров)
        clean_chat_id = str(chat_id).replace("-", "m")
        verify_url = f"https://t.me/{bot_username}?start=verify_{clean_chat_id}"
        
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=TRANSLATIONS[lang]["btn_verify"], url=verify_url, style="success")]
        ])
        
        text = TRANSLATIONS[lang]["group_greet"].format(name=user_name, chat_name=chat_name)
        
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=markup,
                parse_mode="HTML"
            )
            # Запоминаем ID сообщения, чтобы удалить его позже
            group_prompts[(chat_id, user_id)] = msg.message_id
            logger.info(f"[ШЛЮЗ] Временное сообщение отправлено в чат {chat_id} для {user_id}. MsgID: {msg.message_id}")
            
            # Запускаем фоновую задачу таймаута
            asyncio.create_task(verification_timeout_task(chat_id, user_id, bot, lang))
            
        except TelegramAPIError as e:
            logger.error(f"Не удалось отправить приветственное сообщение в группу: {e}")


@router.message(F.left_chat_member)
async def handle_left_chat_member(message: Message):
    """
    Мгновенно удаляет служебные системные сообщения Telegram вида 'Пользователь покинул группу',
    чтобы чат оставался абсолютно чистым.
    """
    try:
        await message.delete()
        logger.info(f"[ОЧИСТКА] Системное сообщение о выходе в чате {message.chat.id} успешно удалено.")
    except TelegramAPIError as e:
        logger.warning(f"Не удалось удалить системное сообщение о выходе: {e}")


@router.message(Command(commands=["start"]), F.chat.type == "private")
async def handle_start_private(message: Message, bot: Bot):
    """
    Обрабатывает команду /start с параметром verify_CHATID в личных сообщениях.
    Генерирует и отправляет пользователю Emoji-капчу.
    """
    args = message.text.split()
    if len(args) != 2 or not args[1].startswith("verify_"):
        await message.answer("👋 Привет! Я QuietLobbyBot — бот-модератор.\nЯ помогаю защищать публичные группы от спамеров.")
        return

    try:
        # Восстанавливаем ID чата (символ 'm' меняем обратно на минус)
        raw_chat_id = args[1].split("_")[1]
        if raw_chat_id.startswith("m"):
            chat_id = int("-" + raw_chat_id[1:])
        else:
            chat_id = int(raw_chat_id)
            
        user_id = message.from_user.id
        
        # Проверяем, есть ли этот пользователь в списке ожидания в группе
        if (chat_id, user_id) not in group_prompts:
            await message.answer("Заявка на проверку не найдена или время верификации истекло.")
            return
            
        # Определяем язык
        user_lang = message.from_user.language_code or "en"
        if user_lang.startswith("ru"):
            lang = "ru"
        elif user_lang.startswith("vi"):
            lang = "vi"
        else:
            lang = "en"
            
        # Получаем данные о группе для приветствия
        try:
            chat = await bot.get_chat(chat_id)
            chat_name = chat.title or "группы"
        except TelegramAPIError:
            chat_name = "группы"

        # Генерируем капчу
        target_word, markup = generate_emoji_captcha(chat_id, user_id, lang)
        
        text = TRANSLATIONS[lang]["greet"].format(
            name=html.escape(message.from_user.first_name),
            chat_name=html.escape(chat_name),
            target_word=html.escape(target_word.upper())
        )
        
        await message.answer(text=text, reply_markup=markup, parse_mode="HTML")
        logger.info(f"[КАПЧА] Бот отправил капчу в ЛС пользователю {user_id}. Цель: {target_word}")
        
    except (IndexError, ValueError) as e:
        logger.error(f"Ошибка при парсинге параметров старта: {e}")
        await message.answer("Неверный формат ссылки верификации.")


@router.callback_query(F.data.startswith("c_clk:"))
async def handle_captcha_click(callback: CallbackQuery, bot: Bot):
    """
    Проверяет клик по эмодзи в ЛС. 
    При успехе: размучивает в группе, удаляет временное сообщение в группе, пишет 'Успешно' в ЛС.
    callback_data: c_clk:{is_correct}:{chat_id}:{user_id}:{sent_timestamp}
    """
    parts = callback.data.split(":")
    if len(parts) != 5:
        await callback.answer("Ошибка клавиатуры", show_alert=True)
        return
        
    is_correct = parts[1] == "1"
    chat_id = int(parts[2])
    user_id = int(parts[3])
    sent_timestamp = int(parts[4])
    
    # Безопасность: на кнопку может нажать только получатель капчи
    if callback.from_user.id != user_id:
        await callback.answer("Это не твоя кнопка!", show_alert=True)
        return
        
    reaction_time = time.time() - sent_timestamp
    logger.info(f"[TIME TRAP] Клик сделан через {reaction_time:.2f} сек.")
    
    user_lang = callback.from_user.language_code or "en"
    if user_lang.startswith("ru"):
        lang = "ru"
    elif user_lang.startswith("vi"):
        lang = "vi"
    else:
        lang = "en"
        
    try:
        # Защита от роботов (Time Trap): слишком быстро
        if reaction_time < 1.5:
            logger.warning(f"[РОБОТ!] Слишком быстрый клик ({reaction_time:.2f}с) от {user_id}. Отклоняем!")
            await callback.message.edit_text(TRANSLATIONS[lang]["too_fast"], reply_markup=None)
            
            # Кикаем из группы
            try:
                await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
                await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            except TelegramAPIError as e:
                logger.error(f"Не удалось кикнуть быстрого кликера: {e}")
                
            # Удаляем временное сообщение из группы
            group_msg_id = group_prompts.pop((chat_id, user_id), None)
            if group_msg_id:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=group_msg_id)
                except TelegramAPIError:
                    pass
            return

        if is_correct:
            # РАЗМУЧИВАЕМ в группе (возвращаем полные права)
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_audios=True,
                    can_send_documents=True,
                    can_send_photos=True,
                    can_send_videos=True,
                    can_send_video_notes=True,
                    can_send_voice_notes=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True
                )
            )
            logger.info(f"[UNMUTE] Пользователь {user_id} успешно размучен в чате {chat_id}.")
            
            # Удаляем временное сообщение из группы
            group_msg_id = group_prompts.pop((chat_id, user_id), None)
            if group_msg_id:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=group_msg_id)
                    logger.info(f"[ОЧИСТКА] Временное сообщение {group_msg_id} удалено из группы {chat_id}.")
                except TelegramAPIError as e:
                    logger.error(f"Не удалось удалить временное сообщение: {e}")
                    
            # Динамически формируем ссылку на группу для быстрого возврата пользователя
            group_url = None
            try:
                chat = await bot.get_chat(chat_id)
                if chat.username:
                    group_url = f"https://t.me/{chat.username}"
                elif chat.invite_link:
                    group_url = chat.invite_link
            except TelegramAPIError:
                pass
                
            if not group_url:
                chat_id_str = str(chat_id)
                if chat_id_str.startswith("-100"):
                    clean_id = chat_id_str.replace("-100", "")
                    group_url = f"https://t.me/c/{clean_id}"
                elif chat_id_str.startswith("-"):
                    clean_id = chat_id_str.replace("-", "")
                    group_url = f"https://t.me/c/{clean_id}"
                else:
                    group_url = f"https://t.me/c/{chat_id_str}"
                    
            markup_return = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=TRANSLATIONS[lang]["btn_return"], url=group_url, style="primary")]
            ])
            
            await callback.message.edit_text(TRANSLATIONS[lang]["success"], reply_markup=markup_return)
            await callback.answer("Успешно!")
            
        else:
            # Неправильный ответ
            logger.info(f"[ОШИБКА] Пользователь {user_id} нажал неверный эмодзи.")
            await callback.message.edit_text(TRANSLATIONS[lang]["wrong"], reply_markup=None)
            await callback.answer("Неверно!", show_alert=True)
            
            # Кикаем из группы
            try:
                await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
                await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            except TelegramAPIError as e:
                logger.error(f"Не удалось кикнуть после ошибки: {e}")
                
            # Удаляем временное сообщение из группы
            group_msg_id = group_prompts.pop((chat_id, user_id), None)
            if group_msg_id:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=group_msg_id)
                except TelegramAPIError:
                    pass

    except TelegramAPIError as e:
        logger.error(f"Ошибка API при клике по капче: {e}")
        await callback.answer("Произошла ошибка, попробуйте еще раз.", show_alert=True)


def get_last_log_lines(n: int) -> list:
    """
    Возвращает последние N строк из файла bot.log.
    """
    log_file_path = "bot.log"
    if not os.path.exists(log_file_path):
        return ["Файл логов bot.log пока не создан."]
    try:
        with open(log_file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            if not lines:
                return ["Файл логов пуст."]
            return lines[-n:]
    except Exception as e:
        return [f"Ошибка при чтении логов: {e}"]


def format_log_message(lines: list, n: int) -> str:
    """
    Форматирует строки логов для моноширинного вывода в Telegram
    с защитой от превышения лимита в 4096 символов.
    """
    import html
    header = f"<b>Последние {len(lines)} строк логов:</b>\n"
    log_text = "".join(lines)
    escaped_log = html.escape(log_text)
    
    # Резервируем место под теги и заголовок
    max_code_len = 4096 - len(header) - 35
    if len(escaped_log) > max_code_len:
        escaped_log = escaped_log[-max_code_len:]
        # Обрезаем первую строку до ближайшего переноса, чтобы не показывать огрызки строк
        newline_idx = escaped_log.find("\n")
        if newline_idx != -1:
            escaped_log = escaped_log[newline_idx + 1:]
        escaped_log = "... [логи обрезаны сверху из-за лимита сообщений] ...\n" + escaped_log
        
    return f"{header}<pre><code>{escaped_log}</code></pre>"


def generate_logs_keyboard():
    """
    Генерирует инлайн-кнопки быстрого выбора количества строк логов.
    """
    buttons = [
        [
            InlineKeyboardButton(text="10 строк", callback_data="log_lines:10"),
            InlineKeyboardButton(text="20 строк", callback_data="log_lines:20"),
            InlineKeyboardButton(text="50 строк", callback_data="log_lines:50"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command(commands=["logs"]), F.chat.type == "private")
async def handle_get_logs(message: Message):
    """
    Показывает логи бота в ЛС администратору.
    Поддерживает аргументы, например: /logs 30
    Если аргументов нет, показывает меню с кнопками.
    """
    admin_id_str = os.getenv("ADMIN_ID")
    if not admin_id_str:
        await message.answer("Ошибка: ADMIN_ID не настроен в файле .env.")
        return
        
    try:
        admin_id = int(admin_id_str)
    except ValueError:
        await message.answer("Ошибка: ADMIN_ID в .env должен быть числом.")
        return
        
    if message.from_user.id != admin_id:
        await message.answer("У вас нет прав для просмотра логов.")
        return
        
    # Проверяем аргументы команды (например, /logs 15)
    args = message.text.split()
    if len(args) == 2:
        try:
            n = int(args[1])
            if n <= 0:
                await message.answer("Количество строк должно быть больше 0.")
                return
            n = min(n, 200)  # Ограничиваем разумным максимумом
            lines = get_last_log_lines(n)
            text = format_log_message(lines, n)
            await message.answer(text, parse_mode="HTML", reply_markup=generate_logs_keyboard())
            return
        except ValueError:
            pass
            
    # Если аргументов нет, отправляем приветственное интерактивное меню
    await message.answer(
        "Выбери количество строк для просмотра или напиши команду с числом, например: <code>/logs 30</code>",
        parse_mode="HTML",
        reply_markup=generate_logs_keyboard()
    )


@router.callback_query(F.data.startswith("log_lines:"))
async def handle_log_lines_callback(callback: CallbackQuery):
    """
    Обрабатывает нажатие кнопок выбора количества строк логов,
    обновляя текущее сообщение в реальном времени.
    """
    admin_id_str = os.getenv("ADMIN_ID")
    if not admin_id_str:
        await callback.answer("ADMIN_ID не настроен.", show_alert=True)
        return
        
    try:
        admin_id = int(admin_id_str)
    except ValueError:
        await callback.answer("ADMIN_ID должен быть числом.", show_alert=True)
        return
        
    if callback.from_user.id != admin_id:
        await callback.answer("Нет прав.", show_alert=True)
        return
        
    try:
        n = int(callback.data.split(":")[1])
        lines = get_last_log_lines(n)
        text = format_log_message(lines, n)
        
        await callback.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=generate_logs_keyboard()
        )
        await callback.answer()
    except TelegramAPIError as e:
        logger.error(f"Не удалось обновить сообщение с логами: {e}")
        await callback.answer("Ошибка обновления логов.", show_alert=True)


@router.message(Command(commands=["clear_logs"]), F.chat.type == "private")
async def handle_clear_logs(message: Message):
    """
    Очищает файл логов bot.log.
    """
    admin_id_str = os.getenv("ADMIN_ID")
    if not admin_id_str:
        await message.answer("Ошибка: ADMIN_ID не настроен в файле .env.")
        return
        
    try:
        admin_id = int(admin_id_str)
    except ValueError:
        await message.answer("Ошибка: ADMIN_ID в .env должен быть числом.")
        return
        
    if message.from_user.id != admin_id:
        await message.answer("У вас нет прав для очистки логов.")
        return
        
    log_file_path = "bot.log"
    if not os.path.exists(log_file_path):
        await message.answer("Файл логов bot.log не найден.")
        return
        
    try:
        with open(log_file_path, "w", encoding="utf-8") as f:
            f.truncate(0)
        await message.answer("Файл логов bot.log успешно очищен.")
    except Exception as e:
        await message.answer(f"Не удалось очистить файл логов: {e}")
