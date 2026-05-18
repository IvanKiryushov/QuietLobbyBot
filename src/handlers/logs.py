import os
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.exceptions import TelegramAPIError

from database import get_all_active_chats

logger = logging.getLogger(__name__)
logs_router = Router()

def get_last_log_lines(n: int) -> list:
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
    import html
    header = f"<b>Последние {len(lines)} строк логов:</b>\n"
    log_text = "".join(lines)
    escaped_log = html.escape(log_text)
    max_code_len = 4096 - len(header) - 35
    if len(escaped_log) > max_code_len:
        escaped_log = escaped_log[-max_code_len:]
        newline_idx = escaped_log.find("\n")
        if newline_idx != -1:
            escaped_log = escaped_log[newline_idx + 1:]
        escaped_log = "... [логи обрезаны сверху из-за лимита сообщений] ...\n" + escaped_log
    return f"{header}<pre><code>{escaped_log}</code></pre>"

def generate_logs_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="10 строк", callback_data="log_lines:10"),
            InlineKeyboardButton(text="20 строк", callback_data="log_lines:20"),
            InlineKeyboardButton(text="50 строк", callback_data="log_lines:50"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@logs_router.message(Command(commands=["logs"]), F.chat.type == "private")
async def handle_get_logs(message: Message):
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
    args = message.text.split()
    if len(args) == 2:
        try:
            n = int(args[1])
            if n <= 0:
                await message.answer("Количество строк должно быть больше 0.")
                return
            n = min(n, 200)
            lines = get_last_log_lines(n)
            text = format_log_message(lines, n)
            await message.answer(text, parse_mode="HTML", reply_markup=generate_logs_keyboard())
            return
        except ValueError:
            pass
    await message.answer(
        "Выбери количество строк для просмотра или напиши команду с числом, например: <code>/logs 30</code>",
        parse_mode="HTML",
        reply_markup=generate_logs_keyboard()
    )

@logs_router.callback_query(F.data.startswith("log_lines:"))
async def handle_log_lines_callback(callback: CallbackQuery):
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

@logs_router.message(Command(commands=["clear_logs"]), F.chat.type == "private")
async def handle_clear_logs(message: Message):
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

@logs_router.message(Command(commands=["chats"]), F.chat.type == "private")
async def handle_get_chats(message: Message, bot: Bot):
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
        await message.answer("У вас нет прав для просмотра чатов.")
        return

    try:
        chats = await get_all_active_chats()
    except Exception as e:
        await message.answer(f"⚠️ Ошибка при обращении к базе данных: <code>{e}</code>", parse_mode="HTML")
        logger.error(f"Ошибка БД в handle_get_chats: {e}")
        return

    if not chats:
        await message.answer("Бот пока не добавлен ни в один активный чат.")
        return

    try:
        text = "<b>Список активных чатов, где добавлен бот:</b>\n\n"
        import html
        for idx, chat_data in enumerate(chats, 1):
            chat_id = chat_data["chat_id"]
            lang = chat_data.get("language", "en")
            title_cached = chat_data.get("title") or "Без названия"
            try:
                chat = await bot.get_chat(chat_id)
                title = chat.title or title_cached
                title = html.escape(title)
            except TelegramAPIError:
                title = f"{html.escape(title_cached)} (Чат недоступен)"
            
            text += f"{idx}. <b>{title}</b>\n   ID: <code>{chat_id}</code> | Язык: <code>{lang}</code>\n\n"

        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка при обработке или отправке списка чатов: <code>{e}</code>", parse_mode="HTML")
        logger.error(f"Ошибка при формировании /chats: {e}")
