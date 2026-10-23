import asyncio
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
# Если в TICKETS_CATEGORY_ID указан прочерк ("-"), тикет создается в самом верху без категории
TICKETS_CATEGORY_ID: Union[int, str, None] = "-"  # ID категории для тикетов заявок или "-" (без категории вверху)
APPLICATIONS_CHANNEL_ID: Union[int, str, None] = 1561407778168377394  # Канал первичных заявок (или "-" если не нужен)
ARCHIVE_CHANNEL_ID: Union[int, str, None] = 1561407978983260230
REJECTED_CHANNEL_ID: Union[int, str, None] = 1561407835466629170
ACCEPTED_CHANNEL_ID: Union[int, str, None] = 1561407867159056504
ROLE_PENDING_ID: int = 1561451769001812079
ROLE_ACCEPTED_ID: int = 1560076972854349904


class TicketsCog(commands.Cog):
    """Модуль системы анкет и тикетов заявок на вступление в клан."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Кэш соответствия: user_id -> ticket_channel_id
        self.ticket_channels: dict[int, int] = {}
        # Кэш сообщений в первичном канале: user_id -> (channel_id, message_id)
        self.application_messages: dict[int, Tuple[int, int]] = {}

    # --- ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ МОДУЛЯ ---

    @staticmethod
    def _parse_id(val: Any) -> Optional[int]:
        """Безопасно преобразует значение в числовой ID канала или роли (прочерк '-' возвращает None)."""
        if val is None or val == "-" or val == "" or val == 0:
            return None
        try:
            parsed = int(str(val).strip())
            return parsed if parsed > 0 else None
        except ValueError:
            return None

    async def _get_channel_safe(self, channel_id: int) -> Optional[disnake.abc.Messageable]:
        """Получает канал из кэша или через API."""
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (disnake.NotFound, disnake.Forbidden, disnake.HTTPException):
                channel = None

        if isinstance(channel, disnake.abc.Messageable):
            return channel
        return None

    async def _resolve_category(self, guild: disnake.Guild) -> Optional[disnake.CategoryChannel]:
        """Определяет категорию для создания каналов тикетов."""
        cat_id = self._parse_id(TICKETS_CATEGORY_ID)
        if not cat_id:
            return None

        category = guild.get_channel(cat_id)
        if category is None:
            try:
                category = await self.bot.fetch_channel(cat_id)
            except (disnake.NotFound, disnake.Forbidden, disnake.HTTPException):
                category = None

        if isinstance(category, disnake.CategoryChannel):
            return category
        return None

    def _find_user_ticket_channel(
        self, guild: disnake.Guild, user_id: int
    ) -> Optional[disnake.TextChannel]:
        """Ищет активный текстовый канал тикета для указанного пользователя."""
        # 1. Сначала проверяем кэш сессий в памяти
        channel_id = self.ticket_channels.get(user_id)
        if channel_id:
            ch = guild.get_channel(channel_id)
            if isinstance(ch, disnake.TextChannel):
                return ch

        # 2. Ищем по топику канала
        target_marker = f"ID: {user_id}"
        for ch in guild.text_channels:
            if ch.topic and target_marker in ch.topic:
                self.ticket_channels[user_id] = ch.id
                return ch

        return None

    @staticmethod
    async def _delayed_delete_channel(
        channel: disnake.TextChannel, delay: int = 3, reason: str = ""
    ) -> None:
        """Удаляет канал тикета с небольшой задержкой."""
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await channel.delete(reason=reason)
        except (disnake.NotFound, disnake.Forbidden, disnake.HTTPException) as e:
            logger.warning("Не удалось удалить канал тикета %s: %s", channel.id, e)

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
        """Создает Embed для новой заявки на вступление."""
        embed = disnake.Embed(
            title="Заявка на вступление",
            description=(
                f"**Пользователь:** {user.mention}\n"
                f"**Имя/Прозвище:** {name}\n"
                f"**Возраст:** {age}\n"
                f"**Игровые умения:** {bio}\n"
                f"**Часовой пояс:** {clock}\n"
                f"**Время в день:** {time_per_day}\n\n"
                f"**Статус:** Активно"
            ),
            color=0x4F6653,
            timestamp=disnake.utils.utcnow(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
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
        status_block = f"**Статус:** {status_text}"
        if moderator:
            status_block += f"\n**Модератор:** {moderator.mention}"

        if "**Статус:**" in desc:
            new_desc = re.sub(
                r"\*\*Статус:\*\*.*",
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
                )
        return None

    async def _send_log_notification(
        self,
        channel_id: int,
        title: str,
        user_mention: str,
        moderator: Union[disnake.Member, disnake.User],
        color: disnake.Color,
    ) -> None:
        """Отправляет краткое уведомление в канал логов принятых/отклоненных заявок."""
        channel = await self._get_channel_safe(channel_id)
        if not channel:
            logger.warning("Канал логирования заявок %s не найден или недоступен", channel_id)
            return

        embed = disnake.Embed(
            title=title,
            description=(
                f"**Пользователь:** {user_mention}\n"
                f"**Модератор:** {moderator.mention}"
            ),
            color=color,
            timestamp=disnake.utils.utcnow(),
        )

        try:
            await channel.send(embed=embed)
        except (disnake.Forbidden, disnake.HTTPException) as e:
            logger.error("Ошибка при отправке лога в канал %s: %s", channel_id, e)

    async def _archive_application(
        self,
        embed: disnake.Embed,
        steam_btn: Optional[ui.Button],
    ) -> None:
        """Дублирует рассмотренную анкету в архивный канал."""
        arch_id = self._parse_id(ARCHIVE_CHANNEL_ID)
        if not arch_id:
            return

        archive_channel = await self._get_channel_safe(arch_id)
        if not archive_channel:
            logger.warning("Канал архива заявок %s не найден или недоступен", arch_id)
            return

        final_components = [steam_btn] if steam_btn else []
        try:
            await archive_channel.send(embed=embed, components=final_components)
        except (disnake.Forbidden, disnake.HTTPException) as e:
            logger.error("Ошибка при отправке анкеты в архив: %s", e)

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
        """Отправляет контейнер UI v2 с кнопкой подачи анкеты."""
        channel = target_channel or inter.channel
        if not channel or not isinstance(channel, disnake.abc.Messageable):
            return await inter.response.send_message(
                "Не удалось определить канал для отправки формы.",
                ephemeral=True,
            )

        container = ui.Container(
            ui.Section(
                "Хотите вступить в наш клан?",
                accessory=ui.Button(
                    label="Подать анкету",
                    custom_id="modal_form",
                    style=disnake.ButtonStyle.green,
                ),
            )
        )

        try:
            await channel.send(components=[container])
            channel_label = getattr(channel, "mention", f"#{getattr(channel, 'name', 'канал')}")
            await inter.response.send_message(
                f"Сообщение с кнопкой отправлено в {channel_label}!",
                ephemeral=True,
            )
        except disnake.Forbidden:
            await inter.response.send_message(
                "У бота нет прав для отправки сообщений в выбранный канал.",
                ephemeral=True,
            )

    async def modal_form(self, inter: disnake.MessageInteraction):
        """Открывает модальное окно с анкетой на вступление."""
        guild = inter.guild
        if isinstance(inter.author, disnake.Member):
            # Проверяем, не состоит ли пользователь уже в клане
            if any(r.id == ROLE_ACCEPTED_ID for r in inter.author.roles):
                return await inter.response.send_message(
                    "Вы уже являетесь участником клана!",
                    ephemeral=True,
                )
            if any(r.id == ROLE_PENDING_ID for r in inter.author.roles):
                return await inter.response.send_message(
                    "Ваша заявка уже находится на рассмотрении администрации. Ожидайте решения!",
                    ephemeral=True,
                )

        # Проверяем, нет ли уже открытого тикета у пользователя
        if guild:
            existing_ticket = self._find_user_ticket_channel(guild, inter.author.id)
            if existing_ticket:
                return await inter.response.send_message(
                    f"У вас уже есть открытый тикет заявки: {existing_ticket.mention}!",
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
                label="Игровые навыки",
                custom_id="bio_input",
                placeholder="Провожу турели, сортировка, опыт PvP/фермы и т.д.",
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
        if not inter.custom_id.startswith("form_user"):
            return

        guild = inter.guild
        if not guild or not isinstance(inter.author, disnake.Member):
            return await inter.response.send_message(
                "Подача заявок доступна только на сервере.",
                ephemeral=True,
            )

        raw_name_age = inter.text_values.get("name_age_input", "").strip()
        name, age = self._parse_name_and_age(raw_name_age)

        raw_steam_link = inter.text_values.get("steam_link_input", "").strip()
        clock = inter.text_values.get("clock_input", "Н/Д").strip()
        bio = inter.text_values.get("bio_input", "Н/Д").strip()
        time_per_day = inter.text_values.get("time_per_day_input", "Н/Д").strip()

        is_valid_steam, steam_link = self._validate_steam_url(raw_steam_link)
        if not is_valid_steam:
            return await inter.response.send_message(
                "Заявка заполнена неверно!\n"
                "Укажите корректную ссылку на Steam (начинающуюся с https://steamcommunity.com/)",
                ephemeral=True,
                delete_after=10,
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
            ),
            ui.Button(
                label="Принять",
                custom_id=f"accept_{inter.author.id}",
                style=disnake.ButtonStyle.green,
                disabled=True,
            ),
            ui.Button(
                label="Отклонить",
                custom_id=f"reject_{inter.author.id}",
                style=disnake.ButtonStyle.red,
                disabled=True,
            ),
            ui.Button(
                label="Steam профиль",
                url=steam_link,
            ),
        ]

        # --- СОЗДАНИЕ ТИКЕТ-КАНАЛА ---
        category = await self._resolve_category(guild)

        overwrites: dict[Union[disnake.Role, disnake.Member], disnake.PermissionOverwrite] = {}

        # Наследуем права категории, если она задана
        if category and category.overwrites:
            for target, target_overwrites in category.overwrites.items():
                if isinstance(target, (disnake.Role, disnake.Member)):
                    overwrites[target] = target_overwrites

        # @everyone не имеет доступа к тикету
        overwrites[guild.default_role] = disnake.PermissionOverwrite(
            view_channel=False,
            read_messages=False,
        )

        # Кандидат имеет доступ и может писать в тикете
        overwrites[inter.author] = disnake.PermissionOverwrite(
            view_channel=True,
            read_messages=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        )

        # Бот имеет полные права управления каналом
        if guild.me:
            overwrites[guild.me] = disnake.PermissionOverwrite(
                view_channel=True,
                read_messages=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
                embed_links=True,
                attach_files=True,
            )

        # Даем доступ ролям администрации и модерации
        for role in guild.roles:
            if role not in overwrites and role != guild.default_role:
                if (
                    role.permissions.administrator
                    or role.permissions.manage_guild
                    or role.permissions.manage_roles
                    or role.permissions.manage_channels
                    or role.permissions.kick_members
                    or role.permissions.ban_members
                ):
                    overwrites[role] = disnake.PermissionOverwrite(
                        view_channel=True,
                        read_messages=True,
                        send_messages=True,
                        read_message_history=True,
                        manage_messages=True,
                        embed_links=True,
                        attach_files=True,
                    )

        # Формируем имя и топик тикета
        sanitized_name = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ_-]", "", name).lower().strip("-_")
        if not sanitized_name:
            sanitized_name = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ_-]", "", inter.author.name).lower().strip("-_")
        if not sanitized_name:
            sanitized_name = str(inter.author.id)
        channel_name = f"тикет-{sanitized_name}"[:32]
        topic = f"Тикет-заявка в клан | Кандидат: {inter.author} (ID: {inter.author.id})"

        ticket_channel: Optional[disnake.TextChannel] = None
        try:
            if category:
                ticket_channel = await guild.create_text_channel(
                    name=channel_name,
                    category=category,
                    overwrites=overwrites,
                    topic=topic,
                    reason=f"Тикет заявки в клан от {inter.author}",
                )
            else:
                # Если в коде указан прочерк ("-") — создается в самом верху без категории
                ticket_channel = await guild.create_text_channel(
                    name=channel_name,
                    position=0,
                    overwrites=overwrites,
                    topic=topic,
                    reason=f"Тикет заявки в клан от {inter.author}",
                )
        except Exception as e:
            logger.error("Ошибка при создании канала тикета: %s", e)
            return await inter.response.send_message(
                "Произошла ошибка при создании канала тикета. Обратитесь к администрации.",
                ephemeral=True,
                delete_after=10,
            )

        self.ticket_channels[inter.author.id] = ticket_channel.id

        # Отправляем текст заявки в созданный тикет-чат
        welcome_content = (
            f"{inter.author.mention}, ваша анкета на вступление успешно создана!\n"
            f"Ожидайте ответа администрации клана. Здесь вы можете ответить на уточняющие вопросы."
        )
        try:
            await ticket_channel.send(
                content=welcome_content,
                embed=embed,
                components=buttons,
            )
        except Exception as e:
            logger.error("Ошибка при отправке анкеты в тикет: %s", e)

        # Если задан первичный канал заявок — дублируем и туда
        app_chan_id = self._parse_id(APPLICATIONS_CHANNEL_ID)
        if app_chan_id:
            app_chan = await self._get_channel_safe(app_chan_id)
            if app_chan:
                try:
                    app_msg = await app_chan.send(
                        content=f"Новая заявка в тикете: {ticket_channel.mention}",
                        embed=embed,
                        components=buttons,
                    )
                    if isinstance(app_msg, disnake.Message):
                        self.application_messages[inter.author.id] = (app_chan_id, app_msg.id)
                except Exception as e:
                    logger.warning("Не удалось продублировать заявку в первичный канал: %s", e)

        await inter.response.send_message(
            f"Спасибо за вашу заявку! Для вас создан тикет: {ticket_channel.mention}.",
            ephemeral=True,
            delete_after=60,
        )

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
                "Действие доступно только на сервере.",
                ephemeral=True,
            )

        # Проверка прав модератора/администратора
        if not is_staff_member(inter.author):
            return await inter.response.send_message(
                "У вас нет прав для управления заявками.",
                ephemeral=True,
            )

        parts = custom_id.split("_", 1)
        if len(parts) < 2 or not parts[1].isdigit():
            return await inter.response.send_message(
                "Некорректный идентификатор пользователя в кнопке.",
                ephemeral=True,
            )

        action = parts[0]
        user_id = int(parts[1])

        if not inter.message or not inter.message.embeds:
            return await inter.response.send_message(
                "В сообщении отсутствует эмбед заявки.",
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

        # Проверка: без нажатия «Рассмотреть» нельзя принять или отклонить заявку
        if action in ("accept", "reject"):
            is_under_review = False
            for comp in ui.walk_components(inter.message.components):
                if isinstance(comp, (disnake.Button, ui.Button)):
                    if comp.custom_id and comp.custom_id.startswith("checked_"):
                        is_under_review = True
                        break

            if not is_under_review:
                return await inter.response.send_message(
                    "Сначала необходимо нажать кнопку «Рассмотреть», чтобы взять заявку в работу.",
                    ephemeral=True,
                )

        # Находим связанный канал тикета (если существует)
        ticket_channel = self._find_user_ticket_channel(guild, user_id)

        # 1. Взять на рассмотрение
        if action == "check":
            if member:
                role_pending = guild.get_role(ROLE_PENDING_ID) or disnake.Object(id=ROLE_PENDING_ID)
                if not any(r.id == ROLE_PENDING_ID for r in member.roles):
                    try:
                        await member.add_roles(
                            role_pending,
                            reason=f"Заявка взята на рассмотрение: {inter.author}",
                        )
                    except disnake.Forbidden:
                        logger.warning("Нет прав для выдачи роли рассмотрения %s", ROLE_PENDING_ID)

            status_text = "Ожидает проверки" if member else "Ожидает проверки (пользователь покинул сервер)"
            new_embed = self._update_application_embed(
                embed=current_embed,
                status_text=status_text,
                color=disnake.Color.blurple(),
                moderator=inter.author,
            )

            new_buttons = [
                ui.Button(
                    label=f"Взял: {inter.author.display_name[:15]}",
                    custom_id=f"checked_{user_id}",
                    style=disnake.ButtonStyle.secondary,
                    disabled=True,
                ),
                ui.Button(
                    label="Принять",
                    custom_id=f"accept_{user_id}",
                    style=disnake.ButtonStyle.green,
                ),
                ui.Button(
                    label="Отклонить",
                    custom_id=f"reject_{user_id}",
                    style=disnake.ButtonStyle.red,
                ),
            ]
            if steam_btn:
                new_buttons.append(steam_btn)

            await inter.response.edit_message(embed=new_embed, components=new_buttons)

            # Если действие вызвано вне тикета, отправляем оповещение в сам тикет
            if ticket_channel and inter.channel_id != ticket_channel.id:
                try:
                    await ticket_channel.send(
                        f"Модератор {inter.author.mention} взял вашу заявку на рассмотрение."
                    )
                except Exception:
                    pass
            return

        # Вспомогательная функция для очистки дублированного сообщения в первичном канале
        async def _cleanup_primary_message():
            app_info = self.application_messages.pop(user_id, None)
            if app_info:
                c_id, m_id = app_info
                ch = guild.get_channel(c_id)
                if ch and isinstance(ch, disnake.abc.Messageable):
                    try:
                        m = await ch.fetch_message(m_id)
                        await m.delete()
                    except Exception:
                        pass
            elif inter.channel_id != getattr(ticket_channel, "id", 0):
                try:
                    await inter.message.delete()
                except Exception:
                    pass

        # 2. Принять в клан
        if action == "accept":
            if not member:
                return await inter.response.send_message(
                    "Пользователь не найден на сервере. Невозможно выдать роль участника клана.",
                    ephemeral=True,
                )

            role_pending = guild.get_role(ROLE_PENDING_ID) or disnake.Object(id=ROLE_PENDING_ID)
            role_accepted = guild.get_role(ROLE_ACCEPTED_ID) or disnake.Object(id=ROLE_ACCEPTED_ID)

            # Снимаем роль рассмотрения
            if any(r.id == ROLE_PENDING_ID for r in member.roles):
                try:
                    await member.remove_roles(
                        role_pending,
                        reason=f"Заявка принята: {inter.author}",
                    )
                except disnake.Forbidden:
                    logger.warning("Нет прав для снятия роли рассмотрения")

            # Выдаем роль участника клана
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
                    f"Поздравляем! Ваша заявка на вступление в клан на сервере **{guild.name}** "
                    f"была принята модератором {inter.author.mention}."
                )
            except (disnake.Forbidden, disnake.HTTPException):
                pass

            new_embed = self._update_application_embed(
                embed=current_embed,
                status_text="Принят в клан",
                color=disnake.Color.green(),
                moderator=inter.author,
            )

            # Дублируем анкету в архивный канал
            await self._archive_application(embed=new_embed, steam_btn=steam_btn)

            # Логируем в канал принятых заявок
            acc_id = self._parse_id(ACCEPTED_CHANNEL_ID)
            if acc_id:
                user_mention = member.mention if member else f"<@{user_id}>"
                await self._send_log_notification(
                    channel_id=acc_id,
                    title="Заявка принята",
                    user_mention=user_mention,
                    moderator=inter.author,
                    color=disnake.Color.green(),
                )

            # Очищаем сообщение первичного канала
            await _cleanup_primary_message()

            # Удаляем тикет-чат
            if ticket_channel:
                if inter.channel_id == ticket_channel.id:
                    await inter.response.send_message(
                        "Заявка принята! Данный канал тикета будет автоматически удален через несколько секунд...",
                        ephemeral=False,
                    )
                    asyncio.create_task(
                        self._delayed_delete_channel(
                            ticket_channel,
                            delay=4,
                            reason=f"Заявка принята модератором {inter.author}",
                        )
                    )
                else:
                    await inter.response.send_message(
                        "Заявка принята и перемещена в архив. Канал тикета удален.",
                        ephemeral=True,
                    )
                    asyncio.create_task(
                        self._delayed_delete_channel(
                            ticket_channel,
                            delay=1,
                            reason=f"Заявка принята модератором {inter.author}",
                        )
                    )
            else:
                await inter.response.send_message(
                    "Заявка принята и перемещена в архив.",
                    ephemeral=True,
                )

            self.ticket_channels.pop(user_id, None)
            return

        # 3. Отклонить заявку
        if action == "reject":
            if member:
                role_pending = guild.get_role(ROLE_PENDING_ID) or disnake.Object(id=ROLE_PENDING_ID)
                if any(r.id == ROLE_PENDING_ID for r in member.roles):
                    try:
                        await member.remove_roles(
                            role_pending,
                            reason=f"Заявка отклонена: {inter.author}",
                        )
                    except disnake.Forbidden:
                        logger.warning("Нет прав для снятия роли рассмотрения")

                try:
                    await member.send(
                        f"К сожалению, ваша заявка на вступление в клан на сервере **{guild.name}** "
                        f"была отклонена."
                    )
                except (disnake.Forbidden, disnake.HTTPException):
                    pass

            status_text = "Отклонено" if member else "Отклонено (пользователь покинул сервер)"
            new_embed = self._update_application_embed(
                embed=current_embed,
                status_text=status_text,
                color=disnake.Color.red() if member else disnake.Color.dark_gray(),
                moderator=inter.author,
            )

            # Дублируем анкету в архивный канал
            await self._archive_application(embed=new_embed, steam_btn=steam_btn)

            # Логируем в канал отклоненных заявок
            rej_id = self._parse_id(REJECTED_CHANNEL_ID)
            if rej_id:
                user_mention = member.mention if member else f"<@{user_id}>"
                await self._send_log_notification(
                    channel_id=rej_id,
                    title="Заявка отклонена",
                    user_mention=user_mention,
                    moderator=inter.author,
                    color=disnake.Color.red(),
                )

            # Очищаем сообщение первичного канала
            await _cleanup_primary_message()

            # Удаляем тикет-чат
            if ticket_channel:
                if inter.channel_id == ticket_channel.id:
                    await inter.response.send_message(
                        "Заявка отклонена. Данный канал тикета будет автоматически удален через несколько секунд...",
                        ephemeral=False,
                    )
                    asyncio.create_task(
                        self._delayed_delete_channel(
                            ticket_channel,
                            delay=4,
                            reason=f"Заявка отклонена модератором {inter.author}",
                        )
                    )
                else:
                    await inter.response.send_message(
                        "Заявка отклонена и перемещена в архив. Канал тикета удален.",
                        ephemeral=True,
                    )
                    asyncio.create_task(
                        self._delayed_delete_channel(
                            ticket_channel,
                            delay=1,
                            reason=f"Заявка отклонена модератором {inter.author}",
                        )
                    )
            else:
                await inter.response.send_message(
                    "Заявка отклонена и перемещена в архив.",
                    ephemeral=True,
                )

            self.ticket_channels.pop(user_id, None)
            return


def setup(bot: commands.Bot):
    bot.add_cog(TicketsCog(bot))
