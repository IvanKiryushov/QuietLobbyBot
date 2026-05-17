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
        "greet": "Привет, {name}!\n\nТы вступаешь в группу <b>{chat_name}</b>.\n\nДля защиты от спама выбери <b>{target_word}</b> среди кнопок ниже:",
        "wrong": "Ошибка! Ты выбрал не тот символ. Ты заблокирован.",
        "too_fast": "Слишком быстро! Люди так быстро не кликают. Доступ заблокирован.",
        "success": "Проверка пройдена! Ты разблокирован(а) в чате, добро пожаловать!",
        "not_your_button": "Это не твоя кнопка!",
        "group_greet": "<b>ДОСТУП ОГРАНИЧЕН</b>\n\nПривет, <b>{name}</b>!\nЧтобы писать в группе <b>{chat_name}</b>, тебе нужно пройти быструю проверку против спам-ботов в ЛС.\n\n<b>ВНИМАНИЕ:</b> У тебя есть ровно <b>60 секунд</b>, иначе ты будешь автоматически удален из чата!\n\n<b>НАЖМИ КНОПКУ НИЖЕ ДЛЯ РАЗБЛОКИРОВКИ:</b>",
        "btn_verify": "ЖМИ СЮДА ДЛЯ РАЗБЛОКИРОВКИ",
        "too_slow": "Время вышло! Ты не успел пройти проверку вовремя и был удален из чата."
    },
    "en": {
        "greet": "Hello, {name}!\n\nYou are joining <b>{chat_name}</b>.\n\nTo prevent spam, please select the <b>{target_word}</b> from the buttons below:",
        "wrong": "Error! You selected the wrong emoji. You are blocked.",
        "too_fast": "Too fast! Live humans don't click that quickly. Request denied.",
        "success": "Verification passed! You are unmuted in the group, welcome!",
        "not_your_button": "This button is not for you!",
        "group_greet": "<b>ACCESS RESTRICTED</b>\n\nHello, <b>{name}</b>!\nTo write in <b>{chat_name}</b>, you must pass a quick anti-spam verification in PM.\n\n<b>ATTENTION:</b> You have exactly <b>60 seconds</b>, or you will be automatically kicked from the group!\n\n<b>CLICK THE BUTTON BELOW TO UNMUTE:</b>",
        "btn_verify": "CLICK HERE TO UNMUTE",
        "too_slow": "Time is up! You failed to verify in time and were removed from the group."
    },
    "vi": {
        "greet": "Xin chào, {name}!\n\nBạn đang tham gia nhóm <b>{chat_name}</b>.\n\nĐể chống tin rác, vui lòng chọn <b>{target_word}</b> từ các nút bên dưới:",
        "wrong": "Lỗi! Bạn đã chọn sai biểu tượng. Bạn đã bị chặn.",
        "too_fast": "Quá nhanh! Con người không thể bấm nhanh như vậy. Đã chặn truy cập.",
        "success": "Đã xác nhận! Bạn đã được mở chặn trong nhóm, chào mừng bạn!",
        "not_your_button": "Nút này không dành cho bạn!",
        "group_greet": "<b>TRUY CẬP BỊ HẠN CHẾ</b>\n\nXin chào, <b>{name}</b>!\nĐể gửi tin nhắn trong nhóm <b>{chat_name}</b>, bạn phải vượt qua xác minh chống tin rác trong tin nhắn riêng.\n\n<b>CHÚ Ý:</b> Bạn có đúng <b>60 giây</b>, nếu không bạn sẽ tự động bị xóa khỏi nhóm!\n\n<b>BẤM VÀO NÚT BÊN DƯỚI ĐỂ MỞ CHẶN:</b>",
        "btn_verify": "BẤM VÀO ĐÂY ĐỂ MỞ CHẶN",
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
