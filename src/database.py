import aiosqlite
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = "bot_data.db"

async def init_db():
    """Инициализирует базу данных и создает необходимые таблицы."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                language TEXT DEFAULT 'en',
                captcha_strictness INTEGER DEFAULT 1,
                is_active BOOLEAN DEFAULT 1,
                joined_at TIMESTAMP
            )
        ''')
        await db.commit()
        logger.info("База данных инициализирована.")

async def register_chat(chat_id: int):
    """Регистрирует новый чат в БД при добавлении бота, или активирует существующий."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO chat_settings (chat_id, is_active, joined_at)
            VALUES (?, 1, ?)
            ON CONFLICT(chat_id) DO UPDATE SET is_active = 1
        ''', (chat_id, datetime.now()))
        await db.commit()

async def deactivate_chat(chat_id: int):
    """Помечает чат как неактивный (при кике бота)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            UPDATE chat_settings SET is_active = 0 WHERE chat_id = ?
        ''', (chat_id,))
        await db.commit()

async def get_chat_settings(chat_id: int) -> dict:
    """Получает настройки конкретного чата."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM chat_settings WHERE chat_id = ?', (chat_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return {}

async def get_all_active_chats() -> list:
    """Возвращает список всех активных чатов из базы данных."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM chat_settings WHERE is_active = 1') as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def update_chat_setting(chat_id: int, key: str, value):
    """Обновляет конкретную настройку для чата."""
    allowed_keys = ['language', 'captcha_strictness']
    if key not in allowed_keys:
        raise ValueError(f"Настройка {key} не разрешена.")
        
    async with aiosqlite.connect(DB_PATH) as db:
        # Безопасно, так как key валидируется выше
        await db.execute(f'''
            UPDATE chat_settings SET {key} = ? WHERE chat_id = ?
        ''', (value, chat_id))
        await db.commit()
