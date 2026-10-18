import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Union
import disnake
from disnake.ext import commands
from disnake import ui

from functions.utils import is_staff_member

logger = logging.getLogger(__name__)


@dataclass
class ContainerItem:
    """Элемент внутри блока контейнера."""
    item_type: str  # "text", "media", "separator", "section"
    content: str = ""
    media_url: str = ""
    button_label: str = ""
    button_url: str = ""


@dataclass
class ContainerBlock:
    """Блок одного контейнера, содержащий список элементов."""
    items: list[ContainerItem] = field(default_factory=list)


@dataclass
class BuilderSession:
    """Сессия конструирования сообщения-контейнера."""
    target_channel_id: int
    containers: list[ContainerBlock] = field(default_factory=lambda: [ContainerBlock()])
    current_container_idx: int = 0

    def add_item(self, item: ContainerItem) -> None:
        """Добавляет элемент в текущий редактируемый контейнер."""
        if not self.containers:
            self.containers.append(ContainerBlock())
            self.current_container_idx = 0
        self.containers[self.current_container_idx].items.append(item)

    def add_container(self) -> None:
        """Создает новый контейнер и переключает редактирование на него."""
        self.containers.append(ContainerBlock())
        self.current_container_idx = len(self.containers) - 1

    def remove_last_item(self) -> bool:
        """Удаляет последний элемент из текущего контейнера."""
        if not self.containers:
            return False
        current_block = self.containers[self.current_container_idx]
        if current_block.items:
            current_block.items.pop()
            return True
        elif len(self.containers) > 1:
            self.containers.pop(self.current_container_idx)
            self.current_container_idx = max(0, len(self.containers) - 1)
            return True
        return False

    def clear(self) -> None:
        """Сбрасывает структуру сессии."""
        self.containers = [ContainerBlock()]
        self.current_container_idx = 0

    def to_disnake_containers(self) -> list[disnake.ui.Container]:
        """Преобразует структуру сессии в список компонентов disnake.ui.Container."""
        disnake_containers: list[disnake.ui.Container] = []
        for block in self.containers:
            child_components: list[Any] = []
            for item in block.items:
                if item.item_type == "text" and item.content:
                    child_components.append(ui.TextDisplay(item.content))
                elif item.item_type == "media" and item.media_url:
                    child_components.append(
                        ui.MediaGallery(disnake.MediaGalleryItem(media=item.media_url))
                    )
                elif item.item_type == "separator":
                    child_components.append(ui.Separator())
                elif item.item_type == "section" and item.content:
                    if item.button_url:
                        child_components.append(
                            ui.Section(
                                item.content,
                                accessory=ui.Button(
                                    label=item.button_label or "Перейти",
                                    url=item.button_url,
                                ),
                            )
                        )
                    else:
                        child_components.append(ui.TextDisplay(item.content))

            if child_components:
                disnake_containers.append(ui.Container(*child_components))

        return disnake_containers

    def get_summary(self) -> str:
        """Генерирует сводку текущей структуры черновика."""
        lines = [
            "**Модульный конструктор сообщений-контейнеров**",
            f"Целевой канал: <#{self.target_channel_id}>",
            f"Всего контейнеров: **{len(self.containers)}** | Редактируется: **#{self.current_container_idx + 1}**\n",
            "**Структура сообщения:**",
        ]
        total_items = 0
        for idx, block in enumerate(self.containers, start=1):
            is_active = (idx - 1) == self.current_container_idx
            marker = " (активен)" if is_active else ""
            lines.append(f"• **Контейнер #{idx}**{marker}:")
            if not block.items:
                lines.append("  *(пусто)*")
            else:
                for item_idx, item in enumerate(block.items, start=1):
                    total_items += 1
                    if item.item_type == "text":
                        preview = (item.content[:40] + "...") if len(item.content) > 40 else item.content
                        lines.append(f"  {item_idx}. Текст: `{preview}`")
                    elif item.item_type == "media":
                        preview = (item.media_url[:40] + "...") if len(item.media_url) > 40 else item.media_url
                        lines.append(f"  {item_idx}. Изображение: `{preview}`")
                    elif item.item_type == "separator":
                        lines.append(f"  {item_idx}. Разделитель (линия)")
                    elif item.item_type == "section":
                        preview = (item.content[:30] + "...") if len(item.content) > 30 else item.content
                        lines.append(f"  {item_idx}. Секция с кнопкой: `{preview}` [{item.button_label}]")

        if total_items == 0:
            lines.append("\n*(Добавьте элементы с помощью кнопок управления ниже)*")
        else:
            lines.append("\n*Используйте «Предпросмотр», чтобы увидеть реальный вид, или «Отправить в канал».*")

        return "\n".join(lines)


class ContainerCreatorCog(commands.Cog):
    """Модуль создания и публикации сообщений-контейнеров (UI v2) для рекламы и объявлений."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sessions: dict[int, BuilderSession] = {}

    def _get_or_create_session(self, user_id: int, default_channel_id: int) -> BuilderSession:
        """Получает текущую сессию пользователя или создает новую."""
        if user_id not in self.sessions:
            self.sessions[user_id] = BuilderSession(target_channel_id=default_channel_id)
        return self.sessions[user_id]

    def _build_control_panel(self, session: BuilderSession) -> list[disnake.ui.ActionRow]:
        """Формирует интерактивные кнопки управления конструктором."""
        row_1 = ui.ActionRow(
            ui.Button(label="+ Текст", custom_id="builder_add_text", style=disnake.ButtonStyle.blurple),
            ui.Button(label="+ Картинка", custom_id="builder_add_media", style=disnake.ButtonStyle.blurple),
            ui.Button(label="+ Секция с ссылкой", custom_id="builder_add_section", style=disnake.ButtonStyle.blurple),
            ui.Button(label="+ Разделитель", custom_id="builder_add_separator", style=disnake.ButtonStyle.secondary),
        )

        row_2 = ui.ActionRow(
            ui.Button(label="+ Новый контейнер", custom_id="builder_add_container", style=disnake.ButtonStyle.secondary),
            ui.Button(label="Удалить элемент", custom_id="builder_delete_last", style=disnake.ButtonStyle.secondary),
            ui.Button(label="Шаблон рекламы", custom_id="builder_template_ad", style=disnake.ButtonStyle.secondary),
            ui.Button(label="Очистить всё", custom_id="builder_clear", style=disnake.ButtonStyle.red),
        )

        row_3 = ui.ActionRow(
            ui.ChannelSelect(
                custom_id="builder_channel_select",
                placeholder="Изменить целевой канал публикации...",
                channel_types=[disnake.ChannelType.text],
            )
        )

        row_4 = ui.ActionRow(
            ui.Button(label="Предпросмотр", custom_id="builder_preview", style=disnake.ButtonStyle.secondary),
            ui.Button(label="Отправить в канал", custom_id="builder_send", style=disnake.ButtonStyle.green),
        )

        return [row_1, row_2, row_3, row_4]

    # --- СЛЭШ-КОМАНДЫ ---

    @commands.slash_command(
        name="container_builder",
        description="Открыть модульный конструктор сообщений-контейнеров",
        default_member_permissions=disnake.Permissions(administrator=True),
    )
    @commands.has_permissions(administrator=True)
    async def container_builder(
        self,
        inter: disnake.ApplicationCommandInteraction,
        target_channel: Optional[disnake.TextChannel] = commands.Param(
            default=None,
            description="Канал для публикации (по умолчанию — текущий)",
        ),
    ):
        """Запускает панель интерактивного сборщика сообщений."""
        channel = target_channel or inter.channel
        if not channel or not isinstance(channel, disnake.abc.Messageable):
            return await inter.response.send_message(
                "Не удалось определить канал для отправки.",
                ephemeral=True,
            )

        session = self._get_or_create_session(inter.author.id, channel.id)
        session.target_channel_id = channel.id

        summary = session.get_summary()
        components = self._build_control_panel(session)

        await inter.response.send_message(
            content=summary,
            components=components,
            ephemeral=True,
        )

    @commands.slash_command(
        name="send_ad",
        description="Быстрая отправка рекламного сообщения-контейнера",
        default_member_permissions=disnake.Permissions(administrator=True),
    )
    @commands.has_permissions(administrator=True)
    async def send_ad(
        self,
        inter: disnake.ApplicationCommandInteraction,
        target_channel: Optional[disnake.TextChannel] = commands.Param(
            default=None,
            description="Канал для отправки (по умолчанию — текущий)",
        ),
    ):
        """Открывает модальное окно для быстрой отправки рекламы."""
        channel = target_channel or inter.channel
        if not channel or not isinstance(channel, disnake.abc.Messageable):
            return await inter.response.send_message(
                "Не удалось определить канал для отправки.",
                ephemeral=True,
            )

        modal = ui.Modal(
            title="Создание рекламы",
            custom_id=f"modal_ad_sender_{channel.id}",
            components=[
                ui.TextInput(
                    label="Верхний текст / заголовок",
                    custom_id="text_1",
                    style=disnake.TextInputStyle.short,
                    max_length=200,
                    required=False,
                ),
                ui.TextInput(
                    label="Ссылка на картинку",
                    custom_id="image_link",
                    style=disnake.TextInputStyle.short,
                    placeholder="https://example.com/banner.png",
                    max_length=500,
                    required=False,
                ),
                ui.TextInput(
                    label="Основной текст рекламы",
                    custom_id="text_2",
                    style=disnake.TextInputStyle.paragraph,
                    placeholder="Описание предложения, сервера или клана...",
                    max_length=2000,
                    required=True,
                ),
                ui.TextInput(
                    label="Текст кнопки",
                    custom_id="text_3",
                    style=disnake.TextInputStyle.short,
                    placeholder="Перейти / Присоединиться",
                    max_length=80,
                    required=False,
                ),
                ui.TextInput(
                    label="Ссылка для кнопки",
                    custom_id="button_link",
                    style=disnake.TextInputStyle.short,
                    placeholder="https://discord.gg/...",
                    max_length=500,
                    required=False,
                ),
            ],
        )

        await inter.response.send_modal(modal)

    @commands.slash_command(
        name="send_message",
        description="Быстрая отправка текстового сообщения-контейнера",
        default_member_permissions=disnake.Permissions(administrator=True),
    )
    @commands.has_permissions(administrator=True)
    async def send_message(
        self,
        inter: disnake.ApplicationCommandInteraction,
        target_channel: Optional[disnake.TextChannel] = commands.Param(
            default=None,
            description="Канал для отправки (по умолчанию — текущий)",
        ),
    ):
        """Открывает модальное окно для отправки простого текста в контейнере."""
        channel = target_channel or inter.channel
        if not channel or not isinstance(channel, disnake.abc.Messageable):
            return await inter.response.send_message(
                "Не удалось определить канал для отправки.",
                ephemeral=True,
            )

        modal = ui.Modal(
            title="Отправка сообщения",
            custom_id=f"modal_msg_sender_{channel.id}",
            components=[
                ui.TextInput(
                    label="Текст сообщения",
                    custom_id="text_to_send",
                    style=disnake.TextInputStyle.paragraph,
                    placeholder="Введите текст объявления...",
                    max_length=2000,
                    required=True,
                ),
            ],
        )

        await inter.response.send_modal(modal)

    # --- СЛУШАТЕЛИ ВЗАИМОДЕЙСТВИЙ ---

    @commands.Cog.listener()
    async def on_button_click(self, inter: disnake.MessageInteraction):
        """Обработка нажатий кнопок в панели конструктора."""
        custom_id: str = (inter.data.custom_id if inter.data and inter.data.custom_id else "") or (inter.component.custom_id if inter.component and inter.component.custom_id else "")
        if not custom_id or not custom_id.startswith("builder_"):
            return

        if not inter.guild or not isinstance(inter.author, disnake.Member):
            return await inter.response.send_message("Действие доступно только на сервере.", ephemeral=True)

        if not is_staff_member(inter.author):
            return await inter.response.send_message("У вас нет прав для использования конструктора.", ephemeral=True)

        session = self._get_or_create_session(inter.author.id, inter.channel_id)

        # 1. Добавить текст
        if custom_id == "builder_add_text":
            modal = ui.Modal(
                title="Добавить текст",
                custom_id="builder_modal_text",
                components=[
                    ui.TextInput(
                        label="Текст для контейнера",
                        custom_id="text_content",
                        style=disnake.TextInputStyle.paragraph,
                        placeholder="Введите текст...",
                        max_length=2000,
                        required=True,
                    ),
                ],
            )
            return await inter.response.send_modal(modal)

        # 2. Добавить картинку
        if custom_id == "builder_add_media":
            modal = ui.Modal(
                title="Добавить изображение",
                custom_id="builder_modal_media",
                components=[
                    ui.TextInput(
                        label="Прямая ссылка на изображение",
                        custom_id="image_url",
                        style=disnake.TextInputStyle.short,
                        placeholder="https://example.com/banner.png",
                        max_length=500,
                        required=True,
                    ),
                ],
            )
            return await inter.response.send_modal(modal)

        # 3. Добавить секцию с кнопкой-ссылкой
        if custom_id == "builder_add_section":
            modal = ui.Modal(
                title="Добавить секцию с ссылкой",
                custom_id="builder_modal_section",
                components=[
                    ui.TextInput(
                        label="Текст секции",
                        custom_id="section_text",
                        style=disnake.TextInputStyle.paragraph,
                        placeholder="Описание...",
                        max_length=1000,
                        required=True,
                    ),
                    ui.TextInput(
                        label="Текст кнопки",
                        custom_id="button_label",
                        style=disnake.TextInputStyle.short,
                        placeholder="Перейти",
                        max_length=80,
                        required=True,
                    ),
                    ui.TextInput(
                        label="Ссылка для кнопки",
                        custom_id="button_url",
                        style=disnake.TextInputStyle.short,
                        placeholder="https://...",
                        max_length=500,
                        required=True,
                    ),
                ],
            )
            return await inter.response.send_modal(modal)

        # 4. Добавить разделитель
        if custom_id == "builder_add_separator":
            session.add_item(ContainerItem(item_type="separator"))
            summary = session.get_summary()
            components = self._build_control_panel(session)
            return await inter.response.edit_message(content=summary, components=components)

        # 5. Новый контейнер
        if custom_id == "builder_add_container":
            session.add_container()
            summary = session.get_summary()
            components = self._build_control_panel(session)
            return await inter.response.edit_message(content=summary, components=components)

        # 6. Удалить последний элемент
        if custom_id == "builder_delete_last":
            removed = session.remove_last_item()
            if not removed:
                return await inter.response.send_message("В черновике нет элементов для удаления.", ephemeral=True)
            summary = session.get_summary()
            components = self._build_control_panel(session)
            return await inter.response.edit_message(content=summary, components=components)

        # 7. Шаблон рекламы
        if custom_id == "builder_template_ad":
            modal = ui.Modal(
                title="Заполнить шаблон рекламы",
                custom_id="builder_modal_template_ad",
                components=[
                    ui.TextInput(
                        label="Верхний текст / заголовок",
                        custom_id="text_1",
                        style=disnake.TextInputStyle.short,
                        max_length=200,
                        required=False,
                    ),
                    ui.TextInput(
                        label="Ссылка на картинку",
                        custom_id="image_link",
                        style=disnake.TextInputStyle.short,
                        placeholder="https://...",
                        max_length=500,
                        required=False,
                    ),
                    ui.TextInput(
                        label="Основной текст рекламы",
                        custom_id="text_2",
                        style=disnake.TextInputStyle.paragraph,
                        max_length=2000,
                        required=True,
                    ),
                    ui.TextInput(
                        label="Текст кнопки",
                        custom_id="text_3",
                        style=disnake.TextInputStyle.short,
                        placeholder="Перейти",
                        max_length=80,
                        required=False,
                    ),
                    ui.TextInput(
                        label="Ссылка для кнопки",
                        custom_id="button_link",
                        style=disnake.TextInputStyle.short,
                        placeholder="https://...",
                        max_length=500,
                        required=False,
                    ),
                ],
            )
            return await inter.response.send_modal(modal)

        # 8. Очистить всё
        if custom_id == "builder_clear":
            session.clear()
            summary = session.get_summary()
            components = self._build_control_panel(session)
            return await inter.response.edit_message(content=summary, components=components)

        # 9. Предпросмотр
        if custom_id == "builder_preview":
            containers = session.to_disnake_containers()
            if not containers:
                return await inter.response.send_message("Черновик пуст. Добавьте элементы перед предпросмотром.", ephemeral=True)
            return await inter.response.send_message("Предпросмотр сообщения:", components=containers, ephemeral=True)

        # 10. Отправить в канал
        if custom_id == "builder_send":
            containers = session.to_disnake_containers()
            if not containers:
                return await inter.response.send_message("Нельзя отправить пустое сообщение.", ephemeral=True)

            target_channel = inter.guild.get_channel(session.target_channel_id)
            if target_channel is None:
                try:
                    target_channel = await self.bot.fetch_channel(session.target_channel_id)
                except (disnake.NotFound, disnake.Forbidden, disnake.HTTPException):
                    target_channel = None

            if not target_channel or not isinstance(target_channel, disnake.abc.Messageable):
                return await inter.response.send_message("Целевой канал не найден или бот не имеет к нему доступа.", ephemeral=True)

            try:
                await target_channel.send(components=containers)
                await inter.response.send_message(
                    f"Сообщение успешно опубликовано в {target_channel.mention}!",
                    ephemeral=True,
                )
                self.sessions.pop(inter.author.id, None)
            except disnake.Forbidden:
                await inter.response.send_message("У бота нет прав на отправку сообщений в целевой канал.", ephemeral=True)
            except Exception as e:
                logger.error("Ошибка при публикации сообщения: %s", e)
                await inter.response.send_message("Произошла ошибка при отправке сообщения в канал.", ephemeral=True)
            return

    @commands.Cog.listener()
    async def on_dropdown(self, inter: disnake.MessageInteraction):
        """Обработка выбора канала в ChannelSelect панели конструктора."""
        custom_id: str = (inter.data.custom_id if inter.data and inter.data.custom_id else "") or (inter.component.custom_id if inter.component and inter.component.custom_id else "")
        if custom_id != "builder_channel_select":
            return

        if not inter.values:
            return await inter.response.defer()

        selected_channel_id = int(inter.values[0])
        session = self._get_or_create_session(inter.author.id, selected_channel_id)
        session.target_channel_id = selected_channel_id

        summary = session.get_summary()
        components = self._build_control_panel(session)
        await inter.response.edit_message(content=summary, components=components)

    @commands.Cog.listener()
    async def on_modal_submit(self, inter: disnake.ModalInteraction):
        """Обработка модальных окон добавления контента в конструктор и быстрых команд."""
        custom_id = inter.custom_id

        # Обработка модальных окон конструктора
        if custom_id.startswith("builder_modal_"):
            session = self._get_or_create_session(inter.author.id, inter.channel_id)

            if custom_id == "builder_modal_text":
                text = inter.text_values.get("text_content", "").strip()
                if text:
                    session.add_item(ContainerItem(item_type="text", content=text))

            elif custom_id == "builder_modal_media":
                url = inter.text_values.get("image_url", "").strip()
                if not url.startswith(("http://", "https://")):
                    return await inter.response.send_message(
                        "Укажите корректную прямую ссылку на картинку (начинающуюся с http:// или https://).",
                        ephemeral=True,
                        delete_after=10,
                    )
                session.add_item(ContainerItem(item_type="media", media_url=url))

            elif custom_id == "builder_modal_section":
                section_text = inter.text_values.get("section_text", "").strip()
                button_label = inter.text_values.get("button_label", "Перейти").strip()
                button_url = inter.text_values.get("button_url", "").strip()
                if button_url and not button_url.startswith(("http://", "https://")):
                    return await inter.response.send_message(
                        "Ссылка для кнопки должна начинаться с http:// или https://",
                        ephemeral=True,
                        delete_after=10,
                    )
                session.add_item(
                    ContainerItem(
                        item_type="section",
                        content=section_text,
                        button_label=button_label,
                        button_url=button_url,
                    )
                )

            elif custom_id == "builder_modal_template_ad":
                text_1 = inter.text_values.get("text_1", "").strip()
                image_link = inter.text_values.get("image_link", "").strip()
                text_2 = inter.text_values.get("text_2", "").strip()
                text_3 = inter.text_values.get("text_3", "").strip()
                button_link = inter.text_values.get("button_link", "").strip()

                session.clear()

                # Контейнер 1: медиа + заголовок
                c1_items: list[ContainerItem] = []
                if image_link and image_link.startswith(("http://", "https://")):
                    c1_items.append(ContainerItem(item_type="media", media_url=image_link))
                    if text_1:
                        c1_items.append(ContainerItem(item_type="separator"))
                        c1_items.append(ContainerItem(item_type="text", content=text_1))
                elif text_1:
                    c1_items.append(ContainerItem(item_type="text", content=text_1))

                if c1_items:
                    session.containers[0].items = c1_items
                    session.add_container()

                # Контейнер 2: основной текст
                if text_2:
                    current = session.containers[session.current_container_idx]
                    current.items.append(ContainerItem(item_type="text", content=text_2))
                    session.add_container()

                # Контейнер 3: кнопка-ссылка или нижняя плашка
                if button_link and button_link.startswith(("http://", "https://")):
                    current = session.containers[session.current_container_idx]
                    current.items.append(
                        ContainerItem(
                            item_type="section",
                            content=text_3 or "Подробнее",
                            button_label="Перейти",
                            button_url=button_link,
                        )
                    )
                elif text_3:
                    current = session.containers[session.current_container_idx]
                    current.items.append(ContainerItem(item_type="text", content=text_3))

            summary = session.get_summary()
            components = self._build_control_panel(session)

            # Отвечаем подтверждением и обновляем сообщение
            await inter.response.send_message("Элемент добавлен в черновик!", ephemeral=True, delete_after=3)
            return

        # Быстрая отправка текста (/send_message)
        if custom_id.startswith("modal_msg_sender"):
            parts = custom_id.split("_")
            channel_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else inter.channel_id
            target_channel = inter.guild.get_channel(channel_id) if inter.guild else None
            if not target_channel or not isinstance(target_channel, disnake.abc.Messageable):
                target_channel = inter.channel

            user_text = inter.text_values.get("text_to_send", "").strip()
            if not user_text:
                return await inter.response.send_message("Текст сообщения не может быть пустым.", ephemeral=True, delete_after=10)

            if not target_channel or not isinstance(target_channel, disnake.abc.Messageable):
                return await inter.response.send_message("Не удалось отправить сообщение в указанный канал.", ephemeral=True)

            try:
                await target_channel.send(components=[ui.Container(ui.TextDisplay(user_text))])
                await inter.response.send_message("Сообщение успешно отправлено!", ephemeral=True, delete_after=10)
            except disnake.Forbidden:
                await inter.response.send_message("У бота нет прав на отправку в целевой канал.", ephemeral=True)
            return

        # Быстрая отправка рекламы (/send_ad)
        if custom_id.startswith("modal_ad_sender"):
            parts = custom_id.split("_")
            channel_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else inter.channel_id
            target_channel = inter.guild.get_channel(channel_id) if inter.guild else None
            if not target_channel or not isinstance(target_channel, disnake.abc.Messageable):
                target_channel = inter.channel

            if not target_channel or not isinstance(target_channel, disnake.abc.Messageable):
                return await inter.response.send_message("Не удалось отправить объявление в целевой канал.", ephemeral=True)

            text_1 = inter.text_values.get("text_1", "").strip()
            text_2 = inter.text_values.get("text_2", "").strip()
            text_3 = inter.text_values.get("text_3", "").strip()
            image_link = inter.text_values.get("image_link", "").strip()
            button_link = inter.text_values.get("button_link", "").strip()

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

            try:
                await target_channel.send(components=containers)
                await inter.response.send_message("Объявление успешно отправлено!", ephemeral=True, delete_after=10)
            except disnake.Forbidden:
                await inter.response.send_message("У бота нет прав на отправку в целевой канал.", ephemeral=True)
            return


def setup(bot: commands.Bot):
    bot.add_cog(ContainerCreatorCog(bot))