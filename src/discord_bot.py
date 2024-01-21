# pylint: disable=missing-module-docstring
import os
import datetime
import pytz
import discord
from discord.ext import commands

async def update_discord_message(
    movies_stats: dict,
    anime_movies_stats: dict,
    tv_show_stats: dict,
    anime_show_stats: dict,
    disk_stats: dict) -> None:
    """
    Update the content of a message in a Discord channel with the given content

    Args:
        message_content (str): stats message to update in discord
    """
    
    with open("discord_template.txt", "r", encoding="utf-8") as file:
        stats = file.read()
        # Date timezone Paris
        stats = stats.replace("[date_sync]", datetime.datetime.now(pytz.timezone('Europe/Paris')).strftime("%d/%m/%Y %H:%M:%S"))
        
        stats = stats.replace("[nb_movies]", str(movies_stats["nb_movies"]))
        stats = stats.replace("[nb_movies_4k]", str(movies_stats["nb_movies_4k"]))
        stats = stats.replace("[size_movies]", str(round(movies_stats["size_movies"] / 1024 / 1024 / 1024, 2)) + " Go")
        
        stats = stats.replace("[nb_movies_anim]", str(anime_movies_stats["nb_movies_anim"]))
        stats = stats.replace("[nb_movies_anim_4k]", str(anime_movies_stats["nb_movies_anim_4k"]))
        stats = stats.replace("[size_movies_anim]", str(round(anime_movies_stats["size_movies_anim"] / 1024 / 1024 / 1024, 2)) + " Go")
        
        stats = stats.replace("[nb_tv]", str(tv_show_stats["nb_tv"]))
        stats = stats.replace("[nb_tv_episode]", str(tv_show_stats["nb_tv_episode"]))
        stats = stats.replace("[nb_tv_4k]", str(tv_show_stats["nb_tv_4k"]))
        stats = stats.replace("[size_tv]", str(round(tv_show_stats["size_tv"] / 1024 / 1024 / 1024, 2)) + " Go")
        
        stats = stats.replace("[nb_series_anim]", str(anime_show_stats["nb_series_anim"]))
        stats = stats.replace("[nb_series_anim_episode]", str(anime_show_stats["nb_series_anim_episode"]))
        stats = stats.replace("[nb_series_anim_4k]", str(anime_show_stats["nb_series_anim_4k"]))
        stats = stats.replace("[size_series_anim]", str(round(anime_show_stats["size_series_anim"] / 1024 / 1024 / 1024, 2)) + " Go")
        
        stats = stats.replace("[size_all]", str(round((disk_stats["totalSpace"] - disk_stats["freeSpace"]) / 1024 / 1024 / 1024, 2)) + " Go")
        stats = stats.replace("[size_remaining]", str(round(disk_stats["freeSpace"] / 1024 / 1024 / 1024, 2)) + " Go")
    
    token = os.getenv("DISCORD_TOKEN")
    intents = discord.Intents.all()
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        print(f"Connecté en tant que {bot.user}")
        await update_message()

    async def get_latest_message_id(channel):
        try:
            async for message in channel.history(limit=1):
                return message.id
        except Exception as e:
            print(f"Erreur lors de la récupération du dernier message : {e}")
            return None

    async def post_message(channel, message):
        try:
            return await channel.send(message)
        except Exception as e:
            print(f"Erreur lors de l'envoi du message : {e}")
            return None

    async def update_message():
        channel_id = 1198277972914864138  # Remplacez par l'ID de votre channel
        channel = bot.get_channel(channel_id)

        if channel is None:
            print("Channel introuvable.")
            return

        message_id = await get_latest_message_id(channel)
        print("Dernier message:", message_id)

        if message_id is None:
            print("Aucun message trouvé. Envoi d'un message temporaire.")
            message = await post_message(channel, "Message temporaire")
            message_id = message.id if message else None

        if message_id:
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(content=stats)
                print("Message mis à jour avec succès.")
            except discord.NotFound:
                print("Message ou channel introuvable.")
            except discord.Forbidden:
                print("Permissions insuffisantes pour éditer le message.")

        await bot.close()

    await bot.start(token)
