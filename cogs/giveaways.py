import asyncio
import json
import logging
import random
import re
from datetime import datetime, timezone
from typing import Any, Optional, Tuple, Union
import disnake
from disnake.ext import commands, tasks
from disnake import ui

from functions.db_helpers import (
    create_new_giveaway,
    finish_current_giveaway,
    get_current_giveaway,
    get_eligible_giveaway_users,
    get_giveaway_counts,
    get_user_giveaway_progress,
    mark_user_entered,
    process_voice_minute_tick,
)
from functions.utils import is_staff_member

logger = logging.getLogger(__name__)

# Эмодзи для порядковых номеров призов
NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def get_number_emoji(index: int) -> str:
    """Возвращает эмодзи порядкового номера (1️⃣, 2️⃣, ..., 10️⃣, 11️⃣...)."""
    if 1 <= index <= 10:
        return NUMBER_EMOJIS[index - 1]
    return f"{index}️⃣"


def format_prizes_list(raw_prizes: str) -> list[str]:
    """Разбивает строку призов по точке с запятой и очищает пробелы."""
    return [p.strip() for p in raw_prizes.split(";") if p.strip()]


def format_prizes_display(prizes: list[str]) -> str:
    """Форматирует список призов с эмодзи порядковых номеров для отображения в сообщении."""
    lines: list[str] = []
    for idx, prize in enumerate(prizes, start=1):
        emoji = get_number_emoji(idx)
        lines.append(f"{emoji} {prize}")
    return "\n".join(lines)


def parse_duration_to_minutes(raw_str: str) -> int:
    """
    Преобразует строку длительности с модификатором времени в целые минуты.
    Поддерживает форматы: 30m, 5h, 1d, 30м, 5ч, 1д, 2h 30m и т.д.
    """
    text = raw_str.strip().lower()
    pattern = r"(\d+)\s*([a-zA-Zа-яА-ЯёЁ]*)"
    matches = re.findall(pattern, text)
    if not matches:
        return 0

    total_seconds = 0
    for val_str, mod in matches:
        val = int(val_str)
        mod = mod.lower()
        if mod in ("s", "с", "sec", "сек", "секунд", "секунды", "секунда"):
            total_seconds += val
        elif mod in ("m", "м", "min", "мин", "минут", "минуты", "минута") or not mod:
            total_seconds += val * 60
        elif mod in ("h", "ч", "hr", "час", "часа", "часов"):
            total_seconds += val * 3600
        elif mod in ("d", "д", "day", "дней", "дня", "день"):
            total_seconds += val * 86400
        elif mod in ("w", "н", "нед", "неделя", "недель", "недели"):
            total_seconds += val * 604800
        else:
            total_seconds += val * 60

    return max(1, total_seconds // 60)


def format_minutes(minutes: int) -> str:
    """Преобразует минуты в понятный текст (например: '2 ч. 30 мин.')."""
    if minutes <= 0:
        return "0 мин."

    days, rem_minutes = divmod(minutes, 1440)
    hours, mins = divmod(rem_minutes, 60)

    parts: list[str] = []
    if days > 0:
        parts.append(f"{days} дн.")
    if hours > 0:
        parts.append(f"{hours} ч.")
    if mins > 0 or not parts:
        parts.append(f"{mins} мин.")

    return " ".join(parts)


class GiveawaysCog(commands.Cog):
    """Модуль управления розыгрышами за активность в голосовых каналах."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Временное хранилище параметров запуска: user_id -> (banner_url, target_channel_id)
        self.pending_setups: dict[int, Tuple[Optional[str], int]] = {}
        # Таймстамп последнего обновления эмбеда розыгрыша
        self.last_embed_update: float = 0.0
        # Фоновый таймер проверки голосовой активности и окончания розыгрышей
        self.voice_activity_tracker.start()

    def cog_unload(self):
        """Останавливает фоновую задачу при выгрузке кога."""
        self.voice_activity_tracker.cancel()

    # --- ФОНОВЫЙ ТРЕКЕР ГОЛОСОВОЙ АКТИВНОСТИ ---

    @tasks.loop(seconds=15)
    async def voice_activity_tracker(self):
        """
        Периодически проверяет голосовые каналы:
        - Если прошло >= 60с с момента последнего начисления минуты пользователю, начисляет минуту (+60с к таймстампу).
        - Если прошло < 60с, ничего не дает (секунды сохраняются в разнице).
        - Проверяет завершение текущего розыгрыша по таймеру.
        """
        now = int(disnake.utils.utcnow().timestamp())

        try:
            gw = await get_current_giveaway()
        except Exception as e:
            logger.error("Ошибка при получении текущего розыгрыша из БД: %s", e)
            return

        if not gw or not gw["is_active"]:
            return

        # 1. Проверяем истечение времени розыгрыша
        if now >= gw["end_time"]:
            try:
                await self._finish_giveaway()
                return
            except Exception as e:
                logger.error("Ошибка при авто-завершении розыгрыша: %s", e)
                return

        # 2. Начисляем голосовую активность участникам
        guild = self.bot.get_guild(gw["guild_id"])
        if not guild:
            return

        active_uids: set[int] = set()
        for vc in guild.voice_channels:
            if guild.afk_channel and vc.id == guild.afk_channel.id:
                continue
            for m in vc.members:
                if not m.bot:
                    active_uids.add(m.id)

        try:
            await process_voice_minute_tick(active_uids, now)
        except Exception as e:
            logger.error("Ошибка при начислении минут войс-активности: %s", e)

        # 3. Периодически обновляем эмбед розыгрыша раз в 30 секунд
        if (now - self.last_embed_update) >= 30:
            self.last_embed_update = float(now)
            channel = guild.get_channel(gw["channel_id"])
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(gw["channel_id"])
                except Exception:
                    channel = None
            if channel and isinstance(channel, disnake.abc.Messageable):
                try:
                    total_count, eligible_count = await get_giveaway_counts(gw["activity_minutes"])
                    prizes = format_prizes_list(gw["prizes"])
                    updated_embed = self._build_giveaway_embed(
                        title=gw["title"],
                        description=gw["description"],
                        prizes=prizes,
                        end_time=gw["end_time"],
                        activity_minutes=gw["activity_minutes"],
                        total_count=total_count,
                        eligible_count=eligible_count,
                        banner_url=gw["banner_url"],
                    )
                    msg = await channel.fetch_message(gw["message_id"])
                    await msg.edit(embed=updated_embed)
                except Exception:
                    pass

    @voice_activity_tracker.before_loop
    async def before_voice_activity_tracker(self):
        await self.bot.wait_until_ready()

    # --- ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ЭМБЕДА И КНОПОК ---

    def _build_giveaway_embed(
        self,
        title: str,
        description: str,
        prizes: list[str],
        end_time: int,
        activity_minutes: int,
        total_count: int = 0,
        eligible_count: int = 0,
        banner_url: Optional[str] = None,
    ) -> disnake.Embed:
        """Формирует эмбед активного розыгрыша в красном/малиновом цвете без футера и времени."""
        prizes_text = format_prizes_display(prizes)

        embed_desc = (
            f"{description}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎁 **Список призов:**\n"
            f"{prizes_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱️ **Завершение:** <t:{end_time}:R> (<t:{end_time}:f>)\n"
            f"🎙️ **Требуемое время в войсе:** `{format_minutes(activity_minutes)}`\n"
            f"👥 **Количество участников:** `{total_count}` участников\n"
            f"✅ **Выполнили условие:** `{eligible_count}` участников\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        embed = disnake.Embed(
            title=f"🎉 {title}",
            description=embed_desc,
            color=0xDC143C,  # Насыщенный малиново-красный
        )

        if banner_url and banner_url.startswith(("http://", "https://")):
            embed.set_image(url=banner_url)

        return embed

    @staticmethod
    def _build_giveaway_buttons() -> list[disnake.ui.Button]:
        """Возвращает кнопку участия под сообщением розыгрыша."""
        return [
            ui.Button(
                label="Испытать удачу",
                custom_id="gw_check",
                style=disnake.ButtonStyle.green,
            ),
        ]

    # --- ЗАВЕРШЕНИЕ РОЗЫГРЫША И ВЫБОР ПОБЕДИТЕЛЕЙ ---

    async def _finish_giveaway(self, reroll: bool = False) -> Optional[list[dict[str, Any]]]:
        """Завершает текущий розыгрыш, выбирает победителей и обновляет сообщение."""
        gw = await get_current_giveaway()
        if not gw:
            return None

        if not reroll and not gw["is_active"]:
            return None

        guild = self.bot.get_guild(gw["guild_id"])
        if not guild:
            try:
                guild = await self.bot.fetch_guild(gw["guild_id"])
            except Exception:
                guild = None

        if not guild:
            logger.warning("Гильдия %s для розыгрыша не найдена", gw["guild_id"])
            return None

        prizes = format_prizes_list(gw["prizes"])
        activity_minutes = gw["activity_minutes"]

        # Получаем участников, набравших >= activity_minutes
        eligible_user_ids = await get_eligible_giveaway_users(activity_minutes)

        valid_members: list[disnake.Member] = []
        for uid in eligible_user_ids:
            member = guild.get_member(uid)
            if not member:
                try:
                    member = await guild.fetch_member(uid)
                except Exception:
                    member = None
            if member and not member.bot:
                valid_members.append(member)

        # Случайно перемешиваем участников
        random.shuffle(valid_members)

        winners_data: list[dict[str, Any]] = []
        for idx, prize in enumerate(prizes, start=1):
            if idx <= len(valid_members):
                winner = valid_members[idx - 1]
                winners_data.append({
                    "prize_index": idx,
                    "prize": prize,
                    "user_id": winner.id,
                    "user_name": str(winner),
                    "user_mention": winner.mention,
                })
            else:
                winners_data.append({
                    "prize_index": idx,
                    "prize": prize,
                    "user_id": None,
                    "user_name": None,
                    "user_mention": "Недостаточно участников",
                })

        total_count, _ = await get_giveaway_counts(activity_minutes)
        eligible_valid_count = len(valid_members)

        # Записываем результат завершения в БД
        winners_json = json.dumps(winners_data, ensure_ascii=False)
        await finish_current_giveaway(winners_json)

        # Ищем канал и обновляем сообщение розыгрыша
        channel = guild.get_channel(gw["channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(gw["channel_id"])
            except Exception:
                channel = None

        if channel and isinstance(channel, disnake.abc.Messageable):
            winners_lines: list[str] = []
            for item in winners_data:
                emoji = get_number_emoji(item["prize_index"])
                winners_lines.append(f"{emoji} {item['user_mention']} — **{item['prize']}**")
            winners_text = "\n".join(winners_lines)

            finished_desc = (
                f"{gw['description']}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏆 **Победители и призы:**\n"
                f"{winners_text}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏱️ **Статус:** Завершен\n"
                f"🎙️ **Требовалось в войсе:** `{format_minutes(activity_minutes)}`\n"
                f"👥 **Количество участников:** `{total_count}` участников\n"
                f"✅ **Выполнили условие:** `{eligible_valid_count}` участников\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

            finished_embed = disnake.Embed(
                title=f"🎉 [ЗАВЕРШЕН] {gw['title']}",
                description=finished_desc,
                color=0x2F3136,
            )
            if gw["banner_url"]:
                finished_embed.set_image(url=gw["banner_url"])

            disabled_components = [
                ui.Button(
                    label="Розыгрыш завершен",
                    style=disnake.ButtonStyle.secondary,
                    disabled=True,
                )
            ]

            try:
                msg = await channel.fetch_message(gw["message_id"])
                await msg.edit(embed=finished_embed, components=disabled_components)
            except Exception as e:
                logger.warning("Не удалось отредактировать сообщение розыгрыша: %s", e)

            # Отправляем поздравление в чат в виде эмбеда
            real_winner_mentions = [w["user_mention"] for w in winners_data if w["user_id"]]
            if real_winner_mentions:
                pings = ", ".join(real_winner_mentions)
                announcement_embed = disnake.Embed(
                    title=f"🎉 Итоги розыгрыша «{gw['title']}»",
                    description=(
                        f"🏆 **Победители:**\n"
                        f"{winners_text}\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"👥 **Количество участников:** `{total_count}` чел.\n"
                        f"✅ **Выполнили условие:** `{eligible_valid_count}` чел.\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"📩 Свяжитесь с администрацией для получения своих призов! 🏆"
                    ),
                    color=0xDC143C,
                )
                if gw["banner_url"]:
                    announcement_embed.set_thumbnail(url=gw["banner_url"])

                try:
                    await channel.send(content=f"🎉 {pings}", embed=announcement_embed)
                except Exception as e:
                    logger.warning("Не удалось отправить сообщение с поздравлением: %s", e)
            else:
                announcement_embed = disnake.Embed(
                    title=f"📢 Итоги розыгрыша «{gw['title']}»",
                    description=(
                        f"К сожалению, ни один участник не выполнил необходимое условие по времени в голосовых каналах "
                        f"(`{format_minutes(activity_minutes)}`).\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"👥 **Количество участников:** `{total_count}` чел.\n"
                        f"✅ **Выполнили условие:** `0` чел.\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    ),
                    color=0xDC143C,
                )
                try:
                    await channel.send(embed=announcement_embed)
                except Exception as e:
                    logger.warning("Не удалось отправить сообщение об итогах: %s", e)

        # Отправляем уведомления победителям в ЛС в виде эмбеда
        for item in winners_data:
            if item["user_id"]:
                member = guild.get_member(item["user_id"])
                if member:
                    try:
                        dm_embed = disnake.Embed(
                            title="🏆 Поздравляем с победой в розыгрыше!",
                            description=(
                                f"Вы выиграли приз **{item['prize']}** в розыгрыше **«{gw['title']}»** на сервере **{guild.name}**!\n\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"👥 **Количество участников:** `{total_count}` чел.\n"
                                f"✅ **Выполнили условие:** `{eligible_valid_count}` чел.\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                                f"📩 Свяжитесь с организатором или администрацией сервера для получения своего приза."
                            ),
                            color=0xDC143C,
                        )
                        if guild.icon:
                            dm_embed.set_thumbnail(url=guild.icon.url)
                        elif gw["banner_url"]:
                            dm_embed.set_thumbnail(url=gw["banner_url"])

                        await member.send(embed=dm_embed)
                    except Exception as e:
                        logger.warning("Не удалось отправить уведомление в ЛС пользователю %s: %s", item["user_id"], e)

        return winners_data

    # --- ЕДИНАЯ СЛЭШ-КОМАНДА /giveaway С СУБКОМАНДАМИ ---

    @commands.slash_command(
        name="giveaway",
        description="Управление розыгрышами за голосовую активность",
        default_member_permissions=disnake.Permissions(administrator=True),
    )
    async def giveaway(self, inter: disnake.ApplicationCommandInteraction):
        """Базовая группа команд управления розыгрышами."""
        pass

    @giveaway.sub_command(
        name="start",
        description="Запустить новый розыгрыш за активность в голосовых каналах",
    )
    @commands.has_permissions(administrator=True)
    async def giveaway_start(
        self,
        inter: disnake.ApplicationCommandInteraction,
        banner_url: Optional[str] = commands.Param(
            default=None,
            description="Прямая ссылка на PNG/JPG баннер (опционально)",
        ),
        target_channel: Optional[disnake.TextChannel] = commands.Param(
            default=None,
            description="Канал для публикации розыгрыша (по умолчанию — текущий)",
        ),
    ):
        """Открывает модальное окно для создания розыгрыша на активность."""
        if not inter.guild or not isinstance(inter.author, disnake.Member):
            return await inter.response.send_message("Команда доступна только на сервере.", ephemeral=True)

        if not is_staff_member(inter.author):
            return await inter.response.send_message("У вас нет прав для создания розыгрышей.", ephemeral=True)

        channel = target_channel or inter.channel
        if not channel or not isinstance(channel, disnake.abc.Messageable):
            return await inter.response.send_message("Не удалось определить целевой канал.", ephemeral=True)

        self.pending_setups[inter.author.id] = (banner_url, channel.id)

        modal = ui.Modal(
            title="Создание розыгрыша на активность",
            custom_id="modal_giveaway_create",
            components=[
                ui.TextInput(
                    label="Заголовок",
                    custom_id="title_input",
                    placeholder="Розыгрыш скинов за голосовую активность!",
                    style=disnake.TextInputStyle.short,
                    max_length=150,
                    required=True,
                ),
                ui.TextInput(
                    label="Описание",
                    custom_id="description_input",
                    placeholder="Проводите время в войс-каналах клана и получайте призы!\n[Правила](https://...)",
                    style=disnake.TextInputStyle.paragraph,
                    max_length=2000,
                    required=True,
                ),
                ui.TextInput(
                    label="Список призов (через точку с запятой ;)",
                    custom_id="prizes_input",
                    placeholder="Хазмат | «Spacesuit»; Гаражная дверь | «Neon Pumpkin»",
                    style=disnake.TextInputStyle.paragraph,
                    max_length=1000,
                    required=True,
                ),
                ui.TextInput(
                    label="Таймер (например: 30m, 5h, 1d)",
                    custom_id="timer_input",
                    placeholder="30m или 5h или 1d",
                    style=disnake.TextInputStyle.short,
                    max_length=30,
                    required=True,
                ),
                ui.TextInput(
                    label="Время в войсе для шанса (30m, 5h)",
                    custom_id="activity_input",
                    placeholder="30m или 1h",
                    style=disnake.TextInputStyle.short,
                    max_length=30,
                    required=True,
                ),
            ],
        )

        await inter.response.send_modal(modal)

    @giveaway.sub_command(
        name="end",
        description="Завершить текущий розыгрыш досрочно и определить победителей",
    )
    @commands.has_permissions(administrator=True)
    async def giveaway_end(self, inter: disnake.ApplicationCommandInteraction):
        """Досрочно завершает текущий розыгрыш."""
        await inter.response.defer(ephemeral=True)
        if not isinstance(inter.author, disnake.Member) or not is_staff_member(inter.author):
            return await inter.followup.send("У вас нет прав для управления розыгрышами.", ephemeral=True)

        gw = await get_current_giveaway()
        if not gw or not gw["is_active"]:
            return await inter.followup.send("На сервере нет активного розыгрыша.", ephemeral=True)

        winners = await self._finish_giveaway()
        if winners is not None:
            await inter.followup.send("Розыгрыш успешно завершен досрочно!", ephemeral=True)
        else:
            await inter.followup.send("Не удалось завершить розыгрыш.", ephemeral=True)

    @giveaway.sub_command(
        name="reroll",
        description="Перевыбрать победителей для текущего/завершенного розыгрыша",
    )
    @commands.has_permissions(administrator=True)
    async def giveaway_reroll(self, inter: disnake.ApplicationCommandInteraction):
        """Перевыбирает победителей розыгрыша среди выполнивших условие участников."""
        await inter.response.defer(ephemeral=True)
        if not isinstance(inter.author, disnake.Member) or not is_staff_member(inter.author):
            return await inter.followup.send("У вас нет прав для управления розыгрышами.", ephemeral=True)

        gw = await get_current_giveaway()
        if not gw:
            return await inter.followup.send("Розыгрыш не найден в базе данных.", ephemeral=True)

        winners = await self._finish_giveaway(reroll=True)
        if winners is not None:
            await inter.followup.send("Победители успешно перевыбраны!", ephemeral=True)
        else:
            await inter.followup.send("Не удалось перевыбрать победителей.", ephemeral=True)

    @giveaway.sub_command(
        name="status",
        description="Текущий статус розыгрыша и статистика активности",
    )
    @commands.has_permissions(administrator=True)
    async def giveaway_status(self, inter: disnake.ApplicationCommandInteraction):
        """Показывает текущее состояние розыгрыша."""
        await inter.response.defer(ephemeral=True)
        if not inter.guild:
            return await inter.followup.send("Команда доступна только на сервере.", ephemeral=True)

        gw = await get_current_giveaway()
        if not gw:
            return await inter.followup.send("В базе данных нет активного или завершенного розыгрыша.", ephemeral=True)

        status_str = "Активен" if gw["is_active"] else "Завершен"
        total, eligible = await get_giveaway_counts(gw["activity_minutes"])

        embed = disnake.Embed(
            title=f"Статус розыгрыша: {gw['title']}",
            description=(
                f"**Статус:** {status_str}\n"
                f"**Канал:** <#{gw['channel_id']}>\n"
                f"**Окончание:** <t:{gw['end_time']}:R> (<t:{gw['end_time']}:f>)\n"
                f"**Требуемое время в войсе:** `{format_minutes(gw['activity_minutes'])}`\n"
                f"**Нажали кнопку участия:** `{total}` участников\n"
                f"**Выполнили порог активности:** `{eligible}` участников"
            ),
            color=0x4F6653,
        )
        await inter.followup.send(embed=embed, ephemeral=True)

    # --- СЛУШАТЕЛИ ВЗАИМОДЕЙСТВИЙ (МОДАЛКИ И КНОПКИ) ---

    @commands.Cog.listener()
    async def on_modal_submit(self, inter: disnake.ModalInteraction):
        """Обработка отправки формы создания розыгрыша."""
        if inter.custom_id != "modal_giveaway_create":
            return

        # Немедленно подтверждаем взаимодействие во избежание таймаута
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)

        guild = inter.guild
        if not guild or not isinstance(inter.author, disnake.Member):
            return await inter.followup.send("Действие доступно только на сервере.", ephemeral=True)

        title = inter.text_values.get("title_input", "").strip()
        raw_desc = inter.text_values.get("description_input", "").strip()
        raw_prizes = inter.text_values.get("prizes_input", "").strip()
        raw_timer = inter.text_values.get("timer_input", "").strip()
        raw_activity = inter.text_values.get("activity_input", "").strip()

        # Поддержка буквальных переносов строк \n
        desc = raw_desc.replace("\\n", "\n")

        # Извлекаем опциональный баннер
        banner_url, target_channel_id = self.pending_setups.pop(
            inter.author.id, (None, inter.channel_id)
        )

        if not banner_url:
            banner_match = re.search(r"banner(?:_url)?:\s*(https?://[^\s]+)", desc, re.IGNORECASE)
            if banner_match:
                banner_url = banner_match.group(1).strip()
                desc = desc[: banner_match.start()] + desc[banner_match.end() :]
                desc = desc.strip()

        if banner_url and not banner_url.startswith(("http://", "https://")):
            banner_url = None

        prizes = format_prizes_list(raw_prizes)
        if not prizes:
            return await inter.followup.send(
                "Список призов не может быть пустым. Разделяйте призы точкой с запятой (;).",
                ephemeral=True,
            )

        duration_minutes = parse_duration_to_minutes(raw_timer)
        if duration_minutes < 1:
            return await inter.followup.send(
                "Некорректный таймер розыгрыша. Укажите длительность (минимум 1m), например: `30m`, `5h`, `1d`.",
                ephemeral=True,
            )

        activity_minutes = parse_duration_to_minutes(raw_activity)
        if activity_minutes <= 0:
            return await inter.followup.send(
                "Некорректное значение активности. Укажите время в войсе (например: `15m`, `30m`, `2h`).",
                ephemeral=True,
            )

        if activity_minutes > duration_minutes:
            return await inter.followup.send(
                f"Требуемое время в войсе (`{format_minutes(activity_minutes)}`) не может превышать "
                f"общую длительность розыгрыша (`{format_minutes(duration_minutes)}`)!",
                ephemeral=True,
            )

        target_channel = guild.get_channel(target_channel_id)
        if not target_channel or not isinstance(target_channel, disnake.abc.Messageable):
            target_channel = inter.channel

        if not target_channel or not isinstance(target_channel, disnake.abc.Messageable):
            return await inter.followup.send("Не удалось получить канал для отправки розыгрыша.", ephemeral=True)

        now = int(disnake.utils.utcnow().timestamp())
        end_time = now + (duration_minutes * 60)

        giveaway_embed = self._build_giveaway_embed(
            title=title,
            description=desc,
            prizes=prizes,
            end_time=end_time,
            activity_minutes=activity_minutes,
            total_count=0,
            eligible_count=0,
            banner_url=banner_url,
        )

        buttons = self._build_giveaway_buttons()

        try:
            msg = await target_channel.send(embed=giveaway_embed, components=buttons)
        except disnake.Forbidden:
            return await inter.followup.send("У бота нет прав на отправку сообщений в выбранный канал.", ephemeral=True)
        except Exception as e:
            logger.error("Ошибка при публикации розыгрыша: %s", e)
            return await inter.followup.send("Произошла ошибка при отправке сообщения розыгрыша.", ephemeral=True)

        # При старте нового розыгрыша стираем старые данные и записываем новый розыгрыш
        gw_id = await create_new_giveaway(
            guild_id=guild.id,
            channel_id=target_channel.id,
            message_id=msg.id,
            title=title,
            description=desc,
            prizes=raw_prizes,
            banner_url=banner_url,
            start_time=now,
            end_time=end_time,
            activity_minutes=activity_minutes,
            creator_id=inter.author.id,
        )

        channel_mention = getattr(target_channel, "mention", f"#{getattr(target_channel, 'name', 'канал')}")
        await inter.followup.send(
            f"Розыгрыш **«{title}»** успешно запущен в канале {channel_mention}!\n"
            f"ID розыгрыша: **{gw_id}** | Завершение: <t:{end_time}:R>",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_button_click(self, inter: disnake.MessageInteraction):
        """Обработка нажатий на кнопки под сообщением розыгрыша."""
        custom_id: str = (
            (inter.data.custom_id if inter.data and inter.data.custom_id else "")
            or (inter.component.custom_id if inter.component and inter.component.custom_id else "")
        )
        if custom_id != "gw_check":
            return

        if not inter.guild or not isinstance(inter.author, disnake.Member):
            return await inter.response.send_message("Действие доступно только на сервере.", ephemeral=True)

        gw = await get_current_giveaway()
        if not gw:
            return await inter.response.send_message("Активный розыгрыш не найден.", ephemeral=True)

        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)

        voice_minutes, _, _, is_entered = await get_user_giveaway_progress(inter.author.id)
        required_minutes = gw["activity_minutes"]

        # Если пользователь уже нажал кнопку и участвует — выводим красный эмбед без лишнего текста
        if is_entered:
            embed = disnake.Embed(description="Вы уже участвуете", color=disnake.Color.red())
            return await inter.followup.send(embed=embed, ephemeral=True)

        if not gw["is_active"]:
            return await inter.followup.send("Этот розыгрыш уже завершен.", ephemeral=True)

        now = int(disnake.utils.utcnow().timestamp())
        is_in_voice = bool(
            inter.author.voice
            and inter.author.voice.channel
            and (not inter.guild.afk_channel or inter.author.voice.channel.id != inter.guild.afk_channel.id)
        )

        # Регистрируем участника в БД и начинаем отсчет времени с этого момента
        await mark_user_entered(inter.author.id, is_in_voice=is_in_voice, current_timestamp=now)

        embed = disnake.Embed(
            description=(
                f"Вы успешно зарегистрированы в розыгрыше! Желаем удачи!\n\n"
                f"🎙️ Для получения шанса на победу проведите в голосовых каналах "
                f"не менее **{format_minutes(required_minutes)}**."
            ),
            color=disnake.Color.green(),
        )
        await inter.followup.send(embed=embed, ephemeral=True)

        # Обновляем счетчик на эмбеде
        if inter.message and gw["is_active"]:
            try:
                total_count, eligible_count = await get_giveaway_counts(required_minutes)
                prizes = format_prizes_list(gw["prizes"])
                updated_embed = self._build_giveaway_embed(
                    title=gw["title"],
                    description=gw["description"],
                    prizes=prizes,
                    end_time=gw["end_time"],
                    activity_minutes=required_minutes,
                    total_count=total_count,
                    eligible_count=eligible_count,
                    banner_url=gw["banner_url"],
                )
                await inter.message.edit(embed=updated_embed)
            except Exception:
                pass
        return


def setup(bot: commands.Bot):
    bot.add_cog(GiveawaysCog(bot))
