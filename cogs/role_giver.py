import disnake
from disnake.ext import commands

class RolesGiverCog(commands.Cog):
    """Модуль управления ролями в боте."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot







def setup(bot: commands.Bot):
    bot.add_cog(RolesGiverCog(bot))