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

# Тексты переводов (без декоративных эмодзи)
TRANSLATIONS = {
    "ru": {
        "greet": "Привет, {name}!\n\nДля возможности писать в чате <b>{chat_name}</b> выбери <b>{target_word}</b> среди кнопок ниже:",
        "wrong": "Неверно! Ты заблокирован.",
        "too_fast": "Слишком быстро! Ты заблокирован.",
        "success": "Ура!\nТеперь ты можешь писать в чате, добро пожаловать!",
        "not_your_button": "Эта кнопка предназначена для другого пользователя.",
        "group_greet": "Привет, <b>{name}</b>!\nЧтобы писать в чате <b>{chat_name}</b>, пройди быструю проверку на бота в ЛС.\n{time_limit_info}",
        "time_limit_info": "У тебя есть <b>{timeout_min} мин.</b>\n",
        "btn_verify": "НАЖМИ, ЕСЛИ ТЫ НЕ БОТ",
        "btn_return": "Вернуться в чат",
        "too_slow": "Время вышло! Ты не успел пройти проверку вовремя и был удален из чата."
    },
    "en": {
        "greet": "Hello, {name}!\n\nYou are joining <b>{chat_name}</b>.\n\nTo prevent spam, please select the <b>{target_word}</b> from the buttons below:",
        "wrong": "Error! You selected the wrong emoji. You are blocked.",
        "too_fast": "Too fast! Live humans don't click that quickly. Request denied.",
        "success": "Verification passed!\nYou are unmuted in the group, welcome!",
        "not_your_button": "This verification is for another user.",
        "group_greet": "<b>ACCESS RESTRICTED</b>\n\nHello, <b>{name}</b>!\nTo write in <b>{chat_name}</b>, you must pass a quick anti-spam verification in PM.\n\n{time_limit_info}<b>CLICK THE BUTTON BELOW TO UNMUTE:</b>",
        "time_limit_info": "<b>ATTENTION:</b> You have exactly <b>{timeout_min} minutes</b>, or you will be automatically kicked from the group!\n\n",
        "btn_verify": "CLICK IF YOU ARE NOT A BOT",
        "btn_return": "Return to Chat",
        "too_slow": "Time is up! You failed to verify in time and were removed from the group."
    },
    "vi": {
        "greet": "Xin chào, {name}!\n\nBạn đang tham gia nhóm <b>{chat_name}</b>.\n\nĐể chống tin rác, vui lòng chọn <b>{target_word}</b> từ các nút bên dưới:",
        "wrong": "Lỗi! Bạn đã chọn sai biểu tượng. Bạn đã bị chặn.",
        "too_fast": "Quá nhanh! Con người không thể bấm nhanh như vậy. Đã chặn truy cập.",
        "success": "Đã xác nhận!\nBạn đã được mở chặn trong nhóm, chào mừng bạn!",
        "not_your_button": "Nút này không dành cho bạn!",
        "group_greet": "<b>TRUY CẬP BỊ HẠN CHẾ</b>\n\nXin chào, <b>{name}</b>!\nĐể gửi tin nhắn trong nhóm <b>{chat_name}</b>, bạn phải vượt qua xác minh chống tin rác trong tin nhắn riêng.\n\n{time_limit_info}<b>BẤM VÀO NÚT BÊN DƯỚI ĐỂ MỞ CHẶN:</b>",
        "time_limit_info": "<b>CHÚ Ý:</b> Bạn có đúng <b>{timeout_min} phút</b>, nếu không bạn sẽ tự động bị xóa khỏi nhóm!\n\n",
        "btn_verify": "BẤM NẾU BẠN KHÔNG PHẢI BOT",
        "btn_return": "Quay lại nhóm",
        "too_slow": "Hết giờ! Bạn đã không xác minh kịp thời và đã bị xóa khỏi nhóm."
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
