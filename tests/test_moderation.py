import pytest
import pytest_asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Добавляем src в PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

import database
from handlers.moderation import unmute_command

TEST_DB_PATH = "test_bot_data_mod.db"

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
@patch('handlers.moderation.is_user_admin')
async def test_unmute_command_by_reply(mock_is_admin):
    # Настраиваем мок админа (отправитель команды - админ)
    mock_is_admin.return_value = True

    # Инициализируем БД данными предупреждений для пользователя
    chat_id = -100111
    target_user_id = 999
    await database.add_user_warning(chat_id, target_user_id)
    await database.add_user_warning(chat_id, target_user_id)
    
    # Проверяем, что предупреждения записались
    assert await database.get_user_warnings(chat_id, target_user_id) == 2

    # Создаем мок Bot
    bot = AsyncMock()
    
    # Создаем мок сообщения с reply
    message = AsyncMock()
    message.chat.id = chat_id
    message.from_user.id = 123  # ID админа
    
    reply_message = MagicMock()
    reply_message.from_user.id = target_user_id
    reply_message.from_user.mention_html.return_value = "TargetUser"
    message.reply_to_message = reply_message
    
    command = MagicMock()
    command.args = None

    # Вызываем тестируемую команду
    with patch('database.DB_PATH', TEST_DB_PATH):
        await unmute_command(message, bot, command)

    # Проверяем, что был вызван restrict_chat_member для размьюта
    bot.restrict_chat_member.assert_called_once()
    call_args = bot.restrict_chat_member.call_args[1]
    assert call_args['chat_id'] == chat_id
    assert call_args['user_id'] == target_user_id
    assert call_args['permissions'].can_send_messages is True

    # Проверяем, что предупреждения сброшены в БД
    assert await database.get_user_warnings(chat_id, target_user_id) == 0

    # Проверяем, что бот ответил сообщением об успешном размьюте
    message.answer.assert_called_once()
    assert "разблокирован" in message.answer.call_args[0][0]
    
    # Проверяем, что сообщение админа с командой удалено
    message.delete.assert_called_once()


@pytest.mark.asyncio
@patch('handlers.moderation.is_user_admin')
async def test_unmute_command_by_id(mock_is_admin):
    mock_is_admin.return_value = True

    chat_id = -100111
    target_user_id = 888
    await database.add_user_warning(chat_id, target_user_id)
    
    # Проверяем, что предупреждение записалось
    assert await database.get_user_warnings(chat_id, target_user_id) == 1

    bot = AsyncMock()
    
    message = AsyncMock()
    message.chat.id = chat_id
    message.from_user.id = 123
    message.reply_to_message = None
    
    command = MagicMock()
    command.args = "888"

    # Вызываем тестируемую команду
    with patch('database.DB_PATH', TEST_DB_PATH):
        await unmute_command(message, bot, command)

    # Проверяем вызов restrict_chat_member
    bot.restrict_chat_member.assert_called_once()
    call_args = bot.restrict_chat_member.call_args[1]
    assert call_args['chat_id'] == chat_id
    assert call_args['user_id'] == target_user_id

    # Проверяем сброс предупреждений
    assert await database.get_user_warnings(chat_id, target_user_id) == 0
