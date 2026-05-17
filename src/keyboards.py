import random
import time
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Мультиязычный словарь эмодзи
EMOJI_CAPTCHA_DICT = {
    "🍎": {"ru": "яблоко", "en": "apple", "vi": "quả táo"},
    "🐱": {"ru": "кошку", "en": "cat", "vi": "con mèo"},
    "🚗": {"ru": "машину", "en": "car", "vi": "xe hơi"},
    "🔑": {"ru": "ключ", "en": "key", "vi": "chìa khóa"},
    "🍕": {"ru": "пиццу", "en": "pizza", "vi": "bánh pizza"},
    "✈️": {"ru": "самолет", "en": "airplane", "vi": "máy bay"},
    "🏀": {"ru": "мяч", "en": "ball", "vi": "quả bóng"},
    "🏠": {"ru": "дом", "en": "house", "vi": "ngôi nhà"},
}

# Тексты переводов
TRANSLATIONS = {
    "ru": {
        "greet": "👋 Привет, {name}!\n\nТы подал(а) заявку в группу <b>{chat_name}</b>.\n\nДля защиты от спама выбери <b>{target_word}</b> среди кнопок ниже:",
        "wrong": "❌ Ошибка! Ты выбрал не тот символ. Попробуй подать заявку заново.",
        "too_fast": "❌ Слишком быстро! Люди так быстро не кликают. Доступ заблокирован.",
        "success": "✅ Проверка пройдена! Заявка одобрена, добро пожаловать в чат.",
        "not_your_button": "Это не твоя кнопка!"
    },
    "en": {
        "greet": "👋 Hello, {name}!\n\nYou have requested to join <b>{chat_name}</b>.\n\nTo prevent spam, please select the <b>{target_word}</b> from the buttons below:",
        "wrong": "❌ Error! You selected the wrong emoji. Please try to join again.",
        "too_fast": "❌ Too fast! Live humans don't click that quickly. Request denied.",
        "success": "✅ Verification passed! Welcome to the group.",
        "not_your_button": "This button is not for you!"
    },
    "vi": {
        "greet": "👋 Xin chào, {name}!\n\nBạn đã gửi yêu cầu tham gia nhóm <b>{chat_name}</b>.\n\nĐể chống tin rác, vui lòng chọn <b>{target_word}</b> từ các nút bên dưới:",
        "wrong": "❌ Lỗi! Bạn đã chọn sai biểu tượng. Vui lòng gửi lại yêu cầu.",
        "too_fast": "❌ Quá nhanh! Con người không thể bấm nhanh như vậy. Đã chặn truy cập.",
        "success": "✅ Đã xác nhận! Yêu cầu được duyệt, chào mừng bạn vào nhóm.",
        "not_your_button": "Nút này không dành cho bạn!"
    }
}

def generate_emoji_captcha(chat_id: int, user_id: int, lang: str = "en"):
    """
    Генерирует случайную капчу на языке пользователя с защитой по времени.
    """
    if lang not in TRANSLATIONS:
        lang = "en"

    all_keys = list(EMOJI_CAPTCHA_DICT.keys())
    chosen_emojis = random.sample(all_keys, 4)
    correct_emoji = random.choice(chosen_emojis)
    target_word = EMOJI_CAPTCHA_DICT[correct_emoji][lang]
    
    # Текущее серверное время (целое число) для временного капкана
    now_ts = int(time.time())
    
    buttons = []
    for emoji in chosen_emojis:
        is_correct = "1" if emoji == correct_emoji else "0"
        # callback_data: c_clk:is_correct:chat_id:user_id:timestamp
        cb_data = f"c_clk:{is_correct}:{chat_id}:{user_id}:{now_ts}"
        buttons.append(InlineKeyboardButton(text=emoji, callback_data=cb_data))
        
    keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons])
    return target_word, keyboard
