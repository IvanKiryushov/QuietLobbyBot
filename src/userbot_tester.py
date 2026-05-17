import asyncio
import os
import random
from telethon import TelegramClient, events
from dotenv import load_dotenv

# Подгружаем настройки
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_USERNAME = os.getenv("TEST_BOT_USERNAME", "QuietLobbyBot")

if not API_ID or not API_HASH:
    print("\n[!] Ошибка: Настройте API_ID и API_HASH в файле .env!\n")
    exit(1)

# Словарь знаний "Умного Взломщика" (сопоставление слов из текста с эмодзи)
# Бот-тестировщик знает переводы на всех 3 языках
SMART_VOCABULARY = {
    # Русские слова
    "яблоко": "🍎", "кошку": "🐱", "машину": "🚗", "ключ": "🔑",
    "пиццу": "🍕", "самолет": "✈️", "мяч": "🏀", "дом": "🏠",
    # Английские слова
    "apple": "🍎", "cat": "🐱", "car": "🚗", "key": "🔑",
    "pizza": "🍕", "airplane": "✈️", "ball": "🏀", "house": "🏠",
    # Вьетнамские слова
    "quả táo": "🍎", "con mèo": "🐱", "xe hơi": "🚗", "chìa khóa": "🔑",
    "bánh pizza": "🍕", "máy bay": "✈️", "quả bóng": "🏀", "ngôi nhà": "🏠",
}

client = TelegramClient(os.path.join(os.path.dirname(__file__), '..', 'tester_session'), int(API_ID), API_HASH)

@client.on(events.NewMessage(incoming=True))
async def handle_incoming_message(event):
    sender = await event.get_sender()
    if not sender:
        return

    sender_username = getattr(sender, 'username', '')
    if sender_username and sender_username.lower() == BOT_USERNAME.lower():
        print(f"\n[📥] Перехвачено сообщение от @{BOT_USERNAME}:")
        print("-" * 50)
        print(event.raw_text)
        print("-" * 50)

        if not event.reply_markup:
            print("[⚠️] Сообщение без кнопок. Ожидаем дальше.")
            return

        # 🧠 МОЗГИ ЭМУЛЯТОРА: Пытаемся понять текст сообщения
        print("[🧠] Анализирую текст сообщения...")
        text_lower = event.raw_text.lower()
        
        target_emoji = None
        matched_word = None
        
        # Сканируем текст на наличие слов из нашего словаря
        for word, emoji in SMART_VOCABULARY.items():
            if word in text_lower:
                target_emoji = emoji
                matched_word = word
                break
        
        if not target_emoji:
            print("[⚠️] Не удалось распознать кодовое слово в тексте. Пробую кликнуть наугад.")
        else:
            print(f"[💡] УСПЕХ! Найдено кодовое слово: '{matched_word.upper()}'. Ищу эмодзи: {target_emoji}")

        # ⏱️ ИМИТАЦИЯ ПОВЕДЕНИЯ ЧЕЛОВЕКА
        # Задаем случайное время чтения и реакции (от 3.5 до 5.5 секунд)
        human_delay = round(random.uniform(3.5, 5.5), 2)
        print(f"[⏱️] Имитирую человека: 'читаю' текст и выбираю кнопку... Ждём {human_delay} сек.")
        await asyncio.sleep(human_delay)

        try:
            if target_emoji:
                # Пытаемся кликнуть именно на кнопку с нужным эмодзи
                print(f"[🎯] Кликаю по ПРАВИЛЬНОЙ кнопке с эмодзи {target_emoji}...")
                # Telethon умеет искать кнопку по её тексту
                await event.click(text=target_emoji)
            else:
                # Резервный вариант, если слово не распознано
                print("[🎲] Кодовое слово не найдено. Кликаю на первую кнопку наугад...")
                await event.click(0)
                
            print("[✅] Клик эмулирован успешно!")
        except Exception as e:
            print(f"[❌] Ошибка при клике: {e}")

async def main():
    print("=" * 70)
    print(f"🤖 ЗАПУСК УМНОГО ЭМУЛЯТОРА ЧЕЛОВЕКА (Smart UserBot) для @{BOT_USERNAME}")
    print("=" * 70)
    
    await client.start()
    
    me = await client.get_me()
    print(f"\n[🎉] Авторизован аккаунт: {me.first_name} (@{me.username})")
    print("[*] Эмулятор запущен. Ждём прихода капчи...")
    print("[*] Чтобы протестировать, выйдите из группы и подайте заявку снова.")
    
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n[👋] Эмулятор человека остановлен.")
