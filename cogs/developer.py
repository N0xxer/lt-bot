import os
import sys
import disnake
from disnake.ext import commands
import aiosqlite
from typing import Any
import json

with open("configs/bot_config.json", "r", encoding="utf-8") as f:
    config = json.load(f)


class DeveloperCog(commands.Cog):
    """Модуль управления ботом для разработчика и общая информация."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- ОБЩЕДОСТУПНАЯ КОМАНДА ИНФОРМАЦИИ ---
    @commands.slash_command(
        name="about",
        description="Информация о боте и разработчике"
    )
    async def about(self, inter: disnake.ApplicationCommandInteraction):
        """Выводит краткую сводку о проекте."""
        info_text = (
            f"🤖 **Информация о боте**\n"
            f"──────────────────────────────\n"
            f"👨‍💻 **Разработчик:** <@980162487250980895>\n"
            f"⚙️ **Библиотека:** disnake (Python)\n"
            f"📡 **Задержка:** {round(self.bot.latency * 1000)}ms\n"
            f"📊 **Версия:** {config['version']}\n"
            f"──────────────────────────────"
        )
        await inter.response.send_message(embed=disnake.Embed(description=info_text, color=0x741b47))

    # --- КОМАНДЫ ТОЛЬКО ДЛЯ ВЛАДЕЛЬЦА (DEVELOPER) ---

    async def cog_autocomplete(
        self, inter: disnake.ApplicationCommandInteraction, user_input: str
    ):
        cogs = [ext.replace("cogs.", "") for ext in self.bot.extensions.keys()]

        filtered_cogs = [
            cog for cog in cogs if user_input.lower() in cog.lower()
        ]

        return filtered_cogs[:25]

    @commands.slash_command(
        name="reload", description="[DEV] Перезагрузить конкретный cog"
    )
    @commands.has_permissions(administrator=True)
    async def reload_cog(
        self,
        inter: disnake.ApplicationCommandInteraction,
        cog_name: str = commands.Param(autocomplete=cog_autocomplete),
    ):
        """Перезагрузка одного кога без остановки всего бота."""
        try:
            formatted_name = (
                cog_name if cog_name.startswith("cogs.") else f"cogs.{cog_name}"
            )

            self.bot.reload_extension(formatted_name)
            await inter.response.send_message(
                f"✅ Модуль `{formatted_name}` успешно перезагружен!",
                ephemeral=True,
            )
        except Exception as e:
            await inter.response.send_message(
                f"❌ Ошибка при перезагрузке `{cog_name}`:\n```{e}```",
                ephemeral=True,
            )

    @commands.slash_command(
        name="restart",
        description="[DEV] Полная перезагрузка бота"
    )
    @commands.has_permissions(administrator=True)
    async def restart_bot(self, inter: disnake.ApplicationCommandInteraction):
        """Полностью перезапускает процесс Python."""
        await inter.response.send_message("🔄 Перезапуск бота...", ephemeral=True)
        # Перезапускает текущий скрипт с теми же аргументами
        os.execv(sys.executable, [sys.executable] + sys.argv)

    @staticmethod
    async def _get_tables() -> list[str]:
        """Возвращает список всех таблиц базы данных."""
        async with aiosqlite.connect("dbs/main.db") as db:
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';") as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows]

    @staticmethod
    async def _get_table_info(table_name: str) -> tuple[list[str], str]:
        """Возвращает список колонок таблицы и имя её первичного ключа (первой колонки по умолчанию)."""
        async with aiosqlite.connect("dbs/main.db") as db:
            async with db.execute(f"PRAGMA table_info({table_name});") as cursor:
                columns_info = await cursor.fetchall()
                if not columns_info:
                    return [], ""
                columns = [col[1] for col in columns_info]
                # Ищем первичный ключ, если нет — берем первый столбец
                pk_col = next((col[1] for col in columns_info if col[5] > 0), columns[0])
                return columns, pk_col

    # --- Autocomplete функции ---

    async def autocomplete_tables(self, inter: disnake.ApplicationCommandInteraction, user_input: str) -> list[str]:
        tables = await self._get_tables()
        return [t for t in tables if user_input.lower() in t.lower()][:25]

    async def autocomplete_columns(self, inter: disnake.ApplicationCommandInteraction, user_input: str) -> list[str]:
        table = inter.filled_options.get("table")
        if not table:
            return []
        columns, _ = await self._get_table_info(table)
        return [c for c in columns if user_input.lower() in c.lower()][:25]

    async def autocomplete_rows(self, inter: disnake.ApplicationCommandInteraction, user_input: str) -> list[str]:
        table = inter.filled_options.get("table")
        if not table:
            return []
        columns, pk_col = await self._get_table_info(table)
        if not pk_col:
            return []

        async with aiosqlite.connect("dbs/main.db") as db:
            async with db.execute(f"SELECT {pk_col} FROM {table} LIMIT 50;") as cursor:
                rows = await cursor.fetchall()
                pk_values = [str(r[0]) for r in rows]
                return [val for val in pk_values if user_input.lower() in val.lower()][:25]

    # --- Слэш-команда ---

    @commands.slash_command(
        name="db_edit",
        description="Изменить значение конкретного поля в БД (Dev)",
        default_member_permissions=disnake.Permissions(administrator=True)
    )
    async def db_edit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        table: str = commands.Param(
            description="Таблица базы данных",
            autocomplete=autocomplete_tables
        ),
        row_id: str = commands.Param(
            description="Идентификатор строки (Primary Key)",
            autocomplete=autocomplete_rows
        ),
        column: str = commands.Param(
            description="Колонка для изменения",
            autocomplete=autocomplete_columns
        ),
        new_value: str = commands.Param(
            description="Новое значение (напиши NULL для установки None)"
        )
    ):
        """Универсальное изменение значения в указанной таблице."""
        tables = await self._get_tables()
        if table not in tables:
            embed = disnake.Embed(
                title="❌ Ошибка",
                description=f"Таблица `{table}` не найдена в базе данных.",
                color=disnake.Color.red()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        columns, pk_col = await self._get_table_info(table)
        if column not in columns:
            embed = disnake.Embed(
                title="❌ Ошибка",
                description=f"Колонка `{column}` не найдена в таблице `{table}`.",
                color=disnake.Color.red()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        # Преобразование типов данных (int/float/NULL/str)
        val_to_set: Any = None
        if new_value.strip().upper() == "NULL":
            val_to_set = None
        elif new_value.isdigit() or (new_value.startswith("-") and new_value[1:].isdigit()):
            val_to_set = int(new_value)
        else:
            try:
                val_to_set = float(new_value)
            except ValueError:
                val_to_set = new_value

        async with aiosqlite.connect("dbs/main.db") as db:
            # Получаем предыдущее значение
            async with db.execute(f"SELECT {column} FROM {table} WHERE {pk_col} = ?;", (row_id,)) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    embed = disnake.Embed(
                        title="❌ Строка не найдена",
                        description=f"Запись с `{pk_col}` = `{row_id}` в таблице `{table}` отсутствует.",
                        color=disnake.Color.red()
                    )
                    return await inter.response.send_message(embed=embed, ephemeral=True)
                old_val = row[0]

            # Применяем обновление
            await db.execute(
                f"UPDATE {table} SET {column} = ? WHERE {pk_col} = ?;",
                (val_to_set, row_id)
            )
            await db.commit()

        embed = disnake.Embed(
            title="⚙️ База данных обновлена",
            description=(
                f"**Таблица:** `{table}`\n"
                f"**Строка ({pk_col}):** `{row_id}`\n"
                f"**Колонка:** `{column}`\n\n"
                f"**Было:** `{old_val}`\n"
                f"**Стало:** `{val_to_set}`"
            ),
            color=disnake.Color.green()
        )
        await inter.response.send_message(embed=embed, ephemeral=True)


def setup(bot: commands.Bot):
    bot.add_cog(DeveloperCog(bot))