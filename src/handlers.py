import aiohttp
import logging
import time
from aiogram import Router, F, Bot
from aiogram.types import ChatJoinRequest, CallbackQuery
from aiogram.exceptions import TelegramAPIError
from keyboards import generate_emoji_captcha, TRANSLATIONS

logger = logging.getLogger(__name__)
router = Router()

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

@router.chat_join_request()
async def handle_join_request(join_request: ChatJoinRequest, bot: Bot):
    """
    Обрабатывает заявку, логирует RAW-язык, присылает мультиязычную капчу.
    """
    chat_id = join_request.chat.id
    user_id = join_request.from_user.id
    
    # 🔥 1. ПРЕВЕНТИВНАЯ ЗАЩИТА: Проверяем в глобальной базе спамеров
    if await is_global_spammer(user_id):
        try:
            await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
            logger.info(f"[🛡️ CAS] Заявка спамера {user_id} отклонена автоматически.")
        except TelegramAPIError as e:
            logger.error(f"Не удалось отклонить спамера {user_id}: {e}")
        return

    # Дальше идет обычная логика
    user_name = join_request.from_user.first_name
    chat_name = join_request.chat.title or "нашего чата"
    
    # 🔍 ОТЛАДКА ЯЗЫКА: Выводим в логи то, что нам реально прислал Telegram
    raw_lang = join_request.from_user.language_code
    logger.info(f"[ЯЗЫК] Telegram прислал raw_lang={raw_lang} для пользователя {user_id} (@{join_request.from_user.username})")
    
    # Смена страховки по умолчанию на English ("en")
    user_lang = raw_lang or "en"
    
    if user_lang.startswith("ru"):
        lang = "ru"
    elif user_lang.startswith("vi"):
        lang = "vi"
    else:
        lang = "en"
        
    logger.info(f"[ЯЗЫК] Выбранный язык для капчи: {lang}")
        
    target_word, markup = generate_emoji_captcha(chat_id, user_id, lang)
    
    template = TRANSLATIONS[lang]["greet"]
    text = template.format(name=user_name, chat_name=chat_name, target_word=target_word.upper())
    
    try:
        await bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=markup,
            parse_mode="HTML"
        )
        logger.info(f"Капча ({lang}) отправлена пользователю {user_id}. Цель: {target_word}")
    except TelegramAPIError as e:
        logger.error(f"Не удалось написать в ЛС {user_id}: {e}")

@router.callback_query(F.data.startswith("c_clk:"))
async def handle_captcha_click(callback: CallbackQuery, bot: Bot):
    """
    Проверяет клик, вычисляет время реакции (Временной Капкан).
    callback_data: c_clk:{is_correct}:{chat_id}:{user_id}:{sent_timestamp}
    """
    parts = callback.data.split(":")
    if len(parts) != 5:
        await callback.answer("Ошибка данных клавиатуры", show_alert=True)
        return
        
    is_correct = parts[1] == "1"
    chat_id = int(parts[2])
    user_id = int(parts[3])
    sent_timestamp = int(parts[4])
    
    # Вычисляем разницу во времени (серверное время в секундах)
    reaction_time = time.time() - sent_timestamp
    logger.info(f"[TIME TRAP] Пользователь {user_id} кликнул через {reaction_time:.2f} сек.")
    
    # Определяем язык
    user_lang = callback.from_user.language_code or "en"
    if user_lang.startswith("ru"):
        lang = "ru"
    elif user_lang.startswith("vi"):
        lang = "vi"
    else:
        lang = "en"
        
    # Защита: чужой не может нажать
    if callback.from_user.id != user_id:
        await callback.answer(TRANSLATIONS[lang]["not_your_button"], show_alert=True)
        return
        
    try:
        # 🕵️‍♂️ ВРЕМЕННОЙ КАПКАН: Если клик сделан быстрее чем за 2.0 секунды
        if reaction_time < 2.0:
            logger.warning(f"[🛡️ РОБОТ!] Юзер {user_id} кликнул слишком быстро ({reaction_time:.2f}с). Отклоняем!")
            await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
            
            await callback.message.edit_text(
                TRANSLATIONS[lang]["too_fast"],
                reply_markup=None
            )
            await callback.answer("Робот детектирован! (Too fast)", show_alert=True)
            return

        # Клик в нормальное время
        if is_correct:
            await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
            await callback.message.edit_text(
                TRANSLATIONS[lang]["success"],
                reply_markup=None
            )
            await callback.answer("Доступ разрешен!")
            logger.info(f"Юзер {user_id} прошел капчу за {reaction_time:.2f}с.")
        else:
            await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
            await callback.message.edit_text(
                TRANSLATIONS[lang]["wrong"],
                reply_markup=None
            )
            await callback.answer("Неверный ответ!", show_alert=True)
            logger.info(f"Юзер {user_id} нажал неправильно.")
            
    except TelegramAPIError as e:
        logger.error(f"Ошибка API при обработке клика {user_id}: {e}")
        await callback.answer("Произошла ошибка, попробуйте заново.", show_alert=True)
