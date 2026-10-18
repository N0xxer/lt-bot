import disnake
from disnake.ext import commands, tasks
import json
import os

class StatusCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config_file = "dbs/status_config.json"
        
        # Параметры по умолчанию
        self.default_config = {
            "statuses": ["Disnake Bot", "Защита сервера"],
            "delay": 30,
            "activity_type": "playing"
        }
        
        # Загружаем данные из файла
        self.config = self.load_config()
        self.current_index = 0
        
        # Запускаем цикл смены статуса
        self.change_status.start()

    def load_config(self):
        """Загрузка конфигурации из файла"""
        if os.path.exists(self.config_file):
            with open(self.config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return self.default_config

    def save_config(self):
        """Сохранение текущей конфигурации в файл"""
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=4)

    @tasks.loop(seconds=10) # Базовый интервал, изменится в команде
    async def change_status(self):
        if not self.config["statuses"]:
            return

        status_text = self.config["statuses"][self.current_index]
        
        # Преобразуем строку из конфига в тип активности
        act_type_str = self.config.get("activity_type", "playing")
        activity_type = getattr(disnake.ActivityType, act_type_str)

        await self.bot.change_presence(
            activity=disnake.Activity(type=activity_type, name=status_text)
        )
        
        # Меняем индекс для следующего круга
        self.current_index = (self.current_index + 1) % len(self.config["statuses"])

    @change_status.before_loop
    async def before_change_status(self):
        # Ждем готовности и сразу выставляем нужный интервал из конфига
        await self.bot.wait_until_ready()
        self.change_status.change_interval(seconds=self.config["delay"])

    @commands.slash_command(name="set_statuses", description="Настроить ротацию статусов с сохранением")
    async def set_statuses(
        self, 
        inter: disnake.ApplicationCommandInteraction, 
        statuses: str = commands.Param(description="Статусы через точку с запятой (;)"), 
        seconds: int = commands.Param(gt=4, description="Интервал (минимум 5 сек)"),
        type: str = commands.Param(
            choices={"Играет": "playing", "Смотрит": "watching", "Слушает": "listening"}, 
            default="playing"
        )
    ):
        # Парсим входящую строку
        new_list = [s.strip() for s in statuses.split(";") if s.strip()]
        
        if not new_list:
            return await inter.response.send_message("❌ Список пуст!", ephemeral=True)

        # Обновляем данные в оперативной памяти
        self.config["statuses"] = new_list
        self.config["delay"] = seconds
        self.config["activity_type"] = type
        self.current_index = 0
        
        # Сохраняем в JSON файл
        self.save_config()
        
        # Обновляем работающий таймер
        self.change_status.change_interval(seconds=seconds)
        
        await inter.response.send_message(
            f"✅ Настройки сохранены в `{self.config_file}` и применены!\n"
            f"Загружено статусов: **{len(new_list)}**", 
            ephemeral=True
        )

def setup(bot):
    bot.add_cog(StatusCog(bot))