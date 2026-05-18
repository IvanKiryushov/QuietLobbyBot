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
        # Безопасная миграция: добавляем колонку title, если её еще нет в существующей таблице
        try:
            await db.execute("ALTER TABLE chat_settings ADD COLUMN title TEXT")
        except aiosqlite.OperationalError:
            pass  # Колонка уже создана

        # Безопасная миграция: добавляем колонку welcome_message для кастомных приветствий новичков
        try:
            await db.execute("ALTER TABLE chat_settings ADD COLUMN welcome_message TEXT")
        except aiosqlite.OperationalError:
            pass  # Колонка уже создана

        await db.commit()
        logger.info("База данных инициализирована.")

async def register_chat(chat_id: int, title: str = None):
    """Регистрирует новый чат в БД при добавлении бота, или активирует существующий с сохранением названия."""
    async with aiosqlite.connect(DB_PATH) as db:
        if title:
            await db.execute('''
                INSERT INTO chat_settings (chat_id, title, is_active, joined_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(chat_id) DO UPDATE SET is_active = 1, title = ?
            ''', (chat_id, title, datetime.now(), title))
        else:
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
    allowed_keys = ['language', 'captcha_strictness', 'welcome_message']
    if key not in allowed_keys:
        raise ValueError(f"Настройка {key} не разрешена.")
        
    async with aiosqlite.connect(DB_PATH) as db:
        # Безопасно, так как key валидируется выше
        await db.execute(f'''
            UPDATE chat_settings SET {key} = ? WHERE chat_id = ?
        ''', (value, chat_id))
        await db.commit()

async def migrate_chat_id(old_chat_id: int, new_chat_id: int):
    """Мигрирует настройки чата при преобразовании обычной группы в супергруппу."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        # Получаем старые настройки
        async with db.execute('SELECT * FROM chat_settings WHERE chat_id = ?', (old_chat_id,)) as cursor:
            old_settings = await cursor.fetchone()
            
        if not old_settings:
            logger.warning(f"Попытка миграции для несуществующего старого чата {old_chat_id}")
            return
            
        # Проверяем, существует ли уже запись для новой группы
        async with db.execute('SELECT * FROM chat_settings WHERE chat_id = ?', (new_chat_id,)) as cursor:
            new_exists = await cursor.fetchone()
            
        if new_exists:
            # Обновляем новый чат настройками из старого, а старый деактивируем/удаляем
            await db.execute('''
                UPDATE chat_settings 
                SET language = ?, captcha_strictness = ?, welcome_message = ?, title = ?, is_active = 1
                WHERE chat_id = ?
            ''', (
                old_settings['language'],
                old_settings['captcha_strictness'],
                old_settings['welcome_message'],
                old_settings['title'],
                new_chat_id
            ))
            await db.execute('DELETE FROM chat_settings WHERE chat_id = ?', (old_chat_id,))
            logger.info(f"Настройки чата {old_chat_id} объединены с новым супергрупповым ID {new_chat_id}")
        else:
            # Если новой записи еще нет, просто обновляем ID в старой строке
            await db.execute('''
                UPDATE chat_settings 
                SET chat_id = ?, is_active = 1 
                WHERE chat_id = ?
            ''', (new_chat_id, old_chat_id))
            logger.info(f"ID чата {old_chat_id} изменен на новый супергрупповой ID {new_chat_id}")
            
        await db.commit()
