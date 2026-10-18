import disnake
from disnake.ext import commands

class ContainerCreatorCog(commands.Cog):
    """Модуль экономической системы бота."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

def setup(bot: commands.Bot):
    bot.add_cog(ContainerCreatorCog(bot))