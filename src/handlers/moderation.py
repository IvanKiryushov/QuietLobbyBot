import asyncio
import logging
import os
import re
import html
import time
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)
moderation_router = Router()

# Системный ID, используемый Telegram для отправки сообщений от имени анонимных администраторов групп (@GroupAnonymousBot)
TELEGRAM_ANONYMOUS_BOT_ID = 1087968824

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_permissions(is_muted: bool) -> ChatPermissions:
    """Возвращает ChatPermissions для режима MUTE (ограничен) или UNMUTE (разрешен)."""
    s = not is_muted
    return ChatPermissions(
        can_send_messages=s, can_send_audios=s, can_send_documents=s,
        can_send_photos=s, can_send_videos=s, can_send_video_notes=s,
        can_send_voice_notes=s, can_send_polls=s, can_send_other_messages=s,
        can_add_web_page_previews=s
    )

async def is_user_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором или суперадмином бота."""
    # 0. Проверяем, не является ли отправитель анонимным администратором чата
    if user_id == TELEGRAM_ANONYMOUS_BOT_ID:
        return True
        
    # 1. Сначала проверяем суперадмина из файла переменных окружения .env
    admin_id_str = os.getenv("ADMIN_ID")
    if admin_id_str and str(user_id) == admin_id_str:
        return True
    
    # 2. Проверяем по локальному кэшу базы данных
    from database import get_chat_admins
    try:
        cached_admins = await get_chat_admins(chat_id)
        if user_id in cached_admins:
            return True
    except Exception as e:
        logger.error(f"Ошибка при чтении кэша админов чата {chat_id}: {e}")
        
    # 3. Резервный запрос в Telegram API (для актуальности прав)
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        status_str = str(member.status).split('.')[-1].lower()
        if status_str in ['administrator', 'creator', 'owner']:
            return True
    except TelegramAPIError as e:
        logger.error(f"Ошибка get_chat_member для {user_id} в {chat_id}: {e}")
        
    return False

def is_google_maps_link(url: str) -> bool:
    """Проверяет, является ли ссылка вариацией Google Maps."""
    url_lower = url.lower()
    # Разрешаем короткие ссылки maps.app.goo.gl
    if "maps.app.goo.gl" in url_lower:
        return True
    # Разрешаем короткие ссылки goo.gl/maps
    if "goo.gl/maps" in url_lower:
        return True
    # Разрешаем ссылки maps.google.com / maps.google.ru и т.д.
    if "maps.google." in url_lower:
        return True
    # Разрешаем ссылки google.com/maps / google.ru/maps и т.д. с региональными доменами
    if re.search(r'google\.[a-z\.]+/maps', url_lower):
        return True
    return False

async def delete_message_after_delay(msg: Message, delay: int):
    """Фоновая задача для удаления сообщения через заданное количество секунд."""
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except TelegramAPIError:
        pass

def has_links(message: Message) -> bool:
    """Проверяет, содержит ли сообщение ссылки."""
    entities = message.entities or message.caption_entities or []
    for entity in entities:
        if entity.type in ["url", "text_link"]:
            return True
    return False

async def sync_report_messages(bot: Bot, report_id: str, new_status: str, resolved_by_name: str):
    """
    Асинхронно обновляет карточки жалобы у всех администраторов на основе текущего статуса.
    """
    from database import get_report, get_report_messages
    
    report = await get_report(report_id)
    if not report:
        logger.warning(f"Попытка синхронизации несуществующего отчета: {report_id}")
        return
        
    raw_text = report['raw_text']
    chat_id = report['chat_id']
    
    # Формируем новый текст и клавиатуру
    new_text = raw_text
    markup = None
    
    if new_status == 'banned':
        new_text += f"\n\n✅ <b>Выполнен бан нарушителя</b> администратором {resolved_by_name}."
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔓 Разбанить", callback_data=f"rep_unban:{report_id}")]
        ])
    elif new_status == 'muted':
        new_text += f"\n\n✅ <b>Выполнен мьют нарушителя</b> администратором {resolved_by_name}."
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔊 Размьютить", callback_data=f"rep_unmute:{report_id}")]
        ])
    elif new_status == 'deleted':
        new_text += f"\n\n✅ <b>Сообщение удалено</b> администратором {resolved_by_name}."
    elif new_status == 'dismissed':
        new_text += f"\n\n❌ <b>Жалоба отклонена</b> администратором {resolved_by_name}."
    elif new_status == 'unbanned':
        new_text += f"\n\n✅ <b>Выполнен бан нарушителя</b> (разблокирован администратором {resolved_by_name})."
    elif new_status == 'unmuted':
        new_text += f"\n\n✅ <b>Выполнен мьют нарушителя</b> (ограничения сняты администратором {resolved_by_name})."

    # Получаем все отправленные сообщения
    messages = await get_report_messages(report_id)
    
    # Редактируем сообщения
    for admin_id, msg_id in messages:
        try:
            await bot.edit_message_text(
                chat_id=admin_id,
                message_id=msg_id,
                text=new_text,
                reply_markup=markup,
                parse_mode="HTML"
            )
        except TelegramAPIError as e:
            # Игнорируем ошибки, если сообщение удалено или чат заблокирован
            logger.debug(f"Не удалось отредактировать сообщение {msg_id} у админа {admin_id}: {e}")

# --- АНТИСПАМ ССЫЛОК ---

@moderation_router.message(F.chat.type.in_(["group", "supergroup"]), has_links)
async def anti_link_handler(message: Message, bot: Bot):
    """Отслеживает отправку ссылок в группе и блокирует их (кроме Google Maps)."""
    # Игнорируем системные сообщения, сообщения без текста или медиа-подписей
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    
    if not entities:
        return

    # Администраторам разрешено отправлять любые ссылки
    if await is_user_admin(bot, message.chat.id, message.from_user.id):
        return

    has_forbidden_link = False
    
    # Сканируем сущности на наличие ссылок
    for entity in entities:
        if entity.type == "url":
            # Извлекаем ссылку из текста сообщения
            start = entity.offset
            end = entity.offset + entity.length
            url = text[start:end]
            if not is_google_maps_link(url):
                has_forbidden_link = True
                break
        elif entity.type == "text_link":
            # Извлекаем ссылку из скрытого URL-адреса сущности
            url = entity.url
            if not is_google_maps_link(url):
                has_forbidden_link = True
                break
                
    if has_forbidden_link:
        try:
            # Сначала пытаемся удалить спам-сообщение
            await message.delete()
        except TelegramAPIError as e:
            logger.error(f"Не удалось удалить спам-сообщение в чате {message.chat.id}: {e}")
            return
            
        # Отправляем временное предупреждение нарушителю
        warn_text = (
            f"⚠️ {message.from_user.mention_html()}, в этом чате разрешено отправлять ссылки "
            f"только на <b>Google Maps</b>!"
        )
        try:
            warn_msg = await message.answer(warn_text, parse_mode="HTML")
            # Запускаем таймер удаления предупреждения через 10 секунд
            asyncio.create_task(delete_message_after_delay(warn_msg, 10))
        except TelegramAPIError:
            pass

# --- КОМАНДЫ БАНА / РАЗБАНА (BAN / UNBAN) ---

@moderation_router.message(Command(commands=["ban"]), F.chat.type.in_(["group", "supergroup"]))
async def ban_command(message: Message, bot: Bot, command: CommandObject):
    """Команда /ban. Банит пользователя в группе (по reply или по ID)."""
    if not await is_user_admin(bot, message.chat.id, message.from_user.id):
        return

    target_user_id = None
    reason = "Нарушение правил"

    # Сценарий 1: Reply на сообщение нарушителя
    if message.reply_to_message:
        target_user_id = message.reply_to_message.from_user.id
        if command.args:
            reason = command.args
    # Сценарий 2: Передан ID пользователя параметром
    else:
        if command.args:
            parts = command.args.split(maxsplit=1)
            try:
                target_user_id = int(parts[0])
                if len(parts) > 1:
                    reason = parts[1]
            except ValueError:
                pass

    if not target_user_id:
        msg = await message.answer(
            "⚠️ Использование: напишите <code>/ban</code> в ответ на сообщение нарушителя\n"
            "или <code>/ban ID_пользователя [причина]</code>.", 
            parse_mode="HTML"
        )
        asyncio.create_task(delete_message_after_delay(msg, 10))
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        return

    # Нельзя забанить администратора
    if await is_user_admin(bot, message.chat.id, target_user_id):
        msg = await message.answer("⚠️ Нельзя заблокировать администратора чата.")
        asyncio.create_task(delete_message_after_delay(msg, 10))
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        return

    try:
        # Выполняем бан
        await bot.ban_chat_member(chat_id=message.chat.id, user_id=target_user_id)
        
        # Фиксируем кик в таблице участников для статистики/логов
        from database import record_member_leave
        await record_member_leave(message.chat.id, target_user_id, is_kick=True)
        
        logger.info(f"[MODERATION] Админ {message.from_user.id} забанил {target_user_id} в {message.chat.id}. Причина: {reason}")
        
        # Удаляем сообщение нарушителя, если команда была вызвана через reply
        if message.reply_to_message:
            try:
                await message.reply_to_message.delete()
            except TelegramAPIError:
                pass

        # Отправляем информацию в чат
        info_msg = await message.answer(
            f"🚫 Пользователь {message.reply_to_message.from_user.mention_html() if message.reply_to_message else f'<code>{target_user_id}</code>'} "
            f"заблокирован.\n📝 Причина: {html.escape(reason)}",
            parse_mode="HTML"
        )
        asyncio.create_task(delete_message_after_delay(info_msg, 10))
        
    except TelegramAPIError as e:
        logger.error(f"Ошибка бана пользователя {target_user_id}: {e}")
        await message.answer(f"⚠️ Не удалось заблокировать пользователя: {e}")

    # Удаляем само сообщение с командой /ban
    try:
        await message.delete()
    except TelegramAPIError:
        pass


@moderation_router.message(Command(commands=["unban"]), F.chat.type.in_(["group", "supergroup"]))
async def unban_command(message: Message, bot: Bot, command: CommandObject):
    """Команда /unban. Разбанивает пользователя (по reply или ID)."""
    if not await is_user_admin(bot, message.chat.id, message.from_user.id):
        return

    target_user_id = None

    if message.reply_to_message:
        target_user_id = message.reply_to_message.from_user.id
    elif command.args:
        try:
            target_user_id = int(command.args.split()[0])
        except ValueError:
            pass

    if not target_user_id:
        msg = await message.answer(
            "⚠️ Использование: напишите <code>/unban</code> в ответ на сообщение\n"
            "или <code>/unban ID_пользователя</code>.", 
            parse_mode="HTML"
        )
        asyncio.create_task(delete_message_after_delay(msg, 10))
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        return

    try:
        # Разбаниваем пользователя
        await bot.unban_chat_member(chat_id=message.chat.id, user_id=target_user_id, only_if_banned=True)
        logger.info(f"[MODERATION] Админ {message.from_user.id} разбанил {target_user_id} в {message.chat.id}")
        
        info_msg = await message.answer(f"✅ Пользователь <code>{target_user_id}</code> разблокирован.", parse_mode="HTML")
        asyncio.create_task(delete_message_after_delay(info_msg, 10))
    except TelegramAPIError as e:
        logger.error(f"Ошибка разбана пользователя {target_user_id}: {e}")
        await message.answer(f"⚠️ Не удалось разблокировать пользователя: {e}")

    try:
        await message.delete()
    except TelegramAPIError:
        pass

# --- КОМАНДЫ МЬЮТА / АНМЬЮТА (MUTE / UNMUTE) ---

def parse_mute_duration(text: str) -> int | None:
    """Парсит строку с длительностью мьюта (например, 10m, 2h, 1d) и возвращает количество секунд."""
    if not text:
        return None
    text = text.lower().strip()
    match = re.match(r'^(\d+)([mhdy]?)$', text)
    if not match:
        return None
    val = int(match.group(1))
    unit = match.group(2)
    if unit == 'm' or unit == '':
        return val * 60
    elif unit == 'h':
        return val * 3600
    elif unit == 'd':
        return val * 86400
    elif unit == 'y':
        return val * 31536000
    return None

@moderation_router.message(Command(commands=["mute"]), F.chat.type.in_(["group", "supergroup"]))
async def mute_command(message: Message, bot: Bot, command: CommandObject):
    """Команда /mute. Ограничивает отправку сообщений пользователю (по reply или ID)."""
    if not await is_user_admin(bot, message.chat.id, message.from_user.id):
        return

    target_user_id = None
    duration_str = None
    reason = "Нарушение правил"

    # Разбор аргументов для reply и обычного вызова
    if message.reply_to_message:
        target_user_id = message.reply_to_message.from_user.id
        if command.args:
            parts = command.args.split(maxsplit=1)
            duration_str = parts[0]
            if len(parts) > 1:
                reason = parts[1]
    else:
        if command.args:
            parts = command.args.split(maxsplit=2)
            try:
                target_user_id = int(parts[0])
                if len(parts) > 1:
                    duration_str = parts[1]
                if len(parts) > 2:
                    reason = parts[2]
            except ValueError:
                pass

    if not target_user_id:
        msg = await message.answer(
            "⚠️ Использование: напишите <code>/mute [время] [причина]</code> в ответ на сообщение нарушителя\n"
            "или <code>/mute ID_пользователя [время] [причина]</code>.\n"
            "Пример времени: <code>15m</code> (минуты), <code>2h</code> (часы), <code>1d</code> (дни).",
            parse_mode="HTML"
        )
        asyncio.create_task(delete_message_after_delay(msg, 10))
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        return

    if await is_user_admin(bot, message.chat.id, target_user_id):
        msg = await message.answer("⚠️ Нельзя ограничить администратора чата.")
        asyncio.create_task(delete_message_after_delay(msg, 10))
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        return

    # Вычисляем время окончания мьюта
    duration_seconds = parse_mute_duration(duration_str) if duration_str else None
    until_date = None
    duration_desc = "навсегда"
    
    if duration_seconds:
        until_date = int(time.time() + duration_seconds)
        duration_desc = f"на {duration_str}"

    try:
        # Накладываем ограничения в Telegram
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_user_id,
            permissions=get_permissions(is_muted=True),
            until_date=until_date
        )
        
        logger.info(f"[MODERATION] Админ {message.from_user.id} ограничил {target_user_id} в {message.chat.id} {duration_desc}. Причина: {reason}")
        
        # Удаляем сообщение нарушителя
        if message.reply_to_message:
            try:
                await message.reply_to_message.delete()
            except TelegramAPIError:
                pass

        target_mention = message.reply_to_message.from_user.mention_html() if message.reply_to_message else f"<code>{target_user_id}</code>"
        info_msg = await message.answer(
            f"🔇 Пользователю {target_mention} ограничен доступ к отправке сообщений <b>{duration_desc}</b>.\n"
            f"📝 Причина: {html.escape(reason)}",
            parse_mode="HTML"
        )
        asyncio.create_task(delete_message_after_delay(info_msg, 10))
        
    except TelegramAPIError as e:
        logger.error(f"Ошибка мьюта пользователя {target_user_id}: {e}")
        await message.answer(f"⚠️ Не удалось ограничить пользователя: {e}")

    try:
        await message.delete()
    except TelegramAPIError:
        pass

@moderation_router.message(
    F.chat.type.in_(["group", "supergroup"]),
    (F.text.startswith("/report") | F.text.contains("@admin"))
)
async def report_handler(message: Message, bot: Bot):
    """Обрабатывает жалобы пользователей (/report или упоминание @admin) по reply."""
    text = (message.text or "").strip()
    
    # Должно быть строго reply на сообщение
    if not message.reply_to_message:
        return
        
    # 1. Безопасное определение отправителя жалобы (reporter)
    reporter_id = None
    reporter_mention = "Анонимный отправитель"
    is_anonymous_reporter = False
    
    if message.from_user:
        reporter_id = message.from_user.id
        if reporter_id == TELEGRAM_ANONYMOUS_BOT_ID:  # ID GroupAnonymousBot
            is_anonymous_reporter = True
            if message.sender_chat:
                reporter_mention = f"Анонимный администратор ({html.escape(message.sender_chat.title)})"
            else:
                reporter_mention = "Анонимный администратор"
        else:
            reporter_mention = f"{message.from_user.mention_html()} (ID: <code>{reporter_id}</code>)"
    elif message.sender_chat:
        is_anonymous_reporter = True
        reporter_id = message.sender_chat.id
        reporter_mention = f"Канал/Чат: {html.escape(message.sender_chat.title)} (ID: <code>{reporter_id}</code>)"

    # 2. Безопасное определение нарушителя (target)
    target_id = None
    target_mention = "Анонимный пользователь"
    target_is_bot = False
    
    if message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        target_mention = f"{message.reply_to_message.from_user.mention_html()} (ID: <code>{target_id}</code>)"
        target_is_bot = message.reply_to_message.from_user.is_bot
    elif message.reply_to_message.sender_chat:
        target_id = message.reply_to_message.sender_chat.id
        target_mention = f"Канал/Чат: {html.escape(message.reply_to_message.sender_chat.title)} (ID: <code>{target_id}</code>)"

    # 3. Фильтрация недопустимых жалоб
    # Нельзя жаловаться на ботов
    if target_is_bot:
        return
        
    # Нельзя жаловаться на самого себя (если ID известны и совпадают)
    if reporter_id and target_id and reporter_id == target_id:
        return
        
    chat_id = message.chat.id
    chat_title = message.chat.title or "группе"
    message_id = message.reply_to_message.message_id
    
    # Формируем прямую ссылку на сообщение для супергрупп
    chat_id_str = str(chat_id)
    msg_link = None
    if chat_id_str.startswith("-100"):
        clean_chat_id = chat_id_str[4:]
        msg_link = f"https://t.me/c/{clean_chat_id}/{message_id}"
        
    # Формируем текст сообщения нарушителя
    target_text = message.reply_to_message.text or message.reply_to_message.caption or "[Медиасообщение]"
    if len(target_text) > 300:
        target_text = target_text[:300] + "..."
        
    # 4. Обновляем и получаем список администраторов
    from database import get_chat_admins, create_report, add_report_message, set_chat_admins
    
    admin_ids = []
    try:
        # Пытаемся получить свежий список из API Telegram
        admins = await bot.get_chat_administrators(chat_id)
        admin_ids = [admin.user.id for admin in admins if not admin.user.is_bot]
        # Сохраняем в БД для синхронизации
        await set_chat_admins(chat_id, admin_ids)
        logger.info(f"Синхронизировано {len(admin_ids)} администраторов при жалобе в чате {chat_id}")
    except TelegramAPIError as e:
        logger.error(f"Не удалось получить список админов через API: {e}. Используем данные из БД.")
        try:
            admin_ids = await get_chat_admins(chat_id)
        except Exception as db_err:
            logger.error(f"Не удалось получить админов из БД: {db_err}")
            
    if not admin_ids:
        logger.warning(f"Список администраторов для чата {chat_id} пуст. Некуда отправлять жалобу.")
        return
        
    # Удаляем саму жалобу из общего чата для чистоты
    try:
        await message.delete()
    except TelegramAPIError:
        pass
        
    # Отправляем подтверждение отправителю жалобы
    confirm_msg = await message.answer(f"🔔 Жалоба на сообщение отправлена администраторам.")
    asyncio.create_task(delete_message_after_delay(confirm_msg, 5))
    
    # Собираем текст карточки для ЛС админа
    admin_text = (
        f"🚨 <b>Жалоба на сообщение!</b>\n\n"
        f"<b>Группа:</b> {html.escape(chat_title)} (ID: <code>{chat_id}</code>)\n"
        f"<b>Отправитель:</b> {reporter_mention}\n"
        f"<b>Нарушитель:</b> {target_mention}\n\n"
        f"<b>Сообщение:</b>\n<blockquote>{html.escape(target_text)}</blockquote>"
    )
    
    try:
        report_id = await create_report(chat_id, target_id if target_id else 0, message_id, admin_text)
    except Exception as e:
        logger.error(f"Не удалось создать отчет в БД: {e}")
        report_id = f"{chat_id}:{message_id}"
    
    # Набор инлайн-кнопок для быстрой модерации
    buttons = [
        [
            InlineKeyboardButton(text="🚫 Бан", callback_data=f"rep_ban:{report_id}"),
            InlineKeyboardButton(text="🔇 Мьют", callback_data=f"rep_mute:{report_id}")
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить сообщение", callback_data=f"rep_del:{report_id}")
        ]
    ]
    
    if msg_link:
        buttons[1].append(InlineKeyboardButton(text="🔗 Перейти к сообщению", url=msg_link))
        
    buttons.append([InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rep_dismiss:{report_id}")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    # Рассылаем карточки всем админам
    sent_count = 0
    for admin_id in admin_ids:
        try:
            sent_msg = await bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                reply_markup=markup,
                parse_mode="HTML"
            )
            sent_count += 1
            try:
                await add_report_message(report_id, admin_id, sent_msg.message_id)
            except Exception as e:
                logger.error(f"Не удалось сохранить report_message в БД: {e}")
        except TelegramAPIError as e:
            logger.warning(f"Не удалось отправить жалобу администратору {admin_id} в ЛС (возможно, бот не запущен): {e}")
            
    if sent_count == 0:
        logger.error(f"Жалоба в чате {chat_id} не была доставлена ни одному администратору.")


# --- ОБРАБОТЧИКИ КНОПОК БЫСТРОЙ МОДЕРАЦИИ ИЗ ЛС ---

@moderation_router.callback_query(F.data.startswith("rep_ban:"))
async def handle_report_ban(callback: CallbackQuery, bot: Bot):
    """Обрабатывает кнопку «Бан» в карточке жалобы."""
    report_id = callback.data.split(":", 1)[1]
    
    from database import get_report, resolve_report, record_member_leave
    
    report = await get_report(report_id)
    if not report:
        await callback.answer("⚠️ Жалоба устарела или не найдена в базе данных.", show_alert=True)
        return
        
    chat_id = report['chat_id']
    target_user_id = report['target_user_id']
    message_id = report['target_message_id']
    
    if not await is_user_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав администратора в этой группе!", show_alert=True)
        return
        
    if report['status'] != 'pending':
        status_desc = {
            'banned': 'заблокировал нарушителя',
            'muted': 'замьютил нарушителя',
            'deleted': 'удалил сообщение',
            'dismissed': 'отклонил жалобу'
        }.get(report['status'], 'обработал жалобу')
        await callback.answer(f"⚠️ Жалоба уже обработана!\nАдминистратор {report['resolved_by_name']} {status_desc}.", show_alert=True)
        return
        
    admin_name = callback.from_user.full_name
    success = await resolve_report(report_id, 'banned', callback.from_user.id, admin_name)
    if not success:
        report = await get_report(report_id)
        status_desc = {
            'banned': 'заблокировал нарушителя',
            'muted': 'замьютил нарушителя',
            'deleted': 'удалил сообщение',
            'dismissed': 'отклонил жалобу'
        }.get(report['status'], 'обработал жалобу')
        await callback.answer(f"⚠️ Жалоба уже обработана!\nАдминистратор {report['resolved_by_name']} {status_desc}.", show_alert=True)
        return
        
    try:
        # Баним пользователя в группе
        await bot.ban_chat_member(chat_id=chat_id, user_id=target_user_id)
        await record_member_leave(chat_id, target_user_id, is_kick=True)
        
        # Пытаемся удалить сообщение
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            pass
            
        await callback.answer("Пользователь забанен, сообщение удалено.")
        asyncio.create_task(sync_report_messages(bot, report_id, 'banned', callback.from_user.mention_html()))
    except TelegramAPIError as e:
        logger.error(f"Не удалось выполнить бан через репорт: {e}")
        await callback.answer(f"Ошибка выполнения: {e}", show_alert=True)


@moderation_router.callback_query(F.data.startswith("rep_mute:"))
async def handle_report_mute(callback: CallbackQuery, bot: Bot):
    """Обрабатывает кнопку «Мьют» в карточке жалобы."""
    report_id = callback.data.split(":", 1)[1]
    
    from database import get_report, resolve_report
    
    report = await get_report(report_id)
    if not report:
        await callback.answer("⚠️ Жалоба устарела или не найдена в базе данных.", show_alert=True)
        return
        
    chat_id = report['chat_id']
    target_user_id = report['target_user_id']
    message_id = report['target_message_id']
    
    if not await is_user_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав администратора в этой группе!", show_alert=True)
        return
        
    if report['status'] != 'pending':
        status_desc = {
            'banned': 'заблокировал нарушителя',
            'muted': 'замьютил нарушителя',
            'deleted': 'удалил сообщение',
            'dismissed': 'отклонил жалобу'
        }.get(report['status'], 'обработал жалобу')
        await callback.answer(f"⚠️ Жалоба уже обработана!\nАдминистратор {report['resolved_by_name']} {status_desc}.", show_alert=True)
        return
        
    admin_name = callback.from_user.full_name
    success = await resolve_report(report_id, 'muted', callback.from_user.id, admin_name)
    if not success:
        report = await get_report(report_id)
        status_desc = {
            'banned': 'заблокировал нарушителя',
            'muted': 'замьютил нарушителя',
            'deleted': 'удалил сообщение',
            'dismissed': 'отклонил жалобу'
        }.get(report['status'], 'обработал жалобу')
        await callback.answer(f"⚠️ Жалоба уже обработана!\nАдминистратор {report['resolved_by_name']} {status_desc}.", show_alert=True)
        return
        
    try:
        # Мутим навсегда
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target_user_id,
            permissions=get_permissions(is_muted=True)
        )
        
        # Удаляем сообщение
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            pass
            
        await callback.answer("Права пользователя ограничены, сообщение удалено.")
        asyncio.create_task(sync_report_messages(bot, report_id, 'muted', callback.from_user.mention_html()))
    except TelegramAPIError as e:
        logger.error(f"Не удалось выполнить мьют через репорт: {e}")
        await callback.answer(f"Ошибка выполнения: {e}", show_alert=True)


@moderation_router.callback_query(F.data.startswith("rep_unban:"))
async def handle_report_unban(callback: CallbackQuery, bot: Bot):
    """Обрабатывает кнопку «Разбанить» в карточке жалобы."""
    report_id = callback.data.split(":", 1)[1]
    
    from database import get_report, update_report_status_unconditionally
    
    report = await get_report(report_id)
    if not report:
        await callback.answer("⚠️ Жалоба устарела или не найдена в базе данных.", show_alert=True)
        return
        
    chat_id = report['chat_id']
    target_user_id = report['target_user_id']
    
    if not await is_user_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав администратора в этой группе!", show_alert=True)
        return
        
    try:
        await bot.unban_chat_member(chat_id=chat_id, user_id=target_user_id, only_if_banned=True)
        await update_report_status_unconditionally(report_id, 'unbanned', callback.from_user.id, callback.from_user.full_name)
        await callback.answer("Пользователь разбанен.")
        asyncio.create_task(sync_report_messages(bot, report_id, 'unbanned', callback.from_user.mention_html()))
    except TelegramAPIError as e:
        logger.error(f"Не удалось выполнить разбан через репорт: {e}")
        await callback.answer(f"Ошибка разбана: {e}", show_alert=True)


@moderation_router.callback_query(F.data.startswith("rep_unmute:"))
async def handle_report_unmute(callback: CallbackQuery, bot: Bot):
    """Обрабатывает кнопку «Размьютить» в карточке жалобы."""
    report_id = callback.data.split(":", 1)[1]
    
    from database import get_report, update_report_status_unconditionally
    
    report = await get_report(report_id)
    if not report:
        await callback.answer("⚠️ Жалоба устарела или не найдена в базе данных.", show_alert=True)
        return
        
    chat_id = report['chat_id']
    target_user_id = report['target_user_id']
    
    if not await is_user_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав администратора в этой группе!", show_alert=True)
        return
        
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target_user_id,
            permissions=get_permissions(is_muted=False)
        )
        await update_report_status_unconditionally(report_id, 'unmuted', callback.from_user.id, callback.from_user.full_name)
        await callback.answer("Пользователь размьючен.")
        asyncio.create_task(sync_report_messages(bot, report_id, 'unmuted', callback.from_user.mention_html()))
    except TelegramAPIError as e:
        logger.error(f"Не удалось выполнить размьют через репорт: {e}")
        await callback.answer(f"Ошибка размута: {e}", show_alert=True)


@moderation_router.callback_query(F.data.startswith("rep_del:"))
async def handle_report_del(callback: CallbackQuery, bot: Bot):
    """Обрабатывает кнопку «Удалить сообщение» в карточке жалобы."""
    report_id = callback.data.split(":", 1)[1]
    
    from database import get_report, resolve_report
    
    report = await get_report(report_id)
    if not report:
        await callback.answer("⚠️ Жалоба устарела или не найдена в базе данных.", show_alert=True)
        return
        
    chat_id = report['chat_id']
    message_id = report['target_message_id']
    
    if not await is_user_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав администратора в этой группе!", show_alert=True)
        return
        
    if report['status'] != 'pending':
        status_desc = {
            'banned': 'заблокировал нарушителя',
            'muted': 'замьютил нарушителя',
            'deleted': 'удалил сообщение',
            'dismissed': 'отклонил жалобу'
        }.get(report['status'], 'обработал жалобу')
        await callback.answer(f"⚠️ Жалоба уже обработана!\nАдминистратор {report['resolved_by_name']} {status_desc}.", show_alert=True)
        return
        
    admin_name = callback.from_user.full_name
    success = await resolve_report(report_id, 'deleted', callback.from_user.id, admin_name)
    if not success:
        report = await get_report(report_id)
        status_desc = {
            'banned': 'заблокировал нарушителя',
            'muted': 'замьютил нарушителя',
            'deleted': 'удалил сообщение',
            'dismissed': 'отклонил жалобу'
        }.get(report['status'], 'обработал жалобу')
        await callback.answer(f"⚠️ Жалоба уже обработана!\nАдминистратор {report['resolved_by_name']} {status_desc}.", show_alert=True)
        return
        
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        await callback.answer("Сообщение удалено.")
        asyncio.create_task(sync_report_messages(bot, report_id, 'deleted', callback.from_user.mention_html()))
    except TelegramAPIError as e:
        logger.error(f"Не удалось удалить сообщение через репорт: {e}")
        await callback.answer(f"Ошибка выполнения: {e}", show_alert=True)


@moderation_router.callback_query(F.data.startswith("rep_dismiss:"))
async def handle_report_dismiss(callback: CallbackQuery, bot: Bot):
    """Обрабатывает кнопку «Отклонить» в карточке жалобы."""
    report_id = callback.data.split(":", 1)[1]
    
    from database import get_report, resolve_report
    
    report = await get_report(report_id)
    if not report:
        await callback.answer("⚠️ Жалоба устарела или не найдена в базе данных.", show_alert=True)
        return
        
    chat_id = report['chat_id']
    
    if not await is_user_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав администратора в этой группе!", show_alert=True)
        return
        
    if report['status'] != 'pending':
        status_desc = {
            'banned': 'заблокировал нарушителя',
            'muted': 'замьютил нарушителя',
            'deleted': 'удалил сообщение',
            'dismissed': 'отклонил жалобу'
        }.get(report['status'], 'обработал жалобу')
        await callback.answer(f"⚠️ Жалоба уже обработана!\nАдминистратор {report['resolved_by_name']} {status_desc}.", show_alert=True)
        return
        
    admin_name = callback.from_user.full_name
    success = await resolve_report(report_id, 'dismissed', callback.from_user.id, admin_name)
    if not success:
        report = await get_report(report_id)
        status_desc = {
            'banned': 'заблокировал нарушителя',
            'muted': 'замьютил нарушителя',
            'deleted': 'удалил сообщение',
            'dismissed': 'отклонил жалобу'
        }.get(report['status'], 'обработал жалобу')
        await callback.answer(f"⚠️ Жалоба уже обработана!\nАдминистратор {report['resolved_by_name']} {status_desc}.", show_alert=True)
        return
        
    await callback.answer("Жалоба отклонена.")
    asyncio.create_task(sync_report_messages(bot, report_id, 'dismissed', callback.from_user.mention_html()))

# --- АНТИМАТ ---

@moderation_router.message()
async def anti_swear_handler(message: Message, bot: Bot):
    """Глобальный обработчик текстовых сообщений для фильтрации нецензурной лексики."""
    if not message.text and not message.caption:
        return
        
    chat_id = message.chat.id
    if chat_id > 0:
        return  # Не фильтруем личные сообщения
        
    # Ленивый импорт, чтобы не создавать циклических зависимостей
    from database import get_chat_settings, add_user_warning, reset_user_warnings
    from bad_words import contains_swear_words
    
    settings = await get_chat_settings(chat_id)
    if not settings or not settings.get('anti_swear_enabled', 0):
        return
        
    # Проверяем, есть ли мат в сообщении
    text_to_check = message.text or message.caption
    if not contains_swear_words(text_to_check):
        return
        
    # Игнорируем администраторов (в т.ч. анонимных)
    if message.from_user:
        if await is_user_admin(bot, chat_id, message.from_user.id):
            return
            
    # Если это сообщение от канала или анонимного админа (без from_user), и мы сюда дошли - удаляем.
    # Но анонимные админы отсекаются выше, так как у них id = 1087968824.
    
    # 1. Удаляем сообщение с матом
    try:
        await message.delete()
    except TelegramAPIError:
        logger.warning(f"Не удалось удалить сообщение с матом в чате {chat_id} (нет прав?)")
        return
        
    # 2. Обрабатываем страйки (только если есть конкретный пользователь)
    if not message.from_user:
        return
        
    user_id = message.from_user.id
    user_mention = message.from_user.mention_html()
    
    current_warnings = await add_user_warning(chat_id, user_id)
    max_warnings = settings.get('max_swear_warnings', 3)
    
    if current_warnings >= max_warnings:
        # Лимит превышен: выдаем мьют на 24 часа и сбрасываем страйки
        await reset_user_warnings(chat_id, user_id)
        
        permissions = get_permissions(is_muted=True)
        until_date = int(time.time()) + 86400  # 24 часа
        
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=permissions,
                until_date=until_date
            )
            await message.answer(
                f"🛑 {user_mention} превысил лимит предупреждений за мат ({max_warnings}/{max_warnings}).\n"
                f"Выдан мут на 24 часа.", 
                parse_mode="HTML"
            )
        except TelegramAPIError as e:
            logger.error(f"Ошибка мута нарушителя антимата {user_id}: {e}")
    else:
        # Просто предупреждение
        warn_msg = await message.answer(
            f"⚠️ {user_mention}, мат в этом чате запрещен!\n"
            f"Предупреждение {current_warnings}/{max_warnings}.", 
            parse_mode="HTML"
        )
        # Удаляем предупреждение через 10 секунд чтобы не засорять чат
        asyncio.create_task(delete_message_after_delay(warn_msg, 10))

