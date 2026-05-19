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
                verification_timeout INTEGER DEFAULT 0,
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

        # Безопасная миграция: добавляем колонку verification_timeout для лимита времени капчи в минутах
        try:
            await db.execute("ALTER TABLE chat_settings ADD COLUMN verification_timeout INTEGER DEFAULT 0")
        except aiosqlite.OperationalError:
            pass  # Колонка уже создана

        # Таблица администраторов групп для stateless-доступа в ЛС
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_admins (
                chat_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY (chat_id, user_id)
            )
        ''')

        # Таблица участников групп для сбора фидбека при выходе
        await db.execute('''
            CREATE TABLE IF NOT EXISTS group_members (
                chat_id INTEGER,
                user_id INTEGER,
                joined_at TIMESTAMP,
                left_at TIMESTAMP,
                status TEXT,
                PRIMARY KEY (chat_id, user_id)
            )
        ''')

        await db.commit()
        logger.info("База данных инициализирована (все таблицы созданы).")

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
    allowed_keys = ['language', 'captcha_strictness', 'welcome_message', 'verification_timeout']
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
                SET language = ?, captcha_strictness = ?, welcome_message = ?, title = ?, verification_timeout = ?, is_active = 1
                WHERE chat_id = ?
            ''', (
                old_settings['language'],
                old_settings['captcha_strictness'],
                old_settings['welcome_message'],
                old_settings['title'],
                old_settings['verification_timeout'],
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
            
        # Мигрируем админов
        await db.execute('UPDATE chat_admins SET chat_id = ? WHERE chat_id = ?', (new_chat_id, old_chat_id))
        
        # Мигрируем участников
        await db.execute('UPDATE group_members SET chat_id = ? WHERE chat_id = ?', (new_chat_id, old_chat_id))
        
        await db.commit()

async def set_chat_admins(chat_id: int, admin_ids: list[int]):
    """Очищает список админов чата и записывает актуальный."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM chat_admins WHERE chat_id = ?', (chat_id,))
        for user_id in admin_ids:
            await db.execute('INSERT OR REPLACE INTO chat_admins (chat_id, user_id) VALUES (?, ?)', (chat_id, user_id))
        await db.commit()

async def get_admin_chats(user_id: int) -> list[dict]:
    """Возвращает список активных чатов, где данный user_id является администратором."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('''
            SELECT cs.chat_id, cs.title, cs.language, cs.captcha_strictness, cs.verification_timeout
            FROM chat_settings cs
            JOIN chat_admins ca ON cs.chat_id = ca.chat_id
            WHERE ca.user_id = ? AND cs.is_active = 1
        ''', (user_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def record_member_join(chat_id: int, user_id: int):
    """Фиксирует вход пользователя в группу."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO group_members (chat_id, user_id, joined_at, left_at, status)
            VALUES (?, ?, ?, NULL, 'member')
            ON CONFLICT(chat_id, user_id) DO UPDATE SET joined_at = ?, left_at = NULL, status = 'member'
        ''', (chat_id, user_id, datetime.now(), datetime.now()))
        await db.commit()

async def record_member_leave(chat_id: int, user_id: int, is_kick: bool = False):
    """Фиксирует выход или кик пользователя из группы."""
    status = 'kicked' if is_kick else 'left'
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            UPDATE group_members
            SET left_at = ?, status = ?
            WHERE chat_id = ? AND user_id = ?
        ''', (datetime.now(), status, chat_id, user_id))
        await db.commit()
