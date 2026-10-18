import aiosqlite

DB_PATH = "data/users.db"


async def init_db():
    """Создает необходимые таблицы при первом запуске бота."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                pass int DEFAULT 0,
                game_nickname TEXT DEFAULT NULL
            )
        """)
        await db.commit()


async def get_user_info(user_id: int, info_type: str):
    """Получает информацию о пользователе из БД."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT {info_type} FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]
            
            # Если пользователя нет в базе, создаем запись
            await db.execute(
                f"INSERT INTO users (user_id) VALUES (?)", 
                (user_id,)
            )
            await db.commit()
            return 0


async def update_user_info(user_id: int, info_type: str, amount: str):
    """Обновляет информацию о пользователе."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE users SET {info_type} = ? WHERE user_id = ?", 
            (amount, user_id)
        )
        await db.commit()