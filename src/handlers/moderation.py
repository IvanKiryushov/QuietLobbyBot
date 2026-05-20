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
        if member.status in ['administrator', 'creator']:
            return True
    except TelegramAPIError:
        pass
        
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

# --- АНТИСПАМ ССЫЛОК ---

@moderation_router.message(F.chat.type.in_(["group", "supergroup"]))
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


@moderation_router.message(Command(commands=["unmute"]), F.chat.type.in_(["group", "supergroup"]))
async def unmute_command(message: Message, bot: Bot, command: CommandObject):
    """Команда /unmute. Снимает ограничения на отправку сообщений (по reply или ID)."""
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
            "⚠️ Использование: напишите <code>/unmute</code> в ответ на сообщение\n"
            "или <code>/unmute ID_пользователя</code>.",
            parse_mode="HTML"
        )
        asyncio.create_task(delete_message_after_delay(msg, 10))
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        return

    try:
        # Снимаем ограничения (выдаем полные права)
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_user_id,
            permissions=get_permissions(is_muted=False)
        )
        logger.info(f"[MODERATION] Админ {message.from_user.id} снял ограничения с {target_user_id} в {message.chat.id}")
        
        info_msg = await message.answer(f"🔊 Пользователю <code>{target_user_id}</code> разрешено отправлять сообщения.", parse_mode="HTML")
        asyncio.create_task(delete_message_after_delay(info_msg, 10))
    except TelegramAPIError as e:
        logger.error(f"Ошибка размута пользователя {target_user_id}: {e}")
        await message.answer(f"⚠️ Не удалось снять ограничения: {e}")

    try:
        await message.delete()
    except TelegramAPIError:
        pass

# --- ПОЖАЛОВАТЬСЯ АДМИНИСТРАТОРУ (REPORT) ---

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
        
    reporter = message.from_user
    target = message.reply_to_message.from_user
    
    # Нельзя жаловаться на ботов или самого себя
    if target.is_bot or reporter.id == target.id:
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
        
    # Получаем список администраторов из БД
    from database import get_chat_admins
    try:
        admin_ids = await get_chat_admins(chat_id)
    except Exception as e:
        logger.error(f"Не удалось получить список админов чата из БД: {e}")
        admin_ids = []
        
    # Если БД пуста, запрашиваем API
    if not admin_ids:
        try:
            admins = await bot.get_chat_administrators(chat_id)
            admin_ids = [admin.user.id for admin in admins if not admin.user.is_bot]
        except TelegramAPIError:
            pass
            
    if not admin_ids:
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
        f"<b>Отправитель:</b> {reporter.mention_html()} (ID: <code>{reporter.id}</code>)\n"
        f"<b>Нарушитель:</b> {target.mention_html()} (ID: <code>{target.id}</code>)\n\n"
        f"<b>Сообщение:</b>\n<blockquote>{html.escape(target_text)}</blockquote>"
    )
    
    # Набор инлайн-кнопок для быстрой модерации
    buttons = [
        [
            InlineKeyboardButton(text="🚫 Бан", callback_data=f"rep_ban:{chat_id}:{target.id}:{message_id}"),
            InlineKeyboardButton(text="🔇 Мьют", callback_data=f"rep_mute:{chat_id}:{target.id}:{message_id}")
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить сообщение", callback_data=f"rep_del:{chat_id}:{message_id}")
        ]
    ]
    
    if msg_link:
        buttons[1].append(InlineKeyboardButton(text="🔗 Перейти к сообщению", url=msg_link))
        
    buttons.append([InlineKeyboardButton(text="❌ Отклонить", callback_data="rep_dismiss")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    # Рассылаем карточки всем админам
    for admin_id in admin_ids:
        if admin_id == reporter.id:
            # Не шлем жалобу самому себе, если админ пожаловался
            continue
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                reply_markup=markup,
                parse_mode="HTML"
            )
        except TelegramAPIError:
            # Админ не запустил бота в ЛС
            pass

# --- ОБРАБОТЧИКИ КНОПОК БЫСТРОЙ МОДЕРАЦИИ ИЗ ЛС ---

@moderation_router.callback_query(F.data.startswith("rep_ban:"))
async def handle_report_ban(callback: CallbackQuery, bot: Bot):
    """Обрабатывает кнопку «Бан» в карточке жалобы."""
    parts = callback.data.split(":")
    chat_id = int(parts[1])
    target_user_id = int(parts[2])
    message_id = int(parts[3])
    
    if not await is_user_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав администратора в этой группе!", show_alert=True)
        return
        
    try:
        # Баним пользователя в группе
        await bot.ban_chat_member(chat_id=chat_id, user_id=target_user_id)
        from database import record_member_leave
        await record_member_leave(chat_id, target_user_id, is_kick=True)
        
        # Пытаемся удалить сообщение
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            pass
            
        # Обновляем карточку жалобы в ЛС админа
        await callback.message.edit_text(
            text=f"{callback.message.html_text}\n\n✅ <b>Выполнен бан нарушителя</b> администратором {callback.from_user.mention_html()}.",
            reply_markup=None,
            parse_mode="HTML"
        )
        await callback.answer("Пользователь забанен, сообщение удалено.")
    except TelegramAPIError as e:
        logger.error(f"Не удалось выполнить бан через репорт: {e}")
        await callback.answer(f"Ошибка выполнения: {e}", show_alert=True)


@moderation_router.callback_query(F.data.startswith("rep_mute:"))
async def handle_report_mute(callback: CallbackQuery, bot: Bot):
    """Обрабатывает кнопку «Мьют» в карточке жалобы."""
    parts = callback.data.split(":")
    chat_id = int(parts[1])
    target_user_id = int(parts[2])
    message_id = int(parts[3])
    
    if not await is_user_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав администратора в этой группе!", show_alert=True)
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
            
        await callback.message.edit_text(
            text=f"{callback.message.html_text}\n\n✅ <b>Выполнен мьют нарушителя</b> администратором {callback.from_user.mention_html()}.",
            reply_markup=None,
            parse_mode="HTML"
        )
        await callback.answer("Права пользователя ограничены, сообщение удалено.")
    except TelegramAPIError as e:
        logger.error(f"Не удалось выполнить мьют через репорт: {e}")
        await callback.answer(f"Ошибка выполнения: {e}", show_alert=True)


@moderation_router.callback_query(F.data.startswith("rep_del:"))
async def handle_report_del(callback: CallbackQuery, bot: Bot):
    """Обрабатывает кнопку «Удалить сообщение» в карточке жалобы."""
    parts = callback.data.split(":")
    chat_id = int(parts[1])
    message_id = int(parts[2])
    
    if not await is_user_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("У вас нет прав администратора в этой группе!", show_alert=True)
        return
        
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        
        await callback.message.edit_text(
            text=f"{callback.message.html_text}\n\n✅ <b>Сообщение удалено</b> администратором {callback.from_user.mention_html()}.",
            reply_markup=None,
            parse_mode="HTML"
        )
        await callback.answer("Сообщение удалено.")
    except TelegramAPIError as e:
        logger.error(f"Не удалось удалить сообщение через репорт: {e}")
        await callback.answer(f"Ошибка выполнения: {e}", show_alert=True)


@moderation_router.callback_query(F.data == "rep_dismiss")
async def handle_report_dismiss(callback: CallbackQuery):
    """Обрабатывает кнопку «Отклонить» в карточке жалобы."""
    await callback.message.edit_text(
        text=f"{callback.message.html_text}\n\n❌ <b>Жалоба отклонена</b> администратором {callback.from_user.mention_html()}.",
        reply_markup=None,
        parse_mode="HTML"
    )
    await callback.answer("Жалоба отклонена.")
