from typing import Any, Optional, Tuple, Union
import aiosqlite

DB_PATH = "data/users.db"


async def init_db() -> None:
    """Создает необходимые таблицы в базе данных при первом запуске бота."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Существующая таблица пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                pass int DEFAULT 0,
                game_nickname TEXT DEFAULT NULL
            )
        """)

        # Миграция: проверяем структуру таблицы активности
        async with db.execute("PRAGMA table_info(giveaway_activity)") as cursor:
            cols = [row[1] for row in await cursor.fetchall()]
            if cols and "voice_minutes" not in cols:
                await db.execute("DROP TABLE IF EXISTS giveaway_activity")
                await db.execute("DROP TABLE IF EXISTS giveaways")

        # Таблица текущего розыгрыша (при новом старте очищается)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                prizes TEXT NOT NULL,
                banner_url TEXT DEFAULT NULL,
                start_time INTEGER NOT NULL,
                end_time INTEGER NOT NULL,
                activity_minutes INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                is_active INTEGER DEFAULT 1,
                winners TEXT DEFAULT NULL
            )
        """)

        # Таблица отслеживания минут активности участников для текущего розыгрыша
        # Содержит строчку last_minute_awarded — когда последний раз начислялась минута
        await db.execute("""
            CREATE TABLE IF NOT EXISTS giveaway_activity (
                user_id INTEGER PRIMARY KEY,
                voice_minutes INTEGER DEFAULT 0,
                last_minute_awarded INTEGER DEFAULT 0,
                is_in_voice INTEGER DEFAULT 0,
                is_entered INTEGER DEFAULT 0
            )
        """)

        await db.commit()


async def get_user_info(user_id: int, info_type: str) -> Any:
    """Получает информацию о пользователе из БД."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT {info_type} FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]

            await db.execute(
                "INSERT INTO users (user_id) VALUES (?)",
                (user_id,)
            )
            await db.commit()
            return 0


async def update_user_info(user_id: int, info_type: str, amount: str) -> None:
    """Обновляет информацию о пользователе."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE users SET {info_type} = ? WHERE user_id = ?",
            (amount, user_id)
        )
        await db.commit()


# --- ФУНКЦИИ ДЛЯ РАБОТЫ С РОЗЫГРЫШАМИ ЧЕРЕЗ DB_HELPER ---

async def reset_giveaway_db() -> None:
    """
    При старте нового розыгрыша все данные в таблицах розыгрыша стираются/обнуляются.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM giveaways")
        await db.execute("DELETE FROM giveaway_activity")
        await db.commit()


async def create_new_giveaway(
    guild_id: int,
    channel_id: int,
    message_id: int,
    title: str,
    description: str,
    prizes: str,
    banner_url: Optional[str],
    start_time: int,
    end_time: int,
    activity_minutes: int,
    creator_id: int,
) -> int:
    """
    Стирает старые данные розыгрыша и создает новую запись в БД.
    Возвращает ID созданного розыгрыша.
    """
    await reset_giveaway_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO giveaways (
                guild_id, channel_id, message_id, title, description,
                prizes, banner_url, start_time, end_time, activity_minutes,
                creator_id, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                guild_id,
                channel_id,
                message_id,
                title,
                description,
                prizes,
                banner_url,
                start_time,
                end_time,
                activity_minutes,
                creator_id,
            ),
        )
        await db.commit()
        return cursor.lastrowid or 0


async def get_current_giveaway() -> Optional[dict[str, Any]]:
    """Возвращает данные текущего розыгрыша."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM giveaways ORDER BY id DESC LIMIT 1") as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
    return None


async def finish_current_giveaway(winners_json: str) -> None:
    """Помечает текущий розыгрыш как завершенный и записывает победителей."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE giveaways SET is_active = 0, winners = ? WHERE id = (SELECT id FROM giveaways ORDER BY id DESC LIMIT 1)",
            (winners_json,),
        )
        await db.commit()


async def process_voice_minute_tick(active_user_ids: set[int], current_timestamp: int) -> None:
    """
    Обрабатывает посекундный тик проверки голосовых каналов ТОЛЬКО для зарегистрированных участников:
    - Пользователи, которые не нажали кнопку участия, в БД не учитываются вовсе.
    - Минуты начисляются только участникам, нажавшим кнопку (с шагом 60 сек).
    - Пользователи, покинувшие войс, помечаются как is_in_voice = 0.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Получаем всех зарегистрированных участников розыгрыша
        async with db.execute(
            "SELECT user_id, voice_minutes, last_minute_awarded, is_in_voice FROM giveaway_activity"
        ) as cursor:
            participants = await cursor.fetchall()

        if not participants:
            return

        for user_id, voice_minutes, last_awarded, is_in_voice in participants:
            if user_id in active_user_ids:
                if not is_in_voice:
                    # Участник зашел в войс: начинаем отсчет с текущего момента
                    await db.execute(
                        "UPDATE giveaway_activity SET is_in_voice = 1, last_minute_awarded = ? WHERE user_id = ?",
                        (current_timestamp, user_id),
                    )
                else:
                    elapsed = current_timestamp - last_awarded
                    if elapsed >= 60:
                        minutes_to_add = elapsed // 60
                        new_last_awarded = last_awarded + (minutes_to_add * 60)
                        await db.execute(
                            """
                            UPDATE giveaway_activity
                            SET voice_minutes = voice_minutes + ?, last_minute_awarded = ?
                            WHERE user_id = ?
                            """,
                            (minutes_to_add, new_last_awarded, user_id),
                        )
            else:
                if is_in_voice:
                    # Участник вышел из войса
                    await db.execute(
                        "UPDATE giveaway_activity SET is_in_voice = 0 WHERE user_id = ?",
                        (user_id,),
                    )

        await db.commit()


async def get_user_giveaway_progress(user_id: int) -> Tuple[int, int, bool, bool]:
    """Возвращает (voice_minutes, last_minute_awarded, is_in_voice, is_entered) для пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT voice_minutes, last_minute_awarded, is_in_voice, is_entered FROM giveaway_activity WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return int(row[0]), int(row[1]), bool(row[2]), bool(row[3])
    return 0, 0, False, False


async def mark_user_entered(
    user_id: int, is_in_voice: bool = False, current_timestamp: int = 0
) -> None:
    """
    Регистрирует участника в БД при нажатии кнопки участия:
    - Пользователь создается в БД только в этот момент (до этого в БД отсутствует).
    - Минуты начинают копиться только после нажатия кнопки.
    - Если пользователь уже сидит в голосовом канале в момент нажатия, отсчет начинается сразу.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO giveaway_activity (user_id, voice_minutes, last_minute_awarded, is_in_voice, is_entered)
            VALUES (?, 0, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET is_entered = 1
            """,
            (user_id, current_timestamp, 1 if is_in_voice else 0),
        )
        await db.commit()


async def get_eligible_giveaway_users(min_minutes: int) -> list[int]:
    """Возвращает список ID пользователей, нажавших кнопку участия и набравших >= min_minutes в войсе."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM giveaway_activity WHERE voice_minutes >= ?",
            (min_minutes,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [int(r[0]) for r in rows]


async def get_giveaway_counts(min_minutes: int) -> Tuple[int, int]:
    """Возвращает (всего_нажали_участвовать, выполнили_условие_по_времени)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT
                COUNT(*),
                SUM(CASE WHEN voice_minutes >= ? THEN 1 ELSE 0 END)
            FROM giveaway_activity
            """,
            (min_minutes,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                total = int(row[0] or 0)
                eligible = int(row[1] or 0)
                return total, eligible
    return 0, 0
