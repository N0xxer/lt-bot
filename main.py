import os
import json
import disnake
from disnake.ext import commands
from dotenv import load_dotenv

from functions.db_helpers import init_db

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

with open("configs/bot_config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

intents = disnake.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(
    command_prefix=config.get("prefix", "!"),
    intents=intents,
    test_guilds=config.get("test_guilds", [])
)


@bot.event
async def on_ready():
    """Событие готовности бота к работе."""
    await init_db()
    print(f"Бот {bot.user} успешно запущен и готов к работе!")


def load_cogs():
    for filename in os.listdir("./cogs"):
        if filename.endswith(".py"):
            bot.load_extension(f"cogs.{filename[:-3]}")


if __name__ == "__main__":
    load_cogs()
    bot.run(TOKEN)