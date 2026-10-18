import json
import logging
from typing import Optional
import disnake
from disnake.ext import commands
from disnake import ui

logger = logging.getLogger(__name__)

DEFAULT_ROLE_MAP: dict[str, int] = {
    "Фермер": 1491903740012269601,
    "Электрик": 1491903740012269602,
    "Строитель": 1491903740012269603,
    "Стрелок (PvP)": 1491903740012269604,
    "Монументы": 1491903740012269605,
    "Пилот": 1491903740012269606,
}


class RolesGiverCog(commands.Cog):
    """Модуль управления и самостоятельного выбора ролей в боте."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.role_map: dict[str, int] = self._load_role_map()

    @staticmethod
    def _load_role_map() -> dict[str, int]:
        """Загружает маппинг ролей из конфига или возвращает значения по умолчанию."""
        try:
            with open("configs/bot_config.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("role_map", DEFAULT_ROLE_MAP)
        except Exception:
            return DEFAULT_ROLE_MAP

    async def role_namer(self, key: str, guild: Optional[disnake.Guild] = None) -> str:
        """Возвращает название роли с сервера, если она существует, либо ключ словаря."""
        if guild:
            role_id = self.role_map.get(key)
            if role_id:
                role = guild.get_role(role_id)
                if role:
                    return role.name
        return key

    async def get_personal_role_select(self, member: disnake.Member) -> Optional[disnake.ui.StringSelect]:
        """Формирует выпадающий список профессий с предустановленными галочками текущих ролей участника."""
        options: list[disnake.SelectOption] = []
        for key, role_id in self.role_map.items():
            role_name = await self.role_namer(key, member.guild)
            has_role = member.get_role(role_id) is not None

            options.append(
                disnake.SelectOption(
                    label=role_name[:100],
                    value=str(role_id),
                    default=has_role,
                )
            )

        if not options:
            return None

        return disnake.ui.StringSelect(
            custom_id="role_select_multi",
            placeholder="Выберите ваши профессии...",
            min_values=0,
            max_values=len(options),
            options=options,
        )

    def _build_role_menu_container(self, select_component: disnake.ui.StringSelect) -> disnake.ui.Container:
        """Создает контейнер UI v2 для персонального меню выбора ролей."""
        return ui.Container(
            ui.TextDisplay("Выберите роли под ваш стиль игры в клане:"),
            ui.ActionRow(select_component),
            accent_colour=disnake.Colour(0x4F6653),
        )

    @commands.slash_command(
        name="send_role_panel",
        description="Отправить панель с кнопкой настройки ролей",
        default_member_permissions=disnake.Permissions(administrator=True),
    )
    @commands.has_permissions(administrator=True)
    async def send_role_panel(
        self,
        inter: disnake.ApplicationCommandInteraction,
        target_channel: Optional[disnake.TextChannel] = commands.Param(
            default=None,
            description="Канал для отправки (по умолчанию — текущий)",
        ),
    ):
        """Отправляет панель выбора ролей в виде контейнера UI v2."""
        channel = target_channel or inter.channel
        if not channel or not isinstance(channel, disnake.abc.Messageable):
            return await inter.response.send_message(
                "Не удалось определить канал для отправки.",
                ephemeral=True,
            )

        container = ui.Container(
            ui.Section(
                "Используйте кнопку, чтобы выбрать роли под ваш стиль игры",
                accessory=ui.Button(
                    label="Выбрать роли",
                    custom_id="open_personal_role_menu",
                    style=disnake.ButtonStyle.blurple,
                ),
            ),
            accent_colour=disnake.Colour(0x4F6653),
        )

        try:
            await channel.send(components=[container])
            await inter.response.send_message("Панель отправлена!", ephemeral=True)
        except disnake.Forbidden:
            await inter.response.send_message(
                "У бота нет прав для отправки сообщений в выбранный канал.",
                ephemeral=True,
            )

    @commands.Cog.listener()
    async def on_button_click(self, inter: disnake.MessageInteraction):
        """Обработчик нажатия на кнопку вызова меню выбора ролей."""
        custom_id = inter.data.custom_id if inter.data else (inter.component.custom_id if inter.component else "")
        if custom_id != "open_personal_role_menu":
            return

        if not inter.guild or not isinstance(inter.author, disnake.Member):
            return await inter.response.send_message(
                "Действие доступно только на сервере.",
                ephemeral=True,
            )

        select_menu = await self.get_personal_role_select(inter.author)
        if not select_menu:
            return await inter.response.send_message(
                "Список ролей для выбора пуст или еще не настроен.",
                ephemeral=True,
            )

        menu_container = self._build_role_menu_container(select_menu)
        await inter.response.send_message(components=[menu_container], ephemeral=True)

    @commands.Cog.listener()
    async def on_dropdown(self, inter: disnake.MessageInteraction):
        """Обработчик выбора ролей в выпадающем списке."""
        custom_id = inter.data.custom_id if inter.data else (inter.component.custom_id if inter.component else "")
        if custom_id != "role_select_multi":
            return

        if not inter.guild or not isinstance(inter.author, disnake.Member):
            return await inter.response.send_message(
                "Действие доступно только на сервере.",
                ephemeral=True,
            )

        await inter.response.defer(ephemeral=True)

        selected_ids: set[int] = set()
        if inter.values:
            for val in inter.values:
                try:
                    selected_ids.add(int(val))
                except ValueError:
                    pass

        all_managed_ids = list(self.role_map.values())
        roles_to_add: list[disnake.Role] = []
        roles_to_remove: list[disnake.Role] = []

        for r_id in all_managed_ids:
            role_obj = inter.guild.get_role(r_id)
            if not role_obj:
                continue

            if r_id in selected_ids:
                if role_obj not in inter.author.roles:
                    roles_to_add.append(role_obj)
            else:
                if role_obj in inter.author.roles:
                    roles_to_remove.append(role_obj)

        try:
            if roles_to_add:
                await inter.author.add_roles(*roles_to_add, reason="Rolegiver Select")
            if roles_to_remove:
                await inter.author.remove_roles(*roles_to_remove, reason="Rolegiver Select")
        except disnake.Forbidden:
            await inter.followup.send(
                "Ошибка: У бота недостаточно прав для изменения некоторых ролей.",
                ephemeral=True,
            )
            return
        except disnake.HTTPException as e:
            logger.error("Ошибка при обновлении ролей: %s", e)
            await inter.followup.send(
                "Произошла ошибка при обновлении ролей.",
                ephemeral=True,
            )
            return

        # Обновляем оригинальное меню с новым состоянием галочек
        updated_select = await self.get_personal_role_select(inter.author)
        if updated_select:
            updated_container = self._build_role_menu_container(updated_select)
            try:
                await inter.edit_original_response(components=[updated_container])
            except disnake.HTTPException:
                pass

        await inter.followup.send("Ваш список профессий обновлен!", ephemeral=True, delete_after=10)


def setup(bot: commands.Bot):
    bot.add_cog(RolesGiverCog(bot))