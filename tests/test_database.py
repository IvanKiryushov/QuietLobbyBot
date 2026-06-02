import pytest
import pytest_asyncio
import sqlite3
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

import database
import aiosqlite

TEST_DB_PATH = "test_bot_data.db"

@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    # Перенаправляем путь БД для тестов
    database.DB_PATH = TEST_DB_PATH
    
    # Удаляем старую тестовую БД, если есть
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
        
    await database.init_db()
    
    yield
    
    # Очищаем после тестов
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)

@pytest.mark.asyncio
async def test_init_db():
    # Проверяем, что таблицы созданы
    async with aiosqlite.connect(TEST_DB_PATH) as db:
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
            tables = [row[0] for row in await cursor.fetchall()]
            
    assert "chat_settings" in tables
    assert "user_warnings" in tables

@pytest.mark.asyncio
async def test_user_warnings():
    chat_id = -100123
    user_id = 456
    
    count = await database.get_user_warnings(chat_id, user_id)
    assert count == 0
    
    count = await database.add_user_warning(chat_id, user_id)
    assert count == 1
    
    count = await database.add_user_warning(chat_id, user_id)
    assert count == 2
    
    await database.reset_user_warnings(chat_id, user_id)
    count = await database.get_user_warnings(chat_id, user_id)
    assert count == 0

@pytest.mark.asyncio
async def test_chat_settings_anti_swear():
    chat_id = -100123
    await database.register_chat(chat_id, "Test Group")
    
    settings = await database.get_chat_settings(chat_id)
    assert settings['anti_swear_enabled'] == 0
    assert settings['max_swear_warnings'] == 3
    
    await database.update_chat_setting(chat_id, 'anti_swear_enabled', 1)
    await database.update_chat_setting(chat_id, 'max_swear_warnings', 5)
    
    settings = await database.get_chat_settings(chat_id)
    assert settings['anti_swear_enabled'] == 1
    assert settings['max_swear_warnings'] == 5

@pytest.mark.asyncio
async def test_chat_settings_new_fields():
    chat_id = -100123
    await database.register_chat(chat_id, "Test Group")
    
    settings = await database.get_chat_settings(chat_id)
    # По умолчанию
    assert settings['join_buttons_enabled'] == 1
    assert settings['is_soft_mute'] == 0
    
    # Меняем
    await database.update_chat_setting(chat_id, 'join_buttons_enabled', 0)
    await database.update_chat_setting(chat_id, 'is_soft_mute', 1)
    
    settings = await database.get_chat_settings(chat_id)
    assert settings['join_buttons_enabled'] == 0
    assert settings['is_soft_mute'] == 1

@pytest.mark.asyncio
async def test_pending_verifications():
    chat_id = -100123
    user_id = 999
    
    # Проверяем изначально
    assert not await database.is_pending_verification(chat_id, user_id)
    
    # Добавляем
    await database.add_pending_verification(chat_id, user_id)
    assert await database.is_pending_verification(chat_id, user_id)
    
    # Удаляем
    await database.remove_pending_verification(chat_id, user_id)
    assert not await database.is_pending_verification(chat_id, user_id)

@pytest.mark.asyncio
async def test_cached_messages():
    chat_id = -100123
    user_id = 999
    text = "<b>Привет</b>, мир!"
    
    # Проверяем изначально
    cached = await database.get_and_clear_cached_message(chat_id, user_id)
    assert cached is None
    
    # Кэшируем
    await database.cache_user_message(chat_id, user_id, text)
    
    # Получаем и проверяем, что оно удалилось из кэша
    cached = await database.get_and_clear_cached_message(chat_id, user_id)
    assert cached == text
    
    cached_again = await database.get_and_clear_cached_message(chat_id, user_id)
    assert cached_again is None
