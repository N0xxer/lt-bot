import logging
import re
import urllib.parse
from typing import Any, Optional, Tuple, Union
import disnake
from disnake.ext import commands
from disnake import ui

from functions.utils import is_staff_member

logger = logging.getLogger(__name__)

# Идентификаторы каналов и ролей для системы заявок
APPLICATIONS_CHANNEL_ID = 1492111042631827587
ROLE_PENDING_ID = 1491903740012269618
ROLE_ACCEPTED_ID = 1491904531330633889


class TicketsCog(commands.Cog):
    """Модуль системы анкет и заявок на вступление в клан."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ МОДУЛЯ ---

    @staticmethod
    def _parse_name_and_age(raw_text: str) -> Tuple[str, str]:
        """
        Разделяет введенную пользователем строку на имя и возраст.
        Корректно обрабатывает форматы вида:
        'Михаил 30', 'Иван Иванов 24 года', 'Денис (19)', 'Alex', и пустые строки.
        """
        text = raw_text.strip()
        if not text:
            return "Не указано", "Не указан"

        # Ищем число (возраст) в конце строки: " 25", " 25 лет", " (25)", " 25г"
        match = re.search(
            r"[\s,(]+(\d{1,2})\s*(?:лет|года|год|y\.?o\.?|y|г)?\)?$",
            text,
            re.IGNORECASE,
        )
        if match:
            age = match.group(1)
            name = text[: match.start()].strip(" ,()")
            if not name:
                name = "Не указано"
            return name, age

        parts = text.split()
        if len(parts) == 1:
            return parts[0], "Не указан"
        elif len(parts) >= 2:
            return " ".join(parts[:-1]), parts[-1]

        return "Не указано", "Не указан"

    @staticmethod
    def _validate_steam_url(url: str) -> Tuple[bool, str]:
        """
        Проверяет валидность ссылки на профиль Steam и нормализует её.
        Принимает ссылки вида:
        - https://steamcommunity.com/id/...
        - https://steamcommunity.com/profiles/...
        - steamcommunity.com/id/...
        """
        cleaned = url.strip()
        if not cleaned.startswith(("http://", "https://")):
            cleaned = "https://" + cleaned
        elif cleaned.startswith("http://"):
            cleaned = "https://" + cleaned[7:]

        parsed = urllib.parse.urlparse(cleaned)
        domain = parsed.netloc.lower()

        if domain in ("steamcommunity.com", "www.steamcommunity.com") and len(parsed.path.strip("/")) > 0:
            normalized = f"https://steamcommunity.com{parsed.path}"
            if parsed.query:
                normalized += f"?{parsed.query}"
            return True, normalized

        return False, url.strip()

    @staticmethod
    def _create_application_embed(
        user: Union[disnake.User, disnake.Member],
        name: str,
        age: str,
        bio: str,
        clock: str,
        time_per_day: str,
        steam_link: str,
    ) -> disnake.Embed:
        """Создает стильный Embed для новой заявки на вступление."""
        embed = disnake.Embed(
            title="📋 Заявка на вступление в клан",
            description=(
                f"👤 **Кандидат:** {user.mention} (`{user.display_name}`)\n"
                f"🆔 **ID:** `{user.id}`\n"
                f"🌐 **Steam:** [Открыть профиль]({steam_link})\n"
                f"📝 **Имя / Ник:** {name}\n"
                f"🎂 **Возраст:** {age}\n"
                f"⏰ **Часовой пояс:** {clock}\n"
                f"⏳ **Онлайн в день:** {time_per_day}\n\n"
                f"🛠️ **Игровые умения и навыки:**\n```\n{bio}\n```\n"
                f"📌 **Статус:** 🟢 Активно (ожидает рассмотрения)"
            ),
            color=0x4F6653,
            timestamp=disnake.utils.utcnow(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text=f"ID кандидата: {user.id}")
        return embed

    @staticmethod
    def _update_application_embed(
        embed: disnake.Embed,
        status_text: str,
        color: Union[disnake.Color, int],
        moderator: Optional[Union[disnake.Member, disnake.User]] = None,
    ) -> disnake.Embed:
        """Обновляет статус, цвет, временную метку и модератора в существующем Embed заявки."""
        new_embed = embed.copy()
        new_embed.color = color
        new_embed.timestamp = disnake.utils.utcnow()

        desc = new_embed.description or ""
        status_block = f"📌 **Статус:** {status_text}"
        if moderator:
            status_block += f"\n👮 **Модератор:** {moderator.mention}"

        if "📌 **Статус:**" in desc or "**Статус:**" in desc:
            new_desc = re.sub(
                r"(?:📌\s*)?\*\*Статус:\*\*.*",
                status_block,
                desc,
                flags=re.DOTALL,
            )
            new_embed.description = new_desc.strip()
        else:
            new_embed.description = f"{desc}\n\n{status_block}".strip()

        return new_embed

    @staticmethod
    def _extract_steam_button(message: disnake.Message) -> Optional[ui.Button]:
        """Извлекает кнопку ссылки на Steam профиль из компонентов сообщения."""
        for comp in ui.walk_components(message.components):
            if isinstance(comp, (disnake.Button, ui.Button)) and comp.url:
                return ui.Button(
                    label=comp.label or "Steam профиль",
                    url=comp.url,
                    emoji=comp.emoji or "🌐",
                )
        return None

    # --- КОМАНДЫ И МОДАЛЬНЫЕ ОКНА ---

    @commands.slash_command(
        name="send_form",
        description="Отправить сообщение с кнопкой для подачи анкеты",
        default_member_permissions=disnake.Permissions(administrator=True),
    )
    @commands.has_permissions(administrator=True)
    async def form_sender(
        self,
        inter: disnake.ApplicationCommandInteraction,
        target_channel: Optional[disnake.TextChannel] = commands.Param(
            default=None,
            description="Канал для отправки (по умолчанию — текущий)",
        ),
    ):
        """Отправляет стильное сообщение-контейнер с кнопкой для открытия модального окна анкеты."""
        channel = target_channel or inter.channel
        if not channel or not isinstance(channel, disnake.abc.Messageable):
            return await inter.response.send_message(
                "❌ Не удалось определить канал для отправки формы.",
                ephemeral=True,
            )

        container = ui.Container(
            ui.Section(
                "👋 **Хотите вступить в наш клан?**\n"
                "Нажмите на кнопку справа, чтобы заполнить анкету кандидата. "
                "Заявка поступит руководству клана на рассмотрение.",
                accessory=ui.Button(
                    label="Подать анкету",
                    custom_id="modal_form",
                    style=disnake.ButtonStyle.green,
                    emoji="📝",
                ),
            ),
            accent_colour=disnake.Colour(0x4F6653),
        )

        try:
            await channel.send(components=[container])
            channel_label = getattr(channel, "mention", f"#{getattr(channel, 'name', 'канал')}")
            await inter.response.send_message(
                f"✅ Сообщение с кнопкой подачи анкеты успешно отправлено в {channel_label}!",
                ephemeral=True,
            )
        except disnake.Forbidden:
            await inter.response.send_message(
                "❌ У бота нет прав для отправки сообщений в выбранный канал.",
                ephemeral=True,
            )

    async def modal_form(self, inter: disnake.MessageInteraction):
        """Открывает модальное окно с анкетой на вступление."""
        # Проверяем, не состоит ли пользователь уже в клане
        if isinstance(inter.author, disnake.Member):
            if any(r.id == ROLE_ACCEPTED_ID for r in inter.author.roles):
                return await inter.response.send_message(
                    "ℹ️ Вы уже являетесь участником клана!",
                    ephemeral=True,
                )
            if any(r.id == ROLE_PENDING_ID for r in inter.author.roles):
                return await inter.response.send_message(
                    "⏳ Ваша заявка уже находится на рассмотрении администрации. Ожидайте решения!",
                    ephemeral=True,
                )

        components = [
            ui.TextInput(
                label="Ссылка на Steam",
                custom_id="steam_link_input",
                placeholder="https://steamcommunity.com/id/123…",
                style=disnake.TextInputStyle.short,
                max_length=150,
                required=True,
            ),
            ui.TextInput(
                label="Имя и возраст",
                custom_id="name_age_input",
                placeholder="Михаил 30",
                style=disnake.TextInputStyle.short,
                max_length=50,
                required=True,
            ),
            ui.TextInput(
                label="Часовой пояс",
                custom_id="clock_input",
                placeholder="МСК+1 / UTC+4",
                style=disnake.TextInputStyle.short,
                max_length=30,
                required=True,
            ),
            ui.TextInput(
                label="Игровые навыки / О себе",
                custom_id="bio_input",
                placeholder="Провожу турели, сортировка, опыт стрельбы/фарма и т.д.",
                style=disnake.TextInputStyle.paragraph,
                max_length=1000,
                required=True,
            ),
            ui.TextInput(
                label="Онлайн игровой в сутки",
                custom_id="time_per_day_input",
                placeholder="От 5-ти часов",
                style=disnake.TextInputStyle.short,
                max_length=50,
                required=True,
            ),
        ]

        modal = ui.Modal(
            title="Анкета на вступление",
            custom_id=f"form_user_{inter.author.id}",
            components=components,
        )

        await inter.response.send_modal(modal)

    # --- СЛУШАТЕЛИ СОБЫТИЙ ---

    @commands.Cog.listener()
    async def on_modal_submit(self, inter: disnake.ModalInteraction):
        """Обработчик отправки модальных окон."""
        # 1. Анкета на вступление в клан
        if inter.custom_id.startswith("form_user"):
            raw_name_age = inter.text_values.get("name_age_input", "").strip()
            name, age = self._parse_name_and_age(raw_name_age)

            raw_steam_link = inter.text_values.get("steam_link_input", "").strip()
            clock = inter.text_values.get("clock_input", "Н/Д").strip()
            bio = inter.text_values.get("bio_input", "Н/Д").strip()
            time_per_day = inter.text_values.get("time_per_day_input", "Н/Д").strip()

            is_valid_steam, steam_link = self._validate_steam_url(raw_steam_link)
            if not is_valid_steam:
                return await inter.response.send_message(
                    "❌ **Заявка заполнена неверно!**\n"
                    "Укажите корректную ссылку на профиль Steam "
                    "(например: `https://steamcommunity.com/id/...` или `https://steamcommunity.com/profiles/...`)",
                    ephemeral=True,
                    delete_after=15,
                )

            channel = self.bot.get_channel(APPLICATIONS_CHANNEL_ID)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(APPLICATIONS_CHANNEL_ID)
                except (disnake.NotFound, disnake.Forbidden, disnake.HTTPException):
                    channel = None

            if not channel or not isinstance(channel, disnake.abc.Messageable):
                return await inter.response.send_message(
                    "❌ **Ошибка:** Канал для заявок не найден или бот не имеет к нему доступа. "
                    "Пожалуйста, обратитесь к администрации.",
                    ephemeral=True,
                )

            embed = self._create_application_embed(
                user=inter.author,
                name=name,
                age=age,
                bio=bio,
                clock=clock,
                time_per_day=time_per_day,
                steam_link=steam_link,
            )

            buttons = [
                ui.Button(
                    label="Рассмотреть",
                    custom_id=f"check_{inter.author.id}",
                    style=disnake.ButtonStyle.blurple,
                    emoji="🔍",
                ),
                ui.Button(
                    label="Принять",
                    custom_id=f"accept_{inter.author.id}",
                    style=disnake.ButtonStyle.green,
                    emoji="✅",
                ),
                ui.Button(
                    label="Отклонить",
                    custom_id=f"reject_{inter.author.id}",
                    style=disnake.ButtonStyle.red,
                    emoji="❌",
                ),
                ui.Button(
                    label="Steam профиль",
                    url=steam_link,
                    emoji="🌐",
                ),
            ]

            try:
                await channel.send(embed=embed, components=buttons)
                await inter.response.send_message(
                    "✅ **Спасибо за вашу заявку!**\n"
                    "Она передана на рассмотрение администрации клана. "
                    "Мы свяжемся с вами в ближайшее время.",
                    ephemeral=True,
                    delete_after=30,
                )
            except Exception as e:
                logger.error("Ошибка при отправке заявки в канал: %s", e)
                await inter.response.send_message(
                    "❌ Произошла ошибка при отправке заявки. Обратитесь к руководству клана.",
                    ephemeral=True,
                    delete_after=10,
                )
            return

        # 2. Отправка простого текстового контейнера
        if inter.custom_id == "modal_msg_sender":
            user_text = inter.text_values.get("text_to_send", "").strip()
            if not user_text:
                return await inter.response.send_message(
                    "❌ Текст сообщения не может быть пустым.",
                    ephemeral=True,
                    delete_after=10,
                )

            if not inter.channel or not isinstance(inter.channel, disnake.abc.Messageable):
                return await inter.response.send_message(
                    "❌ Не удалось отправить сообщение в текущий канал.",
                    ephemeral=True,
                )

            await inter.response.send_message("✅ Принято!", ephemeral=True, delete_after=10)
            await inter.channel.send(
                components=[
                    ui.Container(
                        ui.TextDisplay(user_text)
                    )
                ]
            )
            return

        # 3. Отправка рекламного сообщения с контейнерами и медиа
        if inter.custom_id == "modal_ad_sender":
            text_1 = inter.text_values.get("text_1", "").strip()
            text_2 = inter.text_values.get("text_2", "").strip()
            text_3 = inter.text_values.get("text_3", "").strip()
            image_link = inter.text_values.get("image_link", "").strip()
            button_link = inter.text_values.get("button_link", "").strip()

            if not inter.channel or not isinstance(inter.channel, disnake.abc.Messageable):
                return await inter.response.send_message(
                    "❌ Не удалось отправить объявление в текущий канал.",
                    ephemeral=True,
                )

            ctg_1: list[Any] = []
            if image_link and image_link.startswith(("http://", "https://")):
                ctg_1.append(ui.MediaGallery(disnake.MediaGalleryItem(media=image_link)))
                if text_1:
                    ctg_1.append(ui.Separator())
                    ctg_1.append(ui.TextDisplay(text_1))
            elif text_1:
                ctg_1.append(ui.TextDisplay(text_1))

            if button_link and button_link.startswith(("http://", "https://")):
                ctg_3: Any = ui.Section(
                    text_3 if text_3 else "Перейти",
                    accessory=ui.Button(label="Перейти", url=button_link),
                )
            else:
                ctg_3 = ui.TextDisplay(text_3 if text_3 else "Подробнее")

            containers: list[ui.Container] = []
            if ctg_1:
                containers.append(ui.Container(*ctg_1))
            if text_2:
                containers.append(ui.Container(ui.TextDisplay(text_2)))
            containers.append(ui.Container(ctg_3))

            await inter.response.send_message("✅ Отправлено!", ephemeral=True, delete_after=10)
            await inter.channel.send(components=containers)
            return

    @commands.Cog.listener()
    async def on_button_click(self, inter: disnake.MessageInteraction):
        """Обработчик нажатия на кнопки."""
        custom_id = inter.data.custom_id if inter.data else (inter.component.custom_id if inter.component else "")
        if not custom_id:
            return

        # Нажатие кнопки подачи анкеты
        if custom_id == "modal_form":
            await self.modal_form(inter)
            return

        # Обработка действий с заявками: check_, accept_, reject_
        if any(custom_id.startswith(prefix) for prefix in ("check_", "accept_", "reject_")):
            await self._handle_application_action(inter, custom_id)

    async def _handle_application_action(self, inter: disnake.MessageInteraction, custom_id: str):
        """Обработка кнопок рассмотрения, принятия и отклонения заявок."""
        guild = inter.guild
        if not guild or not isinstance(inter.author, disnake.Member):
            return await inter.response.send_message(
                "❌ Действие доступно только на сервере.",
                ephemeral=True,
            )

        # Проверка прав модератора/администратора
        if not is_staff_member(inter.author):
            return await inter.response.send_message(
                "❌ У вас нет прав для управления заявками.",
                ephemeral=True,
            )

        parts = custom_id.split("_", 1)
        if len(parts) < 2 or not parts[1].isdigit():
            return await inter.response.send_message(
                "❌ Некорректный идентификатор пользователя в кнопке.",
                ephemeral=True,
            )

        action = parts[0]
        user_id = int(parts[1])

        if not inter.message or not inter.message.embeds:
            return await inter.response.send_message(
                "❌ В сообщении отсутствует эмбед заявки.",
                ephemeral=True,
            )

        current_embed = inter.message.embeds[0]

        # Пытаемся получить участника из кэша или через API
        member = guild.get_member(user_id)
        if not member:
            try:
                member = await guild.fetch_member(user_id)
            except (disnake.NotFound, disnake.HTTPException):
                member = None

        steam_btn = self._extract_steam_button(inter.message)

        # 1. Взять на рассмотрение
        if action == "check":
            if not member:
                return await inter.response.send_message(
                    "❌ Пользователь не найден на сервере (возможно, покинул сервер).",
                    ephemeral=True,
                )

            role_pending = guild.get_role(ROLE_PENDING_ID)
            if role_pending and role_pending not in member.roles:
                try:
                    await member.add_roles(
                        role_pending,
                        reason=f"Заявка взята на рассмотрение: {inter.author}",
                    )
                except disnake.Forbidden:
                    logger.warning("Нет прав для выдачи роли рассмотрения %s", ROLE_PENDING_ID)

            new_embed = self._update_application_embed(
                embed=current_embed,
                status_text="🟡 На рассмотрении",
                color=disnake.Color.blurple(),
                moderator=inter.author,
            )

            new_buttons = [
                ui.Button(
                    label=f"Взял: {inter.author.display_name[:15]}",
                    custom_id=f"checked_{user_id}",
                    style=disnake.ButtonStyle.secondary,
                    disabled=True,
                    emoji="🔍",
                ),
                ui.Button(
                    label="Принять",
                    custom_id=f"accept_{user_id}",
                    style=disnake.ButtonStyle.green,
                    emoji="✅",
                ),
                ui.Button(
                    label="Отклонить",
                    custom_id=f"reject_{user_id}",
                    style=disnake.ButtonStyle.red,
                    emoji="❌",
                ),
            ]
            if steam_btn:
                new_buttons.append(steam_btn)

            await inter.response.edit_message(embed=new_embed, components=new_buttons)
            return

        # 2. Принять в клан
        if action == "accept":
            if not member:
                return await inter.response.send_message(
                    "❌ Пользователь не найден на сервере. Невозможно выдать роль участника клана.",
                    ephemeral=True,
                )

            role_pending = guild.get_role(ROLE_PENDING_ID)
            role_accepted = guild.get_role(ROLE_ACCEPTED_ID)

            # Снимаем роль рассмотрения
            if role_pending and role_pending in member.roles:
                try:
                    await member.remove_roles(
                        role_pending,
                        reason=f"Заявка принята: {inter.author}",
                    )
                except disnake.Forbidden:
                    logger.warning("Нет прав для снятия роли рассмотрения")

            # Выдаем роль участника клана
            if role_accepted:
                try:
                    await member.add_roles(
                        role_accepted,
                        reason=f"Заявка принята: {inter.author}",
                    )
                except disnake.Forbidden:
                    logger.warning("Нет прав для выдачи роли участника клана %s", ROLE_ACCEPTED_ID)

            # Оповещаем пользователя в ЛС
            try:
                await member.send(
                    f"🎉 **Поздравляем!** Ваша заявка на вступление в клан на сервере **{guild.name}** "
                    f"была одобрена модератором {inter.author.mention}!"
                )
            except (disnake.Forbidden, disnake.HTTPException):
                pass

            new_embed = self._update_application_embed(
                embed=current_embed,
                status_text="✅ Принят в клан",
                color=disnake.Color.green(),
                moderator=inter.author,
            )

            # Оставляем только кнопку профиля Steam (если была)
            final_components = [steam_btn] if steam_btn else []
            await inter.response.edit_message(embed=new_embed, components=final_components)
            return

        # 3. Отклонить заявку
        if action == "reject":
            if member:
                role_pending = guild.get_role(ROLE_PENDING_ID)
                if role_pending and role_pending in member.roles:
                    try:
                        await member.remove_roles(
                            role_pending,
                            reason=f"Заявка отклонена: {inter.author}",
                        )
                    except disnake.Forbidden:
                        logger.warning("Нет прав для снятия роли рассмотрения")

                try:
                    await member.send(
                        f"❌ К сожалению, ваша заявка на вступление в клан на сервере **{guild.name}** "
                        f"была отклонена."
                    )
                except (disnake.Forbidden, disnake.HTTPException):
                    pass

            status_text = "❌ Отклонено" if member else "❌ Отклонено (пользователь покинул сервер)"
            new_embed = self._update_application_embed(
                embed=current_embed,
                status_text=status_text,
                color=disnake.Color.red() if member else disnake.Color.dark_gray(),
                moderator=inter.author,
            )

            final_components = [steam_btn] if steam_btn else []
            await inter.response.edit_message(embed=new_embed, components=final_components)
            return


def setup(bot: commands.Bot):
    bot.add_cog(TicketsCog(bot))