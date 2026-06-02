import pytest
import pytest_asyncio
import sqlite3
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

import database
from handlers.captcha import parse_welcome_message

TEST_DB_PATH = "test_bot_data_sa.db"

@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    database.DB_PATH = TEST_DB_PATH
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    await database.init_db()
    yield
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)

@pytest.mark.asyncio
async def test_global_settings_store():
    # Проверяем дефолтные значения
    val = await database.get_global_setting("test_key")
    assert val is None
    
    # Сохраняем
    await database.set_global_setting("test_key", "test_val")
    val = await database.get_global_setting("test_key")
    assert val == "test_val"
    
    # Удаляем
    await database.set_global_setting("test_key", None)
    val = await database.get_global_setting("test_key")
    assert val is None

@pytest.mark.asyncio
async def test_parse_welcome_message_with_sa_button():
    # Без настройки кнопки
    await database.set_global_setting("sa_button_text", None)
    await database.set_global_setting("sa_button_url", None)
    
    text = "Привет!\nКнопка 1 | https://t.me/chat"
    welcome_text, welcome_markup = await parse_welcome_message(text)
    
    assert welcome_text == "Привет!"
    assert welcome_markup is not None
    assert len(welcome_markup.inline_keyboard) == 1
    assert welcome_markup.inline_keyboard[0][0].text == "Кнопка 1"
    assert welcome_markup.inline_keyboard[0][0].url == "https://t.me/chat"
    
    # С настройкой обязательной кнопки
    await database.set_global_setting("sa_button_text", "Автоматизация для тебя!")
    await database.set_global_setting("sa_button_url", "https://t.me/bimivan")
    
    welcome_text, welcome_markup = await parse_welcome_message(text)
    
    assert welcome_text == "Привет!"
    assert welcome_markup is not None
    assert len(welcome_markup.inline_keyboard) == 2
    # Первая кнопка (сверху) — обязательная
    assert welcome_markup.inline_keyboard[0][0].text == "Автоматизация для тебя!"
    assert welcome_markup.inline_keyboard[0][0].url == "https://t.me/bimivan"
    # Вторая кнопка — пользовательская
    assert welcome_markup.inline_keyboard[1][0].text == "Кнопка 1"
    assert welcome_markup.inline_keyboard[1][0].url == "https://t.me/chat"
